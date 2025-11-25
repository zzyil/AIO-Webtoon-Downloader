from __future__ import annotations

import re
import hashlib
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urlencode, urljoin, urlparse

from bs4 import BeautifulSoup, FeatureNotFound, NavigableString

from .base import BaseSiteHandler, SiteComicContext


class MangataroSiteHandler(BaseSiteHandler):
    name = "mangataro"
    domains = ("mangataro.org",)

    def __init__(self) -> None:
        super().__init__()
        self._has_lxml = False
        try:
            from lxml import etree  # noqa: F401

            self._has_lxml = True
        except Exception:
            self._has_lxml = False

    # ------------------------------------------------------------------ Session
    def configure_session(self, scraper, args) -> None:
        scraper.headers.update(
            {
                "Referer": "https://mangataro.org/",
                "Origin": "https://mangataro.org/",
            }
        )

    # ----------------------------------------------------------- Comic Overview
    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        html = make_request(url, scraper).text
        parser = "lxml" if self._has_lxml else "html.parser"
        try:
            soup = BeautifulSoup(html, parser)
        except FeatureNotFound:
            soup = BeautifulSoup(html, "html.parser")

        title = self._extract_title(soup)
        if not title:
            raise RuntimeError("Unable to determine series title.")

        slug = self._extract_slug(url)
        comic_data: Dict[str, object] = {"hid": slug, "title": title}

        description = self._extract_description(soup)
        if description:
            comic_data["desc"] = description

        return SiteComicContext(comic=comic_data, title=title, identifier=slug, soup=soup)

    def extract_additional_metadata(
        self, context: SiteComicContext
    ) -> Dict[str, List[str]]:
        soup = context.soup
        if soup is None:
            return {}

        metadata: Dict[str, List[str]] = {}

        authors = self._extract_people(soup, ("author",))
        artists = self._extract_people(soup, ("artist", "illustrator"))
        if authors:
            metadata["authors"] = authors
        if artists:
            metadata["artists"] = artists

        genres = self._extract_tag_list(soup, "/genre/")
        themes = self._extract_tag_list(soup, "/tag/")
        if genres:
            metadata["genres"] = genres
        if themes:
            metadata["theme"] = themes

        return metadata

    # --------------------------------------------------------------- Chapters --
    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        soup = context.soup
        if soup is None:
            raise RuntimeError("Comic page HTML not available for parsing.")

        chapter_links = soup.select("a[data-chapter-id]")
        chapters = self._parse_chapter_links(chapter_links)
        if chapters:
            return chapters

        manga_id = self._extract_manga_id(soup)
        if not manga_id:
            return []

        api_chapters = self._fetch_chapters_via_api(manga_id, scraper, make_request)
        return api_chapters

    def get_group_name(self, chapter_version: Dict) -> Optional[str]:
        group_name = chapter_version.get("group_name")
        if isinstance(group_name, str) and group_name.strip():
            cleaned = group_name.strip().strip("—").strip()
            return cleaned or None
        return None

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        chapter_url = chapter.get("url")
        if not chapter_url:
            raise RuntimeError("Chapter URL missing.")

        html = make_request(chapter_url, scraper).text
        parser = "lxml" if self._has_lxml else "html.parser"
        try:
            soup = BeautifulSoup(html, parser)
        except FeatureNotFound:
            soup = BeautifulSoup(html, "html.parser")

        candidates = []
        reader_container = None
        for selector in (
            "#readerarea",
            "[data-reader]",
            ".reading-content",
            ".reader-area",
            ".chapter-content",
        ):
            reader_container = soup.select_one(selector)
            if reader_container:
                break
        if reader_container:
            candidates.extend(reader_container.find_all("img"))
        else:
            candidates.extend(soup.find_all("img"))

        image_urls = []
        for img in candidates:
            src = (
                img.get("data-src")
                or img.get("data-original")
                or img.get("src")
                or _first_src_from_srcset(img)
            )
            if not src:
                continue
            src = src.strip()
            if not src:
                continue
            if _looks_like_non_page_asset(src, img):
                continue
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = urljoin(chapter_url, src)
            elif not src.startswith("http"):
                src = urljoin(chapter_url, src)

            if not _looks_like_page_image(src):
                continue

            if src not in image_urls:
                image_urls.append(src)

        entries: List = list(image_urls)

        text_paragraphs = self._extract_text_paragraphs(soup)
        if text_paragraphs:
            entries.append(
                {
                    "type": "text",
                    "paragraphs": text_paragraphs,
                    "title": chapter.get("title") or None,
                }
            )

        if not entries:
            fallback_images = _extract_images_from_scripts(html, chapter_url)
            entries.extend(fallback_images)

        return entries

    # -------------------------------------------------------------- Utilities -
    def _extract_slug(self, url: str) -> str:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if parts:
            return parts[-1]
        return parsed.netloc

    def _extract_title(self, soup: BeautifulSoup) -> Optional[str]:
        meta = soup.find("meta", property="og:title")
        if meta and meta.get("content"):
            return meta["content"].strip()
        h1 = soup.find(["h1", "h2"])
        if h1:
            return h1.get_text(" ", strip=True)
        return None

    def _extract_description(self, soup: BeautifulSoup) -> Optional[str]:
        meta = soup.find("meta", property="og:description")
        if meta and meta.get("content"):
            desc = meta["content"].strip()
            if desc:
                return desc
        paragraph = soup.find("p", class_=re.compile("description", re.I))
        if paragraph:
            text = paragraph.get_text(" ", strip=True)
            if text:
                return text
        return None

    def _extract_people(
        self, soup: BeautifulSoup, keywords: tuple[str, ...]
    ) -> List[str]:
        results: List[str] = []
        keyword_re = re.compile("|".join(re.escape(k) for k in keywords), re.I)

        for label in soup.find_all(string=keyword_re):
            parent = label.parent
            if not parent:
                continue
            # Find a sibling that likely contains the value (usually the previous div)
            candidate = parent.find_previous_sibling()
            if not candidate:
                continue
            text = candidate.get_text(" ", strip=True)
            if not text:
                continue
            results.extend(_split_people(text))

        # Remove duplicates while preserving order
        seen = set()
        unique: List[str] = []
        for name in results:
            if name not in seen:
                seen.add(name)
                unique.append(name)
        return unique

    def _extract_tag_list(self, soup: BeautifulSoup, path_fragment: str) -> List[str]:
        tags: List[str] = []
        selector = f'a[href*="{path_fragment}"]'
        for anchor in soup.select(selector):
            text = anchor.get_text(" ", strip=True)
            if text:
                tags.append(text)

        seen = set()
        unique: List[str] = []
        for tag in tags:
            if tag not in seen:
                seen.add(tag)
                unique.append(tag)
        return unique

    def _extract_chapter_number(self, link) -> Optional[str]:
        fields = [
            link.get("data-number"),
            link.get("data-chapter"),
            link.get("title"),
            link.get_text(" ", strip=True),
        ]
        for field in fields:
            if not field:
                continue
            match = re.search(r"(\d+(?:\.\d+)?)", field)
            if match:
                return match.group(1)
        return None

    def _extract_text_paragraphs(self, soup: BeautifulSoup) -> List[str]:
        container = soup.select_one(".reader-text")
        if not container:
            return []

        paragraphs: List[str] = []

        for node in container.children:
            if isinstance(node, NavigableString):
                text = node.strip()
                if text:
                    paragraphs.append(text)
                continue

            if node.name == "br":
                paragraphs.append("")
                continue

            if node.name in {"p", "div", "span", "blockquote", "h2", "h3", "h4", "h5"}:
                text = node.get_text(" ", strip=True)
                if text:
                    paragraphs.append(text)
                continue

            if node.name == "ul":
                for li in node.find_all("li"):
                    text = li.get_text(" ", strip=True)
                    if text:
                        paragraphs.append(f"• {text}")
                continue

        if not paragraphs:
            text = container.get_text("\n", strip=True)
            if text:
                paragraphs = [line.strip() for line in text.splitlines()]

        return paragraphs

    def _parse_chapter_links(self, chapter_links) -> List[Dict]:
        chapters: List[Dict] = []
        seen_ids = set()

        for link in chapter_links:
            chapter_id = (link.get("data-chapter-id") or "").strip()
            if not chapter_id or chapter_id in seen_ids:
                continue
            seen_ids.add(chapter_id)

            href = link.get("href")
            if not href:
                continue
            chapter_url = urljoin("https://mangataro.org/", href)

            chap_number = self._extract_chapter_number(link)
            if chap_number is None:
                continue

            group_name = (link.get("data-group-name") or "").strip() or None

            chapters.append(
                {
                    "hid": chapter_id,
                    "chap": chap_number,
                    "url": chapter_url,
                    "group_name": group_name,
                    "title": (link.get("title") or "").strip() or None,
                }
            )

        return chapters

    def _extract_manga_id(self, soup: BeautifulSoup) -> Optional[str]:
        container = soup.select_one(".chapter-list[data-manga-id]")
        if container:
            manga_id = (container.get("data-manga-id") or "").strip()
            if manga_id:
                return manga_id
        body = soup.find("body", attrs={"data-manga-id": True})
        if body:
            manga_id = (body.get("data-manga-id") or "").strip()
            if manga_id:
                return manga_id
        generic = soup.find(attrs={"data-manga-id": True})
        if generic:
            manga_id = (generic.get("data-manga-id") or "").strip()
            if manga_id:
                return manga_id
        return None

    def _fetch_chapters_via_api(self, manga_id: str, scraper, make_request) -> List[Dict]:
        token, timestamp = self._generate_api_signature()
        params = {
            "manga_id": manga_id,
            "offset": 0,
            "limit": 500,
            "order": "DESC",
            "_t": token,
            "_ts": timestamp,
        }
        api_url = (
            "https://mangataro.org/auth/manga-chapters?"
            + urlencode(params, doseq=True)
        )

        try:
            response = make_request(api_url, scraper)
            data = response.json()
        except Exception:
            return []

        if not isinstance(data, dict) or not data.get("success"):
            return []

        entries = data.get("chapters") or []
        chapters: List[Dict] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            chapter_id = (entry.get("id") or "").strip()
            if not chapter_id:
                continue
            chapter_url = entry.get("url") or ""
            if not chapter_url:
                continue
            chapter_url = urljoin("https://mangataro.org/", chapter_url)
            chap_number = (entry.get("chapter") or "").strip()
            if not chap_number:
                continue

            likes_raw = entry.get("likes")
            try:
                likes = int(likes_raw)
            except Exception:
                likes = 0

            chapters.append(
                {
                    "hid": chapter_id,
                    "chap": chap_number,
                    "url": chapter_url,
                    "group_name": (entry.get("group_name") or "").strip() or None,
                    "title": (entry.get("title") or "").strip() or None,
                    "lang": (entry.get("language") or "").strip() or None,
                    "up_count": likes,
                }
            )

        return chapters

    def _generate_api_signature(self) -> tuple[str, int]:
        timestamp = int(time.time())
        hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
        secret = f"mng_ch_{hour}"
        digest = hashlib.md5(f"{timestamp}{secret}".encode("utf-8")).hexdigest()
        return digest[:16], timestamp


def _split_people(text: str) -> List[str]:
    parts = re.split(r"[,&/]+", text)
    return [p.strip() for p in parts if p.strip()]


def _looks_like_page_image(url: str) -> bool:
    lowered = url.lower()
    if any(ext in lowered for ext in (".jpg", ".jpeg", ".png", ".webp", ".avif")):
        return True
    return False


def _looks_like_non_page_asset(url: str, tag) -> bool:
    lowered = url.lower()
    if any(
        keyword in lowered
        for keyword in (
            "group-avatars",
            "avatars/",
            "tarop.png",
            "logo",
            "banner",
        )
    ):
        return True

    classes = " ".join(tag.get("class", [])).lower()
    if any(
        keyword in classes
        for keyword in ("avatar", "logo", "banner", "author-avatar")
    ):
        return True

    alt = (tag.get("alt") or "").lower()
    if any(keyword in alt for keyword in ("avatar", "logo", "banner")):
        return True

    return False


def _first_src_from_srcset(tag) -> Optional[str]:
    srcset = tag.get("data-srcset") or tag.get("srcset")
    if not srcset:
        return None
    first = srcset.split(",")[0].strip()
    if not first:
        return None
    return first.split(" ")[0]


def _extract_images_from_scripts(html: str, base_url: str) -> List[str]:
    pattern = re.compile(
        r"(https?://[^\s\"']+\.(?:jpg|jpeg|png|webp|avif))(?:\?[^\"'\s]*)?",
        re.IGNORECASE,
    )
    urls = []
    for match in pattern.findall(html):
        url = match
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = urljoin(base_url, url)
        if url not in urls:
            urls.append(url)
    return urls


__all__ = ["MangataroSiteHandler"]
