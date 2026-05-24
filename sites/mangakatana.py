from __future__ import annotations

import datetime as dt
import re
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, FeatureNotFound

from .base import BaseSiteHandler, SearchHit, SiteComicContext


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

        # MangaKatana renders series metadata in a few different shapes across
        # series (status uses `.value.status` below which suggests label/value
        # rows). `.author a` works on some pages but the 2026-05-19 probe found
        # it empty on solo-leveling.21708. Layer fallbacks:
        #   1. canonical `.author a` (works on the bulk of pages),
        #   2. `a[href*='/authors/']` (the site's catalog-link convention),
        #   3. label/value row scan in `.d39 li` / `.meta li` blocks.
        # See dry_run_komikku_findings.md §A.
        authors = [
            a.get_text(strip=True)
            for a in soup.select(".author a")
            if a.get_text(strip=True)
        ]
        if not authors:
            seen: set = set()
            for a in soup.select("a[href*='/authors/'], a[href*='/author/']"):
                text = a.get_text(strip=True)
                if text and text not in seen:
                    seen.add(text)
                    authors.append(text)
        if not authors:
            for row in soup.select("ul.d39 li, .d39 li, ul.meta li, .meta li"):
                label_node = row.select_one(".label, .name")
                value_node = row.select_one(".value")
                if not label_node or not value_node:
                    continue
                if "author" not in label_node.get_text(strip=True).lower():
                    continue
                anchors = [
                    a.get_text(strip=True)
                    for a in value_node.select("a")
                    if a.get_text(strip=True)
                ]
                if anchors:
                    authors = anchors
                else:
                    txt = value_node.get_text(" ", strip=True)
                    if txt:
                        authors = [
                            p.strip() for p in re.split(r"[,/;]", txt) if p.strip()
                        ]
                break
        # MangaKatana does NOT expose a separate Artist field on its series
        # template (verified 2026-05-19 audit). `artists` stays empty by site
        # limitation; Komikku's details.json artist key reflects "no artist".
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

        # Alt names also surface here as a structured field. `_extract_description`
        # ALREADY merges them into desc as "Alt names: ..." for human display;
        # this second surfaces gives downstream consumers (Komikku details.json,
        # ComicInfo.xml) a clean list.
        alt_node = soup.select_one(".alt_name")
        if alt_node:
            alt_text = alt_node.get_text(" ", strip=True)
            alt_list = [p.strip() for p in re.split(r"[;,/|]", alt_text) if p.strip()]
            if alt_list:
                comic["alt_names"] = alt_list

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

    # ----------------------------------------------------------------- search
    # MangaKatana behaviour: GET /?search=<query>&search_by=book_name
    # If exactly one match → 302 redirect to /manga/<slug>.<id>.
    # If multiple → 200 results page with .item h3 a per result.
    # We follow redirects (default) so both shapes resolve to a parseable
    # response.text, then detect the single-vs-multi case via response.url.
    def search(
        self,
        query: str,
        scraper,
        make_request,
        *,
        language: str = "en",
        limit: int = 20,
    ) -> List[SearchHit]:
        clean = (query or "").strip()
        if not clean:
            return []
        from urllib.parse import quote_plus
        url = f"{self._BASE_URL}/?search={quote_plus(clean)}&search_by=book_name"
        # HTTP errors propagate; orchestrator records the host in the
        # probe-failure cache.
        response = make_request(url, scraper)
        html = response.text
        final_url = getattr(response, "url", "") or ""
        if not html or len(html) < 200:
            return []

        # Single-result redirect case: /manga/<slug>.<id>
        single_match = re.search(r"/manga/[\w\-]+\.\w+", final_url)
        if single_match:
            soup = self._make_soup(html)
            title_node = soup.select_one("h1.heading")
            title = title_node.get_text(strip=True) if title_node else self._slug_from_url(final_url)
            cover = self._extract_thumbnail(soup, final_url)
            alt_node = soup.select_one(".alt_name")
            alt_titles: List[str] = []
            if alt_node:
                for piece in re.split(r"[;,/]", alt_node.get_text(" ", strip=True)):
                    p = piece.strip()
                    if p and p != title:
                        alt_titles.append(p)
            return [
                SearchHit(
                    site=self.name,
                    title=title,
                    url=final_url.split("?")[0].split("#")[0],
                    cover=cover,
                    alt_titles=alt_titles,
                    year=None,
                    language=None,
                    chapter_count_hint=None,
                    raw_score=1.0,
                )
            ]

        # Multi-result case: parse .item h3 a per row.
        soup = self._make_soup(html)
        rows = soup.select("h3 a[href*='/manga/']")
        hits: List[SearchHit] = []
        seen: set = set()
        for idx, link in enumerate(rows):
            if len(hits) >= limit:
                break
            href = (link.get("href") or "").strip()
            if not href or "/manga/" not in href:
                continue
            abs_url = href if href.startswith("http") else urljoin(self._BASE_URL, href)
            abs_url = abs_url.split("?")[0].split("#")[0]
            if abs_url in seen:
                continue
            seen.add(abs_url)
            title = link.get_text(strip=True) or self._slug_from_url(abs_url)
            container = link.find_parent("div", class_=lambda c: c and "item" in c)
            cover: Optional[str] = None
            alt_titles: List[str] = []
            if container is not None:
                img = container.select_one("img")
                if img is not None:
                    src = img.get("data-src") or img.get("src")
                    if src:
                        cover = src if src.startswith("http") else urljoin(self._BASE_URL, src)
                alt_node = container.select_one(".text, .alt_name")
                if alt_node:
                    txt = alt_node.get_text(" ", strip=True)
                    if txt and txt != title:
                        alt_titles.append(txt)
            raw_score = max(0.05, 1.0 - (idx / max(1, len(rows))))
            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=abs_url,
                    cover=cover,
                    alt_titles=alt_titles,
                    year=None,
                    language=None,
                    chapter_count_hint=None,
                    raw_score=raw_score,
                )
            )
        return hits

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
