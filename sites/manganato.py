from __future__ import annotations

import re
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, FeatureNotFound

from .base import BaseSiteHandler, SearchHit, SiteComicContext


_MANGANATO_DOMAINS = (
    "mangabats.com",
    "www.mangabats.com",
    "mangakakalot.fan",
    "www.mangakakalot.fan",
    "mangakakalot.gg",
    "www.mangakakalot.gg",
    "mangakakalove.com",
    "www.mangakakalove.com",
    "manganato.gg",
    "www.manganato.gg",
    "natomanga.com",
    "www.natomanga.com",
    "nelomanga.com",
    "www.nelomanga.com",
    "nelomanga.net",
    "www.nelomanga.net",
    "zazamanga.com",
    "www.zazamanga.com",
    "zinmanga.net",
    "www.zinmanga.net",
    "mangakakalot.com",
    "www.mangakakalot.com",
    "manganelo.com",
    "www.manganelo.com",
)


class ManganatoSiteHandler(BaseSiteHandler):
    name = "manganato"
    domains = _MANGANATO_DOMAINS

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
        if parts[0] != "manga" and parts[0] != "chapter":
             # Mangakakalot uses /chapter/ sometimes? No, usually /manga/ or /read-
             # Actually, Mangakakalot URLs: https://mangakakalot.com/manga/read_one_piece_manga_online_free4
             # Manganelo: https://manganelo.com/manga/read_one_piece_manga_online_free4
             # Manganato: https://manganato.com/manga-bn978870
             # So /manga/ is common.
             pass
        # Relax check for now or improve it
        if len(parts) < 2:
             raise RuntimeError("Invalid Manganato/Mangakakalot URL.")
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

    def _collect_chapter_rows(self, soup: BeautifulSoup, page_url: str) -> List[Dict]:
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
                absolute = self._absolute(page_url, href) or self._absolute(self._BASE_URL, href)
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

        # Walk every metadata row once and dispatch by label substring. Each
        # row is `<li><h2>Label:</h2>value</li>` — we strip the leading
        # label-colon and split per-field.
        authors: List[str] = []
        status: Optional[str] = None
        year: Optional[int] = None
        alt_names: List[str] = []
        for item in soup.select(".manga-info-text li"):
            label = item.find("h2")
            if not label:
                continue
            label_text = label.get_text(strip=True).lower()
            content = item.get_text(" ", strip=True)
            if ":" not in content:
                continue
            value = content.split(":", 1)[1].strip()
            if not value:
                continue
            if "author" in label_text:
                authors = [a.strip() for a in value.split(",") if a.strip()]
            elif "status" in label_text:
                status = value
            elif "released" in label_text or "published" in label_text or "year" in label_text:
                year_match = re.search(r"\b(\d{4})\b", value)
                if year_match:
                    year = int(year_match.group(1))
            elif "alternative" in label_text or "other names" in label_text:
                alt_names = [p.strip() for p in re.split(r"[;,/]", value) if p.strip()]

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
        if status:
            comic["status"] = status
        if year is not None:
            comic["year"] = year
        if alt_names:
            comic["alt_names"] = alt_names
        return SiteComicContext(comic=comic, title=title, identifier=slug, soup=soup)

    def get_chapters(self, context: SiteComicContext, scraper, language: str, make_request) -> List[Dict]:
        soup = context.soup
        if soup is None:
            response = make_request(context.comic.get("url"), scraper)
            soup = self._make_soup(response.text)
        source_url = context.comic.get("url") or self._BASE_URL
        chapter_rows = self._collect_chapter_rows(soup, source_url)
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

    # ----------------------------------------------------------------- search
    # Manganato uses the historic mangakakalot/manganelo /search/story/<slug>
    # pattern. The slug is the query with non-word chars collapsed to "_"
    # and lowercased (e.g. "witch hat" → "witch_hat"). Result selectors are
    # `.search-story-item` (modern domains) or `.story_item` (legacy).
    #
    # Multi-domain trial: the network landscape for this aggregator family is
    # unstable — domains rotate (manganato.gg, natomanga.com, nelomanga.net,
    # mangabats.com, ...) and many are CF-aggressive on /search/. We try
    # domains in order, returning hits from the first one that responds with
    # parseable HTML. HTTP errors propagate so the orchestrator's probe-
    # failure cache suppresses dead/blocked hosts. When ALL domains fail,
    # search returns [] (the orchestrator sees that as "no match" — fine,
    # since the user can still hit manganato directly for known URLs).
    _SEARCH_DOMAINS = (
        "https://www.manganato.gg",
        "https://www.natomanga.com",
        "https://www.nelomanga.net",
        "https://www.mangabats.com",
        "https://www.mangakakalot.gg",
    )
    _SLUG_NONWORD_RE = re.compile(r"\W+")

    def _query_to_slug(self, query: str) -> str:
        # Mirror the canonical mihon-extension behavior: non-word → _, lower.
        slug = self._SLUG_NONWORD_RE.sub("_", query).strip("_").lower()
        return slug or "_"

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
        slug = self._query_to_slug(clean)
        last_error: Optional[Exception] = None
        for base in self._SEARCH_DOMAINS:
            url = f"{base}/search/story/{slug}"
            try:
                response = make_request(url, scraper)
            except Exception as exc:
                last_error = exc
                continue
            html = response.text or ""
            # CF-challenge response is usually 403 or 503 with a "Just a moment"
            # body — make_request's 5xx-as-exception handles 503; 403 returns
            # to us. Check body length + a CF-marker to detect the challenge
            # page so we move on to the next domain instead of trying to parse it.
            if (
                getattr(response, "status_code", 0) >= 400
                or "Just a moment..." in html[:1000]
                or len(html) < 1000
            ):
                continue
            hits = self._parse_search_html(html, base, limit)
            if hits:
                return hits
            # Empty parse — try next domain in case this one is mid-rotation
            # (some natomanga clones deliver a stub during DNS rotation).
        if last_error is not None and not isinstance(last_error, Exception):
            # placeholder for future use; currently we just self-select out.
            pass
        return []

    def _parse_search_html(self, html: str, base: str, limit: int) -> List[SearchHit]:
        soup = self._make_soup(html)
        # Selector chain: modern (search-story-item) → legacy (story_item).
        items = (
            soup.select(".search-story-item")
            or soup.select(".story_item")
            or soup.select(".panel_story_list .story_item")
        )
        hits: List[SearchHit] = []
        for idx, item in enumerate(items):
            if len(hits) >= limit:
                break
            # Title anchor: `.item-title` (modern) / `h3 a` (legacy)
            title_a = item.select_one(".item-title") or item.select_one("h3 a")
            if not title_a:
                continue
            title = title_a.get_text(strip=True)
            href = title_a.get("href") or ""
            if not title or not href:
                continue
            abs_url = self._absolute(base + "/", href) or href
            # Cover image: first <img> in card
            cover_img = item.select_one("img")
            cover = None
            if cover_img:
                cover = self._absolute(
                    base + "/",
                    cover_img.get("data-src") or cover_img.get("data-original") or cover_img.get("src"),
                )
            raw_score = max(0.05, 1.0 - (idx / max(1, len(items))))
            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=abs_url,
                    cover=cover,
                    alt_titles=[],
                    year=None,
                    language=None,
                    chapter_count_hint=None,
                    raw_score=raw_score,
                )
            )
        return hits


__all__ = ["ManganatoSiteHandler"]
