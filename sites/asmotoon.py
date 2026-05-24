from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, FeatureNotFound

from .base import BaseSiteHandler, SearchHit, SiteComicContext


class AsmotoonSiteHandler(BaseSiteHandler):
    # NOTE: Asmotoon's series template surfaces only description + genres +
    # cover. There is no author/artist/status data on the page (verified
    # 2026-05-19 audit, dry_run_komikku_findings.md §C). Komikku's details.json
    # for asmotoon-sourced series will leave author/artist empty and status
    # defaults to "0" (Unknown). Site limitation, not a parser bug.
    name = "asmotoon"
    domains = ("asmotoon.com", "www.asmotoon.com")

    _BASE_URL = "https://asmotoon.com"
    _CDN_URL = "https://cdn.meowing.org/uploads/"

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

    def _normalize_url(self, url: str) -> str:
        if not url.lower().startswith("http"):
            return urljoin(self._BASE_URL, url.lstrip("/"))
        return url

    def _path_parts(self, url: str) -> List[str]:
        parsed = urlparse(url)
        return [segment for segment in parsed.path.split("/") if segment]

    def _series_slug_from_path(self, parts: List[str]) -> Optional[str]:
        if len(parts) >= 2 and parts[0] == "series":
            return parts[1]
        if len(parts) >= 2 and parts[0] == "chapter":
            combo = parts[1].rstrip("/")
            if "-" in combo:
                return combo.split("-", 1)[0]
        return None

    def _chapter_slug_from_path(self, parts: List[str]) -> Optional[str]:
        if len(parts) >= 2 and parts[0] == "chapter":
            return parts[1].rstrip("/")
        return None

    def _meta(self, soup: BeautifulSoup, name: Optional[str] = None, property_name: Optional[str] = None) -> Optional[str]:
        if name:
            tag = soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return tag["content"].strip()
        if property_name:
            tag = soup.find("meta", attrs={"property": property_name})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    def _extract_series_title(self, soup: BeautifulSoup) -> Optional[str]:
        chapter_header = soup.select_one("#chapter_header a[href*='/series/']")
        if chapter_header and chapter_header.get_text(strip=True):
            return chapter_header.get_text(strip=True)
        heading = soup.find("h1")
        if heading:
            text = heading.get_text(strip=True)
            if text:
                return text
        title_tag = soup.find("title")
        if title_tag and title_tag.get_text(strip=True):
            title = title_tag.get_text(strip=True)
            # Remove trailing "Chapter ..." if present
            return re.sub(r"\s*-+\s*Chapter.*$", "", title).strip() or title
        return None

    def _extract_keywords(self, soup: BeautifulSoup) -> List[str]:
        keywords = self._meta(soup, "keywords")
        if not keywords:
            return []
        values = [kw.strip() for kw in keywords.split(",")]
        seen: List[str] = []
        for value in values:
            lower = value.lower()
            if not value:
                continue
            if lower in {"asmotoon", "asmotoons", "asmodeus", "asmodeus scans"}:
                continue
            if value not in seen:
                seen.append(value)
        return seen

    def _extract_series_slug(self, soup: BeautifulSoup, url: str) -> Optional[str]:
        link = soup.select_one("a[href*='/series/']")
        if link:
            href = link.get("href") or ""
            parts = self._path_parts(self._normalize_url(href))
            slug = self._series_slug_from_path(parts)
            if slug:
                return slug
        parts = self._path_parts(url)
        return self._series_slug_from_path(parts)

    def _extract_chapter_links(self, soup: BeautifulSoup) -> List[Tuple[str, str]]:
        container = soup.select_one("#chapters")
        links = container.select("a[href*='/chapter/']") if container else []
        if not links:
            links = [
                link
                for link in soup.select("a[href*='/chapter/']")
                if link.get("title")
            ]
        results: List[Tuple[str, str]] = []
        for link in links:
            href = link.get("href")
            if not href:
                continue
            abs_url = self._normalize_url(href)
            parts = self._path_parts(abs_url)
            slug = self._chapter_slug_from_path(parts)
            if not slug:
                continue
            title = link.get("title") or link.get_text(" ", strip=True) or slug
            results.append((slug, title))
        return results

    def _parse_chapter_number(self, text: str) -> Optional[str]:
        if not text:
            return None
        match = re.search(r"(\d+(?:\.\d+)?)", text.replace(",", "."))
        if match:
            return match.group(1)
        return None

    def _chapter_sort_key(self, chapter: Dict) -> Tuple[float, str]:
        chap = chapter.get("chap")
        if chap is None:
            return (float("inf"), chapter.get("hid", ""))
        try:
            return (float(chap), chapter.get("hid", ""))
        except (ValueError, TypeError):
            return (float("inf"), chapter.get("hid", ""))

    def _image_urls_from_soup(self, soup: BeautifulSoup) -> List[str]:
        image_tags = soup.select("#pages img[uid]")
        images: List[str] = []
        for img in image_tags:
            uid = (img.get("uid") or "").strip()
            if not uid:
                src = img.get("data-src") or img.get("src")
                if src and src.startswith("http"):
                    images.append(src.strip())
                continue
            images.append(urljoin(self._CDN_URL, uid))
        # Deduplicate while preserving order
        seen: set[str] = set()
        ordered: List[str] = []
        for url in images:
            if url not in seen:
                ordered.append(url)
                seen.add(url)
        return ordered

    # ----------------------------------------------------------- Base overrides
    def configure_session(self, scraper, args) -> None:
        scraper.headers.setdefault("Referer", self._BASE_URL + "/")

    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        normalized_url = self._normalize_url(url)
        response = make_request(normalized_url, scraper)
        soup = self._make_soup(response.text)

        series_slug = self._extract_series_slug(soup, normalized_url)
        if not series_slug:
            raise RuntimeError("Unable to determine series identifier.")

        title = self._extract_series_title(soup) or series_slug
        desc = self._meta(soup, "description")
        cover = self._meta(soup, property_name="og:image")
        keywords = self._extract_keywords(soup)

        comic: Dict[str, object] = {
            "hid": series_slug,
            "title": title,
            "url": urljoin(self._BASE_URL, f"/series/{series_slug}/"),
        }
        if desc:
            comic["desc"] = desc
        if cover:
            comic["cover"] = cover
        if keywords:
            comic["genres"] = keywords

        return SiteComicContext(
            comic=comic,
            title=title,
            identifier=series_slug,
            soup=soup,
        )

    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        soup = context.soup
        if soup is None:
            raise RuntimeError("Series page HTML is unavailable.")

        chapters: List[Dict] = []
        for slug, title in self._extract_chapter_links(soup):
            chapter_url = urljoin(self._BASE_URL, f"/chapter/{slug}/")
            chap_number = self._parse_chapter_number(title)
            chapters.append(
                {
                    "hid": slug,
                    "chap": chap_number or title,
                    "title": title,
                    "url": chapter_url,
                }
            )

        if not chapters:
            raise RuntimeError("No chapters were found on this page.")

        chapters.sort(key=self._chapter_sort_key)
        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        chapter_url = chapter.get("url")
        if not chapter_url:
            slug = chapter.get("hid")
            if not slug:
                raise RuntimeError("Chapter URL missing.")
            chapter_url = urljoin(self._BASE_URL, f"/chapter/{slug}/")

        response = make_request(chapter_url, scraper)
        soup = self._make_soup(response.text)

        images = self._image_urls_from_soup(soup)
        if not images:
            raise RuntimeError("No images found for this chapter.")
        return images

    # ----------------------------------------------------------------- search
    # asmotoon's /?s=<query> doesn't actually filter (returns the homepage
    # regardless — the search box is JS-driven). Their /series/ page lists
    # the full catalog (~156 series). Client-side substring match — same
    # pattern as flamecomics/zeroscans/tcbscans.
    #
    # Series cards: <a href="/series/<slug>/" title="..."> with a child div
    # whose `style="background-image:url(<cover_url>)"` carries the cover.
    # The cover URL goes through the wsrv.nl image proxy
    # (https://wsrv.nl/?url=cdn.meowing.org/uploads/<id>&w=600); we keep
    # that wrapper so the chapter-probe path uses the same proxy as the
    # site's frontend.
    _COVER_BG_RE = re.compile(r"background-image:url\((https?://[^)]+)\)", re.IGNORECASE)

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
        url = f"{self._BASE_URL}/series/"
        response = make_request(url, scraper)
        html = response.text or ""
        if len(html) < 200:
            return []
        soup = self._make_soup(html)

        # Find anchors pointing to /series/<id>/ with a title attribute.
        slug_re = re.compile(r"^/series/[a-z0-9]+/?$")
        seen: Dict[str, Tuple[str, Optional[str]]] = {}  # href -> (title, cover)
        for a in soup.find_all("a"):
            href = (a.get("href") or "").strip()
            if not slug_re.match(href):
                continue
            title = (a.get("title") or a.get("alt") or "").strip()
            if not title:
                # Fallback: text content of the inner title div
                inner = a.select_one(".line-clamp-2, .font-bold")
                if inner:
                    title = inner.get_text(strip=True)
            if not title:
                continue
            # Cover from background-image style on a descendant div
            cover = None
            for div in a.find_all("div"):
                style = (div.get("style") or "")
                m = self._COVER_BG_RE.search(style)
                if m:
                    cover = m.group(1).replace("&amp;", "&")
                    break
            href_norm = href.rstrip("/")
            if href_norm not in seen:
                seen[href_norm] = (title, cover)

        ql = clean.lower()
        query_tokens = set(t for t in ql.split() if t)

        scored: List = []
        for href, (title, cover) in seen.items():
            tl = title.lower()
            if ql in tl:
                relevance = 1.0
            elif query_tokens and all(tok in tl for tok in query_tokens):
                relevance = 0.7
            else:
                continue
            scored.append((relevance, title, href, cover))

        scored.sort(key=lambda x: -x[0])

        hits: List[SearchHit] = []
        for idx, (relevance, title, href, cover) in enumerate(scored[:limit]):
            url_full = urljoin(self._BASE_URL, href + "/")
            raw_score = max(0.05, relevance * (1.0 - (idx / max(1, len(scored)))))
            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=url_full,
                    cover=cover,
                    alt_titles=[],
                    year=None,
                    language=None,
                    chapter_count_hint=None,
                    raw_score=raw_score,
                )
            )
        return hits


__all__ = ["AsmotoonSiteHandler"]
