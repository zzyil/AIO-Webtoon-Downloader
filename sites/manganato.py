from __future__ import annotations

from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, FeatureNotFound

from .base import BaseSiteHandler, SiteComicContext


class ManganatoSiteHandler(BaseSiteHandler):
    name = "manganato"
    domains = (
        "manganato.gg",
        "www.manganato.gg",
        "nelomanga.net",
        "www.nelomanga.net",
        "natomanga.com",
        "www.natomanga.com",
        "mangakakalot.gg",
        "www.mangakakalot.gg",
    )

    _BASE_URL = "https://www.manganato.gg"

    def __init__(self) -> None:
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
            raise RuntimeError("Invalid Manganato URL.")
        if parts[0] != "manga":
            raise RuntimeError("Only /manga/... URLs are supported for Manganato.")
        return parts[1]

    def _absolute(self, base: str, href: Optional[str]) -> Optional[str]:
        if not href:
            return None
        href = href.strip()
        if not href:
            return None
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("http"):
            return href
        return urljoin(base, href)

    def _extract_text(self, soup: BeautifulSoup, selectors: List[str]) -> Optional[str]:
        for selector in selectors:
            node = soup.select_one(selector)
            if node:
                text = node.get_text(strip=True)
                if text:
                    return text
        return None

    def _collect_chapter_rows(self, soup: BeautifulSoup) -> List[Dict]:
        results: List[Dict] = []
        selectors = [
            ".list-chapter li a",
            ".chapter-list li a",
            ".row-content-chapter li a",
            ".chapter-list .row a",
        ]
        seen = set()
        for selector in selectors:
            for anchor in soup.select(selector):
                href = anchor.get("href")
                if not href:
                    continue
                absolute = self._absolute(self._BASE_URL, href)
                if not absolute or absolute in seen:
                    continue
                seen.add(absolute)
                title = anchor.get_text(strip=True)
                date_node = anchor.find_next("span")
                uploaded = date_node.get_text(strip=True) if date_node else None
                results.append(
                    {
                        "title": title or absolute.rsplit("/", 1)[-1],
                        "url": absolute,
                        "uploaded": uploaded,
                    }
                )
        return results

    def _chapter_number(self, title: str) -> str:
        for token in title.replace("#", " ").split():
            if token.replace(".", "", 1).isdigit():
                return token
        return title

    # ----------------------------------------------------------- Base overrides
    def configure_session(self, scraper, args) -> None:
        scraper.headers.setdefault("Referer", self._BASE_URL + "/")

    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        slug = self._slug_from_url(url)
        response = make_request(url, scraper)
        soup = self._make_soup(response.text)

        title = self._extract_text(soup, ["h1.manga-info-title", ".manga-info-text h1", "h1"])
        if not title:
            title = slug.replace("-", " ").title()

        cover = None
        cover_img = soup.select_one(".manga-info-pic img, .info-image img, .manga-info-image img")
        if cover_img:
            cover = self._absolute(url, cover_img.get("data-src") or cover_img.get("data-original") or cover_img.get("src"))

        description = self._extract_text(
            soup,
            [
                "#summary",
                "#noidungm",
                ".description",
                ".manga-info-content",
            ],
        )

        authors: List[str] = []
        for item in soup.select(".manga-info-text li"):
            label = item.find("h2")
            if not label:
                continue
            label_text = label.get_text(strip=True).lower()
            if "author" in label_text:
                content = item.get_text(" ", strip=True)
                parts = content.split(":", 1)
                if len(parts) == 2:
                    authors = [a.strip() for a in parts[1].split(",") if a.strip()]
                break

        genres = [
            a.get_text(strip=True)
            for a in soup.select(".manga-info-text a[href*='/genre/'], .manga-info-genres a")
            if a.get_text(strip=True)
        ]

        comic: Dict[str, object] = {
            "hid": slug,
            "title": title,
            "desc": description,
            "cover": cover,
            "authors": authors,
            "genres": genres,
            "url": url,
        }
        return SiteComicContext(comic=comic, title=title, identifier=slug, soup=soup)

    def get_chapters(self, context: SiteComicContext, scraper, language: str, make_request) -> List[Dict]:
        soup = context.soup
        if soup is None:
            response = make_request(context.comic.get("url"), scraper)
            soup = self._make_soup(response.text)
        chapter_rows = self._collect_chapter_rows(soup)
        chapters: List[Dict] = []
        for row in chapter_rows:
            number = self._chapter_number(row["title"])
            chapters.append(
                {
                    "hid": row["url"],
                    "chap": number,
                    "title": row["title"],
                    "url": row["url"],
                    "uploaded": row.get("uploaded"),
                }
            )
        chapters.sort(key=lambda c: float(c.get("chap") or 0), reverse=True)
        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        url = chapter.get("url")
        if not isinstance(url, str):
            raise RuntimeError("Chapter URL missing for Manganato.")
        response = make_request(url, scraper)
        soup = self._make_soup(response.text)
        images: List[str] = []
        for img in soup.select("#chapter-content img, .reading-detail img, .page_chapter img, .container-chapter-reader img"):
            src = img.get("data-src") or img.get("data-original") or img.get("src")
            absolute = self._absolute(url, src)
            if absolute and absolute not in images:
                images.append(absolute)
        if not images:
            raise RuntimeError("Unable to locate Manganato chapter images.")
        return images


__all__ = ["ManganatoSiteHandler"]
