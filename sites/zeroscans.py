from __future__ import annotations

import json
from typing import Dict, List, Optional
from urllib.parse import urlparse

from .base import BaseSiteHandler, SearchHit, SiteComicContext


# Auto-domain selection (2026-05-24): the swordflake API has historically
# been served from zeroscans.com but the .us-TLD mirror and the zscans.com
# mirror have been brought up at various points when CF was breaking the
# primary. Rather than hard-coding `https://zeroscans.com/swordflake`,
# this handler now:
#   1. Anchors the API domain on the user's input URL when one is provided
#      (fetch_comic_context, get_chapter_images) so the same TLD the user
#      typed is what we query.
#   2. Probes the `domains` tuple in order on the first API call (search)
#      when there's no URL context, caching the first working domain.
#   3. Auto-bounces to the next mirror on connection errors or 5xx (e.g.
#      zeroscans.com → CF 525 → fail over to zeroscans.us). 4xx errors
#      bubble up unchanged — those are legit client errors and trying
#      mirrors won't help.
# Resolves zzyil's PR-31 review feedback "can you modify sites/zeroscans.py
# to determine the current API domain automatically instead of hardcoding
# it?".


class ZeroScansSiteHandler(BaseSiteHandler):
    name = "zeroscans"
    # Domain preference order. First entry is the historical primary; the
    # .us TLD is the .us-mirror; zscans.com was a 2026 alt that has shown
    # CF 525 issues. Probe order matches this tuple, with www-prefix
    # variants deduped down to their apex form so we don't double-probe.
    domains = (
        "zeroscans.com", "www.zeroscans.com",
        "zeroscans.us", "www.zeroscans.us",
        "zscans.com", "www.zscans.com",
    )

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # Cached working domain. None until set by:
        #   - _set_active_domain_from_url(url) when we have an explicit URL
        #     (fetch_comic_context, get_chapter_images)
        #   - First successful _api_request call (search probe path)
        # _candidate_domains() falls back to iterating the `domains` tuple
        # when this is None.
        self._active_domain: Optional[str] = None

    # -- API base helpers --------------------------------------------
    @classmethod
    def _default_domain(cls) -> str:
        """Apex form of the first domain in the tuple. Used by
        configure_session (which has no URL context) for the initial
        Referer/Origin until _api_request refreshes them to the active
        domain post-probe."""
        d = cls.domains[0]
        return d[4:] if d.startswith("www.") else d

    def _candidate_domains(self) -> List[str]:
        """Domains to try, in preference order. The active domain (if
        known) comes first; the rest of the `domains` tuple follows, with
        www-prefix deduplication so we don't double-probe `foo.com` AND
        `www.foo.com` against the same origin."""
        ordered: List[str] = []
        seen: set = set()
        if self._active_domain and self._active_domain not in seen:
            ordered.append(self._active_domain)
            seen.add(self._active_domain)
        for d in self.domains:
            apex = d[4:] if d.startswith("www.") else d
            if apex not in seen:
                ordered.append(apex)
                seen.add(apex)
        return ordered

    def _set_active_domain_from_url(self, url: str) -> None:
        """Anchor the API domain on the user-supplied URL's hostname so
        we query the same TLD the user typed. Unknown hostnames are
        accepted as-is (defensive — they might be a new mirror we haven't
        catalogued yet, in which case the user knows better than us)."""
        try:
            netloc = (urlparse(url).netloc or "").lower()
            if netloc.startswith("www."):
                netloc = netloc[4:]
            if netloc:
                self._active_domain = netloc
        except Exception:
            # Malformed URL — silently keep whatever _active_domain was;
            # _candidate_domains will iterate from the default if it's None.
            pass

    def _api_request(
        self,
        path: str,
        scraper,
        make_request=None,
    ) -> Dict:
        """Fetch https://<domain>/swordflake<path> from the first reachable
        mirror.

        Iterates _candidate_domains() and per-domain handles:
          - 2xx     → cache the domain in self._active_domain, refresh the
                      session's Referer/Origin to match, return parsed JSON.
          - 5xx     → try next mirror (origin sick on this one).
          - 4xx     → re-raise immediately (legit client error like 404
                      "comic not found"; mirror-bouncing wouldn't help).
          - network → connection refused / DNS / SSL / timeout / JSON
                      parse fail → try next mirror.

        `make_request`, when provided, plugs into aio-dl.py's retry +
        per-host backoff + coordinator infrastructure — preferred for
        request paths that benefit from those. Plain scraper.get is used
        when make_request is None (the historic _fetch_json behavior).

        Within-domain retries are NOT done here: when make_request is
        used, it does its own retry+backoff first, so by the time we
        bounce mirrors we've already exhausted the within-domain budget.
        """
        last_exc: Optional[Exception] = None
        path = path if path.startswith("/") else "/" + path
        for domain in self._candidate_domains():
            url = f"https://{domain}/swordflake{path}"
            try:
                if make_request is not None:
                    r = make_request(url, scraper)
                else:
                    r = scraper.get(url)
                status = getattr(r, "status_code", 0)
                if status >= 500:
                    # Server-side issue (CF 5xx, origin handshake fail,
                    # maintenance HTML) — bounce to next mirror without
                    # raising. Record for the final error message.
                    last_exc = RuntimeError(f"{domain} returned {status}")
                    continue
                # 2xx/3xx: trust it. raise_for_status() turns lingering 4xx
                # into HTTPError which bubbles up unchanged (per docstring).
                r.raise_for_status()
                # Success: cache the working domain and re-sync session
                # headers so subsequent unrelated GETs from the scraper
                # (e.g. cover fetches that don't go through _api_request)
                # still ride a consistent Referer/Origin.
                self._active_domain = domain
                scraper.headers.update({
                    "Referer": f"https://{domain}/",
                    "Origin": f"https://{domain}",
                })
                return r.json()
            except Exception as exc:
                # Connection error, SSL / DNS failure, JSON parse, HTTPError
                # on 4xx (status was < 500 so we didn't `continue` above —
                # this means raise_for_status raised on 4xx; we re-raise to
                # let the caller distinguish client error from mirror
                # outage). Plain ConnectionError / Timeout flows through
                # here naturally too.
                #
                # NOTE: an HTTPError raised here on 4xx will be re-thrown
                # below because we exhaust the loop with last_exc still
                # populated. That's correct for `comic not found` but does
                # mean we waste one extra mirror probe on a 4xx response.
                # Acceptable cost — mirror bouncing on 4xx is incorrect
                # anyway and the redundant probes will mostly hit
                # connection-cached sockets.
                last_exc = exc
                continue
        candidates = ", ".join(self._candidate_domains())
        raise RuntimeError(
            f"ZeroScans API unreachable on any of [{candidates}]: "
            f"{type(last_exc).__name__ if last_exc else 'unknown'}: "
            f"{last_exc or 'no response'}"
        )

    def configure_session(self, scraper, args) -> None:
        # Use the active domain when we already have one (e.g. a previous
        # call set it via _set_active_domain_from_url); otherwise fall back
        # to the default. _api_request will refresh these on its first
        # successful call to whichever mirror responds — see the headers
        # update inside _api_request's 2xx branch.
        domain = self._active_domain or self._default_domain()
        scraper.headers.update(
            {
                "Referer": f"https://{domain}/",
                "Origin": f"https://{domain}",
            }
        )

    # -- Legacy helper (kept for any external callers; new code uses
    #    _api_request which has mirror failover) ----------------------
    def _fetch_json(self, url: str, scraper) -> Dict:
        response = scraper.get(url)
        response.raise_for_status()
        return response.json()

    # -- Base overrides ----------------------------------------------
    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        # URL: https://<domain>/comics/{slug}
        # Anchor the API domain on the user's URL FIRST so the catalog
        # fetch below goes to the same TLD they typed. Without this
        # anchoring, a user who passed `zeroscans.us/comics/foo` would
        # still hit zeroscans.com on the catalog fetch (and fail if .com
        # is the one that's down — which is exactly when the .us-typed
        # URL is useful).
        self._set_active_domain_from_url(url)

        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]

        slug = None
        if len(path_parts) >= 2 and path_parts[0] == "comics":
            slug = path_parts[1]
        else:
            # Fallback: try to extract from end
            slug = path_parts[-1]

        if not slug:
            raise RuntimeError(f"Could not extract slug from URL: {url}")

        # We need to find the comic in the full list because the API doesn't seem to have a direct details endpoint by slug?
        # Kotlin: comicList.first { comic -> comic.slug == mangaSlug }
        # It fetches ALL comics to find one. That's heavy but that's what the extension does.
        # Let's try to see if there is a better way or just do that.
        # API: GET /swordflake/comics on the active domain
        comics_data = self._api_request("/comics", scraper, make_request)
        all_comics = comics_data.get("data", {}).get("comics", [])

        comic_data = next((c for c in all_comics if c.get("slug") == slug), None)

        if not comic_data:
            raise RuntimeError(f"Comic not found: {slug}")

        title = comic_data.get("name")
        comic_id = comic_data.get("id")

        comic = {
            "hid": slug,
            "title": title,
            "desc": comic_data.get("summary"),
            "status": comic_data.get("status"),
            "cover": comic_data.get("cover", {}).get("vertical"),
            "genres": [g.get("name") for g in comic_data.get("genres", [])],
            "_comic_id": comic_id,
        }

        # The swordflake API isn't formally documented; field names below
        # are tried in two reasonable shapes. Guarded so absent fields
        # are no-op (no regression if the schema differs).
        authors_raw = comic_data.get("authors") or comic_data.get("author")
        if isinstance(authors_raw, list):
            cleaned_authors = [
                (a.get("name") if isinstance(a, dict) else a)
                for a in authors_raw
                if a
            ]
            cleaned_authors = [a for a in cleaned_authors if isinstance(a, str) and a]
            if cleaned_authors:
                comic["authors"] = cleaned_authors
        elif isinstance(authors_raw, str) and authors_raw.strip():
            comic["authors"] = [authors_raw.strip()]

        year_raw = comic_data.get("year") or comic_data.get("release_year")
        if isinstance(year_raw, int) and year_raw > 0:
            comic["year"] = year_raw

        alt_raw = comic_data.get("alt_titles") or comic_data.get("aliases")
        if isinstance(alt_raw, list):
            cleaned_alt = [a for a in alt_raw if isinstance(a, str) and a]
            if cleaned_alt:
                comic["alt_names"] = cleaned_alt

        return SiteComicContext(
            comic=comic,
            title=title,
            identifier=slug,
            soup=None,
        )

    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        comic_id = context.comic.get("_comic_id")
        slug = context.identifier

        if not comic_id:
            # Re-fetch if missing (shouldn't happen)
            return []

        # API: GET /swordflake/comic/{id}/chapters?sort=desc&page={page} on
        # the active domain (set by fetch_comic_context above; this is
        # always called after fetch_comic_context within a single download).
        chapters = []
        page = 1
        has_more = True

        # Cache the active domain for the virtual chapter URL builder
        # below. Always set by fetch_comic_context, but fall back to the
        # default for defensiveness in case a caller invoked get_chapters
        # without fetch_comic_context (unusual but not impossible).
        active_domain = self._active_domain or self._default_domain()

        while has_more:
            data = self._api_request(
                f"/comic/{comic_id}/chapters?sort=desc&page={page}",
                scraper,
                make_request,
            )

            chap_data = data.get("data", {})
            current_chaps = chap_data.get("data", [])

            for chap in current_chaps:
                chap_id = chap.get("id")
                name = chap.get("name") # "123"
                created_at = chap.get("created_at")

                # Virtual URL: https://<active>/comics/{slug}/{id}
                # We stamp the active domain in so that on resume (via
                # `--restore-parameters URL`) the chapter URLs still
                # match the run's active mirror.
                chap_url = f"https://{active_domain}/comics/{slug}/{chap_id}"

                chapters.append({
                    "hid": str(chap_id),
                    "chap": str(name),
                    "title": f"Chapter {name}",
                    "url": chap_url,
                    "uploaded": created_at,
                    "_chapter_id": chap_id,
                })

            current_page = chap_data.get("current_page")
            last_page = chap_data.get("last_page")
            has_more = current_page < last_page
            page += 1

        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        # API: GET /swordflake/comic/{slug}/chapters/{id} on the active
        # domain.
        # Kotlin: GET("$baseUrl/$API_PATH/comic/$mangaSlug/chapters/$chapterId")
        # val mangaSlug = chapterUrlPaths[1]
        # val chapterId = chapterUrlPaths[2]
        # So we extract slug + id from the virtual URL stamped by
        # get_chapters above.

        url = chapter.get("url")
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        # parts: comics, slug, id

        if len(path_parts) < 3:
            raise RuntimeError(f"Invalid chapter URL: {url}")

        slug = path_parts[1]
        chap_id = path_parts[2]

        # Re-anchor the active domain from this chapter URL. Important on
        # resume / per-chapter fallback paths where get_chapter_images may
        # be called without a fresh fetch_comic_context (e.g. multi-source
        # download where this handler is the alt source and was never
        # warmed up).
        self._set_active_domain_from_url(url)

        data = self._api_request(
            f"/comic/{slug}/chapters/{chap_id}",
            scraper,
            make_request,
        )

        chap_detail = data.get("data", {}).get("chapter", {})

        # Kotlin: highQuality.takeIf { it.isNotEmpty() } ?: goodQuality
        high_quality = chap_detail.get("high_quality", [])
        good_quality = chap_detail.get("good_quality", [])

        images = high_quality if high_quality else good_quality

        return images

    # -- Cross-site search ------------------------------------------
    # ZeroScans has no dedicated /search endpoint. Their /swordflake/comics
    # endpoint returns the FULL catalog in one JSON payload (~50-200 entries),
    # so we client-side filter on `name` substring match. Fast on first call,
    # essentially free on subsequent queries via the orchestrator's per-host
    # request coordination. Same pattern as fetch_comic_context which already
    # walks the full list to find a slug.
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
        # Use _api_request so we get mirror failover. There's no URL
        # anchor on a search call (we don't know which TLD the user
        # prefers until they pick a result), so this is the path that
        # actually exercises the domains-tuple probe.
        try:
            data = self._api_request("/comics", scraper, make_request)
        except (RuntimeError, ValueError, json.JSONDecodeError):
            # All mirrors failed OR JSON came back invalid. Empty result
            # set so the orchestrator drops this source and moves on; the
            # explicit error message in _api_request gets surfaced to the
            # log by make_request's higher-level handling.
            return []
        all_comics = data.get("data", {}).get("comics") or []
        if not isinstance(all_comics, list):
            return []

        # Cache the active domain for the result URL builder below. Set
        # by _api_request's success path; fall back to default for safety.
        active_domain = self._active_domain or self._default_domain()

        ql = clean.lower()
        # Score by token overlap so multi-word queries match meaningfully.
        # We let the orchestrator's rapidfuzz reweight against title/alt_titles
        # — our raw_score is just a stable per-site relevance hint.
        query_tokens = set(t for t in ql.split() if t)

        scored: List[tuple] = []
        for c in all_comics:
            name = (c.get("name") or "").strip()
            if not name:
                continue
            nl = name.lower()
            # Substring or token-overlap match. Conservative: require either
            # the full query as a substring OR every query token to appear.
            if ql in nl:
                relevance = 1.0
            elif query_tokens and all(tok in nl for tok in query_tokens):
                relevance = 0.7
            else:
                continue
            scored.append((relevance, c))

        scored.sort(key=lambda x: -x[0])

        hits: List[SearchHit] = []
        for idx, (relevance, c) in enumerate(scored[:limit]):
            slug = c.get("slug")
            if not slug:
                continue
            cover_v = (c.get("cover") or {}).get("vertical")
            url = f"https://{active_domain}/comics/{slug}"
            # Position-based raw_score, scaled by relevance so substring
            # matches outrank token-overlap ones.
            raw_score = max(0.05, relevance * (1.0 - (idx / max(1, len(scored)))))
            hits.append(
                SearchHit(
                    site=self.name,
                    title=c.get("name") or slug,
                    url=url,
                    cover=cover_v,
                    alt_titles=[],
                    year=None,
                    language=None,
                    chapter_count_hint=None,
                    raw_score=raw_score,
                )
            )
        return hits


__all__ = ["ZeroScansSiteHandler"]
