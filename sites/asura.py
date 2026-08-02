from __future__ import annotations

import json
import re
from html import unescape
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BaseSiteHandler, SearchHit, SiteComicContext
from .hardening import configure_throttling


class AsuraSiteHandler(BaseSiteHandler):
    name = "asura"
    domains = (
        "asuracomic.net",
        "www.asuracomic.net",
        "asurascans.net",
        "www.asurascans.net",
        "asurascans.com",
        "www.asurascans.com",
        "asurascans.org",
        "www.asurascans.org",
        "asuracomic.com",
        "www.asuracomic.com",
    )
    _BASE_URL = "https://asurascans.com"
    _COMICS_HREF_RE = re.compile(r"^/comics/[a-z0-9\-]+/?$")

    def configure_session(self, scraper, args) -> None:
        if "Referer" not in scraper.headers:
            scraper.headers.update(
                {
                    "Referer": "https://asurascans.com/",
                    "Origin": "https://asurascans.com",
                }
            )
        
        # Asura is notoriously sensitive to bots (Cloudflare Turnstile + hidden captchas).
        # We increase the page delays slightly to avoid "Are you human?" checks.
        configure_throttling(
            scraper,
            domains=self.domains,
            gaps={
                "default": 1.5,
                "ajax": 2.0,
                "page": 3.0, # Highly restricted
                "image": 0.5, # Images usually fine once pageloaded
            },
            jitter=1.0 # High jitter to look human
        )

    # -- Helpers -----------------------------------------------------
    def _make_soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def _fetch_html(self, url: str, scraper, make_request) -> str:
        response = make_request(url, scraper)
        response.encoding = response.encoding or "utf-8"
        return response.text

    def _unwrap_rsc(self, value):
        """Unwrap React Server Component wire format [type, value] pairs recursively."""
        if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
            return self._unwrap_rsc(value[1])
        if isinstance(value, list):
            return [self._unwrap_rsc(item) for item in value]
        if isinstance(value, dict):
            return {k: self._unwrap_rsc(v) for k, v in value.items()}
        return value

    def _extract_props_json(self, html: str) -> Optional[Dict]:
        """Extract the RSC props JSON from page HTML.

        The site embeds chapter/series data in a custom element attribute like:
            props="{&quot;seriesSlug&quot;:[0,&quot;...&quot;], ...}"
        We decode and parse this.
        """
        # Look for 'props="{' pattern (HTML-encoded JSON in a props attribute)
        match = re.search(r'props="(\{&quot;.*?})"', html, re.DOTALL)
        if not match:
            return None
        raw = unescape(match.group(1))
        try:
            data = json.loads(raw)
            return self._unwrap_rsc(data)
        except (json.JSONDecodeError, ValueError):
            return None

    def _extract_chapters_from_html(self, html: str) -> List[Dict]:
        """Extract chapter list from the comic page HTML.

        Chapters are <a> tags inside a scrollable container:
            div.max-h-[500px] > a[href*="/chapter/"]
        """
        soup = BeautifulSoup(html, "html.parser")
        chapters = []

        # Primary: links inside the scrollable chapter list
        chapter_links = soup.select('a[href*="/chapter/"]')
        seen = set()
        for a in chapter_links:
            href = a.get("href", "")
            if not href or href in seen:
                continue
            seen.add(href)

            # Extract chapter number from URL: /comics/{slug}/chapter/{number}
            ch_match = re.search(r'/chapter/(\d+(?:\.\d+)?)', href)
            if not ch_match:
                continue
            chap_num = ch_match.group(1)

            # Title: try to get it from the link text
            text = a.get_text(strip=True)
            # Typical text: "Chapter109Choicelast week" — extract meaningful part
            title_match = re.match(r'Chapter\s*\d+(?:\.\d+)?\s*(.*?)(?:\d+\s*\w+\s*ago|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)', text, re.IGNORECASE)
            title = title_match.group(1).strip() if title_match else ""
            if not title:
                title = f"Chapter {chap_num}"

            chapters.append({
                "chap": chap_num,
                "title": title,
                "href": href,
            })

        return chapters

    def _extract_title_from_html(self, html: str) -> str:
        """Extract comic title from the page HTML."""
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find("h1")
        if h1:
            return h1.get_text(strip=True)
        return ""

    def _extract_images_from_html(self, html: str) -> List[str]:
        """Extract chapter page image URLs from the HTML.

        Strategy 1: Parse RSC props JSON embedded in the page (has all images).
        Strategy 2: Regex for CDN image URLs in the HTML (fallback).
        """
        images = []

        # Strategy 1: RSC props JSON
        props = self._extract_props_json(html)
        if props and isinstance(props.get("pages"), list):
            for page in props["pages"]:
                if isinstance(page, dict) and page.get("url"):
                    images.append(page["url"])
            if images:
                return images

        # Strategy 2: Regex for all CDN chapter image URLs
        pattern = r'https://cdn\.asurascans\.com/asura-images/chapters/[^"&\s]+'
        # Decode &quot; first for HTML-encoded contexts
        decoded_html = unescape(html)
        urls = re.findall(pattern, decoded_html)
        seen = set()
        for url in urls:
            if url not in seen:
                seen.add(url)
                images.append(url)

        return images

    def _extract_people_from_html(self, html: str) -> Dict[str, List[str]]:
        soup = BeautifulSoup(html, "html.parser")
        authors: List[str] = []
        artists: List[str] = []
        # Look for author/artist info sections
        for h3 in soup.find_all("h3"):
            text = h3.get_text(strip=True).lower()
            next_el = h3.find_next_sibling()
            if not next_el:
                # Try parent's next h3
                parent = h3.parent
                if parent:
                    siblings = parent.find_all("h3")
                    if len(siblings) >= 2 and siblings[0] == h3:
                        next_el = siblings[1]
            if not next_el:
                continue
            value = next_el.get_text(strip=True)
            if "author" in text or "writer" in text:
                authors = [p.strip() for p in re.split(r'[,/]', value) if p.strip()]
            elif "artist" in text or "illustrator" in text:
                artists = [p.strip() for p in re.split(r'[,/]', value) if p.strip()]
        result: Dict[str, List[str]] = {}
        if authors:
            result["authors"] = authors
        if artists:
            result["artists"] = artists
            if not authors:
                result.setdefault("authors", artists)
        return result

    def _slug_from_url(self, url: str) -> str:
        path = urlparse(url).path
        parts = [part for part in path.split("/") if part]
        if not parts:
            return ""
        # New format: /comics/<slug> or /comics/<slug>/chapter/<n>
        if parts[0] == "comics" and len(parts) > 1:
            return parts[1]
        # Old format: /series/<slug> or /series/<slug>/chapter/<n>
        if parts[0] == "series" and len(parts) > 1:
            return parts[1]
        return parts[0]

    def _chapter_url(self, base: str, slug: str, chapter_value: str) -> str:
        return f"{base}/comics/{slug}/chapter/{chapter_value}"

    # -- Series-detail props (the real metadata source) --------------
    # Asura is an Astro/RSC app (mid-2026 rebuild). The series title, author,
    # artist, cover, status, genres and description live ONLY in an
    # <astro-island props="{...}"> attribute as RSC wire format
    # ([type, value] pairs), NOT in the <h1>/<h3> DOM the legacy scrape
    # targeted (soup.find("h1") grabbed the site-header title on a redirected
    # homepage; _extract_people_from_html finds nothing). Everything below
    # sources from that props blob and only falls back to the DOM when it's
    # genuinely absent. grep _extract_series_props for the null-author /
    # homepage-title bug this fixes.

    # Author/artist strings Asura emits when it simply has no data — never let
    # these masquerade as a real credit. Compared lowercased.
    _PEOPLE_PLACEHOLDERS = frozenset(
        {"", "-", "_", "n/a", "na", "none", "null", "updating", "unknown", "tba", "?"}
    )

    def _extract_series_props(self, html: str) -> Optional[Dict]:
        """Return the SERIES-DETAIL props object, or None if this page has none.

        A comic page carries several `props="{...}"` blobs — the series detail
        plus homepage-style sidebar sections (TrendingSection, LatestUpdates,
        PopularSidebar) that ride every page. We pick the one with the
        series-detail shape: a `title` PLUS one of author/chapterCount/seriesId
        (the sidebar cards never carry all of those). The attribute value is
        HTML-entity-encoded, so every inner quote is &quot; and the ONLY literal
        double-quote is the attribute delimiter — that lets `props="([^"]*)"`
        capture the whole value without truncating on a nested brace (the old
        _extract_props_json used a non-greedy `.*?}` that stopped at the FIRST
        brace and grabbed a nav fragment, which is why series metadata was
        empty).

        None is ALSO the reliable "we landed on the homepage / were 301'd off a
        dead domain" signal that _fetch_series_page keys its cross-domain retry
        on — a redirected homepage has sidebar blobs but no series-detail one.
        """
        for enc in re.findall(r'props="([^"]*)"', html):
            if "&quot;title&quot;" not in enc:
                continue
            if not any(
                k in enc
                for k in (
                    "&quot;author&quot;",
                    "&quot;chapterCount&quot;",
                    "&quot;seriesId&quot;",
                )
            ):
                continue
            try:
                data = json.loads(unescape(enc))
            except (json.JSONDecodeError, ValueError):
                continue
            obj = self._unwrap_rsc(data)
            if isinstance(obj, dict) and obj.get("title"):
                return obj
        return None

    @classmethod
    def _split_people(cls, value) -> List[str]:
        """Split an Asura author/artist string on ','/'/' into a clean name list,
        dropping placeholder tokens (see _PEOPLE_PLACEHOLDERS). Real-but-odd
        values (pen names, studio credits) are kept as-is — Asura's own credit,
        which the AniList always-wins overwrite (sites/external_metadata.py,
        grep _staff_names_by_role) supersedes on a confident match."""
        if not value:
            return []
        out: List[str] = []
        for part in re.split(r"[,/]", str(value)):
            name = part.strip()
            if name and name.lower() not in cls._PEOPLE_PLACEHOLDERS:
                out.append(name)
        return out

    @staticmethod
    def _split_alt_titles(value) -> List[str]:
        """Asura's `alternativeTitles` is a single bullet-joined string
        ('SSS级重生猎人 • SSS급 죽어야 사는 헌터 • …'). Split on the bullet into
        alt_names — extra AniList search/scoring titles for obscure series whose
        primary romaji drifts between the site and AniList (harmless for the
        common case: enrich stops at the primary-title hit before touching
        them)."""
        if not value:
            return []
        return [t.strip() for t in str(value).split("•") if t.strip()]

    @staticmethod
    def _clean_description(value) -> str:
        """Strip the <p>/<br> markup Asura wraps its synopsis in → plain text
        suitable for ComicInfo <Summary>. Replaced by AniList's description on a
        confident enrichment match; this is the site fallback."""
        if not value:
            return ""
        s = re.sub(r"<br\s*/?>", "\n", str(value), flags=re.IGNORECASE)
        s = re.sub(r"</p\s*>", "\n\n", s, flags=re.IGNORECASE)
        s = re.sub(r"<[^>]+>", "", s)
        s = unescape(s)
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s.strip()

    def _fetch_series_page(
        self, url: str, scraper, make_request
    ) -> Tuple[str, str]:
        """Fetch a comic page, healing Asura's domain rotation with ONE targeted
        retry.

        Stale Asura domains 301 every /comics/<slug> to the LIVE host's HOMEPAGE
        (dropping the path) — e.g. 2026-07 asuracomic.net -> asurascans.com/.
        make_request follows the redirect, so a stored URL on a dead domain
        silently yields the homepage and the whole scrape (title, author,
        chapters) comes from the wrong page. We detect that (no series-detail
        props) and retry the path ONCE on the known-live host (_BASE_URL,
        asurascans.com).

        We try ONLY _BASE_URL, NOT a sweep over self.domains: the other domains
        are the dead ones that 301 straight back to the homepage, so sweeping
        them adds latency + a hardening cooldown per dead host. During a
        multi-source SEARCH that turned one slow asurascans.com fetch into a
        storm of dead-domain retries and (via the pre-fix stdout cooldown lines)
        corrupted --search-json (2026-07-08 TBATE regression). We also skip the
        retry when the origin IS already _BASE_URL — re-fetching the same host
        just yields the same homepage. Returns (resolved_url, html); the caller
        builds base_url / chapter URLs from the RESOLVED url so chapter fetches
        use the live host.

        Raises when nothing served a series page so a genuine dead-everywhere
        DOWNLOAD fails loud instead of scraping the homepage. In multi-source
        SEARCH this raise is caught per-source by aio_search_cli.py:_fetch_one
        (try/except around fetch_comic_context → logs via on_status to stderr),
        so asura is simply skipped and the search still completes.
        """
        html = self._fetch_html(url, scraper, make_request)
        if self._extract_series_props(html):
            return url, html
        live_host = urlparse(self._BASE_URL).netloc
        origin_host = urlparse(url).netloc
        path = urlparse(url).path
        if live_host and path and origin_host != live_host:
            candidate = f"{self._BASE_URL.rstrip('/')}{path}"
            try:
                alt_html = self._fetch_html(candidate, scraper, make_request)
            except Exception:
                alt_html = ""
            if alt_html and self._extract_series_props(alt_html):
                return candidate, alt_html
        raise RuntimeError(
            f"Asura: '{url}' has no series-detail data — it likely 301'd to the "
            f"homepage (dead domain) or the series isn't on Asura. Retried the "
            f"live host '{live_host}' without success. Re-add the series from the "
            f"current Asura URL. (see _fetch_series_page / _extract_series_props)"
        )

    # -- Base overrides ----------------------------------------------
    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        # Heal dead-domain redirects: returns the RESOLVED url (live host) so
        # base_url / chapter URLs below are built from a host that actually
        # serves the series, not a stale one that 301s to the homepage.
        resolved_url, html = self._fetch_series_page(url, scraper, make_request)

        parsed = urlparse(resolved_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        slug = self._slug_from_url(resolved_url)

        # Series metadata comes from the astro-island props JSON, not the DOM
        # (see _extract_series_props). Title: props title is authoritative;
        # fall back to the <h1> then the slug only if props is somehow absent.
        series = self._extract_series_props(html)
        title = str((series or {}).get("title") or "").strip()
        if not title:
            title = self._extract_title_from_html(html) or slug

        # BOTH "name" (what SiteComicContext.title mirrors + legacy readers) and
        # "title" (the key AniList enrichment reads: comic_data = context.comic,
        # and enrich_from_anilist keys off comic_data["title"] — grep raw_titles).
        # The pre-2026-07-07 handler set only "name", so a LIVE download passed
        # enrich a null title and NEVER matched on the primary title (it only
        # enriched if the series was later --refresh-library-metadata'd, which
        # builds comic_data["title"] from the stored meta). That was the real
        # reason a freshly-downloaded Asura series stayed un-enriched regardless
        # of author data. Dynasty (the model handler) keys "title"; match it.
        comic: Dict = {"name": title, "title": title, "slug": slug}
        if series:
            cover = series.get("coverUrl")
            if cover:
                comic["cover"] = str(cover)
            status = series.get("status")
            if status:
                comic["status"] = str(status)
            desc = self._clean_description(series.get("description"))
            if desc:
                comic["desc"] = desc
            authors = self._split_people(series.get("author"))
            if authors:
                comic["authors"] = authors
            artists = self._split_people(series.get("artist"))
            if artists:
                comic["artists"] = artists
            genres = series.get("genres")
            if isinstance(genres, list) and genres:
                # RSC-unwrapped list of {id,name,slug}; extract_additional_metadata
                # maps these to name strings.
                comic["genres"] = genres
            alt_names = self._split_alt_titles(series.get("alternativeTitles"))
            if alt_names:
                comic["alt_names"] = alt_names

        comic.setdefault("slug", slug)
        comic.setdefault("name", title)
        # Stabilize the series hid against Asura's rotating slug hash. Asura
        # bakes a rotating hex suffix into the slug
        # ("sss-class-suicide-hunter-46f09241"). Using the raw slug made the hid
        # drift across crashes/resumes/site migrations (asurascans.com <->
        # asuracomic.net, /comics/ <-> /series/), which spawned duplicate
        # "(hid=...)" folders and broke resume. Strip the trailing hash so one
        # series maps to one stable folder. The downloader keys folders and
        # resume off context.identifier (aio-dl.py:6877:
        # `hid, title = context.identifier, ...`), NOT comic["hid"], so we
        # return stable_hid as the identifier below. The FULL slug stays in
        # comic["slug"] and is what get_chapters uses to build
        # /comics/<full-slug>/chapter/<n> URLs.
        # Migration: allocate_series_output_dir reuses pre-fix full-slug folders
        # via its hash-tolerant _marker_matches (same -[0-9a-f]{6,}$ strip).
        stable_hid = re.sub(r"-[0-9a-f]{6,}$", "", slug) or slug
        comic["hid"] = stable_hid
        comic["slug"] = slug  # full slug (with hash) for chapter URL building
        if comic.get("cover") and not comic.get("thumb"):
            comic["thumb"] = comic["cover"]
        comic["_base_url"] = base_url

        # Legacy DOM author fallback — only if the props blob carried none
        # (partial/older pages). No-op on the current site where props wins;
        # kept as a safety net. Uses setdefault so it never clobbers props data.
        if not comic.get("authors"):
            for key, value in self._extract_people_from_html(html).items():
                if value:
                    comic.setdefault(key, value)

        # Extract chapter list from HTML
        chapter_list = self._extract_chapters_from_html(html)
        comic["_chapter_list"] = chapter_list

        return SiteComicContext(
            comic=comic,
            title=comic["name"],
            identifier=stable_hid,
            soup=None,
        )

    def extract_additional_metadata(
        self, context: SiteComicContext
    ) -> Dict[str, List[str]]:
        comic = context.comic or {}
        metadata: Dict[str, List[str]] = {}

        description = comic.get("description") or comic.get("summary")
        if description:
            comic["desc"] = description

        genres = comic.get("genres")
        if isinstance(genres, list):
            metadata["genres"] = [g["name"] for g in genres if isinstance(g, dict) and g.get("name")]

        for key, target in (("authors", "authors"), ("artists", "artists")):
            if key in comic and isinstance(comic[key], list):
                if comic[key] and isinstance(comic[key][0], dict):
                    metadata[target] = [
                        item["name"]
                        for item in comic[key]
                        if isinstance(item, dict) and item.get("name")
                    ]
                else:
                    metadata[target] = [str(item).strip() for item in comic[key] if str(item).strip()]

        return metadata

    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        comic = context.comic or {}
        # Full slug (with hash) for chapter URLs — context.identifier is now the
        # hash-stripped stable hid, so build URLs from comic["slug"] instead.
        slug = comic.get("slug") or context.identifier
        base_url = comic.get("_base_url") or "https://asurascans.com"
        chapter_list: List[Dict] = comic.get("_chapter_list", [])
        chapters: List[Dict] = []

        def chapter_sort_key(item):
            try:
                return float(item["chap"])
            except (ValueError, KeyError):
                return float("inf")

        for entry in sorted(chapter_list, key=chapter_sort_key):
            chap_num = entry.get("chap", "")
            href = entry.get("href", "")
            title = entry.get("title", f"Chapter {chap_num}")

            # Build full URL
            if href.startswith("/"):
                chapter_url = f"{base_url}{href}"
            elif href.startswith("http"):
                chapter_url = href
            else:
                chapter_url = self._chapter_url(base_url, slug, chap_num)

            chapters.append(
                {
                    "hid": f"{slug}-{chap_num}",
                    "chap": chap_num,
                    "title": title,
                    "url": chapter_url,
                    "group_name": None,
                }
            )

        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        chapter_url = chapter.get("url")
        if not chapter_url:
            raise RuntimeError("Chapter URL missing for Asura chapter.")

        html = self._fetch_html(chapter_url, scraper, make_request)
        images = self._extract_images_from_html(html)

        if not images:
            raise RuntimeError(
                f"No images found for Asura chapter: {chapter_url}"
            )
        return images

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

        by_href: Dict[str, List] = {}
        for anchor in soup.select('a[href^="/comics/"]'):
            href = (anchor.get("href") or "").strip()
            if not self._COMICS_HREF_RE.match(href):
                continue
            by_href.setdefault(href, []).append(anchor)

        hits: List[SearchHit] = []
        for idx, (href, anchors) in enumerate(by_href.items()):
            if len(hits) >= limit:
                break
            title = ""
            cover = None
            for anchor in anchors:
                if not title:
                    h3 = anchor.select_one("h3")
                    if h3:
                        title = h3.get_text(strip=True)
                if not cover:
                    img = anchor.select_one("img")
                    if img:
                        if not title:
                            title = (img.get("alt") or "").strip()
                        src = (img.get("src") or "").strip()
                        if src:
                            cover = src if src.startswith("http") else urljoin(self._BASE_URL, src)
            if not title:
                continue
            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=urljoin(self._BASE_URL, href),
                    cover=cover,
                    raw_score=max(0.05, 1.0 - (idx / max(1, len(by_href)))),
                )
            )
        return hits


__all__ = ["AsuraSiteHandler"]
