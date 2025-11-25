from __future__ import annotations

import base64
import json
import re
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, FeatureNotFound

from .base import BaseSiteHandler, SiteComicContext


class LikeMangaSiteHandler(BaseSiteHandler):
    name = "likemanga"
    domains = ("likemanga.ink", "www.likemanga.ink")

    _BASE_URL = "https://likemanga.ink"
    _DATE_RE = re.compile(r"^\s*(\w+\s+\d{1,2},\s+\d{4})")
    _CHAPTER_PAGE_REGEX = re.compile(r"load_list_chapter\((\d+)\)")

    def __init__(self) -> None:
        super().__init__()
        try:
            import lxml  # noqa: F401

            self._parser = "lxml"
        except Exception:
            self._parser = "html.parser"

    # ----------------------------------------------------------------- helpers
    def _make_soup(self, html: str) -> BeautifulSoup:
        try:
            return BeautifulSoup(html, self._parser)
        except FeatureNotFound:
            return BeautifulSoup(html, "html.parser")

    def configure_session(self, scraper, args) -> None:
        scraper.headers.setdefault("Referer", self._BASE_URL + "/")

    def _slug_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if parts and parts[0] == "manga":
            return parts[-1]
        return parts[-1] if parts else parsed.netloc

    def _img_attr(self, tag) -> Optional[str]:
        if tag is None:
            return None
        for attr in ("data-cfsrc", "data-src", "data-lazy-src", "srcset", "src"):
            val = tag.get(attr)
            if val:
                if attr == "srcset":
                    val = val.split(" ")[0]
                val = val.strip()
                if not val:
                    continue
                if val.startswith("//"):
                    return "https:" + val
                if val.startswith("http"):
                    return val
                return urljoin(self._BASE_URL, val)
        return None

    def _parse_date(self, text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        match = self._DATE_RE.search(text)
        return match.group(1) if match else text.strip()

    def _extract_people(self, soup: BeautifulSoup, selector: str) -> List[str]:
        node = soup.select_one(selector)
        if not node:
            return []
        entries = node.find_all("p")
        if len(entries) >= 2:
            text = entries[1].get_text(strip=True)
            if text.lower() == "updating":
                return []
            return [part.strip() for part in re.split(r"[,/]", text) if part.strip()]
        return []

    # ----------------------------------------------------------- Base overrides
    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        response = make_request(url, scraper)
        soup = self._make_soup(response.text)
        slug = self._slug_from_url(url)

        title = soup.select_one("#title-detail-manga").get_text(strip=True)
        description = soup.select_one("#summary_shortened")
        thumb = self._img_attr(soup.select_one(".detail-info img"))
        genres = [
            a.get_text(strip=True) for a in soup.select(".list-info a[href*='/genres/']") if a.get_text(strip=True)
        ]
        authors = self._extract_people(soup, ".list-info .author")
        status_node = soup.select_one(".list-info .status p:nth-of-type(2)")
        status_text = status_node.get_text(strip=True) if status_node else None

        comic: Dict[str, object] = {
            "hid": slug,
            "title": title,
            "desc": description.get_text(strip=True) if description else None,
            "cover": thumb,
            "genres": genres,
            "authors": authors,
            "status": status_text,
            "url": url,
        }

        return SiteComicContext(comic=comic, title=title, identifier=slug, soup=soup)

    def _collect_chapters_from_soup(self, soup: BeautifulSoup) -> List[Dict]:
        chapters: List[Dict] = []
        for item in soup.select(".wp-manga-chapter"):
            link = item.select_one("a")
            if not link:
                continue
            href = link.get("href")
            if not href:
                continue
            title = link.get_text(strip=True)
            date_node = item.select_one(".chapter-release-date")
            uploaded = self._parse_date(date_node.get_text(strip=True) if date_node else None)
            chapters.append(
                {
                    "hid": href.rstrip("/"),
                    "chap": title,
                    "title": title,
                    "url": urljoin(self._BASE_URL, href),
                    "uploaded": uploaded,
                }
            )
        return chapters

    def _fetch_chapter_page(self, manga_id: int, page: int, scraper) -> List[Dict]:
        params = {
            "act": "ajax",
            "code": "load_list_chapter",
            "manga_id": str(manga_id),
            "page_num": str(page),
            "chap_id": "0",
            "keyword": "",
        }
        response = scraper.get(self._BASE_URL, params=params, headers={"Referer": self._BASE_URL})
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("Invalid JSON when fetching LikeManga chapters.") from exc
        html = payload.get("list_chap")
        if not isinstance(html, str):
            return []
        fragment = BeautifulSoup(html, self._parser)
        return self._collect_chapters_from_soup(fragment)

    def get_chapters(
        self,
        context: SiteComicContext,
        scraper,
        language: str,
        make_request,
    ) -> List[Dict]:
        soup = context.soup
        if soup is None:
            response = make_request(context.comic.get("url", ""), scraper)
            soup = self._make_soup(response.text)
        chapters = self._collect_chapters_from_soup(soup)

        pagination = soup.select("div.chapters_pagination a:not(.next)")
        if pagination:
            last = pagination[-1].get("onclick") or ""
            match = self._CHAPTER_PAGE_REGEX.search(last)
            if match:
                try:
                    last_page = int(match.group(1))
                except ValueError:
                    last_page = 1
            else:
                last_page = 1
        else:
            last_page = 1

        manga_data = soup.select_one("#title-detail-manga")
        manga_id = None
        if manga_data:
            try:
                manga_id = int(manga_data.get("data-manga"))
            except (TypeError, ValueError):
                manga_id = None

        if manga_id and last_page > 1:
            for page in range(2, last_page + 1):
                chapters.extend(self._fetch_chapter_page(manga_id, page, scraper))

        return chapters

    def _decode_image_manifest(self, soup: BeautifulSoup) -> Optional[List[str]]:
        token_input = soup.select_one("div.reading input#next_img_token")
        if not token_input:
            return None
        token_value = token_input.get("value")
        if not token_value or "." not in token_value:
            return None
        token = token_value.split(".", 1)[1]
        try:
            decoded = base64.b64decode(token)
            data = json.loads(decoded.decode("utf-8"))
            encoded_array = data["data"]
            pages_json = base64.b64decode(encoded_array).decode("utf-8")
            pages = json.loads(pages_json)
        except (ValueError, KeyError) as exc:
            raise RuntimeError("Unable to decode LikeManga image manifest.") from exc
        cdn_input = soup.select_one("div.reading #currentlink")
        if not cdn_input:
            raise RuntimeError("Missing CDN URL for LikeManga chapter.")
        base = cdn_input.get("value")
        return [f"{base.rstrip('/')}/{item}" for item in pages if isinstance(item, str)]

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        chapter_url = chapter.get("url")
        if not chapter_url:
            raise RuntimeError("Chapter URL missing for LikeManga.")
        response = make_request(chapter_url, scraper)
        soup = self._make_soup(response.text)

        manifest = self._decode_image_manifest(soup)
        if manifest:
            return manifest

        images: List[str] = []
        for img in soup.select("div.reading-detail.box_doc img"):
            src = self._img_attr(img)
            if src and src not in images:
                images.append(src)
        if not images:
            raise RuntimeError("No images found for LikeManga chapter.")
        return images


__all__ = ["LikeMangaSiteHandler"]
