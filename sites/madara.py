from __future__ import annotations

import re
import json
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup, FeatureNotFound
import requests

from .base import BaseSiteHandler, SearchHit, SiteComicContext


MADARA_SEARCH_COVER_FALLBACK_LIMIT = 5


@dataclass
class _MadaraChapter:
    url: str
    title: str
    date_text: Optional[str]


def _normalize_madara_image_url(src: Optional[str], page_url: str) -> Optional[str]:
    if not isinstance(src, str):
        return None
    src = src.strip()
    if not src:
        return None
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return urljoin(page_url, src)
    if not src.startswith(("http://", "https://")):
        return urljoin(page_url, src)
    return src


def _extract_madara_cover_from_soup(soup: BeautifulSoup, page_url: str) -> Optional[str]:
    for selector in (
        ".summary_image img",
        "div.thumb img",
        ".post-thumbnail img",
        ".manga-post-image",
        ".img-responsive",
    ):
        for img in soup.select(selector):
            src = (
                img.get("data-src")
                or img.get("data-lazy-src")
                or img.get("data-cfsrc")
                or img.get("data-original")
                or img.get("src")
            )
            src_norm = _normalize_madara_image_url(src, page_url)
            if not src_norm:
                continue
            src_lower = src_norm.lower()
            # Skip logos, icons, avatars, banners, and headers.
            if any(
                x in src_lower
                for x in (
                    "/logo",
                    "logo.png",
                    "logo.jpg",
                    "logo.webp",
                    "logo_",
                    "logo-",
                    "banner",
                    "avatar",
                    "icon",
                )
            ):
                continue

            # Verify that the image is not inside a header or nav container.
            parent = img.parent
            is_logo = False
            while parent and parent.name not in ("body", "html"):
                if parent.name in ("header", "nav"):
                    is_logo = True
                    break
                p_class = " ".join(parent.get("class") or []).lower()
                p_id = (parent.get("id") or "").lower()
                if any(
                    x in p_class or x in p_id
                    for x in ("logo", "header", "nav", "menu", "brand")
                ):
                    is_logo = True
                    break
                parent = parent.parent
            if is_logo:
                continue
            return src_norm
    return None


def _extract_madara_cover_from_thumbnail(value: Any, base_url: str) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.startswith(("http://", "https://", "//", "/")):
        return _normalize_madara_image_url(text, base_url)
    if "<img" not in text.lower():
        return None
    try:
        soup = BeautifulSoup(text, "html.parser")
    except Exception:
        return None
    img = soup.select_one("img")
    if not img:
        return None
    return _normalize_madara_image_url(
        img.get("data-src")
        or img.get("data-lazy-src")
        or img.get("data-cfsrc")
        or img.get("data-original")
        or img.get("src"),
        base_url,
    )


def _madara_match_key(value: Optional[str]) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^0-9a-z]+", "", value.lower())


def _madara_slug_from_url(value: str) -> str:
    try:
        parsed = urlparse(value)
    except Exception:
        return ""
    parts = [p for p in parsed.path.split("/") if p]
    return parts[-1] if parts else ""


def _fetch_madara_search_cover(hit_url: str, scraper) -> Optional[str]:
    if not hit_url:
        return None
    try:
        response = scraper.get(
            hit_url,
            headers={"Referer": hit_url},
            timeout=8,
        )
    except Exception:
        return None
    if getattr(response, "status_code", 0) >= 400:
        return None
    try:
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception:
        return None
    return _extract_madara_cover_from_soup(soup, hit_url)


def _media_item_image_url(item: Dict[str, Any], base_url: str) -> Optional[str]:
    details = item.get("media_details")
    if isinstance(details, dict):
        sizes = details.get("sizes")
        if isinstance(sizes, dict):
            for size_name in ("medium", "large", "full", "thumbnail"):
                size = sizes.get(size_name)
                if isinstance(size, dict):
                    url = _normalize_madara_image_url(size.get("source_url"), base_url)
                    if url:
                        return url
    url = _normalize_madara_image_url(item.get("source_url"), base_url)
    if url:
        return url
    guid = item.get("guid")
    if isinstance(guid, dict):
        url = _normalize_madara_image_url(guid.get("rendered"), base_url)
        if url:
            return url
    description = item.get("description")
    if isinstance(description, dict):
        rendered = description.get("rendered")
        if isinstance(rendered, str) and "<img" in rendered.lower():
            try:
                soup = BeautifulSoup(rendered, "html.parser")
                img = soup.select_one("img")
                if img:
                    return _normalize_madara_image_url(
                        img.get("data-src")
                        or img.get("data-lazy-src")
                        or img.get("data-cfsrc")
                        or img.get("data-original")
                        or img.get("src"),
                        base_url,
                    )
            except Exception:
                return None
    return None


def _media_item_matches(item: Dict[str, Any], *, target_slug: str, target_title: str) -> bool:
    slug_key = _madara_match_key(target_slug)
    title_key = _madara_match_key(target_title)
    item_slug_key = _madara_match_key(item.get("slug"))
    item_title = item.get("title")
    item_title_key = ""
    if isinstance(item_title, dict):
        item_title_key = _madara_match_key(item_title.get("rendered"))
    elif isinstance(item_title, str):
        item_title_key = _madara_match_key(item_title)

    if slug_key and item_slug_key and item_slug_key.startswith(slug_key):
        return True
    if title_key and item_title_key and item_title_key == title_key:
        return True

    url = _media_item_image_url(item, "")
    filename_key = _madara_match_key((urlparse(url).path.rsplit("/", 1)[-1] if url else ""))
    return bool(slug_key and filename_key and filename_key.startswith(slug_key))


def _fetch_madara_media_cover(
    *,
    base_url: str,
    hit_url: str,
    title: str,
    scraper,
) -> Optional[str]:
    """Fallback for CF-blocked Madara series pages.

    A few sites leave /wp-admin/admin-ajax.php and /wp-json/wp/v2/media open
    while Cloudflare blocks the public series page. Search the media library
    by the series slug first and accept only close slug/title matches to avoid
    assigning a similarly-named series' cover.
    """
    slug = _madara_slug_from_url(hit_url)
    queries = [q for q in (slug, title) if q]
    seen_queries: set = set()
    for query in queries:
        q_key = query.lower()
        if q_key in seen_queries:
            continue
        seen_queries.add(q_key)
        url = (
            f"{base_url.rstrip('/')}/wp-json/wp/v2/media"
            f"?search={quote_plus(query)}&per_page=5"
        )
        try:
            response = scraper.get(
                url,
                headers={"Referer": base_url.rstrip("/") + "/"},
                timeout=8,
            )
        except Exception:
            continue
        if getattr(response, "status_code", 0) >= 400:
            continue
        try:
            payload = response.json()
        except Exception:
            try:
                payload = json.loads(response.text)
            except Exception:
                continue
        if not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, dict):
                continue
            if not _media_item_matches(item, target_slug=slug, target_title=title):
                continue
            cover = _media_item_image_url(item, base_url)
            if cover:
                return cover
    return None


def _cover_ext_from_response(cover_url: str, response) -> str:
    path = urlparse(cover_url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if path.endswith(ext):
            return ext
    ctype = (getattr(response, "headers", {}) or {}).get("content-type", "").lower()
    if "png" in ctype:
        return ".png"
    if "webp" in ctype:
        return ".webp"
    if "gif" in ctype:
        return ".gif"
    return ".jpg"


def _cache_madara_display_cover(
    cover_url: Optional[str],
    *,
    referer: str,
    scraper,
) -> Optional[str]:
    if not isinstance(cover_url, str) or not cover_url.startswith(("http://", "https://")):
        return None

    cache_dir = os.path.join(os.path.expanduser("~"), ".aio-dl", "cache", "search_covers")
    key = hashlib.sha256(cover_url.encode("utf-8")).hexdigest()[:32]

    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        return None

    # Reuse any previous successful cache entry regardless of extension.
    try:
        for name in os.listdir(cache_dir):
            if name.startswith(key + "."):
                path = os.path.join(cache_dir, name)
                if os.path.getsize(path) > 256:
                    return Path(path).as_uri().replace("file://", "localfile://", 1)
    except OSError:
        pass

    try:
        response = scraper.get(
            cover_url,
            headers={"Referer": referer or cover_url},
            timeout=10,
        )
    except Exception:
        return None
    if getattr(response, "status_code", 0) >= 400:
        return None
    data = getattr(response, "content", None)
    if not isinstance(data, (bytes, bytearray)) or len(data) < 256:
        return None
    ctype = (getattr(response, "headers", {}) or {}).get("content-type", "").lower()
    if ctype and "image/" not in ctype and "application/octet-stream" not in ctype:
        return None

    ext = _cover_ext_from_response(cover_url, response)
    path = os.path.join(cache_dir, key + ext)
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(bytes(data))
        os.replace(tmp, path)
        return Path(path).as_uri().replace("file://", "localfile://", 1)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None


class MadaraSiteHandler(BaseSiteHandler):
    """
    Shared scraper for Madara/MadTheme based sites.

    Many of the requested sources (MangaBuddy, MangaBin, SumManga, etc.)
    follow the same HTML structure. This base handler keeps the per-site
    implementation lightweight while still allowing overrides if a site
    deviates from the defaults.
    """

    chapter_selectors: Sequence[str] = (
        "li.wp-manga-chapter",
        "div#chapterlist li",
        "div#chapter-chap li",
        "ul.main.version-chap li",
    )

    reader_selectors: Sequence[str] = (
        "div.reading-content img",
        "div#chapter-images img",
        "div.page-break img",
    )

    def __init__(
        self,
        site_name: str,
        base_url: str,
        extra_domains: Optional[Iterable[str]] = None,
        use_zendriver: bool = False,
    ) -> None:
        super().__init__()
        self.site_name = site_name
        self.base_url = base_url.rstrip("/")
        parsed = urlparse(self.base_url)
        domains = {parsed.netloc}
        if parsed.netloc.startswith("www."):
            domains.add(parsed.netloc[4:])
        else:
            domains.add(f"www.{parsed.netloc}")
        if extra_domains:
            domains.update(extra_domains)
        self.domains = tuple(sorted(domains))
        self.name = site_name
        self.use_zendriver = use_zendriver

        try:
            import lxml  # type: ignore  # noqa: F401

            self._parser = "lxml"
        except Exception:
            self._parser = "html.parser"

    # ------------------------------------------------------------------ helpers
    def _make_soup(self, html: str) -> BeautifulSoup:
        try:
            return BeautifulSoup(html, self._parser)
        except FeatureNotFound:
            return BeautifulSoup(html, "html.parser")

    def _slug_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            return parsed.netloc
        if parts[-1] == "manga":
            return parts[-2] if len(parts) >= 2 else "series"
        return parts[-1]

    def _extract_text(self, soup: BeautifulSoup, selectors: Sequence[str]) -> Optional[str]:
        for selector in selectors:
            node = soup.select_one(selector)
            if node:
                text = node.get_text(strip=True)
                if text:
                    return text
        return None

    def _extract_people(self, soup: BeautifulSoup, labels: Iterable[str]) -> List[str]:
        values: List[str] = []
        rows = soup.select(".post-content_item")
        lowered = [lbl.lower() for lbl in labels]
        for row in rows:
            heading = row.select_one(".summary-heading")
            content = row.select_one(".summary-content")
            if not heading or not content:
                continue
            label = heading.get_text(strip=True).lower()
            if any(lbl in label for lbl in lowered):
                people = re.split(r"[,/]", content.get_text(" ", strip=True))
                for p in people:
                    p = p.strip()
                    if not p:
                        continue
                    # Reject pure-digit values. Some Madara child sites stuff
                    # release years into mislabeled rows (e.g. a series page
                    # whose "Artist" cell renders "2021" — a year, not a
                    # person). Real author/artist names contain letters.
                    # Regression guard:
                    # tests/test_komikku_metadata.py::test_madara_extract_people_rejects_digits
                    if p.isdigit():
                        continue
                    values.append(p)
        return values

    def _extract_alt_titles(self, soup: BeautifulSoup) -> List[str]:
        alternatives: List[str] = []
        for selector in (
            ".summary-heading:-soup-contains('Alternative') + .summary-content",
            ".alternative > span",
        ):
            for node in soup.select(selector):
                text = node.get_text(" ", strip=True)
                if text:
                    for part in re.split(r"[,;/]", text):
                        cleaned = part.strip()
                        if cleaned and cleaned not in alternatives:
                            alternatives.append(cleaned)
        return alternatives

    def _extract_description(self, soup: BeautifulSoup) -> Optional[str]:
        for selector in (".summary__content", ".description-summary p", ".description p", ".summary p"):
            node = soup.select_one(selector)
            if node:
                text = node.get_text("\n", strip=True)
                if text:
                    return text
        return None

    def _extract_cover(self, soup: BeautifulSoup, page_url: str) -> Optional[str]:
        return _extract_madara_cover_from_soup(soup, page_url)

    def _extract_genres(self, soup: BeautifulSoup) -> List[str]:
        entries = [a.get_text(strip=True) for a in soup.select(".genres-content a, .genres a")]
        return [e for e in entries if e]

    def _extract_chapter_number(self, title: str) -> Optional[str]:
        match = re.search(r"(\d+(?:\.\d+)?)", title)
        return match.group(1) if match else None

    def _parse_date(self, text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        return text.strip()

    def _collect_chapter_elements(self, soup: BeautifulSoup) -> List[_MadaraChapter]:
        chapters: List[_MadaraChapter] = []
        for selector in self.chapter_selectors:
            for node in soup.select(selector):
                link = node.select_one("a")
                if not link:
                    continue
                href = link.get("href")
                if not href:
                    continue
                # Ensure URL is absolute (fixes Mangabuddy relative URLs)
                href = urljoin(self.base_url, href)
                
                title = link.get_text(" ", strip=True)
                date_node = node.select_one(".chapter-release-date, .chapter-release-time, .chapter-release > span")
                date_text = date_node.get_text(strip=True) if date_node else None
                chapters.append(_MadaraChapter(url=href, title=title, date_text=date_text))
        return chapters

    def _extract_from_chapter_selector(self, soup: BeautifulSoup, page_url: str) -> List[_MadaraChapter]:
        """
        Fallback for sites like manytoon where chapter list isn't in HTML,
        but we can try to load a chapter page and extract from the dropdown selector.
        """
        chapters: List[_MadaraChapter] = []

        # Try common first chapter URLs
        from urllib.parse import urljoin
        potential_chapter_urls = [
            urljoin(page_url, "chapter-1/"),
            urljoin(page_url, "chapter-01/"),
            urljoin(page_url, "chapter-001/"),
        ]

        for test_url in potential_chapter_urls:
            try:
                import requests
                # Use basic requests here to avoid circular dependency
                response = requests.get(test_url, timeout=10)
                if response.status_code == 200:
                    # Try to parse chapter list from select dropdown
                    chapter_soup = self._make_soup(response.text)
                    selects = chapter_soup.find_all("select")

                    for select in selects:
                        options = select.find_all("option")
                        # Check if this looks like a chapter selector
                        if len(options) > 1:
                            sample_value = options[0].get("value", "")
                            if "chapter" in sample_value.lower():
                                # This is likely the chapter selector
                                for option in options:
                                    chapter_slug = option.get("value", "")
                                    chapter_title = option.get_text(strip=True)
                                    if chapter_slug and chapter_title:
                                        chapter_url = urljoin(page_url, f"{chapter_slug}/")
                                        chapters.append(_MadaraChapter(
                                            url=chapter_url,
                                            title=chapter_title,
                                            date_text=None
                                        ))
                                if chapters:
                                    return chapters

                    # No select found, try probing for chapters (last resort)
                    # This works for sites like adultwebtoon that are fully JS-driven
                    if not chapters:
                        chapters = self._probe_for_chapters(page_url, test_url)
                        if chapters:
                            return chapters

                    # Only break if we found chapters, otherwise try next URL format
                    if chapters:
                        break
            except Exception:
                continue

        return chapters

    def _probe_for_chapters(self, page_url: str, first_chapter_url: str) -> List[_MadaraChapter]:
        """
        Last-resort fallback: probe for chapters by testing sequential URLs.
        Only used when no other method works (e.g., adultwebtoon.com).

        NOTE: This method is not recommended as it makes many requests.
        It should only be used when all other methods fail.
        """
        import requests
        from urllib.parse import urljoin
        import re

        chapters: List[_MadaraChapter] = []

        # Detect the chapter number format from first_chapter_url
        # Examples: chapter-1, chapter-01, chapter-001
        padding = 0
        match = re.search(r'chapter-(\d+)', first_chapter_url)
        if match:
            chapter_num_str = match.group(1)
            padding = len(chapter_num_str)  # 1 = no padding, 2 = "01", 3 = "001"

        # Verify the first chapter actually has images before proceeding
        # Some sites return 200 for any chapter URL but only certain formats have content
        try:
            response = requests.get(first_chapter_url, timeout=10)
            if response.status_code == 200:
                # Check if page has images by looking for common image indicators
                has_images = (
                    'reading-content' in response.text or
                    '<img' in response.text and ('wp-manga-chapter-img' in response.text or 'chapter' in response.text.lower())
                )
                if not has_images:
                    return []
        except Exception:
            return []

        # Limit probing to avoid making too many requests
        max_probe = 100
        consecutive_failures = 0
        max_consecutive_failures = 3

        for i in range(1, max_probe + 1):
            try:
                # Format chapter number with detected padding
                if padding > 1:
                    chapter_slug = f"chapter-{i:0{padding}d}"
                else:
                    chapter_slug = f"chapter-{i}"

                chapter_url = urljoin(page_url, f"{chapter_slug}/")
                response = requests.head(chapter_url, timeout=5, allow_redirects=True)

                if response.status_code == 200:
                    chapters.append(_MadaraChapter(
                        url=chapter_url,
                        title=f"Chapter {i}",
                        date_text=None
                    ))
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= max_consecutive_failures:
                        # Stop if we hit 3 consecutive 404s
                        break
            except Exception:
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    break

        return chapters

    def _load_ajax_chapters(self, soup: BeautifulSoup, scraper, referer: Optional[str] = None) -> Optional[BeautifulSoup]:
        holder = soup.select_one("#manga-chapters-holder, #chapterlist")
        if not holder:
            return None
        post_id = holder.get("data-id") or holder.get("data-post-id")
        if not post_id:
            return None

        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self.base_url,
        }
        if referer:
            headers["Referer"] = referer

        # Method 1: Try direct ajax/chapters/ endpoint (used by some sites like cocomic.co)
        if referer:
            ajax_chapters_url = urljoin(referer, "ajax/chapters/")
            try:
                response = scraper.post(ajax_chapters_url, headers=headers)
                if response.status_code == 200 and len(response.text) > 100:
                    # Check if it looks like chapter HTML (not an error message)
                    if "wp-manga-chapter" in response.text or "chapter" in response.text.lower():
                        return self._make_soup(response.text)
            except Exception:
                pass

        # Method 2: Try standard wp-admin/admin-ajax.php endpoint
        ajax_url = urljoin(self.base_url + "/", "/wp-admin/admin-ajax.php")
        nonce = holder.get("data-nonce") or holder.get("data-security")
        payload = {"action": "manga_get_chapters", "manga": post_id}
        if nonce:
            payload["security"] = nonce
            payload["nonce"] = nonce

        try:
            response = scraper.post(ajax_url, data=payload, headers=headers)
            response.raise_for_status()
            if response.text and response.text != "0":
                return self._make_soup(response.text)
        except requests.HTTPError:
            pass
        except Exception:
            pass

        # Method 3: Try fallback action
        fallback_payload = {
            "action": "wp_manga_load_more_chapter",
            "manga": post_id,
            "page": 1,
            "order": "desc",
        }
        if nonce:
            fallback_payload["security"] = nonce
            fallback_payload["nonce"] = nonce
        try:
            response = scraper.post(ajax_url, data=fallback_payload, headers=headers)
            response.raise_for_status()
            if response.text and response.text != "0":
                return self._make_soup(response.text)
        except Exception:
            pass

        # All methods failed
        return None

    def _parse_chapters(self, soup: BeautifulSoup, scraper, page_url: Optional[str] = None) -> List[Dict]:
        chapters = self._collect_chapter_elements(soup)
        if not chapters:
            ajax_soup = self._load_ajax_chapters(soup, scraper, referer=page_url)
            if ajax_soup:
                chapters = self._collect_chapter_elements(ajax_soup)

        # Fallback: Try to extract from chapter selector dropdown (used by manytoon, etc.)
        if not chapters and page_url:
            chapters = self._extract_from_chapter_selector(soup, page_url)

        chapter_dicts: List[Dict] = []
        for chapter in chapters:
            chap_num = self._extract_chapter_number(chapter.title) or chapter.title
            chapter_dicts.append(
                {
                    "hid": chapter.url.rstrip("/"),
                    "chap": chap_num,
                    "title": chapter.title,
                    "url": chapter.url,
                    "uploaded": self._parse_date(chapter.date_text),
                }
            )
        return chapter_dicts

    def configure_session(self, scraper, args) -> None:
        base = self.base_url
        if hasattr(args, "comic_url") and args.comic_url:
            parsed = urlparse(args.comic_url)
            netloc = parsed.netloc
            netloc_no_www = netloc[4:] if netloc.startswith("www.") else netloc
            # Check if domain matches any in self.domains
            if any(d in (netloc, netloc_no_www) for d in self.domains):
                base = f"{parsed.scheme}://{netloc}"
                self.base_url = base

        scraper.headers["Referer"] = base + "/"
        scraper.headers["Origin"] = base
        scraper.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )


    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        if self.use_zendriver:
            from .crawlee_utils import fetch_html_with_cf_cookies, sync_cf_cookies

            html = fetch_html_with_cf_cookies(url, base_url=self.base_url)
            sync_cf_cookies(scraper, url)
        else:
            response = make_request(url, scraper)
            html = response.text
            try:
                from .crawlee_utils import ZENDRIVER_AVAILABLE, is_cf_challenge, fetch_html_with_cf_cookies, sync_cf_cookies

                if ZENDRIVER_AVAILABLE and is_cf_challenge(response.status_code, html):
                    html = fetch_html_with_cf_cookies(url, base_url=self.base_url)
                    sync_cf_cookies(scraper, url)
            except Exception:
                pass
        soup = self._make_soup(html)

        title = (
            self._extract_text(soup, ("h1.entry-title", ".post-title h1", "h1.manga-name"))
            or self._slug_from_url(url)
        )
        slug = self._slug_from_url(url)

        comic: Dict[str, object] = {
            "hid": slug,
            "title": title,
            "alt_names": self._extract_alt_titles(soup),
            "desc": self._extract_description(soup),
            "cover": self._extract_cover(soup, url),
            "url": url,
        }

        authors = self._extract_people(soup, ("author", "writer"))
        if authors:
            comic["authors"] = authors
        artists = self._extract_people(soup, ("artist", "illustrator"))
        if artists:
            comic["artists"] = artists
        genres = self._extract_genres(soup)
        if genres:
            comic["genres"] = genres

        comic["language"] = "en"

        return SiteComicContext(comic=comic, title=title, identifier=slug, soup=soup)

    def get_chapters(
        self,
        context: SiteComicContext,
        scraper,
        language: str,
        make_request,
    ) -> List[Dict]:
        soup = context.soup
        if soup is None:
            source_url = context.comic.get("url")
            url = source_url if isinstance(source_url, str) else urljoin(self.base_url + "/", context.identifier)
            if self.use_zendriver:
                from .crawlee_utils import fetch_html_with_cf_cookies, sync_cf_cookies

                html = fetch_html_with_cf_cookies(url, base_url=self.base_url)
                sync_cf_cookies(scraper, url)
            else:
                response = make_request(url, scraper)
                html = response.text
                try:
                    from .crawlee_utils import ZENDRIVER_AVAILABLE, is_cf_challenge, fetch_html_with_cf_cookies, sync_cf_cookies

                    if ZENDRIVER_AVAILABLE and is_cf_challenge(response.status_code, html):
                        html = fetch_html_with_cf_cookies(url, base_url=self.base_url)
                        sync_cf_cookies(scraper, url)
                except Exception:
                    pass
            soup = self._make_soup(html)
        page_url = None
        source_url = context.comic.get("url")
        if isinstance(source_url, str):
            page_url = source_url
        return self._parse_chapters(soup, scraper, page_url=page_url)

    # ---------------------------------------------------------------- search
    def search(
        self,
        query: str,
        scraper,
        make_request,
        *,
        language: str = "en",
        limit: int = 20,
    ) -> List[SearchHit]:
        return madara_search_via_admin_ajax(
            base_url=self.base_url,
            site_name=self.name,
            query=query,
            scraper=scraper,
            limit=limit,
        )

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        chapter_url = chapter.get("url")
        if not chapter_url:
            raise RuntimeError("Chapter URL missing.")
        if self.use_zendriver:
            from .crawlee_utils import fetch_html_with_cf_cookies, sync_cf_cookies

            html = fetch_html_with_cf_cookies(chapter_url, base_url=self.base_url)
            sync_cf_cookies(scraper, chapter_url)
        else:
            response = make_request(chapter_url, scraper)
            html = response.text
            try:
                from .crawlee_utils import ZENDRIVER_AVAILABLE, is_cf_challenge, fetch_html_with_cf_cookies, sync_cf_cookies

                if ZENDRIVER_AVAILABLE and is_cf_challenge(response.status_code, html):
                    html = fetch_html_with_cf_cookies(chapter_url, base_url=self.base_url)
                    sync_cf_cookies(scraper, chapter_url)
            except Exception:
                pass
        soup = self._make_soup(html)

        image_urls: List[str] = []
        for selector in self.reader_selectors:
            for img in soup.select(selector):
                src = (
                    img.get("data-src")
                    or img.get("data-srcset")
                    or img.get("data-cfsrc")
                    or img.get("src")
                )
                if not src:
                    continue
                src = src.strip()
                if not src:
                    continue
                if src.startswith("//"):
                    src = "https:" + src
                elif src.startswith("/"):
                    src = urljoin(chapter_url, src)
                elif not src.startswith("http"):
                    src = urljoin(chapter_url, src)
                if src not in image_urls:
                    image_urls.append(src)
            if image_urls:
                break

        if not image_urls:
            raise RuntimeError("Unable to locate images for chapter.")
        return image_urls


def madara_search_via_admin_ajax(
    *,
    base_url: str,
    site_name: str,
    query: str,
    scraper,
    limit: int = 20,
) -> List[SearchHit]:
    """Cross-site Madara search via the standard admin-ajax endpoint.

    All Madara/MadTheme WordPress sites expose:
      POST /wp-admin/admin-ajax.php
        action=wp-manga-search-manga
        title=<query>
    Returns JSON with two shapes seen in the wild:
      1. Rich (toonily, manhwa-flavored): {success, data:[{label, value, url,
         thumbnail, author, artist, is_mature}]}
      2. Simple (manhuaus, generic Madara): {success, data:[{title, url, type}]}
    On no-match: data is a single dict {error:"not found", message:...} rather
    than an empty list. We normalize both shapes to a list of SearchHit.

    Module-level helper so MadaraSiteHandler subclasses get this for free,
    AND non-MadaraSiteHandler-but-still-Madara handlers (ManhuaPlus, ManhuaUS)
    can call it without inheriting the rest of MadaraSiteHandler. Cross-file:
    sites/manhuaplus.py:search and sites/manhuaus.py:search both call this.

    HTTP errors propagate to the caller — orchestrator's _run_one catches and
    records in probe-failure cache. Wraps only JSON-parse and result-extraction
    in try/except (those should never blow up on malformed data; surfacing the
    parse error as an empty result is correct behavior).
    """
    clean = (query or "").strip()
    if not clean:
        return []

    base = base_url.rstrip("/")
    url = f"{base}/wp-admin/admin-ajax.php"
    response = scraper.post(
        url,
        data={"action": "wp-manga-search-manga", "title": clean},
        headers={
            "Referer": base + "/",
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=10,
    )
    if 500 <= response.status_code < 600:
        response.raise_for_status()
    if response.status_code != 200:
        return []

    try:
        payload = response.json()
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        if data.get("error"):
            return []
        data = [data]
    if not isinstance(data, list):
        return []

    hits: List[SearchHit] = []
    seen: set = set()
    cover_fallbacks_remaining = MADARA_SEARCH_COVER_FALLBACK_LIMIT
    for idx, item in enumerate(data):
        if len(hits) >= limit:
            break
        if not isinstance(item, dict):
            continue
        title = (item.get("label") or item.get("value") or item.get("title") or "").strip()
        if not title:
            continue
        hit_url = (item.get("url") or "").strip()
        if not hit_url:
            continue
        if hit_url.startswith("/"):
            hit_url = urljoin(base, hit_url)
        hit_url = hit_url.split("?")[0].split("#")[0]
        if hit_url in seen:
            continue
        seen.add(hit_url)

        cover = (
            _extract_madara_cover_from_thumbnail(item.get("thumbnail"), base)
            or _extract_madara_cover_from_thumbnail(item.get("cover"), base)
        )
        cover_from_fallback = False
        if not cover and cover_fallbacks_remaining > 0:
            # Several Madara sites (coffeemanga.ink, mangadistrict,
            # harimanga, webtoonscan, zinmanga, etc.) return the simple
            # AJAX shape: {title, url, type}, with no thumbnail. Fetch the
            # series page for the first few hits and reuse the same selectors
            # as fetch_comic_context so search cards still get covers.
            cover = _fetch_madara_search_cover(hit_url, scraper)
            if not cover:
                cover = _fetch_madara_media_cover(
                    base_url=base,
                    hit_url=hit_url,
                    title=title,
                    scraper=scraper,
                )
            cover_from_fallback = cover is not None
            cover_fallbacks_remaining -= 1
        if cover_from_fallback:
            cached_cover = _cache_madara_display_cover(
                cover,
                referer=hit_url,
                scraper=scraper,
            )
            if cached_cover:
                cover = cached_cover

        raw_score = max(0.05, 1.0 - (idx / max(1, len(data))))
        hits.append(
            SearchHit(
                site=site_name,
                title=title,
                url=hit_url,
                cover=cover,
                alt_titles=[],
                year=None,
                language=None,
                chapter_count_hint=None,
                raw_score=raw_score,
            )
        )
    return hits


__all__ = ["MadaraSiteHandler", "madara_search_via_admin_ajax"]
