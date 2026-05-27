from __future__ import annotations

import os
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from ._image_io import finalize_pending_image

# curl_cffi powers the fast image-download path used by handlers that opt into
# SUPPORTS_FAST_DOWNLOAD. HTTP/2 multiplex over a single keep-alive
# AsyncSession + Chrome-impersonate TLS fingerprint. Pinned to >=0.7.0 in
# requirements.txt for the AsyncSession API. ImportError fallback flips the
# module-level capability flag to False so opt-in handlers degrade to
# SUPPORTS_FAST_DOWNLOAD=False and the chapter loop reverts to its existing
# ThreadPoolExecutor + cloudscraper path. Cross-file: re-exported from
# sites/mangafire.py for back-compat with anything that grepped for the
# symbol there before this refactor.
try:
    from curl_cffi.requests import AsyncSession as _CurlCffiAsyncSession
    _CURL_CFFI_AVAILABLE = True
except Exception:  # ImportError or any sub-dep failure
    _CurlCffiAsyncSession = None  # type: ignore[assignment]
    _CURL_CFFI_AVAILABLE = False


class IncompleteChapterError(Exception):
    """Raised by handlers when a chapter cannot be fully fetched after the
    handler's own retry logic (e.g. MangaDex's MD@H node-swap loop has
    exhausted re-fetches but pages are still missing).

    The chapter download loop in aio-dl.py:_process_chapter_impl catches
    this around the get_chapter_images call and converts it to
    ChapterSkippedError, so the strict-wrapper retry / multi-source
    fallback / inline-retry machinery treats it the same as a Phase-2
    download failure. Without this signaling path, a handler that returns
    a truncated binary_image list would look "complete" to the validation
    block (since pages_total is computed AFTER the entries are classified).

    Cross-file: see aio-dl.py's existing ChapterSkippedError for the full
    retry contract; this exception only carries the diagnostic fields the
    wrapper needs to re-raise as ChapterSkippedError.
    """

    def __init__(
        self,
        pages_ok: int,
        pages_total: int,
        host: str = "",
        reason: str = "",
    ) -> None:
        self.pages_ok = int(pages_ok)
        self.pages_total = int(pages_total)
        self.host = host or ""
        self.reason = reason or "handler_incomplete"
        super().__init__(
            f"chapter incomplete: {self.pages_ok}/{self.pages_total} from "
            f"{self.host or '?'} ({self.reason})"
        )


class IncompleteChapterError(Exception):
    """Raised by handlers when a chapter cannot be fully fetched after the
    handler's own retry logic (e.g. MangaDex's MD@H node-swap loop has
    exhausted re-fetches but pages are still missing).

    The chapter download loop in aio-dl.py:_process_chapter_impl catches
    this around the get_chapter_images call and converts it to
    ChapterSkippedError, so the strict-wrapper retry / multi-source
    fallback / inline-retry machinery treats it the same as a Phase-2
    download failure. Without this signaling path, a handler that returns
    a truncated binary_image list would look "complete" to the validation
    block (since pages_total is computed AFTER the entries are classified).

    Cross-file: see aio-dl.py's existing ChapterSkippedError for the full
    retry contract; this exception only carries the diagnostic fields the
    wrapper needs to re-raise as ChapterSkippedError.
    """

    def __init__(
        self,
        pages_ok: int,
        pages_total: int,
        host: str = "",
        reason: str = "",
    ) -> None:
        self.pages_ok = int(pages_ok)
        self.pages_total = int(pages_total)
        self.host = host or ""
        self.reason = reason or "handler_incomplete"
        super().__init__(
            f"chapter incomplete: {self.pages_ok}/{self.pages_total} from "
            f"{self.host or '?'} ({self.reason})"
        )


@dataclass
class SiteComicContext:
    comic: Dict
    title: str
    identifier: str
    soup: Optional[BeautifulSoup] = None


@dataclass
class SearchHit:
    """Cross-site search result from a handler.search() call.

    Returned by handlers that implement search; consumed by
    sites/search_orchestrator.py to dedupe across sites and rank candidates.

    Field semantics:
      - site:    handler.name (e.g. 'mangadex'). Lets the orchestrator look up
                 quality_seed priors and route a chosen candidate back to
                 fetch_comic_context via the right handler.
      - url:     canonical comic URL that get_handler_for_url(url) will resolve
                 to this handler. Feeds straight into the existing single-URL
                 download flow when --auto-pick fires.
      - raw_score: site-internal relevance position, normalized 0..1. NOT used
                 directly for cross-site ranking — that's title_match (computed
                 by the orchestrator via rapidfuzz). raw_score is just a stable
                 fallback when the orchestrator can't compute its own score for
                 some reason.
      - alt_titles: used by rapidfuzz to match 'Frieren' against 'Sousou no Frieren'
                 etc. Empty list is fine; orchestrator just falls back to title.
      - chapter_count_hint: site's metadata claim about how many chapters exist
                 (e.g. MangaDex's attributes.lastChapter; MangaFire's "Chap N" badge).
                 Per-site definition — not normalized.
      - actual_chapter_count: how many chapters are actually fetchable in the
                 user's language. Set ONLY when handler bothered to verify (e.g.
                 MangaDex queries /chapter?manga=&limit=1 to read the total field).
                 None = not verified, not "unknown=zero". Used by the orchestrator
                 to detect DMCA-affected MangaDex entries: when chapter_count_hint
                 (metadata) >> actual_chapter_count, the series was likely
                 hollowed out by takedowns and the source is degraded.
      - dmca_likely: per-handler heuristic flag. True when chapter_count_hint
                 substantially exceeds actual_chapter_count (e.g. MD says 96
                 but only 1 EN chapter accessible). The orchestrator surfaces
                 this in JSON so users can see why a source is suspect.
      - is_official: per-hit override for handler.OFFICIAL_PUBLISHER.
                 None (default) = "defer to handler.OFFICIAL_PUBLISHER".
                 False = "this specific hit is NOT publisher-canonical
                 content" — e.g. linewebtoon Canvas user uploads:
                 webtoons.com hosts the file but LINE Webtoon doesn't
                 publish it, so canvas hits must not claim the official-
                 publisher tiebreaker that originals legitimately do.
                 Consumed by sites/search_orchestrator.py at the
                 SourceEntry.is_official assignment (AND'd with site-level
                 so a rogue handler can't claim official without the class
                 attribute set).
    """
    site: str
    title: str
    url: str
    cover: Optional[str] = None
    alt_titles: List[str] = field(default_factory=list)
    year: Optional[int] = None
    language: Optional[str] = None
    chapter_count_hint: Optional[int] = None
    actual_chapter_count: Optional[int] = None
    dmca_likely: bool = False
    raw_score: float = 0.0
    is_official: Optional[bool] = None


class BaseSiteHandler:
    """Base class for site-specific handlers."""

    name: str = "base"
    domains: tuple[str, ...] = ()
    # When True, the orchestrator's image-quality probe phase clamps to a
    # SINGLE sample for low-title-match results (below
    # EXPENSIVE_PROBE_QUICK_THRESHOLD in search_orchestrator.py). Default
    # False — pure-HTTP handlers don't pay much for 5 samples, and we want
    # the full aggregate signal for them. Override to True on handlers
    # whose per-chapter fetch is expensive (Playwright VRF capture). Today
    # only mangafire flips this; new VRF/expensive handlers should opt in.
    EXPENSIVE_PROBE: bool = False

    # When True, the search orchestrator treats this handler as the canonical
    # source for any series it returns — winning the per-candidate tiebreaker
    # over non-official aggregators WITHIN the same SeriesCandidate (post
    # union-find merge) regardless of measured image quality or title-match
    # spread. Set on handlers operating the publisher's own platform where
    # the canonical bytes originate; do NOT set on aggregators that re-host
    # other publishers' content (toonily, asura, mangafire, etc.).
    #
    # Why this matters: the image-quality probe scores resolution + format
    # + decode quality. Webtoons.com serves vertical-scroll PNGs at the
    # intentional 720–800px width — below the probe's res_score 800px floor,
    # so res_score contributes 0/1.0 to the composite. Aggregators that
    # upscale to 1500–2000px JPEG get a HIGHER res_score even though their
    # content is generation-loss from the official PNGs. Without this flag
    # the probe was confidently choosing toonily over webtoons.com despite
    # the latter being the literal publisher. Cross-file:
    # sites/search_orchestrator.py:SourceEntry.is_official + _cmp consume
    # this flag; grep OFFICIAL_PUBLISHER for the consumer side.
    #
    # Current opt-ins (lowercase handler names): linewebtoon.
    OFFICIAL_PUBLISHER: bool = False

    # When True, the chapter loop and the inter-chapter image prefetch route
    # this handler's image fetches through fast_download_images (curl_cffi
    # async + HTTP/2 multiplex over one keep-alive TLS session) instead of
    # the legacy dl_image + cloudscraper ThreadPoolExecutor path. Bench
    # (MangaFire, 83-page chapter, 2026-05-09): cloudscraper 3-thread =
    # 10.20s; curl_cffi async @ conc=8 = 6.04s. The win is HTTP/2 multiplex
    # eliminating per-page TLS handshake. Auto-disabled when curl_cffi
    # failed to import (falls back to the cloudscraper path).
    #
    # Opt in by setting True (typically `_CURL_CFFI_AVAILABLE`); subclasses
    # also override the FAST_DL_* attributes below if they need a custom
    # Referer / UA / TLS impersonate profile / extra headers. The base
    # fast_download_images method handles the rest.
    #
    # Don't opt in handlers whose image CDN requires a non-Chrome UA (e.g.
    # MangaDex's API ToS-mandated UA at sites/mangadex.py:configure_session) —
    # the curl_cffi `impersonate=` parameter overrides UA at the JA3/JA4
    # level too, and the API may reject the impersonated traffic outright.
    SUPPORTS_FAST_DOWNLOAD: bool = False

    # curl_cffi TLS-impersonate profile passed to AsyncSession(impersonate=).
    # "chrome120" gives a Chrome 120-equivalent JA3/JA4 + h2 settings frame.
    # Override only if the CDN requires a different fingerprint (very rare —
    # Cloudflare-fronted CDNs largely accept any modern Chrome profile).
    FAST_DL_IMPERSONATE: str = "chrome120"

    # User-Agent header sent with every fast-download request. None = let
    # curl_cffi fill the UA from the impersonate profile (Chrome's default
    # for that version). Override when consistency with the cloudscraper
    # session matters (MangaFire pins Chrome/122 because Cloudflare may
    # cookie-validate against the UA fingerprint of the cf_clearance cookie
    # captured by Patchright; mismatched UA invalidates the cookie).
    FAST_DL_USER_AGENT: Optional[str] = None

    # Static Referer URL (typically the site's homepage with trailing slash).
    # Empty string = no Referer header. Most aggregators serve images from
    # a separate CDN host and check Referer for anti-hotlink protection;
    # send the site's homepage URL to satisfy that check. Per-URL Referer
    # logic is rare; subclasses needing it override _fast_dl_build_headers
    # rather than this attribute.
    FAST_DL_REFERER_FROM: str = ""

    # Extra headers to send on every fast-download request (e.g.
    # X-Requested-With, custom auth tokens, locale hints). Built into the
    # request before Referer/User-Agent so those two attributes can override
    # entries here if both are set.
    FAST_DL_EXTRA_HEADERS: Dict[str, str] = {}

    def _fast_dl_build_headers(self, host: str) -> Dict[str, str]:
        """Build the headers dict sent with every fast-download request.

        Default implementation reads the FAST_DL_* class attributes. The
        `host` argument is provided so subclasses can override and inject
        per-host headers (rare); the default ignores it and emits a static
        dict driven entirely by the class config.

        Order: extra headers first, then Referer, then User-Agent — the last
        two override extras if a key collision happens (unlikely; called out
        for predictability).
        """
        headers: Dict[str, str] = dict(self.FAST_DL_EXTRA_HEADERS)
        if self.FAST_DL_REFERER_FROM:
            headers["Referer"] = self.FAST_DL_REFERER_FROM
        if self.FAST_DL_USER_AGENT:
            headers["User-Agent"] = self.FAST_DL_USER_AGENT
        return headers

    def fast_download_images(
        self,
        download_tasks: List[Tuple[int, str, str, str]],
        *,
        concurrency: int = 8,
        timeout: float = 30.0,
        is_cancelled: Optional[Callable[[], bool]] = None,
        record_host_failure: Optional[Callable[..., None]] = None,
        scraper: Any = None,
    ) -> List[Tuple[int, Optional[str]]]:
        """Bulk-download chapter images via curl_cffi async + HTTP/2.

        Lifted from the original sites/mangafire.py implementation
        (2026-05-13 generalization) with three substitutions to make it
        handler-agnostic: headers come from _fast_dl_build_headers,
        impersonate comes from FAST_DL_IMPERSONATE, and an optional
        `scraper` kwarg lets callers forward cookies from the cloudscraper
        session to the curl_cffi session (handler-relevant for sites that
        gate their image CDN on session cookies).

        Args:
          download_tasks: list of (page_index, url, folder, filename) tuples,
                          same shape aio-dl.py constructs in Phase 1. The
                          filename is a base placeholder like "5_0001.jpg";
                          finalize_pending_image rewrites the extension based
                          on actual bytes.
          concurrency:    asyncio.Semaphore bound. 8 is the bench-stable
                          default. Past ~12 is diminishing returns on most
                          home networks (network-bandwidth-limited).
          timeout:        Per-request socket timeout. 30s matches aio-dl.py's
                          default _HTTP_TIMEOUT.
          is_cancelled:   Optional callback. When True, every in-flight fetch
                          checks before sending the next request and bails.
          record_host_failure: Optional callback fired when a URL hard-fails.
                          Updates aio-dl.py's _HOST_FAIL_COUNT so the chapter
                          watchdog can poison-detect a flaky CDN. Forward-
                          compatible kwarg signature: callers may pass
                          (host, url) or (host, url, status=..., body_size=...)
                          — the latter feeds the ghost-chapter signature
                          accumulator. Older overrides that pass only
                          (host, url) keep working; new code should forward
                          status + body_size when the response object is in
                          scope.
          scraper:        Optional cloudscraper session. When supplied, the
                          curl_cffi AsyncSession is constructed with cookies
                          forwarded from the scraper that match the host of
                          the first download URL (single-host-per-chapter
                          assumption — true in 99%+ cases). Lets handlers
                          like LineWebtoon ride along their .webtoons.com
                          age-gate cookies even though the curl_cffi session
                          is a separate TLS session from cloudscraper's.

        Returns: list of (page_index, path_or_None), ordered by page_index.
        path_or_None matches dl_image's contract — None signals failure.

        Subclass override pattern: most handlers won't need to override this
        method — set the FAST_DL_* class attributes instead. Subclasses
        that DO override should mirror the cancellation + record_host_failure
        callback shape so the existing aio-dl.py wiring continues to work.
        """
        if not _CURL_CFFI_AVAILABLE:
            raise RuntimeError(
                "fast_download_images called without curl_cffi installed. "
                "Caller should check SUPPORTS_FAST_DOWNLOAD before invoking."
            )
        if not download_tasks:
            return []

        import asyncio

        # Build cookies dict for the URL host. Filter scraper cookies to only
        # include those whose domain matches the target host (or which have
        # no domain at all — those ride along on every same-host request).
        # When scraper is None or has no relevant cookies, dict ends up empty.
        cookies: Optional[Dict[str, str]] = None
        if scraper is not None:
            try:
                first_host = urlparse(download_tasks[0][1]).netloc
                relevant: Dict[str, str] = {}
                for c in scraper.cookies:
                    cookie_domain = (c.domain or "").lstrip(".")
                    # No domain set → ride along on same-host. Domain set →
                    # match if the request host endswith the cookie domain.
                    if not cookie_domain or first_host.endswith(cookie_domain):
                        relevant[c.name] = c.value
                if relevant:
                    cookies = relevant
            except Exception:
                # Cookie extraction is best-effort; swallow and continue
                # with no cookies rather than failing the whole download.
                cookies = None

        # Headers built once per chapter — host parameter is for subclass
        # hooks; default implementation ignores it.
        first_host_for_headers = urlparse(download_tasks[0][1]).netloc
        headers = self._fast_dl_build_headers(first_host_for_headers)

        async def _fetch_one(
            session, sema, page_idx: int, url: str, folder: str, filename: str
        ) -> Tuple[int, Optional[str]]:
            base, _ = os.path.splitext(filename)
            if not base:
                base = filename
            pending_path = os.path.join(folder, f".pending_{base}")
            host = urlparse(url).netloc

            # Two attempts: original + one retry on transient failure. No
            # variant cascade — alternates rarely exist on image CDNs and
            # subclasses can override fast_download_images entirely if they
            # need one. (MangaFire confirmed: alternative path segments
            # /o/, /full/, /orig/ and extensions .png, .webp all 404.)
            for attempt in range(2):
                if is_cancelled is not None and is_cancelled():
                    return page_idx, None
                async with sema:
                    # Re-check after sema acquire — coroutines that were
                    # queued before cancel was set should still bail here
                    # rather than firing a GET they were already cancelled
                    # for. (Without this, large queues + late cancel = the
                    # remaining tail still issues HTTP requests.)
                    if is_cancelled is not None and is_cancelled():
                        return page_idx, None
                    try:
                        r = await session.get(url, headers=headers, timeout=timeout)
                    except Exception as exc:
                        # Per-attempt visibility: shows up in aio-dl's stderr
                        # which the UI's LogPanel surfaces. Ported from the
                        # 2026-05-13-deleted mangafire-specific
                        # fast_download_images for log-verbosity parity.
                        # Subclasses that don't want this can override
                        # fast_download_images and skip the print.
                        import sys
                        print(f"[-] curl_cffi exception: {exc} for URL: {url}", file=sys.stderr)
                        if attempt < 1:
                            await asyncio.sleep(1.0)
                            continue
                        if record_host_failure is not None:
                            try:
                                # No response object (request itself raised),
                                # so no status/body_size to forward. The
                                # callback's kwargs default to None and the
                                # ghost-detector treats absent signatures
                                # as zero-bucket — exceptions that all share
                                # the same cls also register as uniform if
                                # they all hit this path identically.
                                record_host_failure(host, url)
                            except Exception:
                                pass
                        return page_idx, None
                if r.status_code != 200 or not r.content or len(r.content) < 256:
                    import sys
                    body_len = len(r.content) if r.content else 0
                    print(
                        f"[-] curl_cffi status={r.status_code} "
                        f"size={body_len} for URL: {url}",
                        file=sys.stderr,
                    )
                    if attempt < 1:
                        await asyncio.sleep(1.0)
                        continue
                    if record_host_failure is not None:
                        try:
                            # Forward status + body_size so aio-dl.py's
                            # _record_failure can feed the ghost-chapter
                            # signature accumulator. Uniform (status,
                            # body_bucket) across every page of a chapter
                            # is the signal that distinguishes a fake/
                            # placeholder chapter from a transient CDN
                            # issue. See aio-dl.py:_is_ghost_chapter_signature.
                            record_host_failure(
                                host, url,
                                status=r.status_code,
                                body_size=body_len,
                            )
                        except Exception:
                            pass
                    return page_idx, None
                # Bytes look real — write pending file then atomic-rename.
                # finalize_pending_image runs sync; safe inside the coroutine
                # because file I/O is the same cost either way.
                try:
                    os.makedirs(folder, exist_ok=True)
                    with open(pending_path, "wb") as fh:
                        fh.write(r.content)
                except OSError:
                    return page_idx, None
                content_type = ""
                try:
                    content_type = r.headers.get("Content-Type", "") or ""
                except Exception:
                    content_type = ""
                final = finalize_pending_image(
                    pending_path, folder, base, content_type
                )
                return page_idx, final
            return page_idx, None

        async def _run() -> List[Tuple[int, Optional[str]]]:
            sema = asyncio.Semaphore(max(1, int(concurrency)))
            # Single AsyncSession across all pages of this chapter so HTTP/2
            # multiplex + connection keepalive amortize TLS handshake cost.
            # impersonate sets the JA3/JA4 + h2 settings frame to match a
            # real browser — should not strictly be needed for cookieless
            # edge-cached image CDNs, but defensive (and free).
            session_kwargs: Dict[str, Any] = {"impersonate": self.FAST_DL_IMPERSONATE}
            if cookies:
                session_kwargs["cookies"] = cookies
            async with _CurlCffiAsyncSession(**session_kwargs) as s:
                tasks = [
                    _fetch_one(s, sema, p_idx, url, folder, name)
                    for p_idx, url, folder, name in download_tasks
                ]
                return await asyncio.gather(*tasks)

        # Run in this thread's own event loop. asyncio.run constructs a fresh
        # loop, so works whether called from main thread or from a daemon
        # prefetch thread (each has no running loop).
        results = asyncio.run(_run())
        # Preserve original submission order (page_idx ascending). gather()
        # already returns in input order, but sorting is cheap insurance.
        results.sort(key=lambda t: t[0])
        return results

    def matches(self, url: str) -> bool:
        netloc = urlparse(url).netloc.lower()
        return any(domain in netloc for domain in self.domains)

    # --- Session lifecycle -------------------------------------------------
    def configure_session(self, scraper, args) -> None:
        """Give the handler a chance to tweak the HTTP session."""
        return None

    # Handlers needing main-thread setup before their get_chapters can run in
    # worker threads (e.g. comix's Patchright-based chapter-listing token
    # capture, which can't drive its sync API without a main-thread asyncio
    # loop) set this True AND override prepare_chapter_fetch. Read by
    # aio_search_cli._fetch_chapters_for_winner to skip the scraper-build cost
    # on the common path.
    NEEDS_MAIN_THREAD_PREFETCH: bool = False

    def prepare_chapter_fetch(
        self, url: str, scraper, args, make_request
    ) -> None:
        """Optional main-thread pre-warm hook invoked by
        aio_search_cli._fetch_chapters_for_winner BEFORE it dispatches the
        per-source ThreadPoolExecutor that runs fetch_comic_context +
        get_chapters across the candidate's sources.

        Handlers requiring main-thread initialization override this to warm
        any per-title cache so the worker-thread get_chapters call hits the
        cache instead of failing to capture. Default no-op; called only when
        NEEDS_MAIN_THREAD_PREFETCH is True on the subclass.

        Cross-file: aio_search_cli.py:_fetch_chapters_for_winner.
        """
        return None

    # --- Initial comic retrieval ------------------------------------------
    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        """Return the key comic data for downstream processing."""
        raise NotImplementedError

    def extract_additional_metadata(
        self, context: SiteComicContext
    ) -> Dict[str, List[str]]:
        """Optional metadata enrichment hook."""
        return {}

    # --- Chapter helpers ---------------------------------------------------
    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        raise NotImplementedError

    def get_volumes(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        return []

    def get_group_name(self, chapter_version: Dict) -> Optional[str]:
        return None

    def normalize_group_name(self, group_name: Optional[str]) -> Optional[str]:
        if not isinstance(group_name, str):
            return None
        cleaned = group_name.strip().casefold()
        if not cleaned:
            return None
        cleaned = re.sub(r"[_./-]+", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if re.search(r"\b(?:official|webtoons?|naver)\b", cleaned):
            return "official"
        return cleaned

    def get_group_match_key(self, group_name: Optional[str]) -> Optional[str]:
        normalized = self.normalize_group_name(group_name)
        if not normalized:
            return None
        squashed = re.sub(r"[^0-9a-z]+", "", normalized)
        return squashed or normalized

    def select_best_chapter_version(
        self,
        versions: List[Dict],
        preferred_groups: List[str],
        mix_by_upvote: bool,
        allow_group_fallback: bool = True,
        log_debug_fn=None,
    ) -> Optional[Dict]:
        if not versions:
            return None

        def upvotes(v: Dict) -> int:
            return v.get("up_count", 0)

        def _debug(msg):
            if log_debug_fn:
                log_debug_fn(msg)

        def _available_groups() -> str:
            groups: List[str] = []
            for version in versions:
                group_name = self.get_group_name(version)
                if not isinstance(group_name, str):
                    continue
                cleaned = group_name.strip()
                if cleaned and cleaned not in groups:
                    groups.append(cleaned)
            return ", ".join(groups) if groups else "none"

        def _annotate_selection(
            version: Dict,
            *,
            selection_kind: str,
            requested_group: Optional[str] = None,
        ) -> Dict:
            annotated = dict(version)
            annotated["_selection_kind"] = selection_kind
            annotated["_requested_group"] = requested_group
            annotated["_available_groups"] = _available_groups()
            return annotated

        chap_label = versions[0].get("chap", "?")
        best_by_upvote = max(versions, key=upvotes)

        if not preferred_groups:
            _debug(
                f"    Ch {chap_label}: No group specified. Selected by upvotes ({best_by_upvote.get('up_count', 0)})."
            )
            return _annotate_selection(best_by_upvote, selection_kind="upvote_no_group")

        preferred_entries = [
            (group_name, self.get_group_match_key(group_name))
            for group_name in preferred_groups
        ]
        preferred_entries = [
            (group_name, match_key)
            for group_name, match_key in preferred_entries
            if match_key
        ]
        if not preferred_entries:
            _debug(
                f"    Ch {chap_label}: Group filter contained no usable names. Selected by upvotes ({best_by_upvote.get('up_count', 0)})."
            )
            return _annotate_selection(
                best_by_upvote,
                selection_kind="upvote_invalid_group_filter",
            )

        if mix_by_upvote:
            preferred = [
                v
                for v in versions
                if self.get_group_match_key(self.get_group_name(v))
                in {match_key for _, match_key in preferred_entries}
            ]
            if preferred:
                best = max(preferred, key=upvotes)
                _debug(
                    f"    Ch {chap_label}: Mix-by-upvote. Selected '{self.get_group_name(best)}' ({best.get('up_count', 0)} upvotes)."
                )
                return _annotate_selection(
                    best,
                    selection_kind="preferred_mix_by_upvote",
                )
            if not allow_group_fallback:
                _debug(
                    f"    Ch {chap_label}: Mix-by-upvote. None of the requested groups were present. Skipping chapter. Available groups: {_available_groups()}."
                )
                return None
            _debug(
                f"    Ch {chap_label}: Mix-by-upvote. None of the requested groups were present. Falling back to upvotes with '{self.get_group_name(best_by_upvote)}'. Available groups: {_available_groups()}."
            )
            return _annotate_selection(
                best_by_upvote,
                selection_kind="fallback_missing_group",
            )

        for group_name, match_key in preferred_entries:
            candidates = [
                v
                for v in versions
                if self.get_group_match_key(self.get_group_name(v)) == match_key
            ]
            if candidates:
                best = max(candidates, key=upvotes)
                _debug(
                    f"    Ch {chap_label}: Found in priority group '{group_name}'. Selected '{self.get_group_name(best)}'."
                )
                return _annotate_selection(
                    best,
                    selection_kind="preferred_priority",
                    requested_group=group_name,
                )
        if not allow_group_fallback:
            _debug(
                f"    Ch {chap_label}: None of the requested groups were present. Skipping chapter. Available groups: {_available_groups()}."
            )
            return None
        _debug(
            f"    Ch {chap_label}: None of the requested groups were present. Falling back to upvotes with '{self.get_group_name(best_by_upvote)}'. Available groups: {_available_groups()}."
        )
        return _annotate_selection(
            best_by_upvote,
            selection_kind="fallback_missing_group",
        )

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        raise NotImplementedError

    # --- Cross-site search -------------------------------------------------
    def search(
        self,
        query: str,
        scraper,
        make_request,
        *,
        language: str = "en",
        limit: int = 20,
    ) -> List[SearchHit]:
        """Search this site for comics matching `query`.

        Default no-op so unimplemented handlers self-select out of the
        orchestrator (sites/search_orchestrator.py). The orchestrator filters
        via sites.iter_search_capable_handlers(), which compares the bound
        method against this base no-op — so handlers must override this method
        on the class (not assign it post-init) to be picked up.

        Implementations should:
          - Let HTTP errors (5xx, connection errors, timeouts) propagate. The
            orchestrator's _run_one catches them and records the host in the
            probe-failure cache so the next search skips that host. Swallowing
            them here returns [] which looks identical to "no match for query"
            and leaves the cache empty — meaning every search keeps eating
            time on the dead host. Wrap *parsing* in try/except (returning []
            on malformed HTML is fine), not the HTTP call.
          - Use the provided make_request callable so retries, cooldowns, and
            cross-process rate-limit coordination flow through automatically.
          - Cap at `limit` hits to keep the merge step bounded.
        """
        return []

    def probe_sample_image(
        self, hit: "SearchHit", scraper, make_request
    ) -> Optional[bytes]:
        """Return raw bytes of a representative image for image-quality scoring.

        Used by sites/search_orchestrator.py to replace per-site quality_seed
        priors with measured values. Tries the chapter-image path first
        (accurate — reflects what the user actually downloads) and falls back
        to the cover-image path on any failure (faster, broadly available, but
        biased per-site since cover and chapter-image CDN policies often
        differ; e.g., MangaFire ships 280×400 covers but full-res chapter
        pages, while MangaDex ships 690×1000 covers regardless of whether a
        series is DMCA-hollowed).

        Override when you need:
          - site-specific cover URL cleanup → override _probe_cover_image
            (see MangaFire's @<digits> strip).
          - a custom chapter-fetch path that doesn't go through the standard
            fetch_comic_context + get_chapters + get_chapter_images interface
            → override this method directly.

        Returns None on total failure (both chapter and cover paths failed).
        The orchestrator then falls back to the seed prior.
        """
        blob = self._probe_chapter_image(hit, scraper, make_request)
        if blob:
            return blob
        return self._probe_cover_image(hit, scraper, make_request)

    def _probe_chapter_image(
        self, hit: "SearchHit", scraper, make_request
    ) -> Optional[bytes]:
        """Fetch the middle page of a representative chapter for accurate
        image-quality scoring.

        Default flow uses the standard handler interface:
          fetch_comic_context → get_chapters → pick middle chapter →
          get_chapter_images → pick middle page → fetch bytes.

        Handles two get_chapter_images return shapes (interface is currently
        heterogeneous — base.py's type hint says List[str] but MangaReader
        returns List[Dict]):
          - List[str]: URL strings (most handlers). Fetched via scraper.get.
          - List[Dict] with {"type": "binary_image", "data": bytes, ...}:
            pre-fetched bytes (currently MangaReader, which descrambles
            in-handler). Returned as-is.

        Cross-file: orchestrator (sites/search_orchestrator.py:_probe_one)
        calls this through probe_sample_image inside a thread; failures
        propagate as None and probe_sample_image falls back to cover.

        Returns None on any failure to keep the cover-fallback path simple.
        """
        if not hit or not hit.url:
            return None
        try:
            context = self.fetch_comic_context(hit.url, scraper, make_request)
        except Exception:
            return None
        if context is None:
            return None
        try:
            chapters = self.get_chapters(context, scraper, "en", make_request)
        except Exception:
            return None
        if not chapters:
            return None
        chapter = self._pick_representative_chapter(chapters)
        if chapter is None:
            return None
        try:
            image_items = self.get_chapter_images(chapter, scraper, make_request)
        except Exception:
            return None
        if not image_items:
            return None
        item = self._pick_middle_image_item(image_items)
        if item is None:
            return None
        # Pre-fetched binary (MangaReader-style): use the blob directly.
        if isinstance(item, dict):
            if item.get("type") == "binary_image":
                blob = item.get("data")
                if isinstance(blob, (bytes, bytearray)) and len(blob) >= 256:
                    return bytes(blob)
            return None
        # URL string: fetch it directly. We use scraper.get (not make_request)
        # because search_mr's 5xx-as-exception translation isn't useful for
        # image bytes — we just want the file or a clear failure.
        if isinstance(item, str) and item:
            try:
                response = scraper.get(item, timeout=15)
                if response.status_code >= 400:
                    return None
                data = response.content
                if not data or len(data) < 256:
                    return None
                return data
            except Exception:
                return None
        return None

    @staticmethod
    def _pick_representative_chapter(chapters: List[Dict]) -> Optional[Dict]:
        """DEPRECATED (kept as back-compat shim for the legacy
        _probe_chapter_image path at sites/base.py:557 and external
        callers). New code should use _pick_representative_chapters
        (plural) which returns N chapters for breadth sampling — the
        v5 probe pipeline samples 1 page from each of 8 chapters
        instead of 5 pages from 1 chapter.

        Picks a chapter from the middle of the list, preferring whole
        numbers (4.5, 60.1 → omake/extras tend to have atypical page
        counts; whole-numbered are more representative).

        Chapter lists arrive in different orders per handler (MangaDex
        ASC, MangaFire DESC) but the middle is representative regardless.
        """
        if not chapters:
            return None
        whole: List[Dict] = []
        for ch in chapters:
            chap = ch.get("chap")
            if chap is None:
                continue
            try:
                f = float(chap)
            except (TypeError, ValueError):
                continue
            # Whole number: 4.0, 47, "12" all qualify; 4.5, 60.1 don't.
            if f == int(f):
                whole.append(ch)
        pool = whole if whole else list(chapters)
        if not pool:
            return None
        if len(pool) <= 2:
            return pool[0]
        return pool[len(pool) // 2]

    @staticmethod
    def _pick_representative_chapters(
        chapters: List[Dict], n: int = 8,
    ) -> "List[Tuple[int, Dict]]":
        """Pick up to N chapters spread across the series for breadth sampling.

        Returns a list of `(absolute_index_in_chapters_list, chapter_dict)`
        tuples — the absolute index is needed by
        _pick_random_middle_page_index so the deterministic seed is stable
        across runs (cache replay relies on this).

        Strategy:
          - Skip first 1 + last 1 chapter (chapter 1 often differs in cover-
            page treatment; latest chapter may be partial / TBA placeholder).
          - From the trimmed pool, prefer whole-numbered chapters (skipping
            omake .5/.1 fragments that have atypical page counts).
          - Pick N evenly-spaced chapters from the preferred pool; if there
            are fewer than N whole-numbered chapters, fill the remainder
            from the full pool to hit N total.

        Degenerate cases:
          - Empty list → []
          - 1 chapter (oneshot) → [(0, that_chapter)]
          - 2-3 chapters → returns the chapter(s) after skipping first/last
            (down to 1 if all that's left)
          - Long series → up to N indices spread evenly

        Cross-file: called by _probe_chapter_aggregate (this file, post-Phase-2
        rewrite) and by calibrate_quality_probe.py via the same delegation.
        The v5 sampling strategy is documented in
        ~/.claude/plans/how-robust-is-the-memoized-koala.md (Phase 2 section).
        """
        if not chapters:
            return []
        total = len(chapters)
        # Oneshot — only one chapter, return it (skipping first/last would
        # leave nothing, defeating the probe).
        if total == 1:
            return [(0, chapters[0])]
        # Trim first and last 1 chapter when there's room. For total in
        # [2, 3], trimming both would leave 0-1 chapters; trim only as much
        # as keeps the pool non-empty.
        if total >= 4:
            trim_start, trim_end = 1, 1
        elif total == 3:
            trim_start, trim_end = 1, 0  # keep middle + last
        else:  # total == 2
            trim_start, trim_end = 0, 0  # keep both
        trimmed = list(enumerate(chapters))[trim_start: total - trim_end]
        if not trimmed:
            # Defensive (shouldn't hit given the conditions above).
            trimmed = list(enumerate(chapters))

        # Preferred pool: whole-numbered chapters only.
        whole_pool: List[Tuple[int, Dict]] = []
        for abs_idx, ch in trimmed:
            chap = ch.get("chap")
            if chap is None:
                continue
            try:
                f = float(chap)
            except (TypeError, ValueError):
                continue
            if f == int(f):
                whole_pool.append((abs_idx, ch))

        # If the whole-numbered pool meets the budget, sample evenly from it.
        # Otherwise fill from the full trimmed pool — partial chapters are
        # better than no sample.
        primary_pool = whole_pool if whole_pool else trimmed
        if len(primary_pool) <= n:
            base_picks = list(primary_pool)
        else:
            # Evenly-spaced sampling: step = len/N, take indices at
            # step/2, step*3/2, step*5/2, ... so we hit the middle of each
            # bucket (avoids over-weighting endpoints which trim already
            # handled, but bucketing center is still cleaner than edge picks).
            step = len(primary_pool) / n
            picks_idx = [min(int(step * (i + 0.5)), len(primary_pool) - 1) for i in range(n)]
            base_picks = [primary_pool[i] for i in picks_idx]

        # Top up from the full trimmed pool if we under-shot N (only when
        # whole_pool was the smaller primary pool).
        if len(base_picks) < n and primary_pool is whole_pool:
            seen = {abs_idx for abs_idx, _ in base_picks}
            for abs_idx, ch in trimmed:
                if abs_idx in seen:
                    continue
                base_picks.append((abs_idx, ch))
                seen.add(abs_idx)
                if len(base_picks) >= n:
                    break

        # Sort by absolute index for predictable ordering (matches v4's
        # chapter-list traversal direction so cache keys stay stable when
        # the sampler is re-run).
        base_picks.sort(key=lambda t: t[0])
        return base_picks[:n]

    @staticmethod
    def _pick_random_middle_page_index(
        n_pages: int, series_url: str, chapter_index: int,
        chapter: "Optional[Dict]" = None,
    ) -> "Optional[int]":
        """Deterministically pick a page index from the middle 50% of a chapter.

        Returns an int in [n_pages//4, 3*n_pages//4) for non-trivial chapters
        and a safe-middle for very short ones. The seed comes from
        SHA-1((series_url, stable_chapter_key)) where stable_chapter_key
        prefers identifiers that DON'T shift when the chapter list grows:
          1. ``chapter["url"]``    — the chapter URL is the most stable
                                     identifier upstream produces.
          2. ``chapter["chap"]``   — the chapter number (string-coerced
                                     so "47" == 47).
          3. ``chapter["hid"]``    — handler-specific hash ID where present.
          4. ``chapter["id"]``     — MangaDex-style UUID where present.
          5. ``str(chapter_index)``— positional fallback. PRE-v8 behavior;
                                     drifts on list growth but kept so
                                     callers that don't pass ``chapter``
                                     (legacy + tests) work unchanged.

        Why this matters: the previous seed `f"{series_url}:{chapter_index}"`
        keyed on the absolute position in the chapter list. When the
        publisher adds a new chapter (typically prepended → newest-first),
        every existing chapter's absolute index shifts by 1, every SHA-1
        seed changes, and cache replays fetch a DIFFERENT middle page than
        the original probe — defeating the "stable across sessions" claim.
        Keying on an intrinsic identifier preserves the invariant.

        Why stratify to middle 50%: the first quarter of a chapter often
        has cover/title splashes that compress better than typical content;
        the last quarter has credits / translator notes / promo pages that
        aren't representative. The middle is where the actual story content
        lives.

        Returns None when n_pages <= 0 (no pages to pick from).

        Cross-file: consumed by _probe_chapter_aggregate (this file). The
        same function is called from T3 pairwise + paired-comparison in
        sites/search_orchestrator.py — those call sites already pass the
        chapter NUMBER (int(chap_num)) as ``chapter_index`` rather than a
        positional list index, so they already get stability-by-number;
        the ``chapter`` kwarg just adds another preference layer.
        """
        if n_pages <= 0:
            return None
        if n_pages <= 4:
            return n_pages // 2  # very short chapter — just take the middle
        import hashlib
        stable_key: "Optional[str]" = None
        if isinstance(chapter, dict):
            for field in ("url", "chap", "hid", "id"):
                val = chapter.get(field)
                if val is not None and val != "":
                    stable_key = str(val)
                    break
        if stable_key is None:
            stable_key = str(chapter_index)
        seed_input = f"{series_url}:{stable_key}".encode("utf-8")
        digest = hashlib.sha1(seed_input).hexdigest()
        seed_int = int(digest[:8], 16)
        low = n_pages // 4
        high = (3 * n_pages) // 4  # exclusive
        if high <= low:
            return n_pages // 2
        span = high - low
        return low + (seed_int % span)

    @staticmethod
    def _pick_sample_indices(n: int) -> List[int]:
        """Pick up to 5 evenly-spaced page indices from a 0..n-1 image list.

        Targets: start, start-middle, middle, middle-last, last. For short
        chapters (<5 pages) returns all available indices. Dedupes for
        small N (e.g. n=3 → [0,1,2]).

        The 5-point spread is the multi-page probe's core idea: a single-
        sample probe can't tell a healthy site from one whose CDN throttles
        after the first request. By sampling 5 across the chapter we surface
        per-page failures (treated as 0 in the aggregate) — a 1-of-5 site
        scores ~20% of its peak quality, accurately reflecting "this CDN
        can't reliably serve a chapter".
        """
        if n <= 0:
            return []
        if n >= 5:
            raw = [0, n // 4, n // 2, (3 * n) // 4, n - 1]
        else:
            raw = list(range(n))
        return sorted(set(raw))

    def _fetch_probe_item_bytes(self, item, scraper) -> Optional[bytes]:
        """Fetch image bytes for a single probe item.

        Handles both `get_chapter_images` return shapes:
          - `str` (most handlers): URL — fetched via scraper.get
          - `dict` with type=binary_image (MangaReader): pre-fetched bytes
        """
        if isinstance(item, dict):
            if item.get("type") == "binary_image":
                blob = item.get("data")
                if isinstance(blob, (bytes, bytearray)) and len(blob) >= 256:
                    return bytes(blob)
            return None
        if isinstance(item, str) and item:
            try:
                response = scraper.get(item, timeout=15)
                if response.status_code >= 400:
                    return None
                data = response.content
                if not data or len(data) < 256:
                    return None
                return data
            except Exception:
                return None
        return None

    # Throttle-probe tail constants. The throttle-probe tail re-fetches up to
    # N additional pages from the highest-scoring chapter to compute a
    # cdn_reliability ratio. This is the v5 mitigation for the lost throttle-
    # detection signal that came free with the v4 "5 pages × 1 chapter"
    # probe. With breadth sampling we now do "1 page × 8 chapters" which
    # spreads samples across the series (better statistical signal) but only
    # tests the CDN once per chapter — a CDN that throttles after the first
    # request per chapter still serves every breadth sample. The throttle-
    # probe tail catches that by sequentially fetching THROTTLE_TAIL_PAGES
    # additional pages from one chapter, mimicking single-worker download
    # behavior. Stored in metadata as cdn_reliability (succeeded / attempted);
    # NOT folded into the composite score directly so a sleeping CDN doesn't
    # demote an otherwise quality source. Rizzcomic's handler override at
    # sites/rizzcomic.py consumes this field to bottom-out the composite
    # when a CDN is poisoned (cdn_reliability == 0).
    THROTTLE_TAIL_PAGES = 3

    def _probe_chapter_aggregate(
        self, hit: "SearchHit", scraper, make_request,
        max_samples: "Optional[int]" = None,
    ) -> "Optional[tuple]":
        """Breadth-sampled chapter probe — fetches 1 page from each of 8
        chapters spread across the series, plus a throttle-probe tail.

        ``max_samples`` (v5 semantics, BREAKING from v4): when set, clamps
        the probe to that many CHAPTERS (was: that many pages within 1
        chapter). The orchestrator passes ``max_samples=2`` for low-title-
        match results on EXPENSIVE_PROBE handlers (mangafire VRF) per the
        Phase 5 quick-probe clamp. None (default) probes 8 chapters.

        Why breadth instead of depth: research (~/.claude/plans/how-robust-
        is-the-memoized-koala-agent-a42650755ce151e5a.md) showed that
        between-chapter variance (different scanners / dates / encoder
        settings) dwarfs within-chapter variance. Sampling 1 page across
        8 chapters is statistically a much better estimator of "site
        quality" than 5 pages of 1 chapter. The user's request was the
        trigger: "We take 5 images from 5 different chapters and give an
        average rating" — their description was inaccurate (we sampled
        1 chapter, not 5) but the underlying intent was right.

        Throttle-probe tail: after the 8 breadth samples, pick the chapter
        whose page scored highest, and sequentially fetch up to N additional
        pages from it (THROTTLE_TAIL_PAGES, default 3). The
        succeeded/attempted ratio becomes ``metadata["cdn_reliability"]``.
        This preserves the v4 throttle-detection signal (rizzchoros.cloud
        poisoning case from 2026-05-07) without polluting the composite —
        a sleeping CDN that revives mid-probe shouldn't crater the score
        of an otherwise high-quality source.

        Returns (aggregate_score, metadata) or None if every chapter failed
        (orchestrator falls back to cover probe). When all 8 chapters
        produce zero successful image fetches but the chapter list itself
        was readable, returns (0.0, samples=0/8 metadata) directly — same
        v4 "honest broken-CDN" semantics, just at chapter-granularity.

        Cross-file: scoring delegated to
        sites.search_orchestrator._score_image_blob via late import; the
        deterministic page-picker comes from _pick_random_middle_page_index
        (this file) so cache replays stay stable.
        """
        # Late import to avoid module-level circular dep with search_orchestrator.
        from .search_orchestrator import _score_image_blob

        if not hit or not hit.url:
            return None
        try:
            context = self.fetch_comic_context(hit.url, scraper, make_request)
        except Exception:
            return None
        if context is None:
            return None
        try:
            chapters = self.get_chapters(context, scraper, "en", make_request)
        except Exception:
            return None
        if not chapters:
            return None


        # v5 breadth sampling: pick N chapters (default 8, or max_samples
        # when caller clamps for quick-probe). _pick_representative_chapters
        # returns (absolute_index, chapter_dict) tuples; the absolute index
        # feeds _pick_random_middle_page_index's deterministic seed so cache
        # replays pick the same page.
        n_chapters = max_samples if (max_samples is not None and max_samples >= 1) else 8
        chapter_picks = self._pick_representative_chapters(chapters, n=n_chapters)
        if not chapter_picks:
            return None

        # Per-chapter sample loop. Each chapter contributes at most 1 page.
        # Failures (get_chapter_images raised, empty image list, image fetch
        # failed, unscoreable bytes) count as 0.0 — same "honest broken-CDN"
        # contract as v4 but at chapter-granularity.
        #
        # v5.1: pass 1 scores with content_type="unknown" (== bw_manga
        # weights, the v5 default). We need the per-page metadata to
        # classify the series, but classification requires aggregating
        # across all pages — chicken-and-egg. The pragmatic resolution:
        # score once with "unknown", classify from aggregated metadata,
        # then re-score iff the classification produced a non-trivial
        # content_type (re-scoring with "unknown" would be a no-op). The
        # re-score path keeps the cached blob in `per_chapter_blobs` so
        # we don't re-fetch from the CDN.
        per_chapter_scores: List[float] = []
        per_chapter_metas: List[Dict] = []
        per_chapter_image_lists: List[Optional[List]] = []  # for throttle tail
        per_chapter_picked_page_idx: List[Optional[int]] = []
        per_chapter_blobs: List[Optional[bytes]] = []  # v5.1: kept for re-score pass
        for abs_idx, chapter in chapter_picks:
            try:
                image_items = self.get_chapter_images(chapter, scraper, make_request)
            except Exception:
                image_items = None
            if not image_items:
                per_chapter_scores.append(0.0)
                per_chapter_image_lists.append(None)
                per_chapter_picked_page_idx.append(None)
                per_chapter_blobs.append(None)
                continue
            page_idx = self._pick_random_middle_page_index(
                len(image_items), hit.url, abs_idx, chapter=chapter,
            )
            if page_idx is None:
                per_chapter_scores.append(0.0)
                per_chapter_image_lists.append(image_items)
                per_chapter_picked_page_idx.append(None)
                per_chapter_blobs.append(None)
                continue
            blob = self._fetch_probe_item_bytes(image_items[page_idx], scraper)
            if not blob:
                per_chapter_scores.append(0.0)
                per_chapter_image_lists.append(image_items)
                per_chapter_picked_page_idx.append(page_idx)
                per_chapter_blobs.append(None)
                continue
            result = _score_image_blob(blob)
            if result is None:
                per_chapter_scores.append(0.0)
                per_chapter_image_lists.append(image_items)
                per_chapter_picked_page_idx.append(page_idx)
                per_chapter_blobs.append(blob)  # keep blob in case re-score works
                continue
            score, metadata = result
            per_chapter_scores.append(score)
            per_chapter_metas.append(metadata)
            per_chapter_image_lists.append(image_items)
            per_chapter_picked_page_idx.append(page_idx)
            per_chapter_blobs.append(blob)

        # v5.1: classify series content_type from successful pages' metadata.
        # The classifier reads width / height / aspect / is_grayscale /
        # chroma_var — all are fields _score_image_blob already populates.
        # When content_type != "unknown" AND differs from the default
        # bw_manga weights, re-score each blob with the classified
        # content_type so the final T1 reflects per-content-type tuning.
        series_content_type = "unknown"
        if per_chapter_metas:
            try:
                from .search_orchestrator import _classify_series_content
                feature_view = [
                    {
                        "width": m.get("width", 0),
                        "height": m.get("height", 0),
                        "aspect": (m.get("width", 0) / m["height"]) if m.get("height") else 1.0,
                        "is_grayscale_page": bool(m.get("is_grayscale", False)),
                        "chroma_var": float(m.get("chroma_var", 0.0)),
                    }
                    for m in per_chapter_metas
                ]
                series_content_type = _classify_series_content(feature_view)
            except Exception:
                series_content_type = "unknown"

            # Re-score with the classified content_type only when it would
            # change weights/targets. "unknown" maps to bw_manga defaults
            # so we skip the re-score work in that branch (a no-op).
            if series_content_type not in ("unknown", "bw_manga"):
                rescored_scores: List[float] = []
                rescored_metas: List[Dict] = []
                rescore_idx = 0
                for chapter_idx, score in enumerate(per_chapter_scores):
                    if score <= 0.0:
                        # Failure stays a failure regardless of content_type.
                        continue
                    blob = per_chapter_blobs[chapter_idx]
                    if blob is None:
                        # Defensive: success score but no blob is impossible.
                        rescored_scores.append(score)
                        rescored_metas.append(per_chapter_metas[rescore_idx])
                        rescore_idx += 1
                        continue
                    new_result = _score_image_blob(
                        blob, content_type=series_content_type,
                    )
                    if new_result is None:
                        rescored_scores.append(score)
                        rescored_metas.append(per_chapter_metas[rescore_idx])
                    else:
                        new_score, new_meta = new_result
                        rescored_scores.append(new_score)
                        rescored_metas.append(new_meta)
                    rescore_idx += 1
                # Stitch the rescored successes back into per_chapter_scores
                # while preserving alignment with per_chapter_image_lists and
                # per_chapter_picked_page_idx (which track ALL chapters
                # including failed ones).
                new_per_chapter_scores: List[float] = []
                rescored_iter = iter(rescored_scores)
                for old_score in per_chapter_scores:
                    if old_score <= 0.0:
                        new_per_chapter_scores.append(old_score)
                    else:
                        try:
                            new_per_chapter_scores.append(next(rescored_iter))
                        except StopIteration:
                            new_per_chapter_scores.append(old_score)
                per_chapter_scores = new_per_chapter_scores
                per_chapter_metas = rescored_metas

        # All chapters produced 0 — site served chapter lists but no real
        # pages. v4-equivalent canonical broken-CDN signal at chapter
        # granularity. Returning 0.0 (not None) so the orchestrator records
        # the measured failure rather than camouflaging it via cover-probe
        # fallback (rizzchoros.cloud lesson from 2026-05-07).
        if not per_chapter_metas:
            return 0.0, {
                "width": 0,
                "height": 0,
                "format": "FAILED",
                "size_bytes": 0,
                "samples_attempted": len(chapter_picks),
                "samples_succeeded": 0,
                "cdn_reliability": 0.0,
            }

        # Hybrid median/mean aggregation across chapters (was: across pages
        # within one chapter). Same rule as v4: median when all chapters
        # succeed (suppresses content variance across chapters — e.g. a
        # color splash chapter vs a B&W content chapter), mean when any
        # failed (preserves the throttle/failure signal).
        if all(s > 0.0 for s in per_chapter_scores):
            aggregate_score = statistics.median(per_chapter_scores)
        else:
            aggregate_score = sum(per_chapter_scores) / len(per_chapter_scores)

        # Throttle-probe tail: pick the highest-scoring chapter index from
        # the breadth pass and sequentially fetch THROTTLE_TAIL_PAGES more
        # pages from it. This re-introduces the v4 sequential-throttle
        # detection signal that the breadth pass loses (1 page per chapter
        # only tests the CDN's first-request behavior).
        #
        # Gate (v8): skip the entire tail when the caller clamped
        # ``max_samples`` (non-top candidates / quick-probe path in the
        # orchestrator). The tail costs THROTTLE_TAIL_PAGES extra image
        # GETs which dominates the per-source budget for clamped probes:
        # max_samples=1 was supposed to cost 1 GET; the unconditional
        # tail made it cost 4. For the orchestrator's PROBE_PHASE_DEADLINE_S
        # budget that's a 4x overrun, so clamped probes were getting
        # guillotined before completing on slow / CF-protected sites.
        # The tail's value (sequential throttle detection) needs a
        # representative breadth pass to be meaningful — there's no point
        # measuring CDN-reliability on a source where we already gave up
        # on breadth.
        cdn_reliability: Optional[float] = None
        tail_attempted = 0
        tail_succeeded = 0
        if per_chapter_scores and max_samples is None:
            best_chapter_local_idx = max(
                range(len(per_chapter_scores)),
                key=lambda i: per_chapter_scores[i],
            )
            image_items = per_chapter_image_lists[best_chapter_local_idx]
            picked_page_idx = per_chapter_picked_page_idx[best_chapter_local_idx]
            if image_items and picked_page_idx is not None:
                n_pages = len(image_items)
                # Walk forward from the already-fetched page. Dedup against
                # both the picked page AND prior candidates: on short
                # chapters (n_pages <= THROTTLE_TAIL_PAGES) the wrap puts
                # us on the same page twice, and the previous `!=` filter
                # left the duplicates in place — tail_attempted counted
                # repeats while only one distinct page was actually probed,
                # so cdn_reliability mis-reported sequential CDN behavior
                # (re-fetched URL hits the CDN cache from attempt 1 on the
                # success side, or returns the same 5xx on the failure
                # side). Order-preserving set dedup keeps the walk-forward
                # ordering while guaranteeing each page is fetched at most
                # once.
                seen_tail_pages: set = {picked_page_idx}
                candidate_pages: List[int] = []
                for i in range(self.THROTTLE_TAIL_PAGES):
                    p = (picked_page_idx + 1 + i) % n_pages
                    if p in seen_tail_pages:
                        continue
                    seen_tail_pages.add(p)
                    candidate_pages.append(p)
                for p_idx in candidate_pages:
                    tail_attempted += 1
                    blob = self._fetch_probe_item_bytes(image_items[p_idx], scraper)
                    if blob:
                        tail_succeeded += 1
                if tail_attempted > 0:
                    cdn_reliability = tail_succeeded / tail_attempted

        # Metadata aggregation: mean across SUCCESSFUL samples for numeric
        # fields; majority vote for booleans; most-common for categorical.
        # The per-sample metadata schema is now the v5 _compute_t1_score
        # output (see sites/search_orchestrator.py:_compute_t1_score) with
        # many more fields than v4's 4-field schema. We aggregate every
        # numeric field we recognize; unknown fields pass through from the
        # first sample only (forward-compat — new component additions in
        # _compute_t1_score don't require updates here).
        from collections import Counter

        def _mean_field(field: str) -> Optional[float]:
            vals = [m.get(field) for m in per_chapter_metas]
            vals = [float(v) for v in vals if isinstance(v, (int, float))]
            if not vals:
                return None
            return round(sum(vals) / len(vals), 4)

        avg_w = sum(int(m.get("width", 0) or 0) for m in per_chapter_metas) // len(per_chapter_metas)
        avg_h = sum(int(m.get("height", 0) or 0) for m in per_chapter_metas) // len(per_chapter_metas)
        avg_size = sum(int(m.get("size_bytes", 0) or 0) for m in per_chapter_metas) // len(per_chapter_metas)
        fmts = [m.get("format", "UNKNOWN") for m in per_chapter_metas]
        most_common_fmt = Counter(fmts).most_common(1)[0][0] if fmts else "UNKNOWN"

        gs_count = sum(1 for m in per_chapter_metas if m.get("is_grayscale"))
        lossless_count = sum(1 for m in per_chapter_metas if m.get("is_lossless"))
        # v5.1: outlier aggregation uses majority vote (≥half of probed
        # pages must share the same outlier type for the aggregate to
        # inherit it). The v5 "first-found wins" rule was set when there
        # was only one source-level outlier type (webp_below_floor). v5.1
        # added per-page outliers (low_chroma, fake_sharpened,
        # heavy_watermark) that legitimately VARY across pages — e.g.
        # linewebtoon serves mostly PNG but a stray JPEG proxy thumbnail
        # would mark the whole source as low_chroma under first-found.
        # Majority vote also preserves the rizzchoros throttle_detected
        # signal (which fires uniformly across all pages when the CDN is
        # broken).
        outlier_counts = Counter(
            m.get("outlier") for m in per_chapter_metas if m.get("outlier")
        )
        majority_threshold = max(1, (len(per_chapter_metas) + 1) // 2)
        majority_outlier = next(
            (name for name, cnt in outlier_counts.most_common()
             if cnt >= majority_threshold),
            None,
        )

        aggregate_metadata: Dict[str, Any] = {
            "width": int(avg_w),
            "height": int(avg_h),
            "format": most_common_fmt,
            "size_bytes": int(avg_size),
            "samples_attempted": len(chapter_picks),
            "samples_succeeded": len(per_chapter_metas),
            # Numeric T1 components (mean across successful samples).
            "bpp": _mean_field("bpp"),
            "decode_quality": _mean_field("decode_quality"),
            "res_norm": _mean_field("res_norm"),
            "blockiness": _mean_field("blockiness"),
            "fft_hf_ratio": _mean_field("fft_hf_ratio"),
            "tenengrad": _mean_field("tenengrad"),
            "tenengrad_norm": _mean_field("tenengrad_norm"),
            # v5.1 USM-damped Tenengrad — REQUIRED for the v6 cache-load
            # gate (sites/search_orchestrator.py:REQUIRED_V6_FIELDS). Without
            # this aggregate-level entry, every chapter-probe cache write
            # got DROPPED on next-session load because the gate's
            # `all(f in meta for f in REQUIRED_V6_FIELDS)` check failed
            # → 30-day TTL had zero effect on the dominant probe path,
            # every search re-probed from scratch. Grep target:
            # tenengrad_clean. Per-page metadata writes the field at
            # search_orchestrator.py:_compute_t1_score / _compute_t1_score_bw.
            "tenengrad_clean": _mean_field("tenengrad_clean"),
            "jpeg_qf": _mean_field("jpeg_qf"),
            "jpeg_qf_norm": _mean_field("jpeg_qf_norm"),
            "jpeg_nse": _mean_field("jpeg_nse"),
            "t1_score": _mean_field("t1_score"),
            # Majority vote for content-type classifiers. Edge case: 1 color
            # splash + 7 B&W → reports B&W (correct — dominant content type).
            "is_grayscale": gs_count >= max(1, len(per_chapter_metas) // 2 + 1),
            "is_lossless": lossless_count >= max(1, len(per_chapter_metas) // 2 + 1),
            # v5.1: majority-vote outlier (≥half of probed pages share it).
            # See `majority_outlier` derivation above for rationale — first-
            # found was too noisy for per-page outliers added in v5.1.
            "outlier": majority_outlier,
            # Throttle-probe tail result — drives the rizzcomic override
            # short-circuit at sites/rizzcomic.py. None when the tail
            # couldn't run (e.g. only 1 chapter probed total).
            "cdn_reliability": cdn_reliability,
            # Provenance for debugging / cache audit. The picked chapter
            # indices let calibration replay deterministically what was
            # measured.
            "chapter_indices_sampled": [abs_idx for abs_idx, _ in chapter_picks],
            # v5.1 (Phase 4): series-level content_type from
            # _classify_series_content (search_orchestrator.py). The string
            # drives per-content-type T1 weights + res_norm targets in
            # _compute_t1_score; the rescored per-page metadata in
            # per_chapter_metas already reflects the classification.
            "content_type": series_content_type,
            # Mean chroma variance across successful pages — useful for the
            # classifier's color/B&W discrimination and for the UI tooltip.
            "chroma_var": _mean_field("chroma_var"),
        }
        # T2/T3 placeholders propagate from per-sample metadata (every sample
        # carries these; we surface the t2_available bit and any populated
        # T2 fields as means). Phase 3 will populate t2_score / clip_iqa /
        # niqe; Phase 4 populates paired_quality_adjustment.
        aggregate_metadata.setdefault(
            "t2_available",
            any(m.get("t2_available") for m in per_chapter_metas),
        )
        for f in ("t2_score", "clip_iqa_mean", "niqe_score", "niqe_norm"):
            aggregate_metadata.setdefault(f, _mean_field(f))
        # clip_iqa_scores is a per-prompt dict; preserve from first sample
        # that has it populated (all should agree per Phase 3 design).
        for m in per_chapter_metas:
            if m.get("clip_iqa_scores"):
                aggregate_metadata.setdefault("clip_iqa_scores", m["clip_iqa_scores"])
                break
        else:
            aggregate_metadata.setdefault("clip_iqa_scores", None)
        # T3 fields default null (populated post-probe by orchestrator).
        aggregate_metadata.setdefault("paired_quality_adjustment", None)
        aggregate_metadata.setdefault("paired_anchor_site", None)
        aggregate_metadata.setdefault("paired_dists_median", None)
        aggregate_metadata.setdefault("paired_pairs_compared", 0)

        return aggregate_score, aggregate_metadata

    @staticmethod
    def _pick_middle_image_item(image_items: List):
        """Pick the middle item from a chapter's image list.

        Index 0 is often a colored splash (compresses well, biases the
        score high); the last index can be a credits/ad/team-promo page
        that isn't representative either. Items can be URL strings or
        pre-fetched binary dicts; the caller (_probe_chapter_image)
        dispatches on type.
        """
        if not image_items:
            return None
        if len(image_items) <= 2:
            return image_items[0]
        return image_items[len(image_items) // 2]

    def _probe_cover_image(
        self, hit: "SearchHit", scraper, make_request
    ) -> Optional[bytes]:
        """Cover-image fallback when chapter probe fails.

        Faster than chapter probe (1 HTTP request vs 3-4) but biased
        per-site because covers and chapter pages have different
        compression policies on most aggregator CDNs. Override when the
        cover URL needs site-specific cleanup before fetching (see
        MangaFire's _probe_cover_image which strips the @<digits>
        thumbnail token).
        """
        if not hit or not getattr(hit, "cover", None):
            return None
        cover_url = hit.cover
        if isinstance(cover_url, str) and cover_url.startswith("localfile://"):
            try:
                parsed = urlparse(cover_url)
                path = unquote(parsed.path or "")
                if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
                    path = path[1:]
                with open(path, "rb") as f:
                    data = f.read()
                if not data or len(data) < 256:
                    return None
                return data
            except Exception:
                return None
        try:
            response = scraper.get(cover_url, timeout=10)
            if response.status_code >= 400:
                return None
            data = response.content
            if not data or len(data) < 256:
                return None
            return data
        except Exception:
            return None


__all__ = [
    "BaseSiteHandler",
    "SiteComicContext",
    "SearchHit",
]
