from __future__ import annotations

import re
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, FeatureNotFound
import requests

from .base import BaseSiteHandler, SiteComicContext


@dataclass
class _MadaraChapter:
    url: str
    title: str
    date_text: Optional[str]


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

    def __init__(self, site_name: str, base_url: str, extra_domains: Optional[Iterable[str]] = None) -> None:
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
                values.extend([p.strip() for p in people if p.strip()])
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
        cover = soup.select_one(".summary_image img, .img-responsive, div.thumb img")
        if not cover:
            return None
        src = cover.get("data-src") or cover.get("data-lazy-src") or cover.get("src")
        if not src:
            return None
        src = src.strip()
        if src.startswith("//"):
            return "https:" + src
        if src.startswith("/"):
            return urljoin(page_url, src)
        if src.startswith("http"):
            return src
        return urljoin(page_url, src)

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
        scraper.headers.setdefault("Referer", self.base_url + "/")
        scraper.headers.setdefault("Origin", self.base_url)
        scraper.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        response = make_request(url, scraper)
        html = response.text
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
            response = make_request(url, scraper)
            soup = self._make_soup(response.text)
        page_url = None
        source_url = context.comic.get("url")
        if isinstance(source_url, str):
            page_url = source_url
        return self._parse_chapters(soup, scraper, page_url=page_url)

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        chapter_url = chapter.get("url")
        if not chapter_url:
            raise RuntimeError("Chapter URL missing.")
        response = make_request(chapter_url, scraper)
        soup = self._make_soup(response.text)

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


__all__ = ["MadaraSiteHandler"]
