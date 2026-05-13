from __future__ import annotations

import os
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

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
        record_host_failure: Optional[Callable[[str, str], None]] = None,
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
                          watchdog can poison-detect a flaky CDN.
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
                    except Exception:
                        if attempt < 1:
                            await asyncio.sleep(1.0)
                            continue
                        if record_host_failure is not None:
                            try:
                                record_host_failure(host, url)
                            except Exception:
                                pass
                        return page_idx, None
                if r.status_code != 200 or not r.content or len(r.content) < 256:
                    if attempt < 1:
                        await asyncio.sleep(1.0)
                        continue
                    if record_host_failure is not None:
                        try:
                            record_host_failure(host, url)
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

    def get_group_name(self, chapter_version: Dict) -> Optional[str]:
        return None

    def select_best_chapter_version(
        self,
        versions: List[Dict],
        preferred_groups: List[str],
        mix_by_upvote: bool,
        log_debug_fn=None,
    ) -> Optional[Dict]:
        if not versions:
            return None

        def upvotes(v: Dict) -> int:
            return v.get("up_count", 0)

        def _debug(msg):
            if log_debug_fn:
                log_debug_fn(msg)

        chap_label = versions[0].get("chap", "?")
        best_by_upvote = max(versions, key=upvotes)

        if not preferred_groups:
            _debug(
                f"    Ch {chap_label}: No group specified. Selected by upvotes ({best_by_upvote.get('up_count', 0)})."
            )
            return best_by_upvote

        if mix_by_upvote:
            preferred = [
                v for v in versions if self.get_group_name(v) in preferred_groups
            ]
            if preferred:
                best = max(preferred, key=upvotes)
                _debug(
                    f"    Ch {chap_label}: Mix-by-upvote. Selected '{self.get_group_name(best)}' ({best.get('up_count', 0)} upvotes)."
                )
                return best
            _debug(
                f"    Ch {chap_label}: Mix-by-upvote. No preferred groups found. Fallback to upvotes."
            )
            return best_by_upvote

        for group_name in preferred_groups:
            candidates = [
                v for v in versions if self.get_group_name(v) == group_name
            ]
            if candidates:
                best = max(candidates, key=upvotes)
                _debug(
                    f"    Ch {chap_label}: Found in priority group '{group_name}'. Selected."
                )
                return best
        _debug(
            f"    Ch {chap_label}: No priority groups found. Fallback to upvotes."
        )
        return best_by_upvote

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
        """Pick a chapter from the middle of the list, preferring whole
        numbers.

        Why prefer whole-numbered chapters: partial chapters (e.g., 4.5,
        60.1) tend to be omake/extras/special-chapters with atypical page
        counts (sometimes just a 1-page splash). Probing those gives a
        misleading signal about the site's quality. Falls back to any
        chapter if no whole-numbered ones exist (single-chapter series,
        oneshots, or weirdly-numbered series).

        Chapter lists arrive in different orders per handler (MangaDex uses
        ASC, MangaFire/most HTML scrapers use DESC) but the middle is
        representative regardless.
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

    def _probe_chapter_aggregate(
        self, hit: "SearchHit", scraper, make_request,
        max_samples: "Optional[int]" = None,
    ) -> "Optional[tuple]":
        """Multi-page chapter probe — fetches 5 sample pages from a
        representative chapter and aggregates their scores into a single
        quality measure that captures BOTH pixel quality AND CDN reliability
        under sequential fetch.

        ``max_samples``: when set, clamps the probe to that many images.
        The orchestrator passes ``max_samples=1`` for low-title-match
        results on EXPENSIVE_PROBE handlers (mangafire VRF) — see
        sites/search_orchestrator.py:EXPENSIVE_PROBE_QUICK_THRESHOLD for
        rationale. None (default) keeps the historical 5-sample behavior.
        Picks the middle index when clamping to 1 (matches
        _probe_cover_image's middle-page-bias rationale: page 0 is often a
        cover splash, last page is often credits/promo).

        Why this exists: the prior single-page probe couldn't detect CDN
        throttling. A site that returns 1 image fine then throttles on the
        next 4 looks identical to a healthy site if we only sample 1. By
        sampling 5 sequentially, we surface that throttling: failed fetches
        count as 0 in the aggregate, so a 1-of-5 site scores ~20% of full
        quality. Real-world driver: rizzcomic / rizzchoros.cloud during
        2026-05-07 — the single-image probe scored it 0.856, but the
        download couldn't fetch any pages because the CDN was poisoned.

        Sequential (not parallel) is intentional: a parallel burst could
        succeed on a CDN that would throttle a sequential 5-page sequence.
        Sequential mimics a single-worker download and detects throttling.

        Returns (aggregate_score, metadata) or None if every sample failed
        (orchestrator then falls back to cover probe). Metadata fields:
          width, height, format, size_bytes — averaged across SUCCESSFUL
            samples (best signal for "what does a typical page look like")
          samples_attempted, samples_succeeded — provenance

        Cross-file: scoring delegated to sites.search_orchestrator._score_image_blob
        via late import (avoids circular import; resolved at call time when
        the module is fully loaded).
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
        chapter = self._pick_representative_chapter(chapters)
        if chapter is None:
            return None
        try:
            image_items = self.get_chapter_images(chapter, scraper, make_request)
        except Exception:
            return None
        if not image_items:
            return None

        indices = self._pick_sample_indices(len(image_items))
        if not indices:
            return None

        # Quick-probe clamp: when caller asks for fewer samples than the
        # default 5, take the middle index (best-representative page,
        # avoids cover-splash / credits-page biases). For max_samples > 1
        # we'd evenly subsample, but the orchestrator only ever passes 1.
        if max_samples is not None and max_samples >= 1 and len(indices) > max_samples:
            if max_samples == 1:
                indices = [indices[len(indices) // 2]]
            else:
                step = len(indices) / max_samples
                indices = [
                    indices[min(int(i * step), len(indices) - 1)]
                    for i in range(max_samples)
                ]

        scores: List[float] = []
        metas: List[Dict] = []
        for idx in indices:
            blob = self._fetch_probe_item_bytes(image_items[idx], scraper)
            if not blob:
                # Failure counts as 0 in the aggregate — that's the throttle-
                # detection signal. Don't bail early; we want full N/5 ratio.
                scores.append(0.0)
                continue
            result = _score_image_blob(blob)
            if result is None:
                # Unscoreable bytes (truncated, corrupt, placeholder) also
                # count as 0 — site served bytes but they're not a real page.
                scores.append(0.0)
                continue
            score, metadata = result
            scores.append(score)
            metas.append(metadata)

        if not metas:
            # Every slot returned 0 — site served URLs but every fetch failed
            # (CDN throttle / poisoned host / placeholder bytes). This is the
            # canonical "rizzchoros.cloud poisoned" signal. We record 0.0
            # directly instead of returning None, because returning None would
            # camouflage the failure via cover fallback (cover comes from a
            # different CDN and would score normally, hiding the broken-CDN
            # truth). 0.0 with samples=0/5 metadata is the honest measurement.
            return 0.0, {
                "width": 0,
                "height": 0,
                "format": "FAILED",
                "size_bytes": 0,
                "samples_attempted": len(indices),
                "samples_succeeded": 0,
            }

        # Phase H1 (2026-05-08): hybrid median/mean. Median when all probes
        # succeed (suppresses per-image content variance — a color splash
        # vs a B&W panel can vary 5-10× in bpp at the same encoder quality;
        # mean would punish a source for sampling luck). Fall back to mean
        # when any probe failed so the throttle-detection signal survives:
        # 5/5 success at 0.85 = 0.85 (either way).
        # 3/5 success at 0.85 = 0.51 mean (median would give 0.85 and
        #   silently hide CDN unreliability — that's why we don't median
        #   here).
        # 1/5 = 0.17 mean.
        # The orchestrator's comparator then ranks by this aggregate.
        if all(s > 0.0 for s in scores):
            aggregate_score = statistics.median(scores)
        else:
            aggregate_score = sum(scores) / len(indices)

        # Metadata averaged across SUCCESSFUL samples — failures don't have
        # dimensions, so they're skipped here. Width/height as ints to match
        # the existing single-page schema.
        avg_w = sum(m.get("width", 0) for m in metas) // len(metas)
        avg_h = sum(m.get("height", 0) for m in metas) // len(metas)
        from collections import Counter
        fmts = [m.get("format", "UNKNOWN") for m in metas]
        most_common_fmt = Counter(fmts).most_common(1)[0][0] if fmts else "UNKNOWN"
        avg_size = sum(m.get("size_bytes", 0) for m in metas) // len(metas)

        # Phase H aggregate (2026-05-08): roll bpp / is_grayscale / outlier /
        # decode_quality from the per-image metadata into the per-source
        # aggregate so the JSON output (and UI tooltip) can describe WHY a
        # source scored what it did, not just the bare composite score.
        # See sites/search_orchestrator.py:_score_image_blob for field origins.
        bpps = [m["bpp"] for m in metas if m.get("bpp") is not None]
        decode_qs = [m["decode_quality"] for m in metas if m.get("decode_quality") is not None]
        gs_count = sum(1 for m in metas if m.get("is_grayscale"))
        outliers = [m.get("outlier") for m in metas if m.get("outlier")]

        aggregate_metadata = {
            "width": avg_w,
            "height": avg_h,
            "format": most_common_fmt,
            "size_bytes": avg_size,
            "samples_attempted": len(indices),
            "samples_succeeded": len(metas),
            # Mean bpp / decode_quality across samples — a single image can be
            # atypical (color splash on an otherwise B&W chapter) but the mean
            # captures the source's ambient encoder quality.
            "bpp": round(sum(bpps) / len(bpps), 4) if bpps else None,
            "decode_quality": round(sum(decode_qs) / len(decode_qs), 4) if decode_qs else None,
            # Majority vote: the source's CONTENT TYPE for the probed chapter.
            # Most chapters are entirely B&W or entirely color, so >=ceil(n/2)
            # is enough. Edge case: a chapter with one color splash + 4 B&W
            # pages reports B&W (correct — that's the dominant content type).
            "is_grayscale": gs_count >= max(1, len(metas) // 2 + 1),
            # Surface the FIRST non-null outlier flag — currently the only flag
            # is "webp_below_floor", but the field is forward-compatible. If
            # the encoder is broken on one page it's almost certainly broken
            # on all of them, so first-found is sufficient.
            "outlier": outliers[0] if outliers else None,
        }
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
        try:
            response = scraper.get(hit.cover, timeout=10)
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
