from __future__ import annotations

import atexit
import builtins as _builtins
import concurrent.futures as _futures
import json
import os
import queue
import re
import sys
import threading
import time
from typing import Dict, List, Optional, Any, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BaseSiteHandler, GroupInfo, SearchHit, SiteComicContext

# Optional zendriver-backed Cloudflare fallback. comix.to added CF
# protection in upstream's 2026-05 release; direct-HTTP API calls (the
# v1/v2 manga + chapter-list endpoints we hit through the regular
# `scraper` session, NOT the Patchright-routed token capture or
# chapter-detail steal) can drop 403/503 challenge pages. `_cf_aware_request`
# wraps those calls and falls back through a one-shot zendriver session on
# confirmed CF challenges. Soft-import so non-zendriver installs still load
# the module — the wrapper degrades to a straight passthrough.
# Cross-file: sites/crawlee_utils.py:get_cf_session / is_cf_challenge.
try:
    from .crawlee_utils import get_cf_session, is_cf_challenge
    _CF_AVAILABLE = True
except ImportError:
    _CF_AVAILABLE = False


# All bare print() calls in this module emit to stderr by default. Why: this
# handler's Patchright bridge logs [!] diagnostic messages when chapter-API
# capture fails, and when invoked from the orchestrator's search-time probe
# path (sites/search_orchestrator.py:_probe_one) those lines would land on
# stdout — which carries the JSON --search output for piped consumers. The
# UI's searcher.js rejects non-JSON stdout with "Search produced non-JSON
# stdout" so any leak hard-breaks the search results panel. This shim keeps
# stdout clean without touching every print site. Explicit file= overrides
# still work (e.g., pass file=sys.stdout to opt out). Same idiom as
# sites/mangafire.py:_stderr_print.
def _stderr_print(*args, **kwargs):
    kwargs.setdefault("file", sys.stderr)
    return _builtins.print(*args, **kwargs)


print = _stderr_print  # noqa: A001 — intentional shadow of builtins.print


# Probe-time page-capture cap. The image-quality probe
# (ComixSiteHandler._probe_chapter_aggregate) renders only this many pages of
# its one sampled chapter (chapter 1) instead of the whole ~70-page chapter,
# keeping a single browser render at ~8-20s so it fits the orchestrator's 240s
# probe deadline. The probe scores the LATTER half of these pages (skipping the
# cover/splash-prone opening) and medians them — the first live run mis-scored
# the flagship series at 0.1 on a sparse page-3, hence "latter half" + median
# instead of one early page. The download path (get_chapter_images) passes no
# cap → all pages.
_COMIX_PROBE_PAGE_CAP = 8


# ---------------------------------------------------------------------------
# comix's FIRST-PARTY WAF interstitial (2026-08-02)
# ---------------------------------------------------------------------------
# comix.to runs TWO independent bot layers and they need different handling:
#
#   1. Cloudflare — /cdn-cgi/challenge-platform/…, handled by is_cf_challenge +
#      the zendriver cookie capture in sites/crawlee_utils.py. Machine-solvable.
#   2. comix's OWN WAF — a 302 to /@waf/challenge?return=<original-path> serving
#      an INTERACTIVE CAPTCHA ("Verify you're human — drag to rotate the circle
#      until the picture lines up"). A human has to drag it. Verified 2026-08-02:
#      the string "@waf" appears in ZERO client bundles (main/vendor/secure/env,
#      727 KB scanned), so it is entirely server-issued — there is no in-page JS
#      routine to invoke and nothing to replicate. We never attempt to solve it;
#      we detect it, keep a solved session alive so it is rarely hit, and hand
#      the browser to the user when it does fire.
#
# Before this existed the WAF page was INVISIBLE to the handler and surfaced as
# three different lies: "chapter had 0 .rpage-page divs", "no typeahead results",
# and — worst — a silently truncated chapter list (see fetch_chapters_via_dom).
# On the HTTP path it 200s with challenge HTML, so _extract_initial_data_manga
# returned None and fetch_comic_context fell through to its slug-derived title
# fallback, writing junk metadata. Grep _looks_like_waf_challenge for the wiring.
_WAF_PATH_MARKER = "/@waf/"
# Quoted in the user-facing remediation message. The site's own homepage is the
# right destination: hitting /@waf/challenge directly works but reads like an
# error page, and the challenge is issued from whatever route you land on.
_WAF_CHALLENGE_HINT_URL = "https://comix.to/"
_WAF_TEXT_MARKERS = (
    "verify you're human",
    "verify you&#039;re human",
    "drag to rotate",
)
# Debug seam: forces the detector to fire once so the headed-handoff branch can
# be exercised without waiting for the site to actually challenge us (it is
# behavioral, so it can't be summoned on demand). Consumed — not just read — so
# a single run tests exactly one challenge.
_FORCE_WAF_ENV = "AIO_COMIX_FORCE_WAF"

# Interactive-handoff knobs. The handoff opens a VISIBLE browser window on the
# downloader's own profile and waits for the user to complete the check; it
# never touches the widget. Set AIO_COMIX_NO_INTERACTIVE_WAF=1 for unattended
# runs (cron, CI, headless servers) so a challenge fails fast instead of
# blocking on a window nobody will see.
_WAF_NO_INTERACTIVE_ENV = "AIO_COMIX_NO_INTERACTIVE_WAF"
_WAF_SOLVE_TIMEOUT_ENV = "AIO_COMIX_WAF_SOLVE_TIMEOUT"
_WAF_DEFAULT_SOLVE_TIMEOUT_S = 180.0
# One handoff per process. Without this a series whose every chapter trips the
# WAF would pop a window per chapter; after the first failure we want the run to
# end with a clear error, not to keep interrupting.
_COMIX_WAF_HANDOFF_ATTEMPTED = False
_COMIX_WAF_HANDOFF_LOCK = threading.Lock()

# Sanity bound on the pager-reported page count. Real worst case observed is
# 360 (Magic Emperor, ~890 chapters x ~8 groups at 20 rows/page); this only
# exists so a malformed pager can't turn into an unbounded walk, and it doubles
# as the outer-timeout basis in _ComixBrowserBridge.fetch_chapters_via_dom.
_MAX_CHAPTER_SCRAPE_PAGES = 800


def _looks_like_waf_challenge(url: Optional[str], text: Optional[str] = None) -> bool:
    """True when comix's first-party interactive CAPTCHA is in front of us.

    Deliberately NOT merged with sites/crawlee_utils.py:is_cf_challenge — comix
    runs Cloudflare AND this, only this one needs a human, and conflating them
    would send a solvable CF challenge down the "ask the user" path.

    ``url`` is the authoritative signal (the landed/final URL contains
    ``/@waf/``). ``text`` is the fallback for the HTTP path, where the redirect
    may already have been followed and some callers only hold the body. The
    generic word "challenge" is deliberately NOT a marker — it appears in
    ordinary comic synopses.
    """
    if os.environ.pop(_FORCE_WAF_ENV, None):
        return True
    if url and _WAF_PATH_MARKER in url:
        return True
    if text:
        lowered = text.lower()
        # Bound the scan: a challenge page is ~160 KB of mostly inline script,
        # but the markers are visible copy near the top. A full-body lower() on
        # every chapter page would be wasted work on the hot path.
        if len(lowered) > 200_000:
            lowered = lowered[:200_000]
        for marker in _WAF_TEXT_MARKERS:
            if marker in lowered:
                return True
        # "security check" is the <title>; on its own it is too generic to
        # trust, so require it to co-occur with the interstitial's noindex meta.
        if "security check" in lowered and "noindex" in lowered:
            return True
    return False


class ComixWafChallengeError(RuntimeError):
    """comix served its interactive human-verification CAPTCHA and we could not
    get past it (no handoff possible, the user didn't finish in time, or the
    retry landed on it again).

    Raised — never swallowed into a partial result — because every alternative
    is worse: a truncated chapter list gets persisted to .aio_series.json as if
    it were the whole series, and a half-scraped chapter yields a short CBZ.
    aio-dl.py catches it at the top level and prints the remediation.
    """

    def __init__(self, message: str, challenge_url: Optional[str] = None):
        super().__init__(message)
        self.challenge_url = challenge_url


class ComixChapterScrapeError(RuntimeError):
    """The chapter-list scrape could not be completed to the end of pagination.

    Exists so a truncated list can NEVER be mistaken for a short series. The
    2026-08-02 bug this closes: `if not rows: break` treated an empty render as
    end-of-list, so a 890-chapter series scraped 4 of its 360 pager pages and
    the download started at chapter 871.
    """


def _comix_profile_dir() -> str:
    """Absolute path to the app-owned Chromium user-data dir for comix.

    WHY a persistent profile: the old code launched a virgin headless context in
    every process, which is the most bot-like possible fingerprint and is the
    main reason the WAF fires "sometimes". Persisting the whole PROFILE (rather
    than picking cookies out of it) means we never hardcode a cookie name the
    site can rename — cookies, localStorage, and the reader `preload=all`
    setting all survive across runs, and one human solve covers days.

    Deliberately app-owned and NOT the user's real Chrome profile (considered
    and rejected 2026-08-02): Chromium locks a user-data dir, so pointing at the
    real one would fail whenever Chrome is open, and it would mean the
    downloader reads the user's actual browsing data.

    Override with AIO_COMIX_PROFILE_DIR. Delete the directory to force a fresh
    session (the next challenge then needs another manual solve).
    """
    override = (os.environ.get("AIO_COMIX_PROFILE_DIR") or "").strip()
    if override:
        return os.path.abspath(override)
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        root = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        root = os.environ.get("XDG_CACHE_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache"
        )
    return os.path.join(root, "AIO-Webtoon-Downloader", "comix-profile")


def _extract_group_id(url: Optional[str]) -> Optional[str]:
    """Return the ``group_id`` query value from a comix title URL, or None.

    The user pasting /title/<hid>-<slug>?group_id=4856 is asking for ONE
    scanlation group, and comix honors that filter server-side. Honoring it is
    worth a lot: on Magic Emperor it collapses the chapter list from 360 pager
    pages (rows are per chapter × group) to 59 with one row per chapter.

    Cross-file: consumed in get_chapters; captured in fetch_comic_context
    because that method overwrites comic["url"] with the canonical
    query-less URL from the #initial-data blob, so the caller's URL is the only
    place the filter survives. Grep _group_id.
    """
    if not url:
        return None
    try:
        query = urlparse(url).query
    except Exception:
        return None
    if not query:
        return None
    values = parse_qs(query).get("group_id") or []
    for value in values:
        value = (value or "").strip()
        # Digits only — the value is interpolated back into a URL we navigate.
        if value and value.isdigit():
            return value
    return None


def _coerce_chapter_number(
    href: Optional[str], label: Optional[str] = None
) -> Optional[float]:
    """Best-effort numeric chapter number for a scraped row, or None.

    Only used by the early-stop check in fetch_chapters_via_dom, so a None
    (an unparseable special/oneshot) simply doesn't vote on whether to stop —
    it never drops the row itself. Prefers the href because
    `/title/{slug}/{id}-chapter-{n}` is machine-generated, and falls back to
    the "Ch.<n>" display label.
    """
    if href:
        m = re.search(r"-chapter-(\d+(?:\.\d+)?)", href)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    if label:
        m = re.search(r"(\d+(?:\.\d+)?)", label)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


class ComixSiteHandler(BaseSiteHandler):
    name = "comix"
    domains = ("comix.to", "www.comix.to")

    # comix is a FULL search + --multi-source + image-quality-probe participant
    # (2026-07-12). The old SKIP_QUALITY_PROBE / SKIP_MULTI_SOURCE opt-outs are
    # gone: keyword search runs through the header-typeahead DOM scrape (see
    # `search` / `fetch_search_via_dom`), and the image probe is a custom
    # SINGLE-chapter override (`_probe_chapter_aggregate` below) that renders
    # just one chapter (chapter 1 by preference) with a capped page count so the
    # single-threaded browser bridge fits the orchestrator's 240 s probe
    # deadline. The calibrated 0.74 seed in sites/quality_seed.json is now only
    # the probe's FALLBACK (when the probe returns None), not the ranking signal.
    # Both the SKIP_QUALITY_PROBE (search_orchestrator._probe_one loop) and
    # SKIP_MULTI_SOURCE (aio_search_cli._filter_and_rank_alt_sources + the
    # prefetched-alts path) getattr hooks still exist as generic opt-outs;
    # comix just no longer sets them.

    # Fan-out scheduling (2026-07-12): comix's search() is the only
    # browser-driven one — cold Patchright launch + typeahead render, 28s
    # inner budget — so the orchestrator enqueues it FIRST (slow-first
    # stable sort) to overlap the cheap HTTP handlers instead of adding its
    # full duration in a late wave. Grep SEARCH_COST_HINT in sites/base.py
    # (contract) + search_orchestrator.py (the sort).
    SEARCH_COST_HINT = "slow"

    # The probe override below IGNORES the orchestrator's max_samples clamp
    # (it hard-caps to ONE chapter regardless), so its result is always this
    # probe's full form. The orchestrator's clamped-probe cache (2026-07-12)
    # keys off this flag: comix results are written UNCLAMPED (full 30-day
    # TTL, servable in both top-candidate and non-top roles) — which is what
    # retires the old behavior of re-running the 15-70s browser probe on
    # EVERY search whenever comix wasn't the top candidate. Grep
    # PROBE_SAMPLES_FIXED in search_orchestrator.py (_desired_max_samples
    # serve rule + _probe_one write rule).
    PROBE_SAMPLES_FIXED = True

    # Patchright's sync API requires that every call run on the same thread
    # that started the browser, AND that thread must own an asyncio loop.
    # Probe-phase workers (sites/search_orchestrator.py) and aio-dl.py's
    # image-prefetch threads can't satisfy either. So all Patchright work
    # (the chapter-list + chapter-image DOM scrapes — the site is a signed +
    # encrypted SPA, see fetch_comic_context) routes through
    # _COMIX_BROWSER_BRIDGE (bottom of this file), which serializes calls onto
    # a single dedicated worker thread, one process-wide. Block-on-future
    # semantics make the bridge fully synchronous from any caller's
    # perspective. Cross-file idiom: sites/mangadex.py:_report_worker.

    def __init__(self):
        # BaseSiteHandler has no __init__; super().__init__() falls through to
        # object.__init__ (no-args). We override only to attach the per-instance
        # lazy CF session + the chapter-image memo cache below.
        super().__init__()
        # Lazy-init zendriver CF session. Built on first 403/503 in
        # _cf_aware_request when is_cf_challenge confirms the body is a CF
        # interstitial, then reused for subsequent direct-HTTP calls within
        # the same handler instance. Patchright-routed calls (token capture,
        # chapter-detail steal) don't need this — the browser handles CF
        # natively via its own cookie store.
        self._cf_session = None
        # Memoize get_chapter_images results so the prefetch worker and
        # the main download flow don't both run the ~20s canvas scrape
        # per chapter. Both threads share THIS handler instance (the
        # prefetch job carries `handler` by reference). Comix's
        # Patchright bridge serializes every scrape through one daemon
        # worker, so a duplicate main-flow scrape gets queued behind
        # every other in-flight prefetch — turning the supposed-to-be-
        # instant prefetch_hit path into a wait worth several chapters'
        # scrape time (~14-20s each). Keyed by chapter URL because
        # merged-part chapters share an id but have distinct part URLs.
        # 600s TTL matches sites/image_cache._TTL_SECONDS so cached
        # URLs and the cached bytes behind them expire together — a
        # URL whose bytes have been evicted points at a CDN signed
        # token that's almost certainly rotated by then, so we want a
        # fresh scrape rather than serving stale URLs that would
        # 404-on-fetch.
        self._chapter_images_cache: Dict[str, Tuple[List[str], float]] = {}
        self._chapter_images_cache_lock = threading.Lock()

    def configure_session(self, scraper, args) -> None:
        scraper.headers.update({
            "Referer": "https://comix.to/",
            "Origin": "https://comix.to",
        })

    def _get_cf_session(self):
        """Lazy-build a zendriver-backed requests.Session pre-loaded with
        valid CF cookies for comix.to. Returns the cached session on
        subsequent calls; returns None when crawlee_utils isn't importable
        OR the zendriver solve fails (caller treats None as "no fallback
        available" and surfaces the original 403/503).

        Cross-file: sites/crawlee_utils.py:get_cf_session handles the
        zendriver lifecycle + per-domain cookie cache (_CF_COOKIE_TTL).
        """
        if self._cf_session is None and _CF_AVAILABLE:
            try:
                self._cf_session = get_cf_session("https://comix.to")
                self._cf_session.headers.update({
                    "Referer": "https://comix.to/",
                    "Origin": "https://comix.to",
                })
            except Exception as e:
                # Failure modes: zendriver missing, Chrome not installed,
                # CF solve timeout, network blip. Log to stderr (via the
                # _stderr_print shim at module top so we don't corrupt
                # --search JSON on stdout) and fall through — the caller
                # keeps the original response.
                print(f"[!] Comix CF session failed: {e}")
        return self._cf_session

    def _cf_aware_request(self, url: str, scraper, make_request):
        """Wraps make_request with a one-shot zendriver CF fallback.

        Behavior: makes the normal request; on a 403/503 that
        is_cf_challenge confirms IS a CF interstitial (not a legitimate
        403/503 from the API itself, where we want the real status to
        propagate to the caller's error handling), retries through the
        lazy CF session. Any exception in the retry path silently keeps
        the original response so we never make CF resilience itself the
        cause of a hard failure.

        Used only for the direct-HTTP paths (fetch_comic_context,
        get_chapters listing, get_chapter_images HTML fallback). The
        Patchright-routed token capture and chapter-detail steal handle
        CF transparently and don't go through this wrapper.

        Cross-file: same idiom as upstream comix.py's _cf_aware_request;
        ported here on top of the local persistent-browser bridge.

        Also the HTTP-side chokepoint for comix's FIRST-PARTY WAF interstitial
        (see _looks_like_waf_challenge). That one 200s with challenge HTML, so
        without this check fetch_comic_context would parse it, find no
        #initial-data, and silently fall through to its slug-derived-title
        fallback — writing a junk title/author/cover into the library instead of
        failing. Grep _waf_recover_once for the handoff.
        """
        response = make_request(url, scraper)
        response = self._waf_recover_once(response, url, scraper, make_request)
        if _CF_AVAILABLE and response.status_code in (403, 503):
            try:
                if is_cf_challenge(response.status_code, response.text):
                    cf = self._get_cf_session()
                    if cf:
                        # Push the freshly-captured CF cookies into the
                        # caller's scraper so subsequent make_request calls
                        # — chapter API HTML fallback, cover-image download
                        # via the global scraper, anything else hitting
                        # comix.to or its CDN — inherit the cf_clearance
                        # instead of each one re-tripping the 403 + CF retry
                        # cycle on the same cookies we already have. No-op
                        # when the cookie cache is empty (CF wasn't solved).
                        # Cross-file: sites/crawlee_utils.py:sync_cf_cookies.
                        try:
                            from .crawlee_utils import sync_cf_cookies
                            sync_cf_cookies(scraper, url)
                        except Exception:
                            pass
                        response = cf.get(url, timeout=20)
            except Exception:
                # Retry-path failure is non-fatal — keep the original
                # response so the caller's own error path runs.
                pass
        return response

    def _waf_recover_once(self, response, url: str, scraper, make_request):
        """If *response* is comix's interactive WAF interstitial, hand the
        browser to the user, adopt the cookies their solve produced, and retry
        the request ONCE. Returns the (possibly new) response.

        Raises ComixWafChallengeError when the challenge is still in front of us
        afterwards. Failing loud is deliberate: the caller's next step is to
        parse this body, and every parser in this module degrades a challenge
        page into plausible-looking garbage rather than an error.

        The cookie adoption is what makes an HTTP retry meaningful at all — the
        human solves inside the Patchright profile, so without copying that
        session across, cloudscraper would just re-fetch the interstitial. Same
        idea as crawlee_utils.sync_cf_cookies, but sourced from our own
        persistent context rather than the zendriver cache.
        """
        try:
            final_url = getattr(response, "url", None) or url
            body = getattr(response, "text", None)
        except Exception:
            return response
        if not _looks_like_waf_challenge(final_url, body):
            return response

        print(
            "[!] Comix: hit the site's human-verification check "
            "(/@waf/challenge) on a metadata request.",
            flush=True,
        )
        result = _COMIX_BROWSER_BRIDGE.solve_waf_interactively(url) or {}
        if result.get("solved"):
            for cookie in result.get("cookies") or []:
                try:
                    scraper.cookies.set(
                        cookie["name"],
                        cookie["value"],
                        domain=cookie.get("domain") or "comix.to",
                        path=cookie.get("path") or "/",
                    )
                except Exception:
                    continue
            user_agent = result.get("user_agent")
            if user_agent:
                # CF and most WAFs bind a clearance cookie to the UA that earned
                # it, so the retry has to present the browser's UA, not
                # cloudscraper's.
                try:
                    scraper.headers["User-Agent"] = user_agent
                except Exception:
                    pass
            retried = make_request(url, scraper)
            if not _looks_like_waf_challenge(
                getattr(retried, "url", None) or url,
                getattr(retried, "text", None),
            ):
                print("[*] Comix: verification accepted; continuing.", flush=True)
                return retried
            response = retried

        raise ComixWafChallengeError(
            "comix.to is asking for human verification and it was not completed, "
            "so the request could not be trusted. Open "
            f"{_WAF_CHALLENGE_HINT_URL} in a browser, complete the 'Verify "
            "you're human' check once, then re-run. (The downloader keeps its "
            f"own browser profile at {_comix_profile_dir()} — solving it in the "
            "window the downloader opens is what makes the session stick.)",
            challenge_url=getattr(response, "url", None) or url,
        )

    def _extract_initial_data_manga(self, soup) -> Optional[Dict]:
        """Return the full manga-detail dict from the title page's SSR
        React-Query hydration blob, or None.

        2026-07-11: comix.to is now an SPA that gates every /api/v1/* endpoint
        behind a per-request signature AND encrypts the response body
        ({"e":"<blob>"}, decrypted only in-JS), so the old cloudscraper API
        calls return 403 "Missing token." The title-page HTML, however, still
        ships a plaintext <script id="initial-data" type="application/json">
        React-Query hydration blob in the RAW server response. Its
        ["manga","detail",<hid|id>] query value IS the detail object we want:
        title / altTitles / authors / artists / genres / poster / synopsis /
        year / status / latestChapter / url. Plain HTTP, no browser, no
        decryption — this is the replacement for /api/v1/manga/{hid}.

        Match the query key on the ["manga","detail" prefix (parsed as JSON)
        so it works whether comix keys it by hid string ("6e6jz") or numeric
        id (49660), and ignore the sibling ["manga","recommended"/"groups"]
        queries. Some React-Query dumps wrap the payload under a "data" key;
        handle both. Returns a shallow copy so callers can mutate freely.
        """
        try:
            tag = soup.find("script", id="initial-data")
            raw = (tag.string or tag.get_text()) if tag else None
            data = json.loads(raw) if raw else None
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        queries = data.get("queries")
        if isinstance(queries, dict):
            for key, value in queries.items():
                if not isinstance(key, str):
                    continue
                try:
                    parsed_key = json.loads(key)
                except Exception:
                    continue
                if not (
                    isinstance(parsed_key, list)
                    and len(parsed_key) >= 2
                    and parsed_key[0] == "manga"
                    and parsed_key[1] == "detail"
                ):
                    continue
                if isinstance(value, dict):
                    if value.get("title"):
                        return dict(value)
                    inner = value.get("data")
                    if isinstance(inner, dict) and inner.get("title"):
                        return dict(inner)
        # Alt shape: a top-level "manga" object carrying real fields (vs the
        # {hid,id}-only stub the current markup uses).
        manga = data.get("manga")
        if isinstance(manga, dict) and manga.get("title"):
            return dict(manga)
        return None

    def _extract_sync_data(self, soup) -> Optional[Dict]:
        """Return the small <script id="syncData"> JSON (mal-sync integration
        data: {name, manga_id, manga_url, ...}) or None. Present on every
        title page; a cheap title/hid source when #initial-data is absent."""
        try:
            tag = soup.find("script", id="syncData")
            raw = (tag.string or tag.get_text()) if tag else None
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def _normalize_named_list(self, value: Any) -> List[str]:
        """Converts mixed list/dict/string inputs into a clean list of names."""
        if not value:
            return []
        if not isinstance(value, list):
            value = [value]
        names: List[str] = []
        for item in value:
            name = None
            if isinstance(item, dict):
                name = item.get("title") or item.get("name")
            elif isinstance(item, str):
                name = item
            if name:
                name = name.strip()
                if name:
                    names.append(name)
        return names

    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        response = self._cf_aware_request(url, scraper, make_request)
        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        
        # First, extract hash_id from URL
        hash_id = None
        path = urlparse(url).path
        parts = path.split('/')
        if len(parts) >= 3 and parts[1] == 'title':
            slug_part = parts[2]
            if '-' in slug_part:
                hash_id = slug_part.split('-')[0]
            else:
                hash_id = slug_part
        
        # Primary source (2026-07-11): the full manga detail lives plaintext in
        # the title page's <script id="initial-data"> SSR blob — see
        # _extract_initial_data_manga. This replaces the now-dead
        # /api/v1/manga/{hid} API call (403 "Missing token." + encrypted body).
        # The og:image / meta-description / list-normalization steps below run
        # regardless of which branch populated manga_data.
        manga_data = self._extract_initial_data_manga(soup)

        # Fallback 1: the small #syncData mal-sync blob ({name, manga_id}).
        if not manga_data:
            sync = self._extract_sync_data(soup)
            if sync and sync.get("name"):
                _sync_hid = sync.get("manga_id") or hash_id
                manga_data = {
                    "hid": _sync_hid,
                    "hash_id": _sync_hid,
                    "title": sync.get("name"),
                }

        # Fallback 2: a raw "manga_id"/"hash_id"/"title" triple in the HTML.
        if not manga_data:
            match = re.search(r'"manga_id":(\d+)', html)
            if match:
                hash_match = re.search(r'"hash_id":"([^"]+)"', html)
                title_match = re.search(r'"title":"([^"]+)"', html)
                if hash_match and title_match:
                    manga_data = {
                        "manga_id": int(match.group(1)),
                        "hash_id": hash_match.group(1),
                        "title": title_match.group(1),
                        "hid": hash_match.group(1),
                    }

        # Last resort: derive a title from the URL slug (hash_id is only set
        # when slug_part parsed, so slug_part is defined here).
        if not manga_data and hash_id:
            title = slug_part.split('-', 1)[1].replace('-', ' ').title() if '-' in slug_part else slug_part
            manga_data = {
                "hash_id": hash_id,
                "title": title,
                "hid": hash_id,
            }

        if not manga_data:
            raise RuntimeError("Could not find manga data in page.")

        # AniList enrichment reads comic_data["title"]; some callers key on
        # "name". Set both from whichever branch won (asura precedent, CLAUDE.md).
        if manga_data.get("title") and not manga_data.get("name"):
            manga_data["name"] = manga_data["title"]

        # Ensure hid is present
        if "hid" not in manga_data:
            if "hash_id" in manga_data:
                manga_data["hid"] = manga_data["hash_id"]
            elif "slug" in manga_data:
                slug = manga_data["slug"]
                if "-" in slug:
                    manga_data["hid"] = slug.split("-")[0]
                else:
                    manga_data["hid"] = slug
            else:
                # Last resort: try to extract from URL
                if hash_id:
                    manga_data["hid"] = hash_id

        poster = manga_data.get("poster") or manga_data.get("_poster")
        if isinstance(poster, dict):
            cover_url = poster.get("large") or poster.get("medium") or poster.get("small")
            thumb_url = poster.get("medium") or poster.get("small") or cover_url
            if cover_url and not manga_data.get("cover"):
                manga_data["cover"] = cover_url
            if thumb_url and not manga_data.get("thumb"):
                manga_data["thumb"] = thumb_url
        if not manga_data.get("cover"):
            cover_tag = soup.find("meta", property="og:image")
            if cover_tag and cover_tag.get("content"):
                manga_data["cover"] = cover_tag["content"]

        synopsis = manga_data.get("synopsis")
        if synopsis and not manga_data.get("desc"):
            manga_data["desc"] = synopsis.strip()
        if not manga_data.get("desc"):
            desc_meta = soup.find("meta", attrs={"name": "description"})
            if desc_meta and desc_meta.get("content"):
                manga_data["desc"] = desc_meta["content"].strip()

        # The #initial-data detail's `url` is a relative path (e.g.
        # "/title/6e6jz-the-beginning-after-the-end"). get_chapters →
        # fetch_chapters_via_dom → page.goto needs an absolute URL or Patchright
        # raises "Cannot navigate to invalid URL". Normalize here so every caller
        # of context.comic["url"] sees a usable absolute URL. Fall back to the
        # caller-supplied url only when the detail didn't populate the field.
        api_url_value = manga_data.get("url")
        if isinstance(api_url_value, str) and api_url_value.startswith("/"):
            manga_data["url"] = "https://comix.to" + api_url_value
        elif url and not api_url_value:
            manga_data["url"] = url

        # Preserve the caller's ?group_id= filter. It MUST be captured here:
        # the branch directly above replaces comic["url"] with #initial-data's
        # canonical (query-less) path, so this is the last point at which the
        # user's URL is still visible. get_chapters re-appends it to the URL it
        # hands the DOM scrape. Worth the plumbing — comix honors the filter
        # server-side, turning 360 pager pages into 59 on a long multi-group
        # series (rows are per chapter × group). Grep _group_id.
        group_id = _extract_group_id(url)
        if group_id:
            manga_data["_group_id"] = group_id

        list_mappings = {
            "genres": ["genres", "genre"],
            "theme": ["theme"],
            "format": ["format"],
            "authors": ["authors", "author"],
            "artists": ["artists", "artist"],
            "alt_names": ["alt_names", "alt_titles", "altTitles", "aliases", "alternative_names"],
        }
        for target_key, source_keys in list_mappings.items():
            for source_key in source_keys:
                normalized = self._normalize_named_list(manga_data.get(source_key))
                if normalized:
                    manga_data[target_key] = normalized
                    break

        # Year may live under any of these depending on the comix.to API
        # version. Guard tightly: only int values > 0; non-int payloads are
        # silently dropped so downstream consumers always see a clean field.
        for year_key in ("year", "release_year", "year_of_release"):
            year_raw = manga_data.get(year_key)
            if isinstance(year_raw, int) and year_raw > 0:
                manga_data["year"] = year_raw
                break

        return SiteComicContext(
            comic=manga_data,
            title=manga_data.get("title", "Unknown"),
            identifier=manga_data.get("hid") or manga_data.get("hash_id"),
            soup=soup
        )

    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        hash_id = context.identifier
        if not hash_id:
             raise RuntimeError("Missing manga identifier (hash_id).")

        # Title URL feeds the DOM scrape. fetch_comic_context absolutizes this
        # on the comic dict; the hash_id-only fallback exists for callers that
        # constructed a context manually without a URL.
        title_url = context.comic.get("url") or f"https://comix.to/title/{hash_id}"

        # Re-attach the user's ?group_id= filter (captured in
        # fetch_comic_context, which strips the query when it adopts
        # #initial-data's canonical URL). Scoping the list to one group is the
        # single biggest cost lever on this site — 6x fewer pager pages on a
        # 4-group series — and it is what the user asked for by pasting that
        # URL. Trade-off, accepted 2026-08-02: sites/base.py's
        # select_best_chapter_version then has only one version per chapter to
        # rank, i.e. the explicit group choice wins over cross-group ranking.
        # Same escape-hatch philosophy as the `--group <name>` branch (see the
        # group-selection invariant in CLAUDE.md). Absent group_id → unchanged
        # full cross-group behavior.
        group_id = context.comic.get("_group_id")
        if group_id:
            separator = "&" if "?" in title_url else "?"
            title_url = f"{title_url}{separator}group_id={group_id}"
            print(
                f"[*] Comix: scoping the chapter list to group_id={group_id} "
                f"(from the URL you supplied).",
                flush=True,
            )

        # 2026-07-11: /api/v1/manga/{hid}/chapters is signed + encrypted
        # ({"e":...}) and 403s "Missing token." to cloudscraper, so the chapter
        # list is only obtainable from the rendered DOM. The persistent
        # Patchright browser paginates the title page via ?page=N (hard 20/page;
        # ?limit= is ignored and there's no infinite scroll), which can take
        # 30-90s on a large multi-group series. stderr (via the _stderr_print
        # shim) keeps stdout clean for JSON consumers.
        # Cross-file: _ComixBrowserSession.fetch_chapters_via_dom.
        print(
            "[*] Comix: fetching chapter list via persistent-browser DOM scrape "
            "(the JSON API is encrypted/token-gated).",
            flush=True,
        )
        # chapter_floor_hint is advisory (sites/base.py): comix lists
        # newest-first, so an update run that only wants chapters > N can stop
        # paginating as soon as a whole page falls below N instead of walking
        # all 360 pages. None (a full download) paginates to the end as before.
        raw_items = _COMIX_BROWSER_BRIDGE.fetch_chapters_via_dom(
            title_url,
            chapter_floor=getattr(self, "chapter_floor_hint", None),
        ) or []

        chapters: List[Dict] = []
        for item in raw_items:
            # Lenient language filter (ported from upstream's
            # "No chapters selected" fix). Two rules:
            #   1. Items with no `language` field are KEPT — many
            #      comix payloads omit the field on untranslated /
            #      original-language entries. The prior strict
            #      `!= language` silently dropped them (since
            #      None != "en"), surfacing as zero chapters.
            #   2. String match is case-insensitive AND accepts
            #      long-form names: "English" / "english" match
            #      "en" because the API mixes short codes ("en")
            #      with display names ("English") across endpoints.
            # DOM-scrape items always set language=None and so always
            # pass this filter; the per-row UI doesn't surface the
            # language attribute and the title URL implicitly already
            # restricts to whatever language section the user landed on.
            item_lang = item.get("language")
            if language and item_lang is not None:
                lang_lower = language.lower()
                item_lang_lower = item_lang.lower()
                if item_lang_lower != lang_lower and not item_lang_lower.startswith(lang_lower):
                    continue

            chap_num = item.get("number")
            # v1 uses `id`; v2 used `chapter_id`. Try v1 first.
            chap_id = item.get("id") or item.get("chapter_id")
            title = item.get("name") or f"Chapter {chap_num}"

            # Normalize chap_num to a parseable numeric string.
            # The API USUALLY returns int/float (e.g. 47, 47.5), but
            # has been observed returning None / "" / non-numeric
            # strings for special chapters (oneshots, side stories,
            # season-break placeholders). aio-dl.py:5885 calls
            # float(chap) for chapter bucketing and ValueErrors on
            # "None" / non-numeric text → the chapter gets skipped
            # with "Skipping chapter with invalid number: None" and
            # the user sees zero comix chapters downloaded.
            # Resolution order:
            #   1. item["number"] when numeric → "%g" coerce ("47", "47.5").
            #   2. item["number"] as a string with embedded digits
            #      → regex-extract.
            #   3. item["name"] / title → regex-extract.
            # Skip the chapter entirely when no numeric token is
            # available — surfacing a non-numeric `chap` would just
            # trigger the same skip downstream with a misleading
            # "Skipping chapter with invalid number" log line.
            chap_str: Optional[str] = None
            if isinstance(chap_num, (int, float)):
                chap_str = f"{chap_num:g}"
            else:
                for source_text in (
                    chap_num if isinstance(chap_num, str) else None,
                    title,
                ):
                    if not source_text:
                        continue
                    m = re.search(r"(\d+(?:\.\d+)?)", str(source_text))
                    if m:
                        chap_str = m.group(1)
                        break
            if chap_str is None:
                continue

            # Prefer the canonical chapter URL the API/DOM supplies in
            # `item["url"]` when present (ported from upstream's
            # _cf_aware refactor; DOM scrape also populates this).
            # Using the supplied URL avoids drift if comix changes
            # their URL slug format; the construction path below
            # remains the fallback for legacy item shapes that omit
            # the field.
            chap_url = item.get("url")
            if chap_url and not chap_url.startswith("http"):
                chap_url = urljoin("https://comix.to", chap_url)
            if not chap_url:
                # Construct URL
                # Format: https://comix.to/title/{hash_id}-{slug}/{chapter_id}-chapter-{number}
                slug = context.comic.get("slug")

                # If we don't have the slug from API, try to get it from the context URL
                if not slug and context.comic.get("url"):
                    path = urlparse(context.comic["url"]).path
                    parts = path.split('/')
                    if len(parts) >= 3:
                        # This is likely the full slug (hash_id-slug)
                        slug = parts[2]

                if not slug:
                    slug = "unknown"

                # Ensure slug starts with hash_id
                if not slug.startswith(f"{hash_id}-"):
                    slug = f"{hash_id}-{slug}"

                # URL still uses the API's raw `number` value (which is
                # what comix.to's chapter-page URL expects); chap_str is
                # only for our internal bucketing/sorting. Falls back to
                # chap_str when the API field was unparseable so the URL
                # at least targets the right chapter number rather than
                # the literal string "None".
                url_chap_part = chap_num if chap_num not in (None, "") else chap_str
                chap_url = f"https://comix.to/title/{slug}/{chap_id}-chapter-{url_chap_part}"

            # v1 uses `group`; v2 used `scanlation_group`. Try both.
            # DOM-scrape items also populate `group` with {"name": ...}.
            group_info = item.get("group") or item.get("scanlation_group") or {}
            group_name = group_info.get("name") if group_info else None

            chapters.append({
                "url": chap_url,
                "chap": chap_str,
                "title": title,
                "id": chap_id,
                "_groups": (
                    [GroupInfo(
                        name=group_name,
                        group_id=group_info.get("id") if group_info else None,
                    )] if group_name else []
                ),
                "group": group_name,
                # comix is one of only two sites that reports a real vote
                # count (mangataro's API path is the other), so the ranker's
                # upvote tier actually engages here — as a weak tiebreak below
                # MTL/official/track-record, not as the decider it used to be.
                "up_count": item.get("votes", 0),
            })

        return chapters

    # Module-level TTL constant used by _get/_cache_chapter_images.
    # Defined here (class-scope) instead of top-of-file so it stays
    # adjacent to the methods that read it; 600 s matches
    # sites/image_cache._TTL_SECONDS by intent — see __init__'s
    # _chapter_images_cache comment for why both clocks share a TTL.
    _CHAPTER_IMAGES_CACHE_TTL = 600.0

    def _get_cached_chapter_images(self, chapter_url: str) -> Optional[List[str]]:
        """Return a defensive copy of the cached URL list for this
        chapter, or None on miss / TTL-expired. Thread-safe."""
        with self._chapter_images_cache_lock:
            entry = self._chapter_images_cache.get(chapter_url)
            if entry is None:
                return None
            urls, ts = entry
            if time.monotonic() - ts > self._CHAPTER_IMAGES_CACHE_TTL:
                del self._chapter_images_cache[chapter_url]
                return None
            return list(urls)

    def _cache_chapter_images(self, chapter_url: str, urls: List[str]) -> None:
        """Stash this chapter's URL list. No-op on empty inputs
        (don't poison the cache with a known-bad result that would
        short-circuit a future retry). Thread-safe."""
        if not chapter_url or not urls:
            return
        with self._chapter_images_cache_lock:
            self._chapter_images_cache[chapter_url] = (list(urls), time.monotonic())

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        url = chapter.get("url")

        # Memoization fast path. The prefetch worker and the main download flow
        # share this handler instance and BOTH call in for every chapter; the
        # DOM scrape is ~seconds AND serializes through the bridge's single
        # daemon worker behind any pending prefetches, so main was waiting
        # several chapters' scrape time for a result it could serve from memory.
        # Cache hit → return immediately, no browser enqueue. See __init__.
        if url:
            cached = self._get_cached_chapter_images(url)
            if cached is not None:
                return cached

        if not url:
            raise RuntimeError("Comix chapter is missing a url; cannot fetch images.")

        # 2026-07-11: the chapter page is an SPA that fetches a signed +
        # encrypted /api/v1/chapters/{id} ({"e":...}) and decrypts it in-JS to
        # render one lazy-loaded <img> per page. Those pages are now plain,
        # directly-fetchable webp CDN URLs — comix dropped the old server-side
        # tile-scramble, so there is no <canvas> anymore (verified 2026-07-11:
        # no x-scramble-* headers, no CSS transform, fetchable with no referer).
        # Python can neither sign nor decrypt the API, so drive the persistent
        # browser to render the chapter and scrape the page URLs from the DOM.
        # The bridge's page.on("response") listener also caches each <img>'s
        # bytes as they load, so aio-dl.py:dl_image usually serves straight from
        # memory instead of re-fetching. Returns [] on a render miss (the
        # caller's completeness gate then retries on the primary; comix can now
        # ALSO serve as a --multi-source alt, so an alt-source rescue is
        # possible — see the class comment for the multi-source change).
        # Cross-file: _ComixBrowserSession.fetch_chapter_images_via_dom.
        images = _COMIX_BROWSER_BRIDGE.fetch_chapter_images_via_dom(url) or []
        if images:
            self._cache_chapter_images(url, images)
        return images

    # ----------------------------------------------------------------- search
    # 2026-07-12: keyword search is browser-driven. /api/v1/manga?keyword= is
    # signed (per-request in-JS token) AND returns an encrypted body, so a
    # Python HTTP search is impossible — the same double barrier that forces
    # chapters + images through the browser. The header typeahead is the only
    # working keyword surface: type into the search box, scrape the rendered
    # dropdown (see _ComixBrowserSession.fetch_search_via_dom for the DOM). The
    # URL-seed path still works too (aio_search_cli.py:_try_extract_seed_hit
    # resolves a pasted /title/ URL via fetch_comic_context, no browser).
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
        # Drive the header typeahead in the persistent browser. ANY failure
        # (cold-launch timeout, headless CF re-challenge, DOM drift) degrades
        # to [] — NEVER an exception. This deliberately OVERRIDES the base
        # contract's "let HTTP errors propagate so the dead-host cache learns"
        # guidance (base.py:search): comix issues NO HTTP request here, and the
        # orchestrator's persistent ProbeFailureCache (search_orchestrator.py
        # _run_one → record_failure, PROBE_FAILURE_THRESHOLD=2, TTL 3600s) would
        # BLOCKLIST comix.to for an hour after 2 flaky searches if we let this
        # raise. comix's failure modes are transient and retried every fresh
        # --search subprocess, so a dead-host entry is pure harm. Swallow-to-[]
        # is also what both DOM scrapes (chapters + images) already do.
        try:
            rows = _COMIX_BROWSER_BRIDGE.fetch_search_via_dom(
                clean, limit=int(limit), time_budget_s=28.0,
            )
        except Exception:
            return []
        if not rows:
            return []

        hits: List[SearchHit] = []
        n = len(rows)
        for idx, row in enumerate(rows):
            hid = (row.get("hid") or "").strip()
            title = (row.get("title") or "").strip()
            if not hid or not title:
                continue
            cover = (row.get("cover") or "").strip() or None
            # Chapter-count hint from the "Ch.N" typeahead sub-label.
            chapter_count = None
            m = re.search(r"Ch\.([\d.]+)", row.get("sub") or "")
            if m:
                try:
                    chapter_count = int(float(m.group(1)))
                except ValueError:
                    chapter_count = None
            # /title/{hid} resolves without the slug (verified live; the
            # fetch_comic_context hid parse handles the no-slug form). ALL
            # result types (MANGA + OTHER) are kept per the user's search-
            # participation decision — comix's OTHER bucket (manhwa / manhua /
            # webtoon) is exactly what a cross-site comic search wants.
            url_full = f"https://comix.to/title/{hid}"
            # The typeahead is already relevance-ranked, so position 0 = best.
            # raw_score only orders comix's own hits + seeds the _quality_for
            # fallback; the real cross-site ranking comes from the image-quality
            # probe (_probe_chapter_aggregate) + title match.
            raw_score = max(0.05, 1.0 - (idx / max(1, n)))
            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=url_full,
                    cover=cover,
                    alt_titles=[],
                    year=None,
                    language=None,
                    chapter_count_hint=chapter_count,
                    raw_score=raw_score,
                )
            )
        return hits

    # ------------------------------------------------------ image-quality probe
    # comix competes on MEASURED image quality now (2026-07-12), not just the
    # static seed. The standard 8-chapter breadth probe
    # (sites/base.py:_probe_chapter_aggregate) is infeasible here: each
    # chapter's get_chapter_images renders the WHOLE chapter in the
    # single-threaded browser bridge, so 8 serialized renders blow the
    # orchestrator's 240 s probe deadline and fall back to the seed anyway. The
    # override below probes exactly ONE chapter (chapter 1 by preference — user
    # directive; see _pick_probe_chapter) with a capped page render
    # (_COMIX_PROBE_PAGE_CAP), scoring the latter half of those pages (median),
    # so a single render is ~8-20 s. The seed stays as the fallback when the
    # probe returns None.
    def _pick_probe_chapter(
        self, chapters: List[Dict],
    ) -> Optional[Tuple[int, Dict]]:
        """Return (absolute_index, chapter) to probe. Prefers the chapter
        numbered EXACTLY 1 (user directive 2026-07-12: probe chapter 1, "not 0
        or 0.5", unless there is no chapter 1). Fallback ladder when there's no
        ch.1: the lowest WHOLE chapter >= 1 (skips a ch.0 prologue and x.5
        omake/specials), then the lowest-numbered chapter of any kind, then row
        0. The absolute index feeds _pick_random_middle_page_index's
        deterministic page seed. Returns None only on an empty list.
        """
        if not chapters:
            return None
        numbered: List[Tuple[float, int, Dict]] = []
        for idx, ch in enumerate(chapters):
            try:
                num = float(ch.get("chap"))
            except (TypeError, ValueError):
                continue
            numbered.append((num, idx, ch))
        # Exact chapter 1 — the preferred sample.
        for num, idx, ch in numbered:
            if num == 1.0:
                return idx, ch
        # No ch.1 → lowest whole-numbered chapter >= 1 (dodges ch.0 and x.5).
        whole_ge1 = [t for t in numbered if t[0] >= 1.0 and t[0] == int(t[0])]
        if whole_ge1:
            _num, idx, ch = min(whole_ge1, key=lambda t: t[0])
            return idx, ch
        # Any numeric chapter, lowest number (e.g. a ch.0-only oneshot).
        if numbered:
            _num, idx, ch = min(numbered, key=lambda t: t[0])
            return idx, ch
        # No numeric chapters at all — probe the first row as a last resort.
        return 0, chapters[0]

    def _probe_chapter_aggregate(
        self, hit: SearchHit, scraper, make_request,
        max_samples: Optional[int] = None,
        fetch_memo=None,
    ) -> Optional[tuple]:
        """comix override: probe a SINGLE chapter (chapter 1 by preference),
        rendering only the first _COMIX_PROBE_PAGE_CAP pages and scoring the
        LATTER half of them (median), so the browser cost (~8-20 s) fits the
        orchestrator's 240 s probe deadline. See the section comment above for
        why the base 8-chapter breadth probe is infeasible, and the page-sample
        block below for why the latter-half median (not a single page) — the
        first live run mis-scored the flagship series at 0.1 on a sparse opening
        page. ``max_samples`` is IGNORED — this hard-caps to one chapter
        regardless of the orchestrator's rank-based clamp (which assumes cheap
        HTTP handlers). Returns (score, metadata) or None (→ orchestrator falls
        to cover probe → seed, the fallback). Race-free: no shared instance
        state.

        Cross-file: scoring via search_orchestrator._score_image_blob; page
        bytes come from the _fetch_probe_item_bytes override below (reads
        image_cache, which the render just populated). Chapter-1 selection is
        in _pick_probe_chapter.
        """
        from .search_orchestrator import _score_image_blob

        if not hit or not hit.url:
            return None
        # Context + FULL chapter-list scrape — comix lists newest-first, so
        # chapter 1 is the OLDEST entry and only found by paginating the whole
        # list. ~5-50 s for normal series; a pathological 1000+ chapter series
        # may approach the probe deadline and degrade to seed (accepted — rare,
        # and the seed is a calibrated prior). Routed through the per-run
        # FetchMemo when provided (sites/fetch_memo.py, 2026-07-12): T3 and the
        # winner chapter fetch then reuse THIS scrape instead of re-paying the
        # browser cost — for comix that reuse is worth 5-50s per later phase.
        try:
            if fetch_memo is not None:
                chapters = fetch_memo.get_chapters(
                    self, hit.url, "en", scraper, make_request,
                )
            else:
                context = self.fetch_comic_context(hit.url, scraper, make_request)
                if context is None:
                    return None
                chapters = self.get_chapters(context, scraper, "en", make_request)
        except Exception:
            return None
        if not chapters:
            return None
        pick = self._pick_probe_chapter(chapters)
        if pick is None:
            return None
        abs_idx, chapter = pick
        chap_url = chapter.get("url")
        if not chap_url:
            return None
        # Capped render: only the first _COMIX_PROBE_PAGE_CAP pages, not the
        # whole ~70-page chapter. Straight to the bridge (not
        # get_chapter_images) so (a) the cap is honored and (b) the handler's
        # memo cache isn't populated with a truncated (capped) page list a
        # later real download would wrongly serve.
        try:
            image_items = _COMIX_BROWSER_BRIDGE.fetch_chapter_images_via_dom(
                chap_url,
                time_budget_s=60.0,
                max_capture_pages=_COMIX_PROBE_PAGE_CAP,
            )
        except Exception:
            return None
        if not image_items:
            return None
        # Score the LATTER half of the captured pages, not a single page. Two
        # reasons, both from the first live run (main Frieren scored 0.1):
        # (1) the opening pages of chapter 1 (cover / title splash / sparse
        # cold-open) are the LEAST representative, and capping capture to the
        # first N pages meant the base middle-of-N picker landed on an early
        # page (page 3, a 0.03-bpp near-blank) rather than the middle of the
        # real chapter — sampling the latter half of the captured window skips
        # that opening. (2) Median across several pages is robust to a single
        # atypical page; the base probe gets that robustness from 8-chapter
        # breadth, which we deliberately don't have here, so we recover it
        # within the one chapter. Deterministic (index range from the page
        # count) → a re-probe on cache miss samples the same pages.
        n_pages = len(image_items)
        if n_pages <= 2:
            sample_idxs = list(range(n_pages))
        else:
            sample_idxs = list(range(n_pages // 2, n_pages))[:5]
        scores: List[float] = []
        metas: List[Dict] = []
        for si in sample_idxs:
            blob = self._fetch_probe_item_bytes(image_items[si], scraper)
            if not blob:
                continue
            scored = _score_image_blob(blob)
            if scored is None:
                continue
            scores.append(scored[0])
            metas.append(scored[1])
        if not scores:
            return None
        import statistics
        agg_score = statistics.median(scores)
        # Representative metadata = the sample nearest the median score.
        order = sorted(range(len(scores)), key=lambda i: scores[i])
        meta = dict(metas[order[len(order) // 2]])
        meta["samples_attempted"] = len(sample_idxs)
        meta["samples_succeeded"] = len(scores)
        meta["probe_mode"] = "comix_first_chapter"
        meta["chapter_indices_sampled"] = [abs_idx]
        return agg_score, meta

    def _fetch_probe_item_bytes(self, item, scraper) -> Optional[bytes]:
        """comix override: serve probe bytes from image_cache first.

        The browser render (fetch_chapter_images_via_dom) already cached each
        page's bytes — real webp under its CDN URL, or synthetic-key bytes for a
        legacy tile-scrambled <canvas> page under a `comix-page://…` URL. The
        base implementation does scraper.get(url), which (a) can't fetch the
        synthetic scheme → would score that page 0.0, and (b) re-downloads a
        page the browser already holds. Read the cache first (fair scoring for
        BOTH page shapes, no second fetch); fall back to the base HTTP path only
        on a cache miss for a real https URL.

        Cross-file: image_cache populated in _ComixBrowserSession (the
        page.on("response") listener + the canvas toDataURL path); the same
        cache aio-dl.py:dl_image reads.
        """
        if isinstance(item, str) and item:
            try:
                from . import image_cache
                cached = image_cache.get_cached_image(item)
            except Exception:
                cached = None
            if cached is not None and cached[0]:
                return cached[0]
        return super()._fetch_probe_item_bytes(item, scraper)


# ---------------------------------------------------------------------------
# Patchright bridge
# ---------------------------------------------------------------------------
# Patchright's sync API has two hard constraints: (1) every call must run on
# the same thread that called sync_playwright().start(), and (2) that thread
# must own an asyncio event loop. Probe-phase workers in
# sites/search_orchestrator.py and image-prefetch threads in aio-dl.py
# satisfy neither. To make Patchright safely callable from any thread, we
# serialize all Patchright work onto a single dedicated worker thread (one
# process-wide) — the daemon `comix-pw` thread started by
# _ensure_comix_worker(). Callers from any thread submit a (future, fn,
# args, kwargs) tuple to _COMIX_REQUEST_QUEUE and block on the future's
# result with a wall-clock timeout (_COMIX_DEFAULT_TIMEOUT_S, 60 s).
# Synchronous from the caller's perspective.
#
# Mirrors sites/mangadex.py:_report_worker / _enqueue_report (same daemon
# +queue pattern) and sites/mangafire_vrf_simple.py:1965-2106 (the prior
# pattern this module used to follow before the v8 rewrite). Keep the
# three structurally similar so the pattern stays recognizable across
# the codebase.


class _ComixBrowserSession:
    """Patchright lifecycle owner. Every method runs on the daemon
    `comix-pw` worker (see _comix_worker_loop) so sync_playwright's
    same-thread contract is upheld.

    Bodies are lifted verbatim from the prior in-class implementation,
    with the main-thread guard removed (this dedicated thread IS now the
    only valid caller).
    """

    def __init__(self):
        self._pw = None
        self._browser = None
        # _context is an explicit BrowserContext so we can set User-Agent at
        # creation time AND call add_cookies later. browser.new_page() gives
        # an anonymous default context with neither lever exposed — and CF
        # binds cf_clearance to (UA, IP, TLS fp), so a UA mismatch between
        # the zendriver-captured cookie and the Patchright request would
        # make injection useless.
        self._context = None
        self._page = None
        # Monotonic-ish ts of the last crawlee_utils._cf_cookie_cache entry
        # we synced into _context. Used by _sync_cf_cookies to skip
        # redundant add_cookies calls when the cache hasn't changed.
        self._last_cf_cookie_ts: float = 0.0
        # Which mode the live context was launched in. The headed WAF handoff
        # has to tear down and relaunch (headless is fixed at launch time and
        # Chromium locks the profile dir to one context), so _start compares
        # against this to decide whether the cached context is reusable.
        self._headless: bool = True

    def _start(self, headless: bool = True) -> bool:
        """Lazy-launch Patchright on first use. Returns True if the browser
        is ready, False if Patchright/Playwright unavailable or launch failed.
        Subsequent calls are cheap (already-started fast path).

        Launches a PERSISTENT context against the app-owned profile dir
        (_comix_profile_dir) rather than a throwaway one. That single change is
        the main defense against comix's WAF: a virgin profile per process is
        the most bot-like fingerprint available, and persisting the profile
        means a human solve survives across runs AND across processes (search,
        download, and the UI's per-series subprocesses all share it).

        ``headless`` is fixed at launch time by Chromium, and the profile dir
        can only be held by ONE context at a time, so switching modes (the WAF
        handoff) means a full teardown + relaunch — hence the mode comparison on
        the fast path.
        """
        if self._page is not None and self._headless != headless:
            self._cleanup()
        if self._page is not None:
            # CX-1: a non-None page is NOT proof of health. A mid-run browser
            # or context crash leaves the dead page object in place; reusing
            # it makes every later chapter silently yield 0 images. Health-check
            # the page + browser connection before trusting the fast path; if
            # either is dead (or the check itself throws because the underlying
            # transport is gone), tear the whole stack down and relaunch fresh.
            # _cleanup() nulls self._page, so control falls through to the
            # launch block below on the same call.
            try:
                page_dead = self._page.is_closed()
                # With launch_persistent_context there is no Browser handle to
                # ask (context.browser is documented as None for persistent
                # contexts), so the page's own liveness is the primary signal
                # and the browser check only runs when a handle exists.
                browser_dead = (
                    self._browser is not None and not self._browser.is_connected()
                )
            except Exception:
                page_dead = browser_dead = True
            if page_dead or browser_dead:
                print(
                    "[!] Comix: cached Patchright page/browser is dead "
                    "(crash or context loss); relaunching.",
                    flush=True,
                )
                self._cleanup()
            else:
                return True
        try:
            from patchright.sync_api import sync_playwright  # type: ignore
        except ImportError:
            try:
                from playwright.sync_api import sync_playwright  # type: ignore
            except ImportError:
                print("[!] Comix: patchright/playwright not installed; API capture unavailable.")
                return False
        try:
            self._pw = sync_playwright().start()
        except Exception as e:
            print(f"[!] Comix Playwright start failed: {e}")
            return False
        try:
            # Persistent context: cookies, localStorage and the reader's
            # preload setting live in this directory and survive process exit,
            # so one human WAF solve keeps working for days. See
            # _comix_profile_dir for why app-owned rather than the real Chrome
            # profile. The UA kwarg still matches whatever zendriver used for a
            # CF solve (CF binds cf_clearance to the UA); it can only be set at
            # context creation, never on a live context.
            profile_dir = _comix_profile_dir()
            os.makedirs(profile_dir, exist_ok=True)
            ctx_kwargs: Dict[str, Any] = {}
            cached_ua = self._cached_cf_user_agent()
            if cached_ua:
                ctx_kwargs["user_agent"] = cached_ua
            self._context = self._pw.chromium.launch_persistent_context(
                profile_dir,
                headless=headless,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
                **ctx_kwargs,
            )
            self._headless = headless
            # A persistent context opens with one about:blank page already;
            # reuse it so we don't leave an orphan tab holding the profile.
            existing = list(getattr(self._context, "pages", None) or [])
            self._page = existing[0] if existing else self._context.new_page()
            # Documented as None for persistent contexts on some versions;
            # kept only so the health check can use it when it IS available.
            self._browser = getattr(self._context, "browser", None)
        except Exception as e:
            print(f"[!] Comix Playwright launch failed: {e}")
            self._cleanup()
            return False

        # Session-level <img> byte capture. This is the single most important
        # wire in the comix chapter pipeline: the ~70-80 pages per chapter load
        # as <img> off the CDN, and without capturing the bytes here dl_image
        # would re-fetch each URL over HTTP later — by which time the CDN may be
        # rate-limiting from the parallel scrape traffic. Empirically that was
        # the cause of the [Backoff]/[Fallback]/[Error: Skipping] cascades and
        # the 30s long-retries per chapter. Stashing the bytes at the moment the
        # browser pulls them lets dl_image short-circuit straight to disk.
        #
        # Filter on request.resource_type == "image": only true <img>-tag
        # fetches qualify. JS-driven fetch()/XHR calls are resource_type
        # "fetch"/"xhr" and get skipped — caching them would just waste the
        # 256MB cap. Cross-file: sites/image_cache.py owns the cache + eviction;
        # aio-dl.py:dl_image reads from it at the top before any HTTP work.
        try:
            from . import image_cache as _image_cache_module
        except Exception:
            _image_cache_module = None

        def _capture_image_response(response):
            if _image_cache_module is None:
                return
            try:
                try:
                    if response.request.resource_type != "image":
                        return
                except Exception:
                    pass
                ct = (response.headers.get("content-type") or "").lower()
                if not ct.startswith("image/"):
                    return
                body = response.body()
                if body:
                    _image_cache_module.cache_image(response.url, body, ct)
            except Exception:
                # response.body() can throw if the response was
                # aborted or the page navigated before the body
                # arrived. Silent skip — the chapter's canvas
                # toDataURL path or dl_image's HTTP fallback will
                # handle the missing entry.
                pass

        try:
            self._page.on("response", _capture_image_response)
        except Exception as e:
            print(
                f"[!] Comix: failed to attach image-response "
                f"listener ({type(e).__name__}: {e}); plain <img> "
                f"pages will fall through to HTTP fetch with the "
                f"signed-token-expiry risk that implies.",
                flush=True,
            )

        # Inject any cookies already captured by a prior zendriver solve.
        # Public methods also re-call _sync_cf_cookies in case the cache
        # gets a fresher generation between the bridge launching and the
        # actual navigation.
        self._sync_cf_cookies()
        return True

    def _cleanup(self):
        # context.close() on a persistent context is what FLUSHES cookies and
        # localStorage to the profile dir. Skipping it (or killing the process
        # mid-run) can lose a freshly-solved WAF session, so the WAF handoff
        # always closes through here rather than abandoning the context.
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        self._context = None
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        self._browser = None
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None
        self._page = None
        self._last_cf_cookie_ts = 0.0
        self._headless = True

    def _cached_cf_user_agent(self) -> Optional[str]:
        """Return the User-Agent string from any cached zendriver CF solve
        for comix.to, or None if no solve has run yet. Using THAT exact UA
        in the Patchright context is what keeps the cf_clearance cookie
        valid on Patchright-issued requests — CF rejects cookie+UA
        mismatches as bot signals.

        Cross-file: cache populated by sites/crawlee_utils.py:_solve_cf_async
        via get_cf_session; key is the bare netloc ("comix.to").
        """
        try:
            from . import crawlee_utils as _cu
            with _cu._cf_cookie_lock:
                cached = _cu._cf_cookie_cache.get("comix.to")
            if cached:
                return cached.get("user_agent") or None
        except Exception:
            pass
        return None

    def _sync_cf_cookies(self) -> None:
        """Copy the latest crawlee CF cookies into this bridge's Patchright
        context so the headless DOM scrape inherits the cf_clearance
        that zendriver captured visibly. Idempotent — tracks last-synced
        timestamp and no-ops when the cache is empty or hasn't changed
        since the last sync.

        Caveat: even with matching UA + cookies, CF can still re-challenge
        because the TLS fingerprint of Patchright's bundled Chromium may
        differ from the headed Chrome that zendriver used. If it does,
        the page-1 selector wait still times out and the comix.py
        diagnostic block surfaces it — at which point this strategy is
        exhausted and the user should rerun with --multi-source.

        Cross-file: cookies populated in sites/crawlee_utils.py via
        get_cf_session → _solve_cf_async; serialized through
        _cu._cf_cookie_lock for cross-thread safety.
        """
        if self._context is None:
            return
        try:
            from . import crawlee_utils as _cu
            with _cu._cf_cookie_lock:
                cached = _cu._cf_cookie_cache.get("comix.to")
        except Exception:
            return
        if not cached:
            return
        ts = float(cached.get("ts", 0) or 0)
        if ts <= self._last_cf_cookie_ts:
            return  # already injected this generation
        raw = cached.get("cookies") or []
        if not raw:
            return
        pw_cookies: List[Dict[str, Any]] = []
        for c in raw:
            entry: Dict[str, Any] = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain") or "comix.to",
                "path": c.get("path") or "/",
            }
            pw_cookies.append(entry)
        try:
            self._context.add_cookies(pw_cookies)
            self._last_cf_cookie_ts = ts
            print(
                f"[*] Comix: injected {len(pw_cookies)} CF cookie(s) "
                f"captured by zendriver into the Patchright context",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[!] Comix: failed to inject CF cookies into Patchright: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

    def _waf_blocked(self, context_label: str) -> bool:
        """True if the page currently shows comix's human-verification check.

        Called after every navigation and every pager click. Cheap: reads
        page.url first (the authoritative signal) and only pays for a body read
        when the URL is inconclusive.
        """
        page = self._page
        if page is None:
            return False
        try:
            current_url = page.url
        except Exception:
            return False
        if _looks_like_waf_challenge(current_url):
            print(
                f"[!] Comix: human-verification check hit during "
                f"{context_label} ({current_url}).",
                flush=True,
            )
            return True
        # URL didn't say so — sniff the visible copy. Guarded because a
        # mid-navigation evaluate can throw.
        try:
            body_text = page.evaluate(
                "document.body ? document.body.innerText.slice(0, 2000) : ''"
            ) or ""
        except Exception:
            return False
        if _looks_like_waf_challenge(None, body_text):
            print(
                f"[!] Comix: human-verification check hit during "
                f"{context_label} (interstitial body).",
                flush=True,
            )
            return True
        return False

    def _enforce_no_waf(self, stage: str, return_url: Optional[str] = None) -> None:
        """No-op unless the WAF interstitial is up; otherwise attempt ONE
        interactive handoff and raise ComixWafChallengeError if it doesn't pass.

        POSTCONDITION on a normal return: the browser is back on ``return_url``
        and verified clear. That reload is load-bearing, not tidiness — the
        handoff tears the whole context down and relaunches headless, so
        ``self._page`` comes back pointing at a fresh about:blank. Without
        restoring the target, every caller would resume its DOM waits on a blank
        page and fail *after* the user had successfully completed the check,
        which is the most infuriating possible outcome. (Only the page-1 path
        happened to recover, via its own retry navigation, and only after
        burning a 20s wait on the blank page first.)

        Shared by the chapter-list and chapter-image scrapes so both fail the
        same way. Search deliberately does NOT use this — see
        ComixSiteHandler.search for why a raise there would blocklist the host.
        """
        if not self._waf_blocked(stage):
            return
        challenge_url = None
        try:
            challenge_url = self._page.url
        except Exception:
            pass
        if (self.solve_waf_interactively(return_url) or {}).get("solved"):
            if not return_url:
                # No target to restore (defensive: every browser call site
                # passes one). The session cookie is still banked in the
                # profile, so let the caller proceed on whatever it has.
                return
            try:
                self._page.goto(
                    return_url, wait_until="domcontentloaded", timeout=30000
                )
            except Exception as exc:
                raise ComixChapterScrapeError(
                    f"comix: verification passed but reloading {return_url} "
                    f"afterwards failed ({type(exc).__name__}: {exc})."
                ) from exc
            # A second challenge on the reload means the solve didn't stick;
            # one handoff per process is the cap, so this is terminal.
            if self._waf_blocked(f"{stage} (after verification)"):
                raise ComixWafChallengeError(
                    "comix.to re-issued its human-verification check "
                    f"immediately after one was completed, so {stage} could "
                    "not be read. Re-run in a few minutes.",
                    challenge_url=challenge_url,
                )
            return
        raise ComixWafChallengeError(
            "comix.to is asking for human verification and it was not "
            f"completed, so {stage} could not be read. Re-run and complete the "
            "check in the window the downloader opens, or visit comix.to in a "
            "browser and pass it once.",
            challenge_url=challenge_url,
        )

    def solve_waf_interactively(self, return_url: Optional[str] = None) -> Dict[str, Any]:
        """Open the downloader's own browser profile VISIBLY on comix's
        human-verification page and wait for the user to complete it.

        Returns {"solved": bool, "cookies": [...], "user_agent": str|None,
        "reason": str}. Never raises — callers decide whether an unsolved
        challenge is fatal (it is, for chapter lists and metadata).

        What this does NOT do, deliberately: it does not read, score, rotate, or
        submit the widget, and it sends no synthetic input. It navigates, prints
        an instruction, and POLLS the URL until the site itself decides the
        check passed. The solve is the user's.

        Why it works at all: the visible window runs on the SAME persistent
        profile dir as the headless one (_comix_profile_dir), so the session the
        user's solve produces is written straight into the profile the rest of
        the run uses. We relaunch headless afterwards and the caller retries.
        The returned cookies additionally let the plain-HTTP path
        (ComixSiteHandler._waf_recover_once) adopt the same session, which it
        otherwise could not see.
        """
        global _COMIX_WAF_HANDOFF_ATTEMPTED
        result: Dict[str, Any] = {"solved": False, "cookies": [], "user_agent": None}

        if (os.environ.get(_WAF_NO_INTERACTIVE_ENV) or "").strip() not in ("", "0"):
            result["reason"] = "disabled"
            print(
                f"[!] Comix: {_WAF_NO_INTERACTIVE_ENV} is set; not opening a "
                f"verification window.",
                flush=True,
            )
            return result

        # Headless Linux boxes have nowhere to show the window. Windows/macOS
        # always have a session available to the desktop app and the CLI.
        if sys.platform not in ("win32", "darwin") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        ):
            result["reason"] = "no_display"
            print(
                "[!] Comix: no display available, so the verification window "
                "cannot be shown.",
                flush=True,
            )
            return result

        with _COMIX_WAF_HANDOFF_LOCK:
            if _COMIX_WAF_HANDOFF_ATTEMPTED:
                result["reason"] = "already_attempted"
                return result
            _COMIX_WAF_HANDOFF_ATTEMPTED = True

        try:
            timeout_s = float(
                os.environ.get(_WAF_SOLVE_TIMEOUT_ENV)
                or _WAF_DEFAULT_SOLVE_TIMEOUT_S
            )
        except (TypeError, ValueError):
            timeout_s = _WAF_DEFAULT_SOLVE_TIMEOUT_S

        target = return_url or _WAF_CHALLENGE_HINT_URL
        # Drop the headless context first — Chromium locks the profile dir to a
        # single context, and closing is also what flushes state to disk.
        self._cleanup()
        if not self._start(headless=False):
            result["reason"] = "launch_failed"
            print(
                "[!] Comix: could not open a visible browser for verification.",
                flush=True,
            )
            self._cleanup()
            self._start(headless=True)
            return result

        page = self._page
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            print(
                f"[!] Comix: could not load the verification page "
                f"({type(exc).__name__}: {exc}).",
                flush=True,
            )

        # Every line is [!]-prefixed on purpose. The Electron LogPanel classifies
        # by line shape (UI-source/electron/log-filter.js:classifyLogLevel):
        # a leading "[!]" paints the line as an error, while a line starting
        # with 2+ spaces is demoted to "verbose" — so an indented banner would
        # render the one instruction the user MUST read as dim filler.
        print("[!] " + "=" * 68, flush=True)
        print(
            "[!] ACTION NEEDED - comix.to is asking for human verification.",
            flush=True,
        )
        print(
            "[!] A browser window just opened. Complete the check in it: drag",
            flush=True,
        )
        print(
            "[!] the slider until the picture lines up, then press Verify.",
            flush=True,
        )
        print(
            f"[!] The download resumes by itself once it passes (waiting up to "
            f"{int(timeout_s)}s).",
            flush=True,
        )
        print(
            "[!] The session is saved to disk, so this should be rare.",
            flush=True,
        )
        print("[!] " + "=" * 68, flush=True)

        deadline = time.monotonic() + timeout_s
        solved = False
        while time.monotonic() < deadline:
            try:
                if page.is_closed():
                    # User closed the window. Treat as a decision to abort
                    # rather than waiting out the full timeout.
                    print(
                        "[!] Comix: verification window was closed before the "
                        "check completed.",
                        flush=True,
                    )
                    result["reason"] = "window_closed"
                    break
                current = page.url
            except Exception:
                result["reason"] = "window_lost"
                break
            if current and not _looks_like_waf_challenge(current):
                # The site routed us off the interstitial — that IS the pass
                # signal (it redirects back to ?return=). Small settle wait so
                # the Set-Cookie lands before we read cookies.
                try:
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                solved = True
                break
            try:
                page.wait_for_timeout(1000)
            except Exception:
                break

        if solved:
            print("[*] Comix: verification passed - thanks. Resuming.", flush=True)
            try:
                result["cookies"] = [
                    {
                        "name": c.get("name"),
                        "value": c.get("value"),
                        "domain": c.get("domain"),
                        "path": c.get("path"),
                    }
                    for c in (self._context.cookies() or [])
                    if c.get("name") and c.get("value")
                ]
            except Exception:
                result["cookies"] = []
            try:
                result["user_agent"] = self._page.evaluate("navigator.userAgent")
            except Exception:
                result["user_agent"] = None
            result["solved"] = True
        elif not result.get("reason"):
            result["reason"] = "timeout"
            print(
                f"[!] Comix: verification not completed within {int(timeout_s)}s.",
                flush=True,
            )

        # Back to headless for the rest of the run. The close flushes the
        # solved session into the profile dir, so the relaunch inherits it.
        self._cleanup()
        self._start(headless=True)
        return result

    def fetch_search_via_dom(
        self,
        query: str,
        limit: int = 20,
        time_budget_s: float = 28.0,
    ) -> List[Dict]:
        """Scrape the header typeahead for a keyword search.

        2026-07-12: comix.to's /api/v1/manga?keyword= is signed (per-request
        in-JS token) AND returns an encrypted body, so a Python HTTP search is
        infeasible — the same double barrier that forces chapters + images
        through the browser (see fetch_chapter_images_via_dom). The header
        typeahead is the ONLY working keyword surface: type into the search
        box, let the SPA render its relevance-ranked dropdown, scrape it.

        Returns raw dicts [{hid,title,cover,type,sub}] (kept SearchHit-free
        like fetch_chapters_via_dom — ComixSiteHandler.search maps to SearchHit).
        Every step is explicitly bounded so it can NEVER hang search_all: the
        orchestrator runs handlers in a ThreadPoolExecutor whose per-site
        timeout is NOT a hard kill, so comix must self-bound. Returns [] on any
        miss/timeout and never raises — ComixSiteHandler.search explains why a
        raised exception would poison the persistent probe-failure cache.

        Cross-file: ComixSiteHandler.search maps the dicts; the bridge facade
        _ComixBrowserBridge.fetch_search_via_dom sets the outer wall-clock cap.
        Verified typeahead DOM (2026-07-12): input placeholder "Search any
        title...", result anchor a.search-pop__item-link (href /title/{hid}-…),
        .search-pop__item-title, .search-pop__thumb img, .search-pop__type,
        .search-pop__item-sub ("Ch.N").
        """
        clean = (query or "").strip()
        if not clean:
            return []
        if not self._start():
            return []
        self._sync_cf_cookies()
        import time as _time

        page = self._page
        if page is None:
            return []

        deadline = _time.monotonic() + time_budget_s

        def _remaining_ms(cap_ms: int) -> int:
            # Clamp each step's timeout to what's left of the budget so the
            # cumulative wall clock can't exceed time_budget_s. `or 1` at the
            # call sites turns a 0 into a 1ms poll (Patchright rejects
            # timeout=0 as "wait forever").
            rem = int((deadline - _time.monotonic()) * 1000)
            return max(0, min(cap_ms, rem))

        # Substring match on the placeholder (avoids a "..." vs "…" exact-match
        # break); the header search input is present on every route.
        input_sel = 'input[placeholder*="Search any title"]'

        # Step 1: land on the homepage (reuse the warm page). domcontentloaded
        # is enough — we wait for the specific input next, not full load.
        try:
            page.goto(
                "https://comix.to/",
                wait_until="domcontentloaded",
                timeout=_remaining_ms(15000) or 1,
            )
        except Exception as e:
            print(
                f"[!] Comix search: homepage nav failed "
                f"({type(e).__name__}: {e}); no comix results this run.",
                flush=True,
            )
            return []

        # Step 2: focus the search input. Desktop header shows it directly at
        # the default 1280x720 viewport; a .search-toggle click is the rare
        # collapsed/mobile fallback.
        try:
            page.wait_for_selector(
                input_sel, state="visible", timeout=_remaining_ms(5000) or 1,
            )
        except Exception:
            try:
                page.click(".search-toggle", timeout=_remaining_ms(3000) or 1)
                page.wait_for_selector(
                    input_sel, state="visible",
                    timeout=_remaining_ms(5000) or 1,
                )
            except Exception as e:
                print(
                    f"[!] Comix search: search input never became visible "
                    f"({type(e).__name__}: {e}).",
                    flush=True,
                )
                return []

        # Step 3: type with REAL key events. A synthetic value-set +
        # dispatch('input') was verified NOT to trigger comix's typeahead (it
        # keys off actual keydown/keyup), so page.type with a per-char delay is
        # load-bearing, not cosmetic. Clear first — the warm page may carry a
        # prior value.
        try:
            page.click(input_sel, timeout=_remaining_ms(3000) or 1)
            page.fill(input_sel, "")
            page.type(input_sel, clean, delay=25)
        except Exception as e:
            print(
                f"[!] Comix search: typing the query failed "
                f"({type(e).__name__}: {e}).",
                flush=True,
            )
            return []

        # Step 4: wait for the dropdown to render >=1 result anchor. A timeout
        # here is BOTH "no matches for this query" AND a CF/render miss —
        # indistinguishable, so treat both as [] (drop comix from this search)
        # after one CF-sniff diagnostic. Never raise.
        try:
            page.wait_for_selector(
                ".search-pop__item-link", state="visible",
                timeout=_remaining_ms(10000) or 1,
            )
        except Exception:
            try:
                body_text = page.evaluate(
                    "document.body ? document.body.innerText.slice(0, 300) : ''"
                ) or ""
                # Name the actual cause. Search deliberately does NOT raise or
                # trigger the interactive handoff (see ComixSiteHandler.search:
                # a raise would poison the orchestrator's persistent
                # ProbeFailureCache and blocklist comix.to for an hour, and
                # popping a window mid-search would interrupt a cross-site
                # search the user is watching). Reporting it accurately is the
                # whole win here — this used to read as "no results", which
                # looks like the series simply isn't on comix.
                waf_url = ""
                try:
                    waf_url = page.url or ""
                except Exception:
                    pass
                if _looks_like_waf_challenge(waf_url, body_text):
                    print(
                        f"[!] Comix search: skipped {clean!r} — comix.to is "
                        f"showing its human-verification check. Run a comix "
                        f"download (or open comix.to in a browser) and pass it "
                        f"once; the session is saved and search will work "
                        f"again.",
                        flush=True,
                    )
                    return []
                cf_msg = ""
                if _CF_AVAILABLE:
                    try:
                        if is_cf_challenge(200, body_text):
                            cf_msg = " — looks like a Cloudflare challenge"
                    except Exception:
                        pass
                print(
                    f"[*] Comix search: no typeahead results for {clean!r} "
                    f"within budget{cf_msg} (no match, or render/CF miss).",
                    flush=True,
                )
            except Exception:
                pass
            return []

        # Step 5: one evaluate over the rendered anchors. hid parsed from the
        # /title/{hid}-{slug} href (segment before the first '-'); dedup by hid;
        # cap at `limit`. Pure DOM read — no interpolation, so no json.dumps.
        scrape_js = """(limit) => {
            const out = [];
            const seen = new Set();
            const links = document.querySelectorAll('a.search-pop__item-link');
            for (const a of links) {
                const href = a.getAttribute('href') || '';
                const m = href.match(/\\/title\\/([^\\/?#-]+)/);
                if (!m) continue;
                const hid = m[1];
                if (seen.has(hid)) continue;
                const titleEl = a.querySelector('.search-pop__item-title');
                const title = titleEl ? titleEl.textContent.trim() : '';
                if (!title) continue;
                seen.add(hid);
                const imgEl = a.querySelector('.search-pop__thumb img');
                const cover = imgEl ? (imgEl.getAttribute('src') || '') : '';
                const typeEl = a.querySelector('.search-pop__type');
                const type = typeEl ? typeEl.textContent.trim() : '';
                const subEl = a.querySelector('.search-pop__item-sub');
                const sub = subEl ? subEl.textContent.trim() : '';
                out.push({hid, title, cover, type, sub});
                if (out.length >= limit) break;
            }
            return out;
        }"""
        try:
            rows = page.evaluate(scrape_js, int(limit)) or []
        except Exception as e:
            print(
                f"[!] Comix search: DOM scrape of the dropdown failed "
                f"({type(e).__name__}: {e}).",
                flush=True,
            )
            return []

        print(
            f"[*] Comix search: {len(rows)} typeahead result(s) for {clean!r}.",
            flush=True,
        )
        return rows

    def fetch_chapters_via_dom(
        self,
        title_url: str,
        max_pages: int = 0,
        time_budget_s: float = 0.0,
        chapter_floor: Optional[float] = None,
    ) -> List[Dict]:
        """Paginate the title page in the persistent browser and scrape chapter
        rows from the rendered DOM. The JSON API can't be used: it is
        per-request HMAC-signed AND returns an encrypted body, and the
        signature covers the query string (verified 2026-08-02: replaying a
        valid token with `limit=200` instead of `limit=20` → 403 "Invalid
        token"), so we can neither forge a call nor widen the page size. The
        chapter rows are also absent from the SSR HTML — `?page=5` returns
        byte-identical HTML to `?page=1`. The browser is genuinely required.

        Returns API-item-shaped dicts so the handler's existing per-item
        processing loop (chap_str normalization, lenient language filter,
        URL construction, group extraction) keeps working unchanged. The
        only field that's intentionally None is `language` — comix's DOM
        doesn't surface a per-row language attribute, and the title-page
        URL implicitly already filters to whichever language the user
        landed on; the lenient filter treats None as "keep" anyway.

        REWRITTEN 2026-08-02 around the site's own pager (`.npager`). The old
        loop navigated `?page=N` and treated an empty scrape as end-of-list:

            if not rows: break     # silent on every page after the first

        That cannot distinguish "past the last page" from "the render hadn't
        finished" or "a WAF interstitial is in front of us", and rows are per
        (chapter x group) so a 4-group series only yields ~5 distinct chapters
        per 20-row page. Live failure it caused: Magic Emperor has 360 pager
        pages; the scrape stopped at page 5, collected 78 rows = 20 distinct
        chapters, and the download started at chapter 871 instead of 1 — with
        no warning at all.

        Three rules now hold:
          1. The pager is GROUND TRUTH. One click on "Last page" reports the
             real total up front (verified: 360 unfiltered, 59 with
             ?group_id=), which becomes both the loop bound and the completion
             test. `.npager__num.is-active` is the freshness signal, replacing
             the old first-row-href diff that returned instantly on stale DOM
             during the React swap.
          2. An empty page is a FAILURE, not an ending, unless the pager says
             we're on the last page. It retries once, then raises.
          3. Pagination is CLIENT-SIDE ("Next page" clicks), not a full
             `page.goto` per page. 360 SPA cold boots per series was both slow
             and the most bot-like thing this handler did — the WAF fires on
             behavior, so this directly reduces how often we get challenged.

        Raises ComixChapterScrapeError / ComixWafChallengeError rather than
        returning a short list. A truncated list is worse than an error: it is
        persisted to .aio_series.json as though it were the whole series, so
        every later update run inherits the truncation.

        ``chapter_floor`` is the advisory early-stop from
        BaseSiteHandler.chapter_floor_hint: comix sorts newest-first, so an
        update run that only wants chapters above N stops as soon as a whole
        page falls below N. None = walk to the end.

        ``max_pages`` / ``time_budget_s`` default to 0 = "derive from the
        pager". Explicit non-zero values still cap, for callers that want one.
        """
        if not self._start():
            return []
        self._sync_cf_cookies()
        import time as _time
        # Selectors mirror the DOM probe done during the merge research:
        # `.mchap-item` is the <li> row, `.mchap-row__primary` is the chapter
        # link, `.mchap-row__ch` holds "Ch.<num>", `.mchap-row__title` is the
        # chapter title, `.mchap-row__group` is the scanlation group anchor
        # (with `.is-official` for official publishers). Cross-file: grep
        # `mchap-` in this file's history for the probe context.
        scrape_js = """() => {
            return Array.from(document.querySelectorAll('.mchap-item')).map(li => {
                const a = li.querySelector('.mchap-row__primary');
                const ch = li.querySelector('.mchap-row__ch');
                const ti = li.querySelector('.mchap-row__title');
                const gp = li.querySelector('.mchap-row__group');
                const lk = li.querySelector('.mchap-row__likes');
                return {
                    href: a ? a.getAttribute('href') : null,
                    chap_label: ch ? ch.textContent.trim() : null,
                    title: ti ? ti.textContent.trim() : null,
                    group: gp ? (gp.querySelector('span') ? gp.querySelector('span').textContent.trim() : gp.textContent.trim()) : null,
                    group_official: gp ? gp.classList.contains('is-official') : false,
                    likes: lk ? parseInt((lk.textContent.match(/\\d+/) || ['0'])[0]) : 0,
                };
            });
        }"""
        # Preserve any existing query (notably ?group_id= appended by
        # get_chapters) and add page=N to it. The old code did
        # `title_url.split("?", 1)[0]`, which silently discarded the user's
        # group filter — the difference between 59 and 360 pages to walk.
        base_url, _sep, base_query = title_url.partition("?")

        def _page_url(n: int) -> str:
            prefix = f"{base_query}&" if base_query else ""
            return f"{base_url}?{prefix}page={n}"

        # ── Pager helpers. `.npager` is the site's OWN pagination control and
        # the only trustworthy statement of how many pages exist. Reading it is
        # what lets an empty row list be treated as a failure rather than as
        # end-of-list — the conflation that truncated Magic Emperor at page 5
        # of 360. Shape verified live 2026-08-02: windowed `.npager__num`
        # buttons, `.is-active` on the current one, and First/Previous/Next/Last
        # buttons identified by aria-label.
        pager_js = """() => {
            const nav = document.querySelector('.npager');
            if (!nav) return null;
            const values = [];
            let active = null;
            for (const b of nav.querySelectorAll('.npager__num')) {
                const v = parseInt((b.textContent || '').trim(), 10);
                if (!Number.isFinite(v)) continue;
                values.push(v);
                if (b.classList.contains('is-active')) active = v;
            }
            const enabled = (label) => {
                const b = Array.from(nav.querySelectorAll('button')).find(
                    x => (x.getAttribute('aria-label') || '').toLowerCase() === label);
                return !!b && !b.disabled;
            };
            return {
                active: active,
                max: values.length ? Math.max.apply(null, values) : null,
                hasNext: enabled('next page'),
                hasLast: enabled('last page'),
            };
        }"""

        def _read_pager() -> Optional[Dict[str, Any]]:
            try:
                return self._page.evaluate(pager_js)
            except Exception:
                return None

        def _click_pager(aria_label: str) -> bool:
            """Click a pager button by aria-label. Client-side route change —
            no document reload, which is both ~5x faster than page.goto and far
            less bot-like (360 SPA cold boots per series was the most
            challenge-provoking thing this handler did)."""
            js = """(label) => {
                const nav = document.querySelector('.npager');
                if (!nav) return false;
                const b = Array.from(nav.querySelectorAll('button')).find(
                    x => (x.getAttribute('aria-label') || '').toLowerCase() === label);
                if (!b || b.disabled) return false;
                b.click();
                return true;
            }"""
            try:
                return bool(self._page.evaluate(js, aria_label))
            except Exception:
                return False

        def _rows_rendered() -> int:
            try:
                return int(
                    self._page.evaluate(
                        "document.querySelectorAll('.mchap-item').length"
                    ) or 0
                )
            except Exception:
                return 0

        def _wait_for_pager(check, timeout_s: float = 15.0) -> Optional[Dict[str, Any]]:
            """Poll the pager until `check(state)` holds; return that state or
            None on timeout.

            Needed because "rows are on screen" says nothing about WHICH page
            they belong to — comix swaps row content in place, so the previous
            page's rows outlive a click. Callers therefore assert on pager
            state (active number, Next availability) rather than on rows.
            """
            end = _time.monotonic() + timeout_s
            while _time.monotonic() < end:
                state = _read_pager()
                if state:
                    try:
                        if check(state):
                            return state
                    except Exception:
                        pass
                try:
                    self._page.wait_for_timeout(250)
                except Exception:
                    break
            return None

        def _wait_for_page(expected: Optional[int], timeout_s: float = 15.0) -> bool:
            """Block until rows are rendered AND (when known) the pager reports
            `expected` as the active page.

            The active-page number replaces the old first-row-href diff. That
            diff was unreliable in exactly the case that mattered: comix's React
            list swaps row CONTENT without unmounting, so the previous page's
            nodes survive the navigation and an href comparison can pass on
            stale DOM — or, on a slow render, time out and leave the caller
            scraping an empty list that it then read as end-of-list.
            """
            end = _time.monotonic() + timeout_s
            while _time.monotonic() < end:
                if _rows_rendered() > 0:
                    if expected is None:
                        return True
                    state = _read_pager()
                    # No pager at all = single-page series; rows are enough.
                    # Otherwise demand an EXACT active-page match: accepting a
                    # transient active=None would let a mid-render read pass on
                    # the previous page's still-mounted rows, which is the
                    # stale-DOM failure this whole rewrite exists to kill.
                    if state is None:
                        return True
                    if state.get("active") == expected:
                        return True
                try:
                    self._page.wait_for_timeout(250)
                except Exception:
                    break
            return False

        def _waf_guard(stage: str, page_num: int = 1) -> None:
            # page_num matters: _enforce_no_waf restores the page it is given
            # after a successful handoff, and handing it page 1 from a
            # mid-pagination call site would silently rewind the walk to the
            # start while the loop counter still said page N.
            self._enforce_no_waf(stage, _page_url(page_num))

        items: List[Dict] = []
        seen_ids: set = set()

        # ── Page 1 lands via a real navigation; every later page is a pager
        # click on the already-loaded SPA.
        try:
            self._page.goto(
                _page_url(1), wait_until="domcontentloaded", timeout=30000
            )
        except Exception as e:
            raise ComixChapterScrapeError(
                f"comix chapter list: could not load {_page_url(1)} "
                f"({type(e).__name__}: {e})."
            ) from e
        _waf_guard("the chapter list")
        if not _wait_for_page(None, 20.0):
            # Retry the navigation once — a cold SPA boot behind a slow network
            # can exceed 20s, and this is cheap next to failing the run.
            try:
                self._page.goto(
                    _page_url(1), wait_until="domcontentloaded", timeout=30000
                )
            except Exception:
                pass
            _waf_guard("the chapter list")

        if _rows_rendered() == 0:
            # Rich diagnostics before failing: a sandboxed Chromium can
            # masquerade a CF challenge or a renamed selector as an empty
            # series, and these three facts distinguish them.
            detail = ""
            try:
                page_title = self._page.title() or "(no title)"
                page_url = self._page.url
                body_text = self._page.evaluate(
                    "document.body ? document.body.innerText.slice(0, 500) : ''"
                ) or ""
                snippet = body_text.replace("\n", " ").strip()
                primary_count = self._page.evaluate(
                    "document.querySelectorAll('.mchap-row__primary').length"
                )
                cf_msg = ""
                if _CF_AVAILABLE:
                    try:
                        if is_cf_challenge(200, body_text):
                            cf_msg = " Looks like a Cloudflare challenge."
                    except Exception:
                        pass
                detail = (
                    f" title={page_title!r} url={page_url!r} "
                    f".mchap-row__primary={primary_count}.{cf_msg} "
                    f"Visible text: {snippet[:300]}"
                )
                if primary_count:
                    detail += (
                        " NOTE: chapter links exist but no `.mchap-item` rows — "
                        "comix likely renamed the row container; update the "
                        "selectors in fetch_chapters_via_dom."
                    )
            except Exception:
                pass
            raise ComixChapterScrapeError(
                "comix chapter list: no chapter rows rendered on page 1." + detail
            )

        # ── Total page count, straight from the pager. One "Last page" click
        # is the cheapest possible way to learn it (verified: 360 unfiltered /
        # 59 with group_id on Magic Emperor), and it converts the whole loop
        # from "walk until something looks like the end" to a bounded,
        # verifiable traversal.
        pager = _read_pager()
        total_pages = 1
        if pager:
            # Floor from what's already visible. The pager window only shows a
            # few numbers, so this is a LOWER bound, never the answer — but it
            # guarantees we can never conclude "1 page" when the DOM plainly
            # shows more, which is how the first cut of this code still
            # truncated the list.
            total_pages = max(total_pages, int(pager.get("max") or 1))
            if pager.get("hasLast"):
                # A live Last button PROVES pages exist beyond the visible
                # window, so the window maximum is a lower bound and must never
                # be accepted as the total: doing so would walk ~5 pages of a
                # 360-page series and report it complete, which is exactly the
                # truncation this rewrite exists to kill. Determine the real
                # last page or fail — no fallback.
                determined: Optional[int] = None
                for attempt in (1, 2):
                    if _click_pager("last page"):
                        # Wait for the pager to actually LAND on the last page —
                        # "rows exist" is not enough. comix's React list swaps
                        # row content in place, so page 1's rows survive the
                        # click and a rows-only wait returns instantly on stale
                        # DOM reporting active=1. The last page has no Next.
                        last_state = _wait_for_pager(
                            lambda s: not s.get("hasNext") and bool(s.get("active")),
                            20.0,
                        )
                        if last_state and last_state.get("active"):
                            determined = int(last_state["active"])
                            break
                    if attempt == 1:
                        print(
                            "[!] Comix DOM scrape: could not read the last page "
                            "number from the pager; retrying.",
                            flush=True,
                        )
                        # Let a mid-render pager settle before the second try.
                        _wait_for_pager(lambda s: s.get("hasLast"), 5.0)
                if determined is None:
                    raise ComixChapterScrapeError(
                        "comix chapter list: the pager advertises more pages "
                        "than it displays, but the last-page number could not "
                        f"be read after two attempts (highest visible page: "
                        f"{total_pages}). Refusing to walk only the visible "
                        f"window — that would silently return a partial series."
                    )
                total_pages = max(total_pages, determined)
                # Back to the start. Fall back to a hard navigation if the
                # First button doesn't take.
                if not (_click_pager("first page") and _wait_for_page(1, 20.0)):
                    try:
                        self._page.goto(
                            _page_url(1),
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                    except Exception:
                        pass
                    _wait_for_page(1, 20.0)
                _waf_guard("the chapter list")
        if _rows_rendered() == 0:
            raise ComixChapterScrapeError(
                "comix chapter list: rows disappeared after reading the pager; "
                "the page state is unusable."
            )
        if max_pages and max_pages > 0:
            total_pages = min(total_pages, max_pages)
        if total_pages > _MAX_CHAPTER_SCRAPE_PAGES:
            print(
                f"[!] Comix DOM scrape: pager reports {total_pages} pages, "
                f"above the {_MAX_CHAPTER_SCRAPE_PAGES}-page sanity bound; "
                f"clamping.",
                flush=True,
            )
            total_pages = _MAX_CHAPTER_SCRAPE_PAGES

        # Budget scales with the now-KNOWN size instead of being a fixed guess
        # that a long series silently ran out of. ~1.5s per pager click plus
        # slack, floored at the historical 300s.
        budget = (
            time_budget_s
            if time_budget_s and time_budget_s > 0
            else max(300.0, total_pages * 1.5 + 30.0)
        )
        started_at = _time.monotonic()
        deadline = started_at + budget
        print(
            f"[*] Comix DOM scrape: {total_pages} page(s) of chapter rows to "
            f"walk (budget {int(budget)}s).",
            flush=True,
        )

        page_n = 0
        while True:
            page_n += 1
            try:
                rows = self._page.evaluate(scrape_js) or []
            except Exception as e:
                raise ComixChapterScrapeError(
                    f"comix chapter list: DOM scrape failed on page {page_n} "
                    f"of {total_pages} ({type(e).__name__}: {e})."
                ) from e
            if not rows:
                # An empty page is NOT end-of-list. This is precisely the bug
                # that made an 890-chapter series start downloading at chapter
                # 871: the old code did a bare `break` here, silently, on every
                # page after the first. The pager knows whether more pages
                # exist, so ask it instead of guessing.
                on_last_page = page_n >= total_pages
                _waf_guard(f"chapter list page {page_n}", page_n)
                if not on_last_page:
                    # Re-navigate this page directly and try once more; a slow
                    # SPA render is the common benign cause.
                    print(
                        f"[!] Comix DOM scrape: page {page_n} of {total_pages} "
                        f"rendered no rows; retrying it.",
                        flush=True,
                    )
                    try:
                        self._page.goto(
                            _page_url(page_n),
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                    except Exception:
                        pass
                    _waf_guard(f"chapter list page {page_n}", page_n)
                    _wait_for_page(page_n, 20.0)
                    try:
                        rows = self._page.evaluate(scrape_js) or []
                    except Exception:
                        rows = []
                if not rows:
                    if on_last_page:
                        # Genuinely nothing on the final page. Benign.
                        break
                    raise ComixChapterScrapeError(
                        f"comix chapter list: page {page_n} of {total_pages} "
                        f"rendered no chapter rows even after a retry. The list "
                        f"would have been truncated at {len(items)} chapter(s) "
                        f"— refusing to return a partial series (a short list "
                        f"gets persisted as if it were the whole thing)."
                    )
            # Progress heartbeat so a long scrape doesn't look hung. stderr
            # keeps stdout clean for --search-json consumers.
            if page_n % 20 == 0:
                elapsed = int(_time.monotonic() - started_at)
                print(
                    f"[*] Comix DOM scrape: page {page_n}/{total_pages}, "
                    f"{len(items)} unique chapters so far ({elapsed}s elapsed).",
                    flush=True,
                )
            page_added = 0
            for row in rows:
                href = row.get("href")
                if not href:
                    continue
                # Parse `/title/{slug}/{chap_id}-chapter-{chap_num}` —
                # chap_id is digits, chap_num is the rest (allows .5/.1 etc).
                m = re.match(r".*/title/[^/]+/(\d+)-chapter-(.+)$", href)
                if not m:
                    continue
                chap_id_str, chap_num_str = m.group(1), m.group(2)
                if chap_id_str in seen_ids:
                    continue
                seen_ids.add(chap_id_str)
                # Absolutize URL — comix anchors are href-relative on the page.
                chap_url = href if href.startswith("http") else ("https://comix.to" + href)
                # Coerce chap_num to int/float where possible so the handler's
                # `isinstance(chap_num, (int, float))` branch hits the fast
                # %g formatter; non-numeric specials fall through to the
                # regex-extract branch (handles oneshots / "1.5" / etc).
                num_val: Any = chap_num_str
                try:
                    fv = float(chap_num_str)
                    num_val = int(fv) if fv.is_integer() else fv
                except ValueError:
                    pass
                items.append({
                    "id": int(chap_id_str),
                    "number": num_val,
                    "name": row.get("title") or row.get("chap_label"),
                    "url": chap_url,
                    "group": {"name": row.get("group")} if row.get("group") else None,
                    "votes": row.get("likes") or 0,
                    # Language is unknown from the DOM — the lenient filter
                    # in get_chapters keeps `None` items, matching the
                    # "untagged items shouldn't be silently dropped" rule
                    # ported from upstream.
                    "language": None,
                })
                page_added += 1

            # Canary: with pager-driven navigation every page should contribute
            # new rows. An all-duplicate page means the click didn't actually
            # advance the list (stale render) — under the OLD code that state
            # silently ended the scrape, so keep it loud even though the
            # active-page wait should now prevent it.
            if page_added == 0 and rows:
                print(
                    f"[!] Comix DOM scrape: page {page_n}/{total_pages} "
                    f"returned {len(rows)} row(s) but none were new — the "
                    f"pager may not have advanced.",
                    flush=True,
                )

            # ── Early stop on the advisory chapter floor. comix sorts
            # newest-first, so once a whole page sits below the floor every
            # remaining page does too. Uses the page MAXIMUM (not the minimum)
            # so a row out of order can't cut the walk short, which makes this
            # exact rather than heuristic — no grace margin needed.
            if chapter_floor is not None:
                page_numbers = [
                    n for n in (
                        _coerce_chapter_number(r.get("href"), r.get("chap_label"))
                        for r in rows
                    ) if n is not None
                ]
                if page_numbers and max(page_numbers) < float(chapter_floor):
                    print(
                        f"[*] Comix DOM scrape: page {page_n}/{total_pages} is "
                        f"entirely below the requested floor "
                        f"(ch.{float(chapter_floor):g}); stopping early with "
                        f"{len(items)} chapter(s).",
                        flush=True,
                    )
                    break

            # ── Advance. Termination is the PAGER's word, not a heuristic.
            if page_n >= total_pages:
                break
            if _time.monotonic() > deadline:
                raise ComixChapterScrapeError(
                    f"comix chapter list: ran out of time ({int(budget)}s) at "
                    f"page {page_n} of {total_pages} with {len(items)} "
                    f"chapter(s) collected. Refusing to return a partial "
                    f"series — narrow the range with --chapters, or pass a "
                    f"?group_id= URL to cut the list to one scanlation group."
                )
            next_page = page_n + 1
            # The pager click is an OPTIMIZATION (client-side route change, no
            # document reload), never the only way forward — a mid-render pager
            # can briefly have no usable Next button, and letting that end the
            # scrape would resurrect the truncation bug in a new disguise. So:
            # click, else wait for the button and click, else hard-navigate.
            advanced = False
            if _click_pager("next page") or (
                _wait_for_pager(lambda s: s.get("hasNext"), 5.0)
                and _click_pager("next page")
            ):
                advanced = _wait_for_page(next_page, 20.0)
            if not advanced:
                try:
                    self._page.goto(
                        _page_url(next_page),
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                except Exception:
                    pass
                _waf_guard(f"chapter list page {next_page}", next_page)
                advanced = _wait_for_page(next_page, 20.0)
            if not advanced:
                raise ComixChapterScrapeError(
                    f"comix chapter list: could not reach page {next_page} of "
                    f"{total_pages} ({len(items)} chapter(s) collected so far) "
                    f"— neither the pager's Next button nor a direct navigation "
                    f"landed on it. Refusing to return a partial series."
                )

        # Final tally, always emitted so the caller's "fetching chapter list"
        # line has a matching completion line. Now reports pages walked vs the
        # true total, which is what makes a truncation visible at a glance.
        print(
            f"[*] Comix DOM scrape: complete. {len(items)} chapter(s) "
            f"collected across {page_n}/{total_pages} page(s).",
            flush=True,
        )
        return items


    def fetch_chapter_images_via_dom(
        self,
        chapter_url: str,
        time_budget_s: float = 300.0,
        max_capture_pages: Optional[int] = None,
    ) -> list:
        """Capture chapter pages by scrolling each .rpage-page into view and
        reading the rendered element one at a time.

        Why the browser at all: the chapter page is an SPA whose page list
        comes from a signed + encrypted /api/v1/chapters/{id} ({"e":...})
        response that only the in-page JS can decrypt — Python can neither sign
        nor decrypt it — and the <img> src is lazy-set per page as it nears the
        viewport (not in #initial-data or any data-* attribute). So we let the
        browser render and scrape the DOM.

        Two page shapes, checked in order (see the per-page poll below):
          - <img> (the NORMAL path as of 2026-07-11): comix dropped the old
            server-side tile-scramble, so pages are plain, directly-fetchable
            webp CDN URLs. Use img.src verbatim; aio-dl.py:dl_image fetches it
            (and the session-level page.on("response") listener already cached
            its bytes as it loaded, so that's usually a memory hit).
          - <canvas> (LEGACY fallback): kept in case comix re-enables the tile
            scramble it used through mid-2026 (webp shipped with x-scramble-seed
            / x-scramble-grid headers, unscrambled in-JS onto a canvas). Read
            the pixels via canvas.toDataURL and stash the bytes in image_cache
            under a synthetic comix-page://<chap_id>/<NNNN>.webp key so dl_image
            serves them without any HTTP fetch (the real /si/ URL would return
            the scrambled bytes). Costs nothing when no canvas is present.

        Flow:
          1. Pre-flight: visit comix.to once to set localStorage
             `reader.default.preload = 'all'` so the reader renders eagerly.
          2. Navigate to the chapter URL; wait for the React app to mount and
             populate one .rpage-page <div> per page (= the page count).
          3. For each page 1..N: scrollIntoView (triggers the lazy load), poll
             up to 10 s for a rendered <img>/<canvas>, collect the URL/key.

        Cross-file: called from ComixSiteHandler.get_chapter_images via
        _COMIX_BROWSER_BRIDGE.fetch_chapter_images_via_dom; image_cache
        populated here is read by aio-dl.py:dl_image. Runs on the comix-pw
        daemon worker per the bridge's same-thread Patchright contract.
        """
        if not self._start():
            return []
        self._sync_cf_cookies()
        import base64 as _b64
        import re as _re
        import time as _time

        page = self._page
        if page is None:
            return []

        try:
            from . import image_cache as _image_cache
            # No clear_cache() here. The image-prefetch chain in
            # aio-dl.py runs scrape N+1 while chapter N's
            # downloader is still draining cached bytes for N, so
            # a clear would wipe still-needed entries and force
            # dl_image to fall through to HTTP (where the signed
            # CDN tokens may have already expired). Eviction is
            # TTL- and size-based — see sites/image_cache.py.
        except Exception:
            _image_cache = None

        m_id = _re.search(r"/(\d+)-chapter-\d+", chapter_url or "")
        chap_id = m_id.group(1) if m_id else "unknown"

        deadline = _time.monotonic() + time_budget_s

        # ── Step 1: set preload=all in localStorage on the comix.to
        # origin. Localstorage is per-origin so we navigate to the
        # homepage first (cheap because we already have CF cookies).
        # If this fails we still proceed — the per-page scrollIntoView
        # loop below works without preload-all, just slower.
        try:
            page.goto(
                "https://comix.to/",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            page.evaluate("""() => {
                try {
                    const k = 'reader.default';
                    const cur = JSON.parse(localStorage.getItem(k) || '{}');
                    cur.preload = 'all';
                    localStorage.setItem(k, JSON.stringify(cur));
                } catch (e) {}
            }""")
        except Exception as e:
            print(
                f"[*] Comix: localStorage preload-all setup failed "
                f"({type(e).__name__}: {e}); continuing with default "
                f"preload setting.",
                flush=True,
            )

        # ── Step 2: navigate to chapter and wait for .rpage-page divs.
        try:
            page.goto(
                chapter_url,
                wait_until="domcontentloaded",
                timeout=30000,
            )
        except Exception as e:
            print(
                f"[!] Comix chapter image canvas scrape: nav failed for "
                f"{chapter_url}: {type(e).__name__}: {e}",
                flush=True,
            )
            return []

        # The WAF fires on behavior, and a chapter render is the heaviest thing
        # we do (one navigation plus ~40-80 image loads), so this is where it
        # most often lands. Without the check the interstitial just produced
        # "0 .rpage-page divs" below, which reads as a broken chapter.
        self._enforce_no_waf("the chapter page", chapter_url)

        # Wait for the React app to mount and the chapter API to fire,
        # which populates .rpage-page divs. Poll up to 30 s — most
        # chapters mount in 3-8 s but the CF turnstile / slow networks
        # can push that out.
        page_count = 0
        for _ in range(60):
            if _time.monotonic() > deadline:
                break
            try:
                page_count = page.evaluate(
                    "() => document.querySelectorAll('.rpage-page').length"
                ) or 0
            except Exception:
                page_count = 0
            if page_count > 0:
                break
            page.wait_for_timeout(500)

        if page_count == 0:
            # Re-check before blaming the render: a WAF redirect can land here
            # too (it may arrive after the initial navigation settled).
            self._enforce_no_waf("the chapter page", chapter_url)
            print(
                f"[!] Comix: chapter had 0 .rpage-page divs in DOM "
                f"after wait. Either the React app failed to mount or "
                f"CF re-challenged. URL={chapter_url}",
                flush=True,
            )
            return []

        print(
            f"[*] Comix: chapter has {page_count} pages; capturing "
            f"each via Patchright (<img> src, or canvas pixels if a page "
            f"is tile-scrambled).",
            flush=True,
        )

        # ── Step 3: per-page scroll + capture.
        # Per-page wait is capped at 10 s. Pages that don't render in
        # time are logged and skipped (very long chapters may still
        # come up; the user can retry with a longer time_budget_s).
        urls: list = []
        canvas_count = 0
        img_count = 0
        failed_pages: list = []

        for p in range(1, page_count + 1):
            if _time.monotonic() > deadline:
                print(
                    f"[!] Comix: hit time budget {time_budget_s:.0f}s "
                    f"at page {p}/{page_count} — returning what we have.",
                    flush=True,
                )
                break

            # Scroll the page's div into view. instant + center so the
            # IntersectionObserver fires immediately and the canvas
            # ends up vertically centered, helping the surrounding
            # pages preload too.
            try:
                page.evaluate(
                    "(n) => { const el = document.querySelector("
                    "'.rpage-page[data-page=\"' + n + '\"]'); "
                    "if (el) el.scrollIntoView("
                    "{behavior: 'instant', block: 'center'}); }",
                    p,
                )
            except Exception:
                pass

            # Poll for the page to be ready. The polling JS returns
            # either {type: canvas, ...} or {type: img, ...} once a
            # rendered child exists with non-zero dimensions and the
            # parent has shed the .is-loading class.
            ready = None
            for _attempt in range(40):  # 40 * 250ms = 10s
                if _time.monotonic() > deadline:
                    break
                try:
                    ready = page.evaluate(
                        "(n) => { "
                        "const el = document.querySelector("
                        "'.rpage-page[data-page=\"' + n + '\"]'); "
                        "if (!el) return null; "
                        "const isLoading = "
                        "el.classList.contains('is-loading'); "
                        "const c = el.querySelector('canvas'); "
                        "if (c && c.width > 0 && c.height > 0 "
                        "&& !isLoading) "
                        "return {type: 'canvas', w: c.width, h: c.height}; "
                        "const i = el.querySelector('img'); "
                        "if (i && i.src && i.complete "
                        "&& i.naturalWidth > 0) "
                        "return {type: 'img', src: i.src, "
                        "w: i.naturalWidth, h: i.naturalHeight}; "
                        "return null; }",
                        p,
                    )
                except Exception:
                    ready = None
                if ready:
                    break
                page.wait_for_timeout(250)

            if not ready:
                failed_pages.append(p)
                continue

            if ready.get("type") == "canvas":
                # Read canvas pixels. Use webp at q=0.95 — comparable
                # to the original (the source is already webp) and
                # smaller than PNG by a factor of 5-10x.
                try:
                    data_url = page.evaluate(
                        "(n) => { const c = document.querySelector("
                        "'.rpage-page[data-page=\"' + n + '\"] canvas'); "
                        "return c ? c.toDataURL('image/webp', 0.95) "
                        ": null; }",
                        p,
                    )
                except Exception as e:
                    print(
                        f"  page {p}: toDataURL threw "
                        f"{type(e).__name__}: {e}",
                        flush=True,
                    )
                    failed_pages.append(p)
                    continue
                if not data_url or not data_url.startswith("data:image/"):
                    failed_pages.append(p)
                    continue
                try:
                    _hdr, b64 = data_url.split(",", 1)
                    decoded = _b64.b64decode(b64)
                except Exception:
                    failed_pages.append(p)
                    continue
                # Synthetic URL key — comix's real /si/ URLs cannot
                # be re-fetched by cloudscraper (they'd return the
                # SCRAMBLED bytes, and we can't undo the scrambling
                # in Python). The cache hit short-circuits dl_image
                # before any HTTP work.
                synthetic_url = (
                    f"comix-page://{chap_id}/{p:04d}.webp"
                )
                if _image_cache is not None:
                    _image_cache.cache_image(
                        synthetic_url, decoded, "image/webp",
                    )
                urls.append(synthetic_url)
                canvas_count += 1
            else:
                # Plain image — non-scrambled. img.src is the real
                # CDN URL; cloudscraper can fetch it the normal way.
                urls.append(ready["src"])
                img_count += 1

            # Probe path: stop after the cap so a single-chapter quality
            # probe renders _COMIX_PROBE_PAGE_CAP pages, not the whole chapter
            # (see ComixSiteHandler._probe_chapter_aggregate). None (the
            # download path) never trips this — it captures every page.
            if max_capture_pages is not None and len(urls) >= max_capture_pages:
                break

        # Probe-capture path logs its own line (a capped "4/70" is success,
        # not the partial-failure the download summary below would imply) and
        # returns early — the failed_pages accounting is a download concern.
        if max_capture_pages is not None:
            print(
                f"[*] Comix probe capture: grabbed {len(urls)} page(s) "
                f"(cap {max_capture_pages}) of {page_count} for image-quality "
                f"sampling.",
                flush=True,
            )
            return urls

        # Final summary so the user knows the capture rate. Failed
        # pages aren't FATAL on their own — aio-dl.py:_process_chapter
        # will treat the chapter as incomplete and inline-retry, which
        # gives the reader another shot to render any laggards.
        if failed_pages:
            sample = ", ".join(str(p) for p in failed_pages[:10])
            more = (
                f" (+{len(failed_pages) - 10} more)"
                if len(failed_pages) > 10 else ""
            )
            print(
                f"[!] Comix canvas scrape: {len(urls)}/{page_count} "
                f"pages captured ({canvas_count} via canvas, "
                f"{img_count} via <img>). {len(failed_pages)} pages "
                f"failed to render in 10 s each: pages {sample}{more}.",
                flush=True,
            )
        else:
            print(
                f"[*] Comix canvas scrape: {len(urls)}/{page_count} "
                f"pages captured ({canvas_count} via canvas, "
                f"{img_count} via <img>). All pages rendered.",
                flush=True,
            )
        return urls



    def close(self):
        self._cleanup()


# v8 bridge rewrite (2026-05-24): replaced module-level ThreadPoolExecutor
# with a daemon thread + queue.Queue, mirroring sites/mangadex.py's report
# pipeline. The TPE approach had two latent failure modes that the
# code review surfaced:
#
#   1. INTERPRETER HANG AT EXIT. concurrent.futures._python_exit
#      registers with `threading._register_atexit` and runs BEFORE the
#      atexit module's hooks. It calls join() on every TPE worker
#      unconditionally — even after shutdown(wait=False, cancel_futures=True).
#      If Patchright nav was wedged on a Cloudflare turnstile spin at
#      Ctrl-C, the comix-pw worker stayed blocked in page.goto and
#      the user's process hung for up to 30s waiting for the goto's
#      own timeout. Same anti-pattern that sites/mangadex.py's daemon
#      rewrite explicitly addressed earlier in the same diff
#      (mangadex.py:41-58 comment).
#   2. CALLER DEADLOCK ON HUNG NAV. `fut.result()` had no timeout, so
#      any single hung Patchright op deadlocked every concurrent caller
#      submitting through the same single-worker executor (and there
#      IS only one worker — max_workers=1). The probe phase has 6 parallel
#      probe-pool workers all routing through this bridge; one comix
#      candidate getting stuck would freeze all six.
#
# Daemon thread + queue resolves both: daemons are skipped by _python_exit
# (clean Ctrl-C semantics), and the worker dequeues one job at a time so
# we can attach an explicit per-call timeout on fut.result() without
# changing the single-thread-owns-the-browser invariant. Bridge public
# API (_COMIX_BROWSER_BRIDGE) is unchanged so existing call sites in
# this file don't move.
_COMIX_REQUEST_QUEUE: queue.Queue = queue.Queue()
_COMIX_WORKER_STARTED = False
_COMIX_WORKER_LOCK = threading.Lock()
_COMIX_BROWSER: Optional[_ComixBrowserSession] = None  # owned by the worker thread
_COMIX_SHUTDOWN_SENTINEL = object()
# Per-call wall-clock cap on Patchright work. Real-world page.goto
# timeouts inside _ComixBrowserSession sit at 30s; the bridge cap is
# the sum of those plus a small slack so a legitimate slow nav still
# completes but a stuck one surfaces as TimeoutError rather than
# deadlocking the caller. Search-phase callers should also have their
# own outer deadline (PROBE_PHASE_DEADLINE_S in search_orchestrator);
# this is the inner guard.
_COMIX_DEFAULT_TIMEOUT_S = 60.0


def _comix_worker_loop() -> None:
    """Daemon thread that owns the single Patchright browser instance.

    Pulls (future, fn_name, args, kwargs) tuples and sets the future's
    result/exception. Exits cleanly on the shutdown sentinel. Lazy-inits
    the session singleton on the first non-sentinel job so import-time
    cost stays at zero for non-comix runs (sites/__init__.py imports
    this module eagerly so every aio-dl process touches these globals,
    but no Patchright launch happens until a user actually hits comix).
    """
    global _COMIX_BROWSER
    while True:
        item = _COMIX_REQUEST_QUEUE.get()
        if item is _COMIX_SHUTDOWN_SENTINEL:
            try:
                if _COMIX_BROWSER is not None:
                    try:
                        _COMIX_BROWSER.close()
                    except Exception:
                        pass
                    _COMIX_BROWSER = None
            finally:
                return
        try:
            fut, fn_name, args, kwargs = item
        except (TypeError, ValueError):
            # Malformed enqueue — skip without dying. Belt-and-suspenders
            # against future maintainers putting unexpected sentinels on
            # the queue (matches the mangadex worker's None-safe pattern).
            continue
        # Caller's fut.result(timeout=...) may have already given up and
        # the future could be cancelled; honor the cancel without doing
        # the work (avoids redundant Patchright nav for callers who
        # already moved on).
        if fut.cancelled():
            continue
        try:
            if _COMIX_BROWSER is None:
                _COMIX_BROWSER = _ComixBrowserSession()
            fn = getattr(_COMIX_BROWSER, fn_name)
            result = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — propagate to caller
            # Race: caller's fut.result(timeout=...) may have hit the
            # timeout and called fut.cancel() AFTER our cancelled-check
            # above but BEFORE we got here. set_exception raises
            # InvalidStateError on a cancelled future, which would kill
            # the worker thread. Suppress — the caller already moved on.
            try:
                fut.set_exception(exc)
            except _futures.InvalidStateError:
                pass
        else:
            try:
                fut.set_result(result)
            except _futures.InvalidStateError:
                # Same race as above, success path. Worker just discards
                # its result because the caller no longer cares.
                pass


def _ensure_comix_worker() -> None:
    """Lazy-start the single Patchright worker daemon. Double-checked
    locking so concurrent first-callers don't race to spawn duplicates.
    """
    global _COMIX_WORKER_STARTED
    if _COMIX_WORKER_STARTED:
        return
    with _COMIX_WORKER_LOCK:
        if _COMIX_WORKER_STARTED:
            return
        threading.Thread(
            target=_comix_worker_loop,
            name="comix-pw",
            daemon=True,
        ).start()
        _COMIX_WORKER_STARTED = True


def _comix_call(fn_name: str, *args, _timeout_s: float = _COMIX_DEFAULT_TIMEOUT_S, **kwargs):
    """Submit a session method call onto the daemon worker and block on
    its result, bounded by ``_timeout_s`` (default 60 s). Synchronous
    from the caller's perspective — same contract as the previous
    ThreadPoolExecutor-based implementation, but with an explicit
    wall-clock cap so a hung Patchright nav surfaces as TimeoutError
    instead of an indefinite deadlock.

    Per-call timeout can be overridden via the keyword `_timeout_s`
    (kw-only so it doesn't collide with method args). Cancellation
    after timeout sets the future cancelled; the worker honors the
    cancel and skips the underlying call if it hadn't started yet.
    """
    _ensure_comix_worker()
    fut: _futures.Future = _futures.Future()
    _COMIX_REQUEST_QUEUE.put((fut, fn_name, args, kwargs))
    try:
        return fut.result(timeout=_timeout_s)
    except _futures.TimeoutError:
        # Best-effort cancel so the worker can skip the call if it
        # hasn't started. If the worker is already executing this
        # future, set_running_or_notify_cancel returns False internally
        # and the underlying Patchright op continues (no thread
        # cancellation in Python) — but at least subsequent callers
        # aren't blocked behind a future we've stopped waiting for.
        fut.cancel()
        raise


class _ComixBrowserBridge:
    """Thread-safe facade over _ComixBrowserSession. Every method routes
    through _comix_call so the underlying Patchright calls always run
    on the daemon worker thread that owns the browser instance.

    Cross-file: mirrors sites/mangafire_vrf_simple.py:_VRFBridge in
    spirit; the v8 rewrite swaps the executor for daemon+queue (see
    block-comment near _COMIX_REQUEST_QUEUE for rationale).
    """

    def fetch_chapters_via_dom(
        self,
        title_url: str,
        max_pages: int = 0,
        time_budget_s: float = 0.0,
        chapter_floor: Optional[float] = None,
    ) -> List[Dict]:
        """Bridge facade for the chapter-list DOM scrape.

        The outer wall clock has to accommodate a scrape whose real size is
        only known once the pager has been read: a 360-page series at ~1.5s per
        pager click is ~9 minutes, and the inner method derives its own budget
        the same way. Passing 0 (the default) means "let the inner method size
        itself"; the outer cap then mirrors that formula with slack, plus room
        for an interactive WAF solve (default 180s) which can legitimately
        happen mid-scrape. The inner budget stays the load-bearing one.

        Cross-file: see _ComixBrowserSession.fetch_chapters_via_dom.
        """
        if time_budget_s and time_budget_s > 0:
            outer = time_budget_s + 30.0
        else:
            # Worst case the inner method could choose, plus solve headroom.
            # _MAX_CHAPTER_SCRAPE_PAGES bounds an unbounded pager.
            outer = _MAX_CHAPTER_SCRAPE_PAGES * 1.5 + 30.0
        outer += _WAF_DEFAULT_SOLVE_TIMEOUT_S + 60.0
        return _comix_call(
            "fetch_chapters_via_dom",
            title_url,
            max_pages,
            time_budget_s,
            chapter_floor,
            _timeout_s=outer,
        )

    def solve_waf_interactively(self, return_url: Optional[str] = None) -> Dict[str, Any]:
        """Bridge facade for the interactive WAF handoff.

        Timeout is the solve window plus generous slack for the two browser
        relaunches (headless → headed → headless) the handoff performs.
        Never raises: a bridge-level timeout degrades to "not solved" so the
        caller's own error path (which carries the user-facing remediation)
        runs instead of a bare TimeoutError.
        """
        try:
            solve_timeout = float(
                os.environ.get(_WAF_SOLVE_TIMEOUT_ENV)
                or _WAF_DEFAULT_SOLVE_TIMEOUT_S
            )
        except (TypeError, ValueError):
            solve_timeout = _WAF_DEFAULT_SOLVE_TIMEOUT_S
        try:
            return _comix_call(
                "solve_waf_interactively",
                return_url,
                _timeout_s=solve_timeout + 120.0,
            )
        except Exception:
            return {"solved": False, "cookies": [], "user_agent": None,
                    "reason": "bridge_error"}

    def fetch_search_via_dom(
        self,
        query: str,
        limit: int = 20,
        time_budget_s: float = 28.0,
    ) -> List[Dict]:
        """Bridge facade for the header-typeahead keyword search.

        Outer wall-clock cap is ``time_budget_s + 12`` — TIGHTER than the
        chapter scrapes' +30 because search BLOCKS search_all's cross-site
        fan-in (a slow comix becomes the long pole for the WHOLE search),
        whereas a chapter scrape only blocks its own download. The inner
        time_budget_s is the load-bearing bound; this is the outer safety net.
        Cross-file: _ComixBrowserSession.fetch_search_via_dom.
        """
        return _comix_call(
            "fetch_search_via_dom",
            query,
            limit,
            time_budget_s,
            _timeout_s=time_budget_s + 12.0,
        )

    def fetch_chapter_images_via_dom(
        self,
        chapter_url: str,
        time_budget_s: float = 300.0,
        max_capture_pages: Optional[int] = None,
    ) -> List[str]:
        """Bridge facade for chapter-page image capture.

        The chapter page's `/api/v1/chapters/{id}` response is signed +
        encrypted (`{"e": "..."}`) and Python can't reproduce/decrypt it, so
        drive the browser to render the chapter and scrape the page URLs. Pages
        are plain <img> webp CDN URLs now (comix dropped the tile-scramble); a
        legacy <canvas> branch remains as a fallback — see
        _ComixBrowserSession.fetch_chapter_images_via_dom for the details.

        Default 300 s budget covers ~126-page chapters; a typical chapter takes
        ~1-2 s per page (scroll + render wait). Bump this for chapters that
        exceed the budget. Inner deadline + 30 s outer cap matches
        fetch_chapters_via_dom. Populates sites/image_cache.py with page bytes;
        aio-dl.py:dl_image reads real CDN URLs from there, and canvas-captured
        pages via synthetic `comix-page://<chap_id>/<NNNN>.webp` keys.

        ``max_capture_pages`` (default None = every page, the download path)
        stops the render after that many pages — the image-quality probe
        (ComixSiteHandler._probe_chapter_aggregate) passes _COMIX_PROBE_PAGE_CAP
        so one chapter renders in ~5-15 s instead of minutes.
        """
        return _comix_call(
            "fetch_chapter_images_via_dom",
            chapter_url,
            time_budget_s,
            max_capture_pages,
            _timeout_s=time_budget_s + 30.0,
        )

    def close(self) -> None:
        try:
            _comix_call("close", _timeout_s=5.0)
        except Exception:
            pass


_COMIX_BROWSER_BRIDGE = _ComixBrowserBridge()


def _shutdown_comix_bridge():
    """At-exit best-effort cleanup. Daemon worker dies with the
    interpreter regardless (the whole reason for the daemon+queue
    rewrite), so the goal here is just to close the Patchright session
    cleanly when there's time. We enqueue the shutdown sentinel and
    rely on the daemon to drain — no join, no wait."""
    if not _COMIX_WORKER_STARTED:
        return
    try:
        _COMIX_REQUEST_QUEUE.put_nowait(_COMIX_SHUTDOWN_SENTINEL)
    except queue.Full:
        # The unbounded queue can't actually go full here; the except
        # is defensive belt-and-suspenders in case the queue is ever
        # given a maxsize. Silent drop matches the rest of the bridge's
        # at-exit semantics.
        pass


atexit.register(_shutdown_comix_bridge)
