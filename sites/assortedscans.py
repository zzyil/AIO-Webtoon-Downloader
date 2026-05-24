from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, FeatureNotFound

from .base import BaseSiteHandler, SearchHit, SiteComicContext


class AssortedScansSiteHandler(BaseSiteHandler):
    name = "assortedscans"
    domains = ("assortedscans.com", "www.assortedscans.com")

    _BASE_URL = "https://assortedscans.com"

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
            url = urljoin(self._BASE_URL, url.lstrip("/"))
        return url

    def _series_url(self, slug: str) -> str:
        return f"{self._BASE_URL}/reader/{slug}/"

    def _extract_slug(self, url: str) -> str:
        parsed = urlparse(url)
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) >= 2 and segments[0] == "reader":
            return segments[1]
        return segments[-1] if segments else parsed.netloc

    def _extract_title(self, soup: BeautifulSoup) -> Optional[str]:
        main_heading = soup.select_one("main#content h1")
        if not main_heading:
            title_tag = soup.find("title")
            return title_tag.get_text(strip=True) if title_tag else None
        link = main_heading.find("a")
        if link:
            return link.get_text(strip=True)
        return main_heading.get_text(strip=True)

    def _meta_content(
        self,
        soup: BeautifulSoup,
        name: Optional[str] = None,
        property_name: Optional[str] = None,
    ) -> Optional[str]:
        if name:
            tag = soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return tag["content"].strip()
        if property_name:
            tag = soup.find("meta", attrs={"property": property_name})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    def _has_dropdown(self, soup: BeautifulSoup) -> bool:
        return bool(soup.select_one("div.chapter-list"))

    def _find_first_chapter_url(self, soup: BeautifulSoup, slug: str) -> Optional[str]:
        pattern = re.compile(rf"/reader/{re.escape(slug)}/[^/]+/[^/]+/?$")
        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href") or ""
            if pattern.search(href):
                return urljoin(self._BASE_URL, href)
        return None

    def _chapter_href_to_page(self, href: str, page: Optional[int] = 1) -> str:
        absolute = urljoin(self._BASE_URL, href)
        parsed = urlparse(absolute)
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) <= 4 and page:
            segments.append(str(page))
        elif page and len(segments) > 4:
            segments[4] = str(page)
        path = "/" + "/".join(segments) + "/"
        return urlunparse(parsed._replace(path=path, query="", fragment=""))

    def _extract_volume_chapter(self, href: str) -> Tuple[Optional[str], Optional[str]]:
        parsed = urlparse(urljoin(self._BASE_URL, href))
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) >= 4 and segments[0] == "reader":
            return segments[2], segments[3]
        return None, None

    def _safe_float(self, value: Optional[str]) -> float:
        if value is None:
            return float("inf")
        try:
            return float(value)
        except ValueError:
            return float("inf")

    def _extract_page_number(self, url_or_text: str) -> Optional[int]:
        match = re.search(r"(\d+)(?:/?$)", url_or_text)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    def _extract_page_urls(self, soup: BeautifulSoup, base_url: str) -> List[str]:
        page_links = soup.select("div.page-list a")
        pages: List[Tuple[int, str]] = []
        for link in page_links:
            href = link.get("href")
            if not href:
                continue
            abs_url = urljoin(base_url, href)
            page_no = self._extract_page_number(abs_url) or self._extract_page_number(
                link.get_text(" ", strip=True)
            )
            if page_no is None:
                continue
            pages.append((page_no, self._chapter_href_to_page(abs_url, page_no)))

        if not pages:
            fallback_page = self._extract_page_number(base_url) or 1
            pages.append((fallback_page, self._chapter_href_to_page(base_url, fallback_page)))

        pages.sort(key=lambda item: item[0])
        ordered_urls: List[str] = []
        for _, url in pages:
            if url not in ordered_urls:
                ordered_urls.append(url)
        return ordered_urls

    def _extract_page_image(self, soup: BeautifulSoup, page_url: str) -> Optional[str]:
        img = soup.select_one("#page-image") or soup.find("img")
        if not img:
            return None
        src = img.get("src")
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

    # ----------------------------------------------------------- Base overrides
    def configure_session(self, scraper, args) -> None:
        scraper.headers.setdefault("Referer", self._BASE_URL + "/")

    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        normalized_url = self._normalize_url(url)
        response = make_request(normalized_url, scraper)
        initial_soup = self._make_soup(response.text)

        slug = self._extract_slug(normalized_url)
        title = self._extract_title(initial_soup) or slug.replace("-", " ").title()

        comic: Dict[str, object] = {
            "hid": slug,
            "title": title,
            "url": self._series_url(slug),
        }
        # AssortedScans-family sites use the MangAdventure framework. On
        # arc-relight.com the server populates `<meta name="description">` and
        # `<meta name="keywords">`; on assortedscans.com itself those tags are
        # left blank and the visible info is rendered in the DOM via Jinja
        # template macros. We try meta first (cheaper / canonical when
        # available) then fall through to DOM scrapes so both sites work.
        # See dry_run_komikku_findings.md §A for the 2026-05-19 audit that
        # surfaced the assortedscans 0/5 case.
        desc = self._meta_content(initial_soup, "description")
        if not desc:
            dom_desc = initial_soup.select_one(
                "article.info p, .info > p, .series-info p, .description, "
                ".info .description"
            )
            if dom_desc is not None:
                text = dom_desc.get_text(" ", strip=True)
                if text:
                    desc = text
        if not desc:
            og_desc = self._meta_content(initial_soup, property_name="og:description")
            if og_desc:
                desc = og_desc
        if desc:
            comic["desc"] = desc
        cover = self._meta_content(initial_soup, property_name="og:image")
        if cover:
            comic["cover"] = cover

        # Authors — MangAdventure's series template renders the value in a
        # plain `.author` div (NOT an /author/<slug> anchor). The "Author:"
        # label is a sibling `<strong>` for human display. Verified live on
        # assortedscans.com 2026-05-19.
        authors: List[str] = []
        seen_authors: set = set()
        for node in initial_soup.select("#series-authors .author, .author"):
            text = node.get_text(" ", strip=True)
            # Skip the wrapper itself if it bundled the "Author:" label —
            # the real value lives inside a child .author div.
            #
            # Match "Author:" or "Author<whitespace>" at the start (case-
            # insensitive). The previous `'Author' not in text.split()` guard
            # was broken because text.get_text(' ', strip=True) collapses the
            # label and value to "Author: Real Name" → split() yields
            # ['Author:', 'Real', 'Name'] (colon attached), and bare 'Author'
            # was never in that list — the wrapper always passed the guard
            # and "Author: Real Name" got appended as a literal author.
            # re.match anchors at start, so a real author name like "Author
            # Smith" (extremely rare) would still be skipped — acceptable
            # because the child .author div carries the bare name as a
            # separate node anyway.
            if (
                text
                and not re.match(r"^author[:\s]", text, re.IGNORECASE)
                and text not in seen_authors
            ):
                seen_authors.add(text)
                authors.append(text)
        # Fallback for variant templates that DO link to /author/<slug>.
        if not authors:
            for a in initial_soup.select("a[href*='/author/']"):
                text = a.get_text(strip=True)
                if text and text not in seen_authors:
                    seen_authors.add(text)
                    authors.append(text)
        if authors:
            comic["authors"] = authors

        # Artists — same pattern.
        artists: List[str] = []
        seen_artists: set = set()
        for node in initial_soup.select("#series-artists .artist, .artist"):
            text = node.get_text(" ", strip=True)
            # Same wrapper-label skip pattern as authors above — see the
            # extended rationale there. `Artist:` / `Artist<space>` matches
            # the bundled label form; the child .artist div renders just
            # the bare name.
            if (
                text
                and not re.match(r"^artist[:\s]", text, re.IGNORECASE)
                and text not in seen_artists
            ):
                seen_artists.add(text)
                artists.append(text)
        if not artists:
            for a in initial_soup.select("a[href*='/artist/']"):
                text = a.get_text(strip=True)
                if text and text not in seen_artists:
                    seen_artists.add(text)
                    artists.append(text)
        if artists:
            comic["artists"] = artists

        keywords = self._meta_content(initial_soup, "keywords")
        if keywords:
            genres = [kw.strip() for kw in keywords.split(",") if kw.strip()]
            if genres:
                comic["genres"] = genres
        # Genres DOM fallback when meta keywords is empty. MangAdventure uses
        # "categories" as the human-facing term — anchors point to
        # /category/<slug>. Some templates also expose .categories a / .tags a.
        # `#series-categories` is the canonical wrapper on assortedscans.
        if "genres" not in comic:
            dom_genres: List[str] = []
            seen_genres: set = set()
            for a in initial_soup.select(
                "#series-categories a, a[href*='/category/'], "
                ".categories a, .tags a, .genre a"
            ):
                text = a.get_text(strip=True)
                if text and text not in seen_genres:
                    seen_genres.add(text)
                    dom_genres.append(text)
            if dom_genres:
                comic["genres"] = dom_genres

        # Status — MangAdventure renders status as `<span>Completed</span>`
        # inside `#series-status`. Layered fallbacks cover variant templates.
        status_node = initial_soup.select_one(
            "#series-status span, .status, span.status, .series-status, .info .status"
        )
        if status_node is not None:
            status_text = status_node.get_text(strip=True)
            if status_text:
                comic["status"] = status_text

        soup = initial_soup
        if not self._has_dropdown(soup):
            first_chapter_url = self._find_first_chapter_url(initial_soup, slug)
            if not first_chapter_url:
                raise RuntimeError("Unable to locate any chapters for this series.")
            response = make_request(first_chapter_url, scraper)
            soup = self._make_soup(response.text)

        return SiteComicContext(
            comic=comic,
            title=title,
            identifier=slug,
            soup=soup,
        )

    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        soup = context.soup
        if soup is None:
            raise RuntimeError("Series page HTML is not available.")

        chapters: List[Dict] = []
        chapter_links = soup.select("div.chapter-list li.chapter-details a")
        if not chapter_links:
            raise RuntimeError("No chapters could be found on this page.")

        for link in chapter_links:
            href = link.get("href")
            if not href:
                continue
            volume, chapter_no = self._extract_volume_chapter(href)
            title = link.get("title") or link.get_text(" ", strip=True)
            page_one_url = self._chapter_href_to_page(href, 1)
            chapters.append(
                {
                    "hid": f"{context.identifier}-{volume or 'v'}-{chapter_no or 'c'}",
                    "chap": chapter_no or title,
                    "vol": volume,
                    "title": title,
                    "url": page_one_url,
                    "chapter_href": urljoin(self._BASE_URL, href),
                }
            )

        chapters.sort(
            key=lambda ch: (
                self._safe_float(ch.get("vol")),
                self._safe_float(ch.get("chap")),
            )
        )
        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        first_page_url = chapter.get("url")
        if not first_page_url:
            raise RuntimeError("Chapter URL missing.")

        first_page_response = make_request(first_page_url, scraper)
        first_page_soup = self._make_soup(first_page_response.text)

        page_urls = self._extract_page_urls(first_page_soup, first_page_url)
        images: List[str] = []
        for idx, page_url in enumerate(page_urls):
            if idx == 0:
                img_src = self._extract_page_image(first_page_soup, page_url)
            else:
                page_html = make_request(page_url, scraper).text
                page_soup = self._make_soup(page_html)
                img_src = self._extract_page_image(page_soup, page_url)
            if img_src and img_src not in images:
                images.append(img_src)

        if not images:
            raise RuntimeError("No images were extracted for this chapter.")
        return images

    # ----------------------------------------------------------------- search
    # AssortedScans-family sites (assortedscans + arc_relight via inheritance)
    # don't have a server-side search endpoint. Their /reader/ page is the
    # full catalog (~56 series for assortedscans) listed as
    # <a href="/reader/<slug>/" title="Title">Title</a>. Client-side filter
    # on title — same pattern as flamecomics/zeroscans/tcbscans.
    #
    # Uses self._BASE_URL so subclasses (arcrelight) route to their own
    # domain via the same code path with no override needed.
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
        url = f"{self._BASE_URL}/reader/"
        response = make_request(url, scraper)
        html = response.text or ""
        if len(html) < 200:
            return []
        soup = self._make_soup(html)

        # Each series is a single anchor with both an href like /reader/<slug>/
        # AND a title= attr. Filter to those — there are other anchors on the
        # page (chapter links, navigation, etc.) we don't want.
        slug_re = re.compile(r"^/reader/[^/]+/?$")
        seen_hrefs: Dict[str, str] = {}  # href -> title (dedupe; keep first non-empty)
        for a in soup.select("a[href]"):
            href = (a.get("href") or "").strip()
            if not slug_re.match(href):
                continue
            title = (a.get("title") or "").strip() or a.get_text(strip=True)
            if not title:
                continue
            href_norm = href.rstrip("/")
            if href_norm not in seen_hrefs:
                seen_hrefs[href_norm] = title

        ql = clean.lower()
        query_tokens = set(t for t in ql.split() if t)

        scored: List = []
        for href, title in seen_hrefs.items():
            tl = title.lower()
            if ql in tl:
                relevance = 1.0
            elif query_tokens and all(tok in tl for tok in query_tokens):
                relevance = 0.7
            else:
                continue
            scored.append((relevance, title, href))

        scored.sort(key=lambda x: -x[0])

        hits: List[SearchHit] = []
        for idx, (relevance, title, href) in enumerate(scored[:limit]):
            url_full = urljoin(self._BASE_URL, href + "/")
            raw_score = max(0.05, relevance * (1.0 - (idx / max(1, len(scored)))))
            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=url_full,
                    cover=None,  # /reader/ index has no thumbnails — chapter probe fetches via fetch_comic_context which has og:image
                    alt_titles=[],
                    year=None,
                    language=None,
                    chapter_count_hint=None,
                    raw_score=raw_score,
                )
            )
        return hits
