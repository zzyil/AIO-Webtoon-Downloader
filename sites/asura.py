"""Asura Scans handler — rewritten 2026-05-07 for the Astro-based asurascans.com.

History: asuracomic.net used to be a Next.js App Router site. The handler
parsed `self.__next_f.push([1, ...])` flight payloads to extract series,
chapter list, and chapter pages. Asura migrated to Astro v5 on a new domain
group (asurascans.com / asurascans.org / asuracomic.com); the old
asuracomic.net domain now serves a static SPA shell with no embedded data.
This rewrite targets the new Astro structure.

What this module owns:
  - configure_session: standard headers
  - search: HTML scrape /browse?search=<query> (already shipped Phase D)
  - fetch_comic_context: parses series page /comics/<slug>-<id>
  - get_chapters: parses chapter list from same series page
  - get_chapter_images: parses chapter reader /comics/<slug>-<id>/chapter/<N>

What reads from it:
  - aio-dl.py main download loop
  - sites.search_orchestrator probe path (chapter-aggregate v3)
  - sites/__init__.py registry

Cross-file:
  - SearchHit / SiteComicContext from .base
  - The Astro structure is reasonably stable but selectors should be
    treated as brittle — if asurascans.com redesigns again, expect to
    re-run probes and update CSS selectors. grep for `selector hint:`
    comments in this file to find the load-bearing ones.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Dict, List, Optional
from urllib.parse import quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BaseSiteHandler, SearchHit, SiteComicContext


class AsuraSiteHandler(BaseSiteHandler):
    name = "asura"
    # Live site as of 2026-05-07: asurascans.com (Astro v5). The .net + asuracomic
    # domains are kept in the tuple so old bookmarks still route to this handler
    # even though their actual content is dead — at least we'll fail with a
    # clear error rather than "no handler matches this URL".
    domains = (
        "asurascans.com",
        "www.asurascans.com",
        "asurascans.org",
        "www.asurascans.org",
        "asuracomic.com",
        "www.asuracomic.com",
        "asuracomic.net",
        "www.asuracomic.net",
        "asurascans.net",
        "www.asurascans.net",
    )
    _BASE_URL = "https://asurascans.com"
    _CDN_BASE = "https://cdn.asurascans.com"

    def configure_session(self, scraper, args) -> None:
        if "Referer" not in scraper.headers:
            scraper.headers.update(
                {
                    "Referer": f"{self._BASE_URL}/",
                    "Origin": self._BASE_URL,
                }
            )

    # --------------------------------------------------------- helpers
    def _make_soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def _fetch_html(self, url: str, scraper, make_request) -> str:
        response = make_request(url, scraper)
        # Force UTF-8 — Asura's Astro server sends HTML without an explicit
        # charset header, so cloudscraper falls back to ISO-8859-1 per
        # requests defaults. That mangles Korean author names ("추공") and
        # smart quotes (' ') in the description into latin-1 mojibake.
        response.encoding = "utf-8"
        return response.text

    def _slug_from_url(self, url: str) -> str:
        """Extract the canonical /comics/<slug-with-id-suffix> identifier.

        Both the series page (/comics/<slug>) and the chapter page
        (/comics/<slug>/chapter/<N>) share the same first-segment slug —
        which itself includes a stable per-series ID suffix (e.g.
        `solo-leveling-b6e039fe`). We keep the full string with suffix as
        the slug because it's required for URL construction.
        """
        path = urlparse(url).path
        parts = [p for p in path.split("/") if p]
        if not parts:
            return ""
        if parts[0] == "comics" and len(parts) > 1:
            return parts[1]
        # Legacy /series/<slug> from the dead Next.js site — best-effort
        # only. Series page won't actually load on the new domain.
        if parts[0] == "series" and len(parts) > 1:
            return parts[1]
        return parts[0]

    def _series_url(self, slug: str) -> str:
        return f"{self._BASE_URL}/comics/{slug}"

    def _chapter_url(self, slug: str, chapter_no: str) -> str:
        return f"{self._BASE_URL}/comics/{slug}/chapter/{chapter_no}"

    @staticmethod
    def _parse_uploaded(text: Optional[str]) -> int:
        """Convert 'May 24, 2023' / 'Jul 13, 2024' to a Unix timestamp.

        Asura's chapter list shows English month-day-year strings. Unparseable
        strings → 0 (matches the convention used elsewhere in the codebase
        for missing dates).
        """
        if not text:
            return 0
        text = text.strip()
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                d = _dt.datetime.strptime(text, fmt)
                return int(d.replace(tzinfo=_dt.timezone.utc).timestamp())
            except ValueError:
                continue
        return 0

    @staticmethod
    def _normalize_chapter_number(value) -> str:
        """Same convention as the rest of the codebase: int when whole, else
        decimal (e.g. 4.0 → '4', 4.5 → '4.5'). String input passed through."""
        if isinstance(value, (int, float)):
            if isinstance(value, float) and not value.is_integer():
                return str(value).rstrip("0").rstrip(".")
            return str(int(value))
        return str(value)

    @staticmethod
    def _extract_label_block(soup: BeautifulSoup, label: str) -> Optional[str]:
        """Find a label-value pair in the Astro layout where the layout is:
            <div>...><span>Status</span></div> ...<span>completed</span>...

        We scan for any element whose direct text matches `label`, then walk
        forward to the next block of meaningful text within the same logical
        container. Used for Status, Type, Updated, Released — fields that
        appear as `<label>:<value>` boxes.

        Selector hint: the actual layout uses `<div class="text-xs ...">`
        for labels and `<span class="text-base font-bold">` for values, but
        the class names are utility-CSS and likely to drift. Walking the DOM
        from a label match is more resilient.
        """
        for el in soup.find_all(string=re.compile(rf"^\s*{re.escape(label)}\s*$", re.IGNORECASE)):
            # Walk to the parent and its next-sibling block — heuristic but
            # works for the current 2026-05 layout.
            parent = el.parent
            if not parent:
                continue
            container = parent.parent
            if not container:
                continue
            for sib in container.find_all(["span", "div"], recursive=True):
                txt = sib.get_text(strip=True)
                if txt and txt.lower() != label.lower():
                    return txt
        return None

    # ----------------------------------------------------- Base overrides
    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        slug = self._slug_from_url(url)
        if not slug:
            raise RuntimeError("Unable to determine Asura slug from URL.")

        # Always fetch the canonical series page even if user passed a chapter
        # URL — the chapter page doesn't expose the chapter list, only the
        # current chapter's images.
        series_url = self._series_url(slug)
        html = self._fetch_html(series_url, scraper, make_request)
        soup = self._make_soup(html)

        # Title — h1 is the series title in the new layout.
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else slug

        # Cover — og:image holds the full-resolution cover URL on
        # cdn.asurascans.com/asura-images/covers/<slug>.<short>.webp
        cover = None
        og_img = soup.find("meta", attrs={"property": "og:image"})
        if og_img and og_img.get("content"):
            cover = og_img["content"].strip()

        # Description — meta name=description has the synopsis (Astro
        # populates this server-side from the same source as the visible
        # description block).
        description = None
        desc_meta = soup.find("meta", attrs={"name": "description"})
        if desc_meta and desc_meta.get("content"):
            description = desc_meta["content"].strip()

        # Authors / artists — anchors with /browse?author= or /browse?artist=
        # query params (both visible-text + URL-encoded). Asura sometimes
        # lists the same author multiple times across translation credits;
        # dedupe.
        authors: List[str] = []
        for a in soup.select('a[href*="/browse?author="]'):
            t = a.get_text(strip=True)
            if t and t not in authors:
                authors.append(t)
        artists: List[str] = []
        for a in soup.select('a[href*="/browse?artist="]'):
            t = a.get_text(strip=True)
            if t and t not in artists:
                artists.append(t)

        # Genres — anchors with /browse?genres=<g>
        genres: List[str] = []
        for a in soup.select('a[href*="/browse?genres="]'):
            t = a.get_text(strip=True)
            if t and t not in genres:
                genres.append(t)

        # Status (completed/ongoing/dropped/etc.) — captured from the label
        # block heuristic. Asura uses lowercase status words ('completed',
        # 'ongoing', 'hiatus'); we normalize to title case for the comic dict.
        status_raw = self._extract_label_block(soup, "Status")
        status = status_raw.title() if status_raw else None

        # Series type (manhwa/manga/manhua) — same heuristic. Stored as
        # additional metadata for downstream display only.
        series_type_raw = self._extract_label_block(soup, "Type")
        series_type = series_type_raw.lower() if series_type_raw else None

        comic: Dict[str, object] = {
            "hid": slug,
            "title": title,
            "url": series_url,
            "_slug": slug,
        }
        if description:
            comic["desc"] = description
        if cover:
            comic["cover"] = cover
        if authors:
            comic["authors"] = authors
        if artists:
            comic["artists"] = artists
        if genres:
            comic["genres"] = genres
        if status:
            comic["status"] = status
        if series_type:
            comic["type"] = series_type

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
            # Re-fetch if context didn't carry the soup (shouldn't happen in
            # the normal flow but be defensive).
            html = self._fetch_html(self._series_url(context.identifier), scraper, make_request)
            soup = self._make_soup(html)

        slug = context.identifier
        # Selector hint: Astro chapter list rows are anchors with the
        # `data-astro-prefetch="hover"` attribute pointing to /comics/<slug>/chapter/<N>.
        # The "First Chapter" / "Latest Chapter" jump buttons at the top of the
        # page do NOT have data-astro-prefetch — that's our filter.
        href_re = re.compile(rf"^/comics/{re.escape(slug)}/chapter/[0-9]+(?:\.[0-9]+)?/?$")
        chap_anchors = [
            a for a in soup.select('a[data-astro-prefetch="hover"]')
            if href_re.match((a.get("href") or "").strip())
        ]

        chapters: List[Dict] = []
        seen: set = set()
        for a in chap_anchors:
            href = (a.get("href") or "").strip().rstrip("/")
            if href in seen:
                continue
            seen.add(href)
            # Chapter number from the URL — authoritative (matches the row
            # text "Chapter N" but immune to whitespace/comment quirks the
            # span text can have, like the inline HTML comment between
            # "Chapter" and the number).
            m = re.search(r"/chapter/([0-9]+(?:\.[0-9]+)?)$", href)
            if not m:
                continue
            chap_no = self._normalize_chapter_number(m.group(1))

            # Optional sub-title (e.g., "Side Story 20" — many series have
            # a per-chapter subtitle in a span.block.truncate).
            sub_title = None
            sub_span = a.select_one("span.block.truncate")
            if sub_span:
                t = sub_span.get_text(strip=True)
                if t:
                    sub_title = t

            # Date in the right-aligned span. Pattern is "Mon DD, YYYY";
            # we scan from the end since the chapter-number span comes
            # first in DOM order.
            uploaded = 0
            for sp in reversed(a.find_all("span")):
                txt = sp.get_text(strip=True)
                if not txt:
                    continue
                if re.match(r"^[A-Z][a-z]{2,9}\s+\d{1,2},\s*\d{4}$", txt):
                    uploaded = self._parse_uploaded(txt)
                    break

            chapters.append(
                {
                    "hid": f"{slug}-{chap_no}",
                    "chap": chap_no,
                    "title": sub_title or f"Chapter {chap_no}",
                    "url": urljoin(self._BASE_URL, href),
                    "uploaded": uploaded,
                    "group_name": None,
                }
            )

        # Sort ascending by chapter number — the DOM order is descending
        # (newest first) but downstream code expects ascending.
        def _sort_key(ch):
            try:
                return float(ch.get("chap") or 0)
            except (ValueError, TypeError):
                return 0.0

        chapters.sort(key=_sort_key)
        return chapters

    def get_group_name(self, chapter_version: Dict) -> Optional[str]:
        return chapter_version.get("group_name")

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        chapter_url = chapter.get("url")
        if not chapter_url:
            raise RuntimeError("Asura chapter URL missing.")
        html = self._fetch_html(chapter_url, scraper, make_request)
        soup = self._make_soup(html)

        # Selector hint: chapter pages are served by cdn.asurascans.com under
        # /asura-images/chapters/<slug>/<N>/<page>.webp. Other CDN images
        # appear on the page (the cover thumbnail in the "now reading" card,
        # site logo, etc.); the /chapters/ path filter excludes them.
        images: List[str] = []
        seen: set = set()
        for img in soup.find_all("img"):
            src = (img.get("src") or "").strip()
            if not src:
                continue
            if "/asura-images/chapters/" not in src:
                continue
            if src in seen:
                continue
            seen.add(src)
            images.append(src)

        if not images:
            raise RuntimeError(
                f"No chapter images found for {chapter_url} — selectors may "
                "have drifted (Astro layout). Re-probe and update the "
                "/asura-images/chapters/ filter."
            )
        return images

    # ----------------------------------------------------------------- search
    # asurascans.com (Astro v5) exposes /browse?search=<query> as a server-
    # side filtered HTML page. Plain GET, no auth, no XHR — Cloudflare-fronted
    # but cloudscraper passes. Each result has TWO anchors with the same
    # `/comics/<slug>-<id-suffix>` href (a cover-image anchor and an h3-text
    # anchor). We dedupe by href and pick whichever has the title text + the
    # cover img.
    #
    # Notes:
    # - URL pattern: `/comics/<slug>-<8-char-suffix>` — the suffix is a
    #   stable per-series ID, not a hash. Returned URLs include it.
    # - Cover URL: cdn.asurascans.com/asura-images/covers/<slug>.<short>-400.webp
    #   for the listing thumbnail; full-res is the same path without `-400`.
    # - `?q=<query>` and `?search=<query>` both work server-side. We use
    #   `search` since it's slightly clearer.
    # - Param `name=<query>` does NOT filter (returns full browse) — known
    #   gotcha; don't use it.
    _COMICS_HREF_RE = re.compile(r"^/comics/[a-z0-9\-]+/?$")

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
        url = f"{self._BASE_URL}/browse?search={quote_plus(clean)}"
        response = make_request(url, scraper)
        html = response.text or ""
        if len(html) < 1000:
            return []
        soup = self._make_soup(html)

        # Group anchors by href — one href = one series, but appears under
        # both a cover-image anchor and a title-text anchor.
        by_href: Dict[str, List] = {}
        for a in soup.select('a[href^="/comics/"]'):
            href = (a.get("href") or "").strip()
            if not self._COMICS_HREF_RE.match(href):
                continue
            by_href.setdefault(href, []).append(a)

        hits: List[SearchHit] = []
        for idx, (href, anchor_list) in enumerate(by_href.items()):
            if len(hits) >= limit:
                break
            title = ""
            cover = None
            for a in anchor_list:
                if not title:
                    h3 = a.select_one("h3")
                    if h3:
                        title = h3.get_text(strip=True)
                if not cover:
                    img = a.select_one("img")
                    if img:
                        if not title:
                            alt = (img.get("alt") or "").strip()
                            if alt:
                                title = alt
                        src = (img.get("src") or "").strip()
                        if src:
                            cover = src if src.startswith("http") else urljoin(self._BASE_URL, src)
            if not title:
                continue
            url_full = urljoin(self._BASE_URL, href)
            raw_score = max(0.05, 1.0 - (idx / max(1, len(by_href))))
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


__all__ = ["AsuraSiteHandler"]
