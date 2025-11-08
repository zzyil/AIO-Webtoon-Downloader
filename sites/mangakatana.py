from __future__ import annotations

import datetime as dt
import re
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, FeatureNotFound

from .base import BaseSiteHandler, SiteComicContext


class MangaKatanaSiteHandler(BaseSiteHandler):
    name = "mangakatana"
    domains = (
        "mangakatana.com",
        "www.mangakatana.com",
    )

    _BASE_URL = "https://mangakatana.com"
    _DATE_FORMAT = "%b-%d-%Y"

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

    def _slug_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if parts and parts[0] == "manga":
            return parts[-1]
        return parts[-1] if parts else parsed.netloc

    def _parse_date(self, text: str) -> int:
        try:
            return int(dt.datetime.strptime(text, self._DATE_FORMAT).timestamp())
        except ValueError:
            return 0

    def _extract_chapter_number(self, text: str) -> Optional[str]:
        match = re.search(r"(\d+(?:\.\d+)?)", text)
        return match.group(1) if match else None

    def _extract_thumbnail(self, soup: BeautifulSoup, page_url: str) -> Optional[str]:
        node = soup.select_one("div.media div.cover img, .cover img")
        if not node:
            return None
        src = node.get("src")
        if not src:
            return None
        if src.startswith("//"):
            return "https:" + src
        if src.startswith("http"):
            return src
        return urljoin(page_url, src)

    def _extract_description(self, soup: BeautifulSoup) -> Optional[str]:
        pieces = []
        summary = soup.select_one(".summary > p")
        if summary:
            pieces.append(summary.get_text(strip=True))
        alt_name = soup.select_one(".alt_name")
        if alt_name:
            text = alt_name.get_text(strip=True)
            if text:
                pieces.append(f"Alt names: {text}")
        return "\n\n".join(pieces) or None

    # ----------------------------------------------------------- Base overrides
    def configure_session(self, scraper, args) -> None:
        scraper.headers.setdefault("Referer", self._BASE_URL + "/")

    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        response = make_request(url, scraper)
        soup = self._make_soup(response.text)
        slug = self._slug_from_url(url)

        title_node = soup.select_one("h1.heading")
        title = title_node.get_text(strip=True) if title_node else slug

        authors = [a.get_text(strip=True) for a in soup.select(".author a") if a.get_text(strip=True)]
        genres = [a.get_text(strip=True) for a in soup.select(".genres a") if a.get_text(strip=True)]

        comic: Dict[str, object] = {
            "hid": slug,
            "title": title,
            "desc": self._extract_description(soup),
            "cover": self._extract_thumbnail(soup, url),
            "authors": authors,
            "genres": genres,
            "url": url,
        }

        status_text = soup.select_one(".value.status")
        if status_text:
            status = status_text.get_text(strip=True)
            if "ongoing" in status.lower():
                comic["status"] = "Ongoing"
            elif "complete" in status.lower():
                comic["status"] = "Completed"

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
            response = make_request(context.comic["url"], scraper)
            soup = self._make_soup(response.text)
        chapters: List[Dict] = []
        for row in soup.select("tr:has(.chapter)"):
            link = row.select_one("a")
            if not link:
                continue
            href = link.get("href")
            if not href:
                continue
            name = link.get_text(strip=True)
            date_cell = row.select_one(".update_time")
            uploaded = self._parse_date(date_cell.get_text(strip=True)) if date_cell else 0
            chapters.append(
                {
                    "hid": href.rstrip("/"),
                    "chap": self._extract_chapter_number(name) or name,
                    "title": name,
                    "url": urljoin(self._BASE_URL, href),
                    "uploaded": uploaded,
                }
            )
        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        chapter_url = chapter.get("url")
        if not chapter_url:
            raise RuntimeError("Chapter URL missing for MangaKatana.")
        response = make_request(chapter_url, scraper)
        soup = self._make_soup(response.text)

        script = None
        for candidate in soup.select("script"):
            data = candidate.string or candidate.get_text()
            if data and "data-src" in data:
                script = data
                break
        if not script:
            raise RuntimeError("Unable to locate image script for MangaKatana.")

        array_name_match = re.search(r"data-src['\"],\s*(\w+)", script)
        if not array_name_match:
            raise RuntimeError("Unable to locate image array name for MangaKatana.")
        array_name = array_name_match.group(1)
        array_regex = re.compile(rf"var {array_name}=\[([^\]]+)]")
        array_match = array_regex.search(script)
        if not array_match:
            raise RuntimeError("Unable to parse image array for MangaKatana.")

        urls = re.findall(r"'([^']+)'", array_match.group(1))
        if not urls:
            raise RuntimeError("No images found for MangaKatana.")
        return [url if url.startswith("http") else urljoin(chapter_url, url) for url in urls]


__all__ = ["MangaKatanaSiteHandler"]
