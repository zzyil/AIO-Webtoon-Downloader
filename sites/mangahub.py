"""MangaHub (mangahub.io) handler: search, series, chapter list and page list
all come from one GraphQL endpoint (api.mghcdn.com), funnelled through
_post_graphql; images are plain GETs on imgx.mghcdn.com.

The API needs an `x-mhub-access` token, which the site hands every visitor as
the `mhub_access` cookie. Each token carries a budget of 10 API calls, after
which the API answers a GraphQL error "API rate limit excessed!", and a spent
token did not refill within 12 minutes (measured 2026-11-06). The site's own
reader recovers by reloading the page with `?reloadKey=1` while still sending
the spent cookie; the server then sets a NEW token with a fresh budget
(measured: 5 reloads in a row, 10 calls each, 50/50 succeeded). The handler
does the same (grep _mint_token). Do NOT drop the cookie before the reload: a
cookieless visit is dealt one of a 3-token shared pool, which was spent.
This file used to send one hardcoded token; once that was spent (by early
Nov 2026), every page list failed and the search ranker rated mangahub
`unreachable` / `pages_failed`.
Registered in sites/__init__.py; tests: tests/test_mangahub_retry.py.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests

from .base import BaseSiteHandler, SearchHit, SiteComicContext


class MangaHubSiteHandler(BaseSiteHandler):
    name = "mangahub"
    domains = ("mangahub.io", "www.mangahub.io")

    _BASE_URL = "https://mangahub.io"
    _GRAPHQL_URL = "https://api.mghcdn.com/graphql"
    _IMAGE_BASE = "https://imgx.mghcdn.com/"
    _THUMB_BASE = "https://thumb.mghcdn.com/"

    # Access-token minting (see the module docstring). Any mangahub.io page sets
    # the cookie, 404s included; /search is a ~22 KB 200 (the homepage is
    # 1.8 MB). Without reloadKey the server just echoes back the cookie the
    # client sends, so a plain revisit never replaces a spent token.
    _TOKEN_COOKIE = "mhub_access"
    _TOKEN_MINT_URL = _BASE_URL + "/search"
    _TOKEN_RELOAD_URL = _BASE_URL + "/search?reloadKey=1"
    _RATE_LIMIT_MARK = "rate limit"
    # Re-mints per _post_graphql call. One reload normally clears it; the loop
    # also stops when the server deals a token this call already spent.
    _MAX_TOKEN_REFRESHES = 3
    # One handler instance serves every scraper and thread (prefetch, match
    # windows); serializing mints lets a thread that lost the race reuse the
    # token the winner just minted instead of dropping it again.
    _token_lock = threading.Lock()

    _MANGA_QUERY = """
    query MangaDetails($slug: String!) {
      manga(x:m01, slug:$slug) {
        id
        title
        slug
        status
        image
        latestChapter
        author
        artist
        genres
        description
        alternativeTitle
        mainSlug
        isYaoi
        isPorn
        isSoftPorn
        isLicensed
        updatedDate
        chapters {
          id
          number
          slug
          title
          date
        }
      }
    }
    """

    _CHAPTER_QUERY = """
    query ChapterPages($slug: String!, $number: Float!) {
      chapter(x:m01, slug:$slug, number:$number) {
        id
        number
        pages
      }
    }
    """

    # GraphQL search field signature confirmed via introspection (2026-05-07):
    #   search(x: MangaSource, mod: SearchMod!, q: String, alt: Boolean,
    #          status: Status, genreID: [Int], hideLicensed: Boolean,
    #          limit: Int, offset: Int) → { rows: [MangaListItem] }
    # MangaListItem fields exposed: id, title, slug, image, latestChapter, author.
    # alternativeTitle is NOT exposed on MangaListItem (only on the full Manga
    # type returned by manga(...)), so alt_titles is always empty here.
    _SEARCH_QUERY = """
    query Search($q: String!, $limit: Int!) {
      search(x: m01, mod: ALPHABET, q: $q, limit: $limit) {
        rows {
          id
          title
          slug
          image
          latestChapter
          author
        }
      }
    }
    """

    def configure_session(self, scraper, args) -> None:
        scraper.headers.setdefault("Referer", self._BASE_URL + "/")
        scraper.headers.setdefault("Origin", self._BASE_URL)
        scraper.headers.setdefault("User-Agent", "Mozilla/5.0")
        # x-mhub-access is NOT set here: it is minted lazily on the first API
        # call (no network in configure_session) and sent per request.

    # ------------------------------------------------------------------ helpers
    def _slug_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            raise RuntimeError("Invalid MangaHub URL.")
        if parts[0] == "manga":
            return parts[1]
        if parts[0] == "chapter":
            return parts[1]
        return parts[-1]

    # Transient-failure retry for the GraphQL POST. api.mghcdn.com sits behind
    # Cloudflare and answers in BURSTS of 525 (CF could not handshake with the
    # origin) that clear within seconds: measured 2026-11-06, two back-to-back
    # searches failed in <1 s with 525, the next five succeeded. A single bare
    # POST turned each burst into a hard failure everywhere this is called
    # (search, series load, every chapter's page list), and two failed searches
    # inside PROBE_FAILURE_TTL_S get the host blocked from search for an hour
    # (grep ProbeFailureCache in search_orchestrator.py). aio-dl.py's
    # make_request retries, but it is GET-only, so the POST retries here.
    # Retried: 429, 5xx (incl. CF 520-527), connection errors. NOT retried: other
    # 4xx and GraphQL `errors` (a bad slug / schema drift won't fix itself).
    # Timeouts are not retried either: a slow origin (5-34 s measured) would
    # just be waited on twice, and the search fan-out's soft barrier
    # (--search-timeout, 20 s default) has already moved on by then.
    _RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527})
    _MAX_ATTEMPTS = 4
    _RETRY_BACKOFF_S = (1.0, 2.0, 4.0)   # sleep before attempt 2, 3, 4 (7 s total)
    _TIMEOUT = (10.0, 40.0)              # (connect, read); the origin can take ~35 s

    def _current_token(self, scraper) -> Optional[str]:
        for c in getattr(scraper, "cookies", None) or ():
            if c.name == self._TOKEN_COOKIE and c.value:
                return c.value
        return None

    def _mint_token(self, scraper, spent: Optional[str]) -> Optional[str]:
        """Return a usable token for `scraper`, visiting the site for one.

        `spent` is the token the caller just saw refused (None on first use).
        If another thread already replaced it, that newer token is returned
        without a visit. On first use a plain visit sets the cookie; for a spent
        token the reload page is fetched WITH the spent cookie still in the jar,
        and the server replaces it with a fresh one. Returns None when the
        visit set no cookie; network errors propagate."""
        with self._token_lock:
            current = self._current_token(scraper)
            if current and current != spent:
                return current
            url = self._TOKEN_RELOAD_URL if spent else self._TOKEN_MINT_URL
            scraper.get(url, timeout=self._TIMEOUT)
            return self._current_token(scraper)

    def _send_graphql(self, scraper, payload: str, token: Optional[str]) -> Dict:
        """One logical POST with transient retries (see _RETRY_STATUSES);
        returns the whole JSON body, `errors` included. Raises the last
        HTTPError / ConnectionError when the failures persist, and requests'
        Timeout at once on a stalled origin."""
        headers = {"Content-Type": "application/json"}
        if token:
            headers["x-mhub-access"] = token
        response = None
        for attempt in range(self._MAX_ATTEMPTS):
            if attempt:
                time.sleep(self._RETRY_BACKOFF_S[min(attempt - 1, len(self._RETRY_BACKOFF_S) - 1)])
            try:
                response = scraper.post(
                    self._GRAPHQL_URL, headers=headers, data=payload, timeout=self._TIMEOUT,
                )
            except requests.exceptions.Timeout:
                raise
            except requests.exceptions.ConnectionError:
                if attempt + 1 >= self._MAX_ATTEMPTS:
                    raise
                continue
            if response.status_code in self._RETRY_STATUSES and attempt + 1 < self._MAX_ATTEMPTS:
                continue
            break
        response.raise_for_status()
        return response.json()

    def _is_rate_limited(self, errors) -> bool:
        return self._RATE_LIMIT_MARK in str(errors).lower()

    def _post_graphql(self, scraper, query: str, variables: Dict) -> Dict:
        """POST one GraphQL query and return its `data` object.

        Mints the access token on first use and re-mints when the API reports
        it spent (module docstring), stopping when the server deals a token this
        call already spent (the pool is exhausted for now). Raises RuntimeError
        on a GraphQL `errors` payload, the rate-limit one included once
        re-minting can't clear it, plus whatever _send_graphql raises. Callers
        rely on it raising (search() lets it propagate so the probe-failure
        cache sees a dead host)."""
        payload = json.dumps({"query": query, "variables": variables})
        token = self._current_token(scraper) or self._mint_token(scraper, None)
        spent: set = set()
        for _ in range(self._MAX_TOKEN_REFRESHES + 1):
            data = self._send_graphql(scraper, payload, token)
            errors = data.get("errors")
            if not (errors and self._is_rate_limited(errors)):
                break
            spent.add(token)
            token = self._mint_token(scraper, token)
            if not token or token in spent:
                break
        if "errors" in data:
            raise RuntimeError(f"MangaHub API error: {data['errors']}")
        return data.get("data") or {}

    def _absolute_thumb(self, image_path: Optional[str]) -> Optional[str]:
        if not image_path:
            return None
        if image_path.startswith("http"):
            return image_path
        return urljoin(self._THUMB_BASE, image_path)

    # ----------------------------------------------------------- Base overrides
    def fetch_comic_context(
        self,
        url: str,
        scraper,
        make_request,  # noqa: D401 - unused
    ) -> SiteComicContext:
        slug = self._slug_from_url(url)
        data = self._post_graphql(scraper, self._MANGA_QUERY, {"slug": slug})
        manga = data.get("manga")
        if not manga:
            raise RuntimeError("Unable to load MangaHub series data.")

        title = manga.get("title") or slug.replace("-", " ").title()
        comic: Dict[str, object] = {
            "hid": str(manga.get("id") or slug),
            "title": title,
            "desc": manga.get("description"),
            "cover": self._absolute_thumb(manga.get("image")),
            "authors": [a.strip() for a in (manga.get("author") or "").split(",") if a.strip()],
            "artists": [a.strip() for a in (manga.get("artist") or "").split(",") if a.strip()],
            "genres": manga.get("genres") or [],
            "alt_names": [a.strip() for a in (manga.get("alternativeTitle") or "").split(";") if a.strip()],
            "status": manga.get("status"),
            "_slug": slug,
            "_chapters": manga.get("chapters") or [],
        }

        return SiteComicContext(comic=comic, title=title, identifier=str(manga.get("id") or slug), soup=None)

    def get_chapters(
        self,
        context: SiteComicContext,
        scraper,
        language: str,  # noqa: D401 - unused
        make_request,  # noqa: D401 - unused
    ) -> List[Dict]:
        slug = context.comic.get("_slug")
        if not isinstance(slug, str):
            raise RuntimeError("Missing MangaHub slug.")
        chapters = context.comic.get("_chapters") or []
        results: List[Dict] = []
        for chapter in chapters:
            number = chapter.get("number")
            if number is None:
                continue
            chap_slug = chapter.get("slug") or f"chapter-{number}"
            url = f"{self._BASE_URL}/chapter/{slug}/{chap_slug}"
            results.append(
                {
                    "hid": str(chapter.get("id") or f"{slug}-{number}"),
                    "chap": str(number),
                    "title": chapter.get("title") or f"Chapter {number}",
                    "url": url,
                    "uploaded": chapter.get("date"),
                    "_slug": slug,
                    "_number": number,
                }
            )
        # Tolerant sort key: a non-numeric "chap" (defensive — number is coerced
        # via str() above, but a future schema drift shouldn't crash the whole
        # get_chapters). Non-numeric labels bucket last. Review finding PYP-3.
        def _chap_key(c):
            try:
                return (0, float(c.get("chap")))
            except (TypeError, ValueError):
                return (1, 0.0)
        results.sort(key=_chap_key)
        return results

    def get_chapter_images(
        self,
        chapter: Dict,
        scraper,
        make_request,  # noqa: D401 - unused
    ) -> List[str]:
        slug = chapter.get("_slug")
        number = chapter.get("_number")
        if not slug or number is None:
            raise RuntimeError("Chapter metadata incomplete for MangaHub.")
        data = self._post_graphql(scraper, self._CHAPTER_QUERY, {"slug": slug, "number": float(number)})
        info = data.get("chapter") or {}
        pages_raw = info.get("pages")
        if not pages_raw:
            raise RuntimeError("MangaHub chapter response missing pages.")
        try:
            pages_data = json.loads(pages_raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Unable to parse MangaHub page list.") from exc
        base_path = pages_data.get("p") or ""
        images = pages_data.get("i") or []
        if not images:
            raise RuntimeError("MangaHub returned an empty page list.")
        path_prefix = (base_path or "").lstrip("/")
        if path_prefix and not path_prefix.endswith("/"):
            path_prefix += "/"
        urls: List[str] = []
        for filename in images:
            if not filename:
                continue
            rel = path_prefix + filename.lstrip("/")
            urls.append(urljoin(self._IMAGE_BASE, rel))
        return urls

    # ----------------------------------------------------------------- search
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
        # GraphQL POST — same scraper.post path as fetch_comic_context. Errors
        # propagate via raise_for_status / RuntimeError so probe-failure cache
        # catches dead host. Wrap parsing only.
        try:
            data = self._post_graphql(
                scraper,
                self._SEARCH_QUERY,
                {"q": clean, "limit": int(limit)},
            )
        except (json.JSONDecodeError, ValueError):
            return []
        rows = (data.get("search") or {}).get("rows") or []
        if not isinstance(rows, list):
            return []

        hits: List[SearchHit] = []
        for idx, row in enumerate(rows):
            slug = row.get("slug")
            title = (row.get("title") or "").strip()
            if not slug or not title:
                continue
            cover = self._absolute_thumb(row.get("image"))
            # latestChapter is an int representing the most recent chapter
            # number — useful as chapter_count_hint (close-enough for series
            # where chapters are sequentially numbered from 1).
            chapter_count = row.get("latestChapter")
            if not isinstance(chapter_count, int):
                chapter_count = None
            raw_score = max(0.05, 1.0 - (idx / max(1, len(rows))))
            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=f"{self._BASE_URL}/manga/{slug}",
                    cover=cover,
                    alt_titles=[],
                    year=None,
                    language=None,
                    chapter_count_hint=chapter_count,
                    raw_score=raw_score,
                )
            )
        return hits


__all__ = ["MangaHubSiteHandler"]
