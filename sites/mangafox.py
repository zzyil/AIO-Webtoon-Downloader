from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, FeatureNotFound

from .base import BaseSiteHandler, SiteComicContext


class MangaFoxSiteHandler(BaseSiteHandler):
    name = "mangafox"
    domains = ("fanfox.net", "www.fanfox.net", "m.fanfox.net")

    _BASE_URL = "https://fanfox.net"
    _MOBILE_URL = "https://m.fanfox.net"

    def __init__(self) -> None:
        super().__init__()
        try:
            import lxml  # type: ignore  # noqa: F401

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
        scraper.headers.setdefault("User-Agent", "Mozilla/5.0")
        scraper.cookies.set("isAdult", "1", domain="fanfox.net", path="/")
        scraper.cookies.set("isAdult", "1", domain=".fanfox.net", path="/")
        scraper.cookies.set("isAdult", "1", domain="www.fanfox.net", path="/")
        scraper.cookies.set("readway", "2", domain="m.fanfox.net", path="/")

    def _slug_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            return parsed.netloc
        if parts[0] == "manga":
            return parts[-1].split(".")[0]
        return parts[-1]

    def _parse_status(self, text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        lowered = text.lower()
        if "ongoing" in lowered:
            return "Ongoing"
        if "completed" in lowered:
            return "Completed"
        return text.strip()

    def _parse_chapter_date(self, text: str) -> int:
        text = text.strip()
        if not text:
            return 0
        lowered = text.lower()
        today = dt.datetime.utcnow().date()
        if "today" in lowered or "ago" in lowered:
            return int(dt.datetime.combine(today, dt.time()).timestamp())
        if "yesterday" in lowered:
            day = today - dt.timedelta(days=1)
            return int(dt.datetime.combine(day, dt.time()).timestamp())
        for fmt in ("%b %d,%Y", "%b %d, %Y"):
            try:
                return int(dt.datetime.strptime(text, fmt).timestamp())
            except ValueError:
                continue
        return 0

    def _extract_cover(self, soup: BeautifulSoup, page_url: str) -> Optional[str]:
        img = soup.select_one(".detail-info-cover-img")
        if not img:
            return None
        src = img.get("src")
        if not src:
            return None
        if src.startswith("//"):
            return "https:" + src
        if src.startswith("http"):
            return src
        return urljoin(page_url, src)

    # ----------------------------------------------------------- Base overrides
    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        response = make_request(url, scraper)
        soup = self._make_soup(response.text)
        slug = self._slug_from_url(url)

        title_node = soup.select_one("h1.detail-info-title")
        title = title_node.get_text(strip=True) if title_node else slug

        info = soup.select_one(".detail-info-right")
        authors = []
        genres = []
        description = None
        status_text = None
        if info:
            authors = [a.get_text(strip=True) for a in info.select(".detail-info-right-say a")]
            genres = [a.get_text(strip=True) for a in info.select(".detail-info-right-tag-list a")]
            desc_node = info.select_one("p.fullcontent")
            description = desc_node.get_text(strip=True) if desc_node else None
            status_node = info.select_one(".detail-info-right-title-tip")
            status_text = self._parse_status(status_node.get_text(strip=True) if status_node else None)

        comic: Dict[str, object] = {
            "hid": slug,
            "title": title,
            "desc": description,
            "cover": self._extract_cover(soup, url),
            "authors": authors,
            "genres": genres,
            "status": status_text,
            "url": url,
        }

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
            response = make_request(context.comic.get("url", ""), scraper)
            soup = self._make_soup(response.text)

        chapters: List[Dict] = []
        for anchor in soup.select("ul.detail-main-list li a"):
            href = anchor.get("href")
            if not href:
                continue
            title_node = anchor.select_one(".detail-main-list-main p")
            name = title_node.get_text(strip=True) if title_node else anchor.get_text(strip=True)
            date_node = anchor.select(".detail-main-list-main p")
            uploaded = 0
            if len(date_node) >= 2:
                uploaded = self._parse_chapter_date(date_node[-1].get_text(strip=True))
            chapters.append(
                {
                    "hid": href.rstrip("/"),
                    "chap": name,
                    "title": name,
                    "url": urljoin(self._BASE_URL, href),
                    "uploaded": uploaded,
                }
            )
        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        chapter_url = chapter.get("url")
        if not chapter_url:
            raise RuntimeError("Chapter URL missing for MangaFox.")

        parsed = urlparse(chapter_url)
        mobile_path = parsed.path.replace("/manga/", "/roll_manga/")
        mobile_url = urljoin(self._MOBILE_URL, mobile_path)

        response = make_request(mobile_url, scraper)
        soup = self._make_soup(response.text)

        images: List[str] = []
        for img in soup.select("#viewer img"):
            src = img.get("data-original") or img.get("data-src") or img.get("src")
            if not src:
                continue
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = urljoin(mobile_url, src)
            elif not src.startswith("http"):
                src = urljoin(mobile_url, src)
            images.append(src)
        if not images:
            raise RuntimeError("No images found for MangaFox chapter.")
        return images


__all__ = ["MangaFoxSiteHandler"]
