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
from typing import Dict, List, NamedTuple, Optional, Any, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from .base import (
    BaseSiteHandler,
    GroupInfo,
    IncompleteChapterError,
    SearchHit,
    SiteComicContext,
)

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

# Cadence of the whole-chapter readiness poll in fetch_chapter_images_via_dom.
# Matches the 250ms the old per-page loop used; the difference is that ONE
# evaluate now reports every page instead of one page per round-trip.
_COMIX_PAGE_POLL_INTERVAL_S = 0.25

# How long the capture loop tolerates ZERO newly-resolved pages before it gives
# up on the stragglers. This replaced a per-page 10s window, and the distinction
# is the whole point: the old loop walked pages in order and gave each one a
# PRIVATE 10s slot, so a page that needed 11s was abandoned while the scrape ran
# on for six more pages with the browser holding it fully decoded (live: Spark in
# Your Eyes ch.104, page 62 of 68, silently dropped). Progress anywhere in the
# chapter now keeps every unresolved page alive, and the budget is spent ONCE at
# the end rather than mid-walk. Still bounded by the caller's time_budget_s.
_COMIX_CAPTURE_STALL_S = 20.0

# Response headers that mark a page as server-side tile-scrambled. comix shipped
# these through mid-2026 and they are the AUTHORITATIVE scramble signal — DOM
# shape is not: the readiness poll checks <canvas> before <img>, so a transient
# canvas would win the race on a perfectly ordinary page and get re-encoded
# through toDataURL for nothing. Recorded (not acted on) so one live run can
# settle whether the canvas branch is still earning its keep — see
# _ComixBrowserSession._capture_image_response and the "capture shapes" summary.
_COMIX_SCRAMBLE_HEADER_PREFIX = "x-scramble-"


class ComixChapterCapture(NamedTuple):
    """Result of one chapter-page DOM capture.

    ``expected_pages`` is the load-bearing field and the reason this is not a
    bare list: aio-dl.py's zero-tolerance gate computes pages_total from the
    length of what the handler RETURNS (grep 'pages_total = len(download_tasks)'),
    so a handler that quietly drops a page reports N/N and sails through the
    completeness check. Carrying the site's own page count lets
    ComixSiteHandler.get_chapter_images raise IncompleteChapterError instead —
    the same signalling path sites/mangadex.py uses.

    ``canvas_pages``/``img_pages``/``canvas_with_img`` feed the diagnostic
    summary only; ``unresolved`` maps page number → last observed DOM state for
    the failure log (None, not {}, because a NamedTuple default is created ONCE
    and shared by every instance — a mutable one would be a cross-capture leak).
    """

    urls: List[str]
    expected_pages: int
    canvas_pages: int = 0
    img_pages: int = 0
    canvas_with_img: int = 0
    scrambled_responses: int = 0
    unresolved: Optional[Dict[int, Any]] = None


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
# Handoff budget. The first cut of this was a single boolean "already
# attempted", which counted SUCCESSES against the allowance and broke a real
# run: the HTTP metadata request tripped the WAF, the user solved it, and then
# the browser's own chapter-list navigation was challenged separately — but the
# allowance was already spent, so the second challenge was never surfaced and
# the run died claiming the check "was not completed" when it had never asked.
#
# Budget the two outcomes separately instead:
#   - successful solves are cheap for the user and legitimately needed more than
#     once per run (the cloudscraper session and the browser are distinct
#     identities to the WAF), so allow a few;
#   - a FAILED prompt (timed out, window closed) means the user declined, so
#     stop asking immediately rather than nagging.
# The interval floor stops a pathological series from popping windows back to
# back. Grep _waf_budget_state for the consumer.
_COMIX_WAF_MAX_SOLVES = 3
_COMIX_WAF_MAX_FAILURES = 1
# Hard depth bound on _enforce_no_waf's solve -> reload -> re-check cycle, kept
# independent of the prompt budget above so termination is provable locally.
_WAF_MAX_ENFORCE_PASSES = 2
_COMIX_WAF_MIN_PROMPT_INTERVAL_S = 20.0
_COMIX_WAF_SOLVES_DONE = 0
_COMIX_WAF_FAILURES = 0
_COMIX_WAF_LAST_PROMPT_AT = 0.0
_COMIX_WAF_HANDOFF_LOCK = threading.Lock()

# Cached "stable" User-Agent for the Patchright profile.
#
# THE reason a solved check didn't stick: headless Chromium advertises
# `HeadlessChrome/147.0.7727.15` while the headed window advertises
# `Chrome/147.0.0.0` (measured 2026-08-02). The WAF binds its clearance to the
# UA that earned it, so a clearance obtained in the headed handoff window was
# rejected the moment the relaunched headless context presented a different UA —
# the user solved the check and was immediately re-challenged.
#
# Pinning ONE UA across both modes fixes that, and independently removes the
# `HeadlessChrome` token, which is about the loudest bot signal a client can
# emit and is very likely part of why the check fires "sometimes" at all.
#
# Cached in the profile dir so only the first run in a fresh profile pays the
# probe-and-relaunch; grep _resolve_stable_user_agent.
_UA_CACHE_FILENAME = "aio-stable-ua.txt"
_COMIX_STABLE_UA: Optional[str] = None
_COMIX_UA_LOCK = threading.Lock()


def _stabilize_user_agent(raw: Optional[str]) -> Optional[str]:
    """Return *raw* with the headless giveaway removed.

    Only rewrites the product token — the version string and everything else
    stay exactly as the real browser reports them, so the result is still a
    truthful description of the engine actually making the request.

    NOT SUFFICIENT ON ITS OWN — see _COMIX_BROWSER_CHANNEL. This rewrites the
    User-Agent STRING; the Sec-CH-UA client hints are generated independently by
    the browser and keep saying HeadlessChrome, so a UA pin alone leaves the
    headless context both detectable AND self-contradictory.
    """
    if not raw:
        return None
    return raw.replace("HeadlessChrome/", "Chrome/")


# Used only when no browser has run yet in this profile, so there is no probed
# UA to reuse. Kept in step with Patchright's bundled Chromium; being a little
# behind is harmless (plenty of real clients are), being a DECADE behind is not
# — see _comix_http_headers.
#
# The trailing `.0.0` is not laziness: Chrome's UA reduction freezes the minor
# fields, so a real Chrome 147 reports exactly `Chrome/147.0.0.0` and the full
# build number lives only in Sec-CH-UA-Full-Version-List. Writing a real build
# here (e.g. 147.0.7727.15) would be the one UA no genuine Chrome ever sends.
_COMIX_FALLBACK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


def _cached_stable_user_agent() -> str:
    """The UA this profile's browser presents, or a modern default.

    Module-level twin of _ComixBrowserSession._resolve_stable_user_agent, for
    callers that have no session (configure_session runs long before any browser
    launch). Deliberately does NOT consult the zendriver CF cache that the
    session method prefers: this feeds the plain-HTTP identity, which should
    match the Patchright browser the rest of the handler uses.
    """
    global _COMIX_STABLE_UA
    with _COMIX_UA_LOCK:
        if _COMIX_STABLE_UA:
            return _COMIX_STABLE_UA
    try:
        path = os.path.join(_comix_profile_dir(), _UA_CACHE_FILENAME)
        with open(path, "r", encoding="utf-8") as fh:
            cached = (fh.read() or "").strip()
    except Exception:
        cached = ""
    if cached:
        with _COMIX_UA_LOCK:
            _COMIX_STABLE_UA = cached
        return cached
    return _COMIX_FALLBACK_UA


def _comix_http_headers() -> Dict[str, str]:
    """One COHERENT modern-Chrome header set for the plain-HTTP session.

    THE PROBLEM THIS SOLVES (measured 2026-08-02). aio-dl builds the scraper as
    `cloudscraper.create_scraper(browser={"browser":"chrome","platform":
    "windows"})`, and cloudscraper answers that by picking a RANDOM entry from
    its bundled profile list — every one of which is 2016-2019:

        Chrome/56.0.2924.87 ... OPR/43.0.2442.1144 (Edition Yx)   (Win 7, 2017)
        Chrome/55.0.2883.87 UBrowser/7.0.125.1629
        Chrome/53.7.2410.8782 Safari/531.83                       (not a real build)

    Twelve samples, twelve ancient UAs, a fresh one per process. It also sends a
    Chrome-56-era `Accept` and no client hints or Sec-Fetch-* headers at all.
    That combination is why comix's WAF challenged the metadata request so
    often: the request announces itself before any behavioural signal is read.

    The values below mirror what this profile's actual Chromium sends (verified
    against a live capture), so the HTTP session and the browser present ONE
    identity instead of two contradictory ones. The brand list is derived from
    the UA's own major version rather than hardcoded, so a UA refresh can't
    silently desynchronise the hints — which is exactly the failure mode that
    made the old post-solve retry worse than no retry (grep _waf_recover_once).
    """
    ua = _cached_stable_user_agent()
    major = "147"
    m = re.search(r"Chrome/(\d+)", ua)
    if m:
        major = m.group(1)
    return {
        "User-Agent": ua,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        # Patchright's Chromium reports "Chromium", not "Google Chrome" — match
        # it. Claiming Google Chrome here would contradict the browser half of
        # the handler the moment a page load follows an HTTP one.
        "sec-ch-ua": f'"Chromium";v="{major}", "Not.A/Brand";v="8"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        # same-origin rather than none: configure_session also pins a comix.to
        # Referer, and a Referer with Sec-Fetch-Site:none is self-contradictory.
        "Sec-Fetch-Site": "same-origin",
        "Upgrade-Insecure-Requests": "1",
    }


# Launch channel for the persistent context. Together with the UA pin above,
# THIS is what makes the headless context and the headed handoff window present
# the same identity. Neither lever is sufficient alone — that is the whole point
# and the reason the previous fix failed.
#
# Measured 2026-08-02 on Patchright's bundled Chromium, hitting a local server
# and reading both the request headers and navigator.userAgentData:
#
#   launch                        UA header          Sec-CH-UA / userAgentData
#   headless, no pin              HeadlessChrome     HeadlessChrome
#   headless, UA pinned           Chrome  (fixed)    HeadlessChrome  (LEAKS)
#   headless, channel, no pin     HeadlessChrome     Chromium        (LEAKS)
#   headless, channel + UA pin    Chrome             Chromium        <- clean
#   headed,  channel + UA pin     Chrome             Chromium        <- identical
#
# Playwright's `user_agent=` calls Emulation.setUserAgentOverride WITHOUT
# userAgentMetadata, so it can only ever fix the header; the channel governs the
# client hints but leaves `HeadlessChrome` in the UA string. Commit b014f12
# shipped the pin alone, which left the context announcing HeadlessChrome in the
# hints while its UA claimed Chrome — a contradiction no real browser emits, so
# arguably a WORSE signal than the plain headless it replaced. That is why a
# clearance earned in the headed window was rejected seconds later by the
# relaunched headless one ("hit during the chapter page (after verification)").
#
# `--headless=new` as a bare arg does NOT work (measured: still HeadlessChrome in
# the hints) — it has to be the channel. Falls back to a channel-less launch when
# the installed Playwright/Patchright doesn't know the channel, so an older
# install degrades to b014f12's behavior rather than failing to launch.
_COMIX_BROWSER_CHANNEL = "chromium"

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


def _waf_failure_message(stage: str, reason: Optional[str]) -> str:
    """User-facing text for an unsolved challenge, keyed on WHY.

    Exists because the first version printed "it was not completed" for every
    outcome — including the case where the downloader never opened a window at
    all because its one-per-process allowance was already spent. Telling someone
    they failed to do something they were never asked to do is the worst
    possible error message, so each outcome now says what actually happened.

    The `solved_*` reasons are the second half of that lesson (2026-08-02): the
    HTTP path had its own hardcoded "it was not completed" string and used it
    even when the user HAD completed the check, because the thing that actually
    failed was the request AFTER the solve. A log that reads "verification
    passed - thanks" and then "it was not completed" four seconds later sends
    you hunting for a bug in the wrong half of the system.
    """
    head = f"comix.to is asking for human verification, so {stage} could not be read."
    tails = {
        "solved_but_browser_still_blocked": (
            " The check WAS completed — thank you — but the site immediately "
            "challenged the very next page load. That usually means it is "
            "challenging aggressively right now. Wait a few minutes and re-run; "
            "the solved session is saved to disk, so it should not ask again."
        ),
        "browser_unavailable": (
            " The page could not be re-read through the downloader's own "
            "browser either. Check that the bundled browser is installed "
            "(patchright install chromium); if it is, the site may simply be "
            "unreachable right now."
        ),
        "already_declined": (
            " A verification window was opened earlier this run and not "
            "completed, so no further windows were opened. Re-run and complete "
            "the check when it appears."
        ),
        "solve_limit": (
            " The check has already been passed several times this run, which "
            "usually means the site is challenging aggressively right now. Try "
            "again in a few minutes."
        ),
        "too_soon": (
            " A verification window was opened moments ago. Re-run in a minute."
        ),
        "disabled": (
            f" Interactive verification is turned off ({_WAF_NO_INTERACTIVE_ENV} "
            "is set). Unset it, or open comix.to in a browser and pass the check "
            "once so the saved session is reused."
        ),
        "no_display": (
            " No display is available for the verification window. Run the "
            "downloader on a desktop session once so the session is saved to "
            f"{_comix_profile_dir()}, or set AIO_COMIX_PROFILE_DIR to a profile "
            "that already has one."
        ),
        "launch_failed": (
            " The verification window could not be opened. Check that the "
            "bundled browser is installed (patchright install chromium)."
        ),
        "window_closed": (
            " The verification window was closed before the check completed. "
            "Re-run and finish it to continue."
        ),
    }
    return head + tails.get(
        reason or "",
        " It was not completed. Re-run and finish the check in the window the "
        "downloader opens, or visit comix.to in a browser and pass it once.",
    )


class _BrowserHtmlResponse:
    """Duck-typed stand-in for a `requests.Response` carrying HTML the BROWSER
    read rather than the HTTP client.

    Exists so `_waf_recover_once` can substitute a browser-sourced body into the
    plain-HTTP metadata path without every downstream consumer learning where
    the bytes came from. Only the three attributes `_cf_aware_request` and
    `fetch_comic_context` actually touch are provided (`text`, `url`,
    `status_code`); anything else is deliberately absent so a future caller that
    needs a real Response fails loudly instead of silently reading a default.
    """

    __slots__ = ("text", "url", "status_code")

    def __init__(self, text: str, url: str):
        self.text = text
        self.url = url
        self.status_code = 200


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
        # The header set matters as much as the Referer here: cloudscraper's
        # default identity is a randomly-chosen 2016-2019 browser, which is a
        # large part of why comix's WAF challenged the metadata request at all.
        # See _comix_http_headers for the measurements.
        scraper.headers.update(_comix_http_headers())
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
        """If *response* is comix's WAF interstitial, re-read the page through
        the downloader's own browser and return THAT html. Returns the original
        response untouched when no challenge is present.

        WHY THE BROWSER RATHER THAN A COOKIE TRANSPLANT (rewritten 2026-08-02).
        This used to open the interactive handoff, copy the solved cookies into
        `scraper`, overwrite `scraper.headers["User-Agent"]`, and retry over
        HTTP. That could not work, and reliably burned a human solve to fail
        anyway (live log: "verification passed - thanks" at 00:48:22, "it was
        not completed" at 00:48:27). Three reasons, none fixable by copying more
        cookies:
          - cloudscraper picks a RANDOM 2016-2019 browser profile per session
            (measured: Chrome 53-72 / Win 7-8 / Opera 43 / UBrowser, and one
            synthetic `Chrome/53.7.2410.8782`), so the request is flagged on
            sight before any cookie is even considered;
          - overwriting only the UA left cloudscraper's Chrome-56-era `Accept`
            (`image/apng`, no `avif`) beside a Chrome-147 UA, with no Sec-CH-UA
            and no Sec-Fetch-* at all — MORE incoherent than the original
            request, not less;
          - the clearance rides an HttpOnly+Secure `session` cookie bound to the
            browser identity that earned it; replaying it from OpenSSL/urllib3
            TLS is not the same client by any measure the WAF uses.

        The browser, by contrast, already holds a PERSISTED session (the profile
        dir keeps `cf_clearance` and `session` across runs and processes), so
        this usually returns the page with NO user interaction at all — which is
        the entire point of having a persistent profile. The interactive handoff
        now fires only when the BROWSER is challenged too, inside
        `fetch_series_html`'s `_enforce_no_waf`.

        Raises ComixWafChallengeError when even the browser can't get a clean
        read. Failing loud is deliberate: the caller's next step is to parse this
        body, and every parser in this module degrades a challenge page into
        plausible-looking garbage rather than an error.
        """
        try:
            final_url = getattr(response, "url", None) or url
            body = getattr(response, "text", None)
        except Exception:
            return response
        if not _looks_like_waf_challenge(final_url, body):
            return response

        print(
            "[!] Comix: the HTTP session hit the site's human-verification "
            "check (/@waf/challenge); re-reading the page through the "
            "downloader's own browser, which keeps a saved session.",
            flush=True,
        )
        # ComixWafChallengeError from _enforce_no_waf propagates untouched — it
        # already carries an outcome-specific message, so don't recast it. Only
        # a bridge wall-clock timeout is caught, because that would otherwise
        # reach the user as a bare "Failed to fetch comic data: " with an empty
        # TimeoutError behind it.
        try:
            html = _COMIX_BROWSER_BRIDGE.fetch_series_html(url)
        except TimeoutError:
            html = None

        if html and not _looks_like_waf_challenge(None, html):
            print(
                "[*] Comix: read the page through the browser; continuing.",
                flush=True,
            )
            # Best-effort: hand the browser's cookies to the HTTP session for
            # the REST of the run (the cover-image download and any CDN fetch
            # still go through `scraper`). Explicitly NOT relied upon for the
            # WAF — see the docstring — and the UA is deliberately left alone,
            # because mixing a modern UA into cloudscraper's period-correct
            # header set is what made the old retry self-contradictory.
            self._adopt_browser_cookies(scraper)
            return _BrowserHtmlResponse(text=html, url=url)

        raise ComixWafChallengeError(
            _waf_failure_message(
                "the series page",
                "solved_but_browser_still_blocked" if html else "browser_unavailable",
            ),
            challenge_url=final_url,
        )

    def _adopt_browser_cookies(self, scraper) -> None:
        """Copy the persistent browser context's comix cookies into *scraper*.

        Best-effort and non-fatal: this is a convenience for later plain-HTTP
        fetches in the same run, NOT the WAF bypass it used to pretend to be.
        Same idiom as crawlee_utils.sync_cf_cookies, sourced from our own
        persistent context rather than the zendriver cache.
        """
        try:
            cookies = _COMIX_BROWSER_BRIDGE.context_cookies() or []
        except Exception:
            return
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            if not name or value is None:
                continue
            try:
                scraper.cookies.set(
                    name,
                    value,
                    domain=cookie.get("domain") or "comix.to",
                    path=cookie.get("path") or "/",
                )
            except Exception:
                continue

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
        # render one lazy-loaded <img> per page. Those pages are MOSTLY plain,
        # directly-fetchable webp CDN URLs — comix was believed to have dropped
        # the old server-side tile-scramble (verified 2026-07-11 on the pages
        # checked: no x-scramble-* headers, no CSS transform, fetchable with no
        # referer) — but the <canvas> branch still fires on ~9% of pages, which
        # the capture-shapes summary now measures rather than assumes.
        # Python can neither sign nor decrypt the API, so drive the persistent
        # browser to render the chapter and scrape the page URLs from the DOM.
        # The bridge's page.on("response") listener also caches each <img>'s
        # bytes as they load, so aio-dl.py:dl_image usually serves straight from
        # memory instead of re-fetching.
        #
        # Three outcomes, and the distinction matters:
        #   - complete   -> the URL list, memoized.
        #   - SHORT      -> IncompleteChapterError (see below). comix can also
        #                   serve as a --multi-source alt, so the caller's
        #                   rescue path is live for this.
        #   - no render  -> [], the pre-existing empty_content miss.
        # Cross-file: _ComixBrowserSession.fetch_chapter_images_via_dom.
        capture = _COMIX_BROWSER_BRIDGE.fetch_chapter_images_via_dom(url)
        images = list(capture.urls or [])

        # ZERO-TOLERANCE HOOK. aio-dl.py's completeness gate computes
        # pages_total from len() of what we return here (grep 'pages_total =
        # len(download_tasks)'), so a silently short list reads as N/N complete
        # and sails straight through: no ChapterSkippedError, no inline retry,
        # no alt-source rescue. Live 2026-08-02 (Spark in Your Eyes ch.104): the
        # reader reported 68 pages, the DOM capture returned 67, and the CBZ was
        # saved one page short — invisibly, because pages get renumbered
        # 0001..0067 on the way in, so there was no gap to notice.
        #
        # expected_pages is 0 whenever the render never got far enough to learn
        # a page count (nav failure, reader never mounted). Those keep the old
        # behaviour — return [], which the caller records as an empty_content
        # miss — because "we were told nothing" is not "we were told 68".
        #
        # The session already did its own reload retry before reporting short
        # (see fetch_chapter_images_via_dom step 4), which is the precondition
        # IncompleteChapterError documents. From here the caller's machinery
        # takes over: inline retry -> multi-source alt rescue -> hard abort.
        if capture.expected_pages and len(images) < capture.expected_pages:
            raise IncompleteChapterError(
                pages_ok=len(images),
                pages_total=capture.expected_pages,
                host="comix.to",
                reason="comix_dom_render_incomplete",
            )

        # Only memoize a COMPLETE capture. The TTL here (600 s) is far longer
        # than the caller's inline-retry backoff (30 s then 60 s), so caching a
        # short list would hand the retry the identical truncated result from
        # memory without ever re-rendering — defeating the retry entirely.
        # Unreachable while the raise above stands, but the two are independent
        # invariants and this one is cheap to keep honest.
        if images and len(images) == capture.expected_pages:
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
        # get_chapter_images) so (a) the cap is honored, (b) the handler's memo
        # cache isn't populated with a truncated (capped) page list a later real
        # download would wrongly serve, and (c) — since the strict
        # IncompleteChapterError raise lives in get_chapter_images — a capped
        # render can never be mistaken for a short chapter. A probe wants
        # representative pages, not every page.
        try:
            capture = _COMIX_BROWSER_BRIDGE.fetch_chapter_images_via_dom(
                chap_url,
                time_budget_s=60.0,
                max_capture_pages=_COMIX_PROBE_PAGE_CAP,
            )
        except Exception:
            return None
        image_items = list(capture.urls or [])
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
        # URLs whose response carried an x-scramble-* header, i.e. pages comix
        # served tile-scrambled. Populated by the page.on("response") listener
        # (which sees every image response anyway) and read once per chapter by
        # the capture summary. Bounded so a long run can't grow it without
        # limit — see _note_scrambled_url.
        self._scrambled_urls: set = set()

    def _start(self, headless: bool = True, _ua_relaunch: bool = False) -> bool:
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

        ``_ua_relaunch`` is internal: it marks the single permitted self-relaunch
        after a HeadlessChrome-leaking UA is detected on the channel-fallback
        path, so termination is provable here rather than resting on cache state.
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
            # profile.
            profile_dir = _comix_profile_dir()
            os.makedirs(profile_dir, exist_ok=True)

            def _launch(**extra):
                return self._pw.chromium.launch_persistent_context(
                    profile_dir,
                    headless=headless,
                    args=["--no-sandbox", "--disable-setuid-sandbox"],
                    **extra,
                )

            # Pin the UA: the channel alone leaves `HeadlessChrome` in the UA
            # STRING (only the client hints move), so both levers are required —
            # see the table at _COMIX_BROWSER_CHANNEL. _resolve_stable_user_agent
            # prefers a zendriver CF solve's UA when one exists, because
            # cf_clearance is issued against that UA and _sync_cf_cookies injects
            # that cookie into this very context.
            ctx_kwargs: Dict[str, Any] = {}
            cf_ua = self._cached_cf_user_agent()
            pinned_ua = self._resolve_stable_user_agent()
            if pinned_ua:
                ctx_kwargs["user_agent"] = pinned_ua

            # channel= is what aligns headless's client hints with the headed
            # handoff window (see _COMIX_BROWSER_CHANNEL). Degrade rather than
            # die if this Playwright/Patchright build rejects the channel — the
            # WAF gets harder to satisfy, but the handler still works.
            try:
                self._context = _launch(
                    channel=_COMIX_BROWSER_CHANNEL, **ctx_kwargs
                )
            except Exception as channel_exc:
                print(
                    f"[!] Comix: channel={_COMIX_BROWSER_CHANNEL!r} launch "
                    f"failed ({type(channel_exc).__name__}: {channel_exc}); "
                    f"falling back to default headless, which advertises "
                    f"HeadlessChrome in Sec-CH-UA. Expect the site's "
                    f"human-verification check to fire more often.",
                    flush=True,
                )
                self._context = _launch(**ctx_kwargs)
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

        # Reconcile the pin against what this browser REALLY is. Re-checked on
        # every launch so the cached UA can never drift from the installed
        # Chromium — and read via CDP, because once a UA override is applied
        # `navigator.userAgent` only reads our own override back. That blind spot
        # is exactly how a stale cached UA would pin itself forever: probe sees
        # the pin, agrees with the pin, re-caches the pin.
        true_ua = self._probe_true_user_agent()
        stable_ua = _stabilize_user_agent(true_ua)
        if stable_ua:
            self._remember_stable_user_agent(stable_ua)
            # Relaunch ONCE when the applied pin isn't the right one (fresh
            # profile → nothing pinned; stale cache → wrong version pinned). A CF
            # pin is exempt: that value is bound to a cf_clearance cookie and
            # deliberately outranks matching the local browser. Bounded by the
            # explicit flag rather than by "the cache is warm now" — making
            # termination depend on distant module state is how an innocent
            # refactor turns this into a launch loop (same reasoning as
            # _WAF_MAX_ENFORCE_PASSES).
            if (
                ctx_kwargs.get("user_agent") != stable_ua
                and not cf_ua
                and not _ua_relaunch
            ):
                self._cleanup()
                return self._start(headless, _ua_relaunch=True)

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
            try:
                try:
                    if response.request.resource_type != "image":
                        return
                except Exception:
                    pass
                headers = response.headers or {}
                ct = (headers.get("content-type") or "").lower()
                if not ct.startswith("image/"):
                    return
                # Scramble bookkeeping runs BEFORE the cache write and is
                # independent of it — the whole point is to learn whether comix
                # still tile-scrambles anything, and that has to hold even if
                # image_cache failed to import. See _COMIX_SCRAMBLE_HEADER_PREFIX.
                try:
                    if any(
                        str(k).lower().startswith(_COMIX_SCRAMBLE_HEADER_PREFIX)
                        for k in headers
                    ):
                        self._note_scrambled_url(response.url)
                except Exception:
                    pass
                if _image_cache_module is None:
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

    # Cap on remembered scramble-flagged URLs. This set only feeds a diagnostic
    # count, so a bounded sample answers the question just as well as an exact
    # tally — and the session outlives the whole run (one persistent browser for
    # every chapter of every series), which is exactly the shape that leaks.
    _SCRAMBLED_URL_MEMORY = 512

    def _note_scrambled_url(self, url: str) -> None:
        """Record that ``url`` was served with an x-scramble-* header.

        Called from the response listener on the Patchright worker thread. That
        thread is the ONLY writer and the only reader (the capture summary runs
        on the same worker), so no lock — consistent with the rest of the
        session's state. Drops silently once full rather than evicting: we want
        to know IF scrambling happens at all, not to enumerate every instance.
        """
        if not url:
            return
        if len(self._scrambled_urls) >= self._SCRAMBLED_URL_MEMORY:
            return
        self._scrambled_urls.add(url)

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

    def _probe_true_user_agent(self) -> Optional[str]:
        """The browser's REAL User-Agent, seen past any page-level override.

        `page.evaluate("navigator.userAgent")` is useless for this once
        `user_agent=` is set on the context: Emulation.setUserAgentOverride makes
        it report our own pin straight back, so a wrong pin looks self-consistent
        and survives forever. CDP `Browser.getVersion` reports the browser
        itself, override or not (verified 2026-08-02: page said the pinned
        `Chrome/999.0.1.2`, getVersion said the true
        `HeadlessChrome/147.0.0.0`).

        Falls back to the page's own view when CDP is unavailable — which is
        correct precisely in the case that makes CDP necessary impossible, i.e.
        when nothing is overriding the UA.
        """
        if self._context is None or self._page is None:
            return None
        cdp = None
        try:
            cdp = self._context.new_cdp_session(self._page)
            version = cdp.send("Browser.getVersion") or {}
            ua = version.get("userAgent")
            if ua:
                return ua
        except Exception:
            pass
        finally:
            if cdp is not None:
                try:
                    cdp.detach()
                except Exception:
                    pass
        try:
            return self._page.evaluate("navigator.userAgent")
        except Exception:
            return None

    def _resolve_stable_user_agent(self) -> Optional[str]:
        """UA to pin on the persistent context, or None if not known yet.

        Priority: a zendriver CF solve's UA (that cookie is bound to it) >
        the cached stable UA for this profile > None, which makes _start probe
        the launched browser and relaunch once with the stabilized value.
        """
        global _COMIX_STABLE_UA
        cf_ua = self._cached_cf_user_agent()
        if cf_ua:
            return cf_ua
        with _COMIX_UA_LOCK:
            if _COMIX_STABLE_UA:
                return _COMIX_STABLE_UA
        try:
            path = os.path.join(_comix_profile_dir(), _UA_CACHE_FILENAME)
            with open(path, "r", encoding="utf-8") as fh:
                cached = (fh.read() or "").strip()
        except Exception:
            cached = ""
        if cached:
            with _COMIX_UA_LOCK:
                _COMIX_STABLE_UA = cached
            return cached
        return None

    def _remember_stable_user_agent(self, ua: str) -> None:
        """Persist the stabilized UA beside the profile it belongs to, so later
        processes launch with it directly instead of re-probing."""
        global _COMIX_STABLE_UA
        with _COMIX_UA_LOCK:
            _COMIX_STABLE_UA = ua
        try:
            path = os.path.join(_comix_profile_dir(), _UA_CACHE_FILENAME)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(ua)
        except Exception:
            # A read-only profile dir just means we re-probe next process.
            pass

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

    def _enforce_no_waf(
        self, stage: str, return_url: Optional[str] = None, _attempt: int = 1
    ) -> None:
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
        outcome = self.solve_waf_interactively(return_url) or {}
        if not outcome.get("solved"):
            raise ComixWafChallengeError(
                _waf_failure_message(stage, outcome.get("reason")),
                challenge_url=challenge_url,
            )
        if not return_url:
            # No target to restore (defensive: every browser call site passes
            # one). The session is banked in the profile, so let the caller
            # proceed on whatever it has.
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
        # Re-challenged on the reload. Historically this was the identity
        # mismatch between the headed solve window and the relaunched headless
        # context. Commit b014f12 pinned the UA STRING to close that, but
        # measurement on 2026-08-02 showed the pin never worked: headless kept
        # advertising HeadlessChrome in Sec-CH-UA and navigator.userAgentData,
        # so this branch fired constantly. `channel=` (see
        # _COMIX_BROWSER_CHANNEL) is the actual fix; reaching here now should be
        # genuinely rare and means the site is challenging aggressively.
        #
        # Bounded by an EXPLICIT depth rather than by the prompt budget in
        # solve_waf_interactively. The budget does stop the real prompting, but
        # making termination depend on distant module state is how you get an
        # infinite loop from an innocent refactor — one that a mocked solve
        # demonstrates immediately.
        if self._waf_blocked(f"{stage} (after verification)"):
            if _attempt >= _WAF_MAX_ENFORCE_PASSES:
                raise ComixWafChallengeError(
                    _waf_failure_message(
                        stage, "solved_but_browser_still_blocked"
                    ),
                    challenge_url=challenge_url,
                )
            self._enforce_no_waf(stage, return_url, _attempt + 1)

    def solve_waf_interactively(self, return_url: Optional[str] = None) -> Dict[str, Any]:
        """Open the downloader's own browser profile VISIBLY on comix's
        human-verification page and wait for the user to complete it.

        Returns {"solved": bool, "reason": str}. Never raises — callers decide
        whether an unsolved challenge is fatal (it is, for chapter lists and
        metadata).

        What this does NOT do, deliberately: it does not read, score, rotate, or
        submit the widget, and it sends no synthetic input. It navigates, prints
        an instruction, and POLLS the URL until the site itself decides the
        check passed. The solve is the user's.

        Why it works at all: the visible window runs on the SAME persistent
        profile dir as the headless one (_comix_profile_dir), so the session the
        user's solve produces is written straight into the profile the rest of
        the run uses. We relaunch headless afterwards and the caller retries.

        It no longer hands cookies back. The plain-HTTP path used to take them
        and retry the request itself, which could not work — see
        ComixSiteHandler._waf_recover_once for why, and note that it now re-reads
        the page through this same browser instead, so the solved session is
        consumed where it is valid rather than exported somewhere it is not.
        """
        global _COMIX_WAF_SOLVES_DONE, _COMIX_WAF_FAILURES, _COMIX_WAF_LAST_PROMPT_AT
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
            if _COMIX_WAF_FAILURES >= _COMIX_WAF_MAX_FAILURES:
                # The user already declined (timed out / closed the window).
                # Don't nag.
                result["reason"] = "already_declined"
                return result
            if _COMIX_WAF_SOLVES_DONE >= _COMIX_WAF_MAX_SOLVES:
                result["reason"] = "solve_limit"
                return result
            waited = time.monotonic() - _COMIX_WAF_LAST_PROMPT_AT
            if _COMIX_WAF_LAST_PROMPT_AT and waited < _COMIX_WAF_MIN_PROMPT_INTERVAL_S:
                result["reason"] = "too_soon"
                return result
            _COMIX_WAF_LAST_PROMPT_AT = time.monotonic()

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
            result["solved"] = True
            with _COMIX_WAF_HANDOFF_LOCK:
                # A success does NOT count against the ask-again budget: the
                # cloudscraper session and the browser are separate identities
                # to the WAF, so one run legitimately needs a solve for each.
                _COMIX_WAF_SOLVES_DONE += 1
                _COMIX_WAF_FAILURES = 0
        else:
            if not result.get("reason"):
                result["reason"] = "timeout"
                print(
                    f"[!] Comix: verification not completed within "
                    f"{int(timeout_s)}s.",
                    flush=True,
                )
            with _COMIX_WAF_HANDOFF_LOCK:
                # The user declined (timed out or closed the window). Stop
                # asking — repeated windows they're ignoring help nobody.
                _COMIX_WAF_FAILURES += 1

        # No UA bookkeeping here any more. It used to re-read
        # navigator.userAgent and cache it, which became actively wrong once the
        # context pins a UA: the page then reports the PIN, so this re-cached
        # our own override instead of the browser. _start now reconciles the pin
        # against CDP Browser.getVersion on every launch — including the
        # relaunch two lines below — so the cache is correct without this.
        #
        # Back to headless for the rest of the run. The close flushes the
        # solved session into the profile dir, so the relaunch inherits it.
        self._cleanup()
        self._start(headless=True)
        return result

    def context_cookies(self) -> List[Dict[str, Any]]:
        """Current cookies from the persistent context, or [] if unavailable.

        Consumer is ComixSiteHandler._adopt_browser_cookies, which seeds the
        plain-HTTP session for non-WAF'd follow-up fetches (cover images). Does
        NOT launch the browser — an unstarted session legitimately has nothing
        to give, and booting Chromium just to read cookies would make a
        convenience path expensive.
        """
        if self._context is None:
            return []
        try:
            return [
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
            return []

    def fetch_series_html(self, url: str) -> Optional[str]:
        """Return the title page's HTML as the BROWSER sees it, or None.

        This is the WAF fallback for the plain-HTTP metadata path
        (ComixSiteHandler._waf_recover_once explains why a cookie transplant
        onto cloudscraper cannot work). The browser carries the persisted
        profile session, so the common case is a clean read with NO user
        interaction — `_enforce_no_waf` only prompts when the browser is
        challenged too, and RAISES ComixWafChallengeError (propagated, not
        swallowed) when the handoff doesn't clear it.

        `page.content()` serializes the LIVE DOM, not the raw server response,
        which is fine for every consumer here: the parse targets are
        `<script id="initial-data">`, `<script id="syncData">` and the og:image
        / description <meta> tags, and React hydration mutates none of them —
        it reads the blob and leaves the element in place. domcontentloaded is
        therefore a sufficient wait; the SSR markup is complete before any of
        the app's own fetches run.

        Returns None (rather than raising) for launch/navigation failures so the
        caller can distinguish "the browser couldn't run" from "the browser ran
        and was blocked" and word the error accordingly.
        """
        if not self._start():
            return None
        self._sync_cf_cookies()
        page = self._page
        if page is None:
            return None
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            print(
                f"[!] Comix: could not load {url} in the browser "
                f"({type(exc).__name__}: {exc}).",
                flush=True,
            )
            return None
        self._enforce_no_waf("the series page", url)
        try:
            return page.content()
        except Exception as exc:
            print(
                f"[!] Comix: could not read the series page HTML "
                f"({type(exc).__name__}: {exc}).",
                flush=True,
            )
            return None

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


    def _apply_reader_preload_pref(self) -> bool:
        """Write ``preload: all`` into the reader's persisted settings so a
        chapter renders every page eagerly instead of lazy-loading on scroll.
        Returns True when the value is confirmed stored.

        THE KEY MATTERS, AND THE OLD ONE WAS NEVER READ. This used to write
        ``localStorage['reader.default'] = {preload: 'all'}``. Verified live
        2026-08-02: a profile that has rendered a chapter AND opened the reader
        settings panel holds `front_t3.searchHistory`, `auth` and
        `reader.webtoon.v3` — `reader.default` never appears at any point. The
        write was inert, and the homepage navigation that existed solely to
        carry it bought nothing on every chapter of every run.

        The real store is `reader.webtoon.v3`, a Zustand-persist envelope whose
        fields live under `state` (the second half of why the old write could
        not have worked even with the right key):

            {"state": {"readingDirection": "ttb", "preload": "some", ...},
             "version": 0}

        So: read-modify-write, so the profile's other reader settings survive,
        and don't touch `version` — rewriting it would make the store's own
        migration logic treat the blob as a different schema generation and
        discard it. When the key is absent (fresh profile, reader not yet run)
        create it in exactly the observed shape.

        Measured on one 75-page chapter, same URL, plain reload: `some` (the
        site default) → 3 pages carrying a loaded <img>; `all` → 68. That is
        the difference between the per-page capture loop polling its full 10s
        lazy-load wait and finding nearly every page already decoded.

        Trade-off, accepted: eager loading is burstier against the CDN than the
        old scroll-staggered pattern. It is the site's own reader option rather
        than a hack, the total bytes are identical (we capture every page
        either way), and the session-level page.on("response") listener banks
        them all as they land — which is exactly what stops dl_image from
        re-fetching against an expired signed token later.

        Deliberately NOT latched behind a "done once" flag. localStorage is
        per-origin, and by the time chapters are being fetched the page is
        essentially always already ON comix.to (the chapter-list scrape or the
        previous chapter left it there), so the steady-state cost is one
        evaluate and NO navigation. Re-asserting each call keeps it
        self-healing if the store ever rewrites the field, which a flag would
        permanently mask.
        """
        page = self._page
        if page is None:
            return False
        try:
            on_origin = "comix.to" in (page.url or "")
        except Exception:
            on_origin = False
        if not on_origin:
            # Only pay a navigation when we genuinely aren't on the origin yet.
            try:
                page.goto(
                    "https://comix.to/",
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
            except Exception as exc:
                print(
                    f"[*] Comix: could not reach comix.to to set the reader's "
                    f"preload preference ({type(exc).__name__}: {exc}); "
                    f"continuing with lazy page loading.",
                    flush=True,
                )
                return False
        try:
            stored = page.evaluate("""() => {
                try {
                    const k = 'reader.webtoon.v3';
                    const raw = localStorage.getItem(k);
                    const cur = raw ? JSON.parse(raw) : {version: 0};
                    if (!cur.state || typeof cur.state !== 'object') {
                        cur.state = {};
                    }
                    cur.state.preload = 'all';
                    localStorage.setItem(k, JSON.stringify(cur));
                    const back = JSON.parse(localStorage.getItem(k) || '{}');
                    return (back.state || {}).preload || null;
                } catch (e) { return null; }
            }""")
        except Exception as exc:
            print(
                f"[*] Comix: reader preload-all setup failed "
                f"({type(exc).__name__}: {exc}); continuing with lazy page "
                f"loading.",
                flush=True,
            )
            return False
        return stored == "all"

    def fetch_chapter_images_via_dom(
        self,
        chapter_url: str,
        time_budget_s: float = 300.0,
        max_capture_pages: Optional[int] = None,
    ) -> ComixChapterCapture:
        """Capture every chapter page by polling the whole reader DOM until it
        converges, then reading each rendered element.

        Why the browser at all: the chapter page is an SPA whose page list
        comes from a signed + encrypted /api/v1/chapters/{id} ({"e":...})
        response that only the in-page JS can decrypt — Python can neither sign
        nor decrypt it — and the <img> src is lazy-set per page as it nears the
        viewport (not in #initial-data or any data-* attribute). So we let the
        browser render and scrape the DOM.

        WHOLE-CHAPTER POLLING, NOT PAGE-BY-PAGE. The original loop walked pages
        in order and gave each a private 10s window (40 polls × 250ms), then
        moved on for good. Two costs, both real:
          - ~2 CDP round-trips minimum per page (scroll + poll), so ≥136 for a
            68-page chapter, when ONE evaluate can report every page at once.
            With `preload: all` set (step 1) the first poll typically harvests
            nearly the whole chapter.
          - A page that needed 11s was LOST even though the scrape kept running
            for six more pages with the browser holding it fully decoded. Live:
            Spark in Your Eyes ch.104 dropped page 62 of 68 — and because pages
            are renumbered on the way into the CBZ, the archive was silently one
            page short rather than visibly gapped.
        Now one evaluate reports all pending pages per round, and any progress
        anywhere in the chapter keeps every straggler alive
        (_COMIX_CAPTURE_STALL_S). The budget is spent once, at the end.

        Two page shapes, checked in this precedence (canvas first — unchanged,
        deliberately; see the capture-shapes summary at the end for the
        measurement that will tell us whether it still needs to be):
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
          1. Pre-flight: set the reader's persisted `preload: all` preference
             so it renders eagerly instead of lazy-loading on scroll — see
             _apply_reader_preload_pref (which usually needs NO navigation).
          2. Navigate to the chapter URL; wait for the React app to mount and
             populate one .rpage-page <div> per page (= the page count).
          3. Converge: one evaluate reports every pending page's state; resolve
             what's ready, nudge the lowest straggler into view, repeat.
          4. Harvest canvas pixels for canvas-resolved pages (BEFORE any reload
             — a reload discards them).
          5. If pages remain unresolved and budget is left, reload once and
             re-converge over just those. This is the handler's own retry, the
             one IncompleteChapterError is documented to come after
             (sites/base.py:IncompleteChapterError).

        Returns a ComixChapterCapture — NOT a bare list — because the caller
        needs the site's own page count to tell a complete chapter from a
        truncated one. See that class for why the distinction is load-bearing.

        Cross-file: called from ComixSiteHandler.get_chapter_images via
        _COMIX_BROWSER_BRIDGE.fetch_chapter_images_via_dom; image_cache
        populated here is read by aio-dl.py:dl_image. Runs on the comix-pw
        daemon worker per the bridge's same-thread Patchright contract.
        """
        # expected_pages=0 on every pre-mount failure, so get_chapter_images
        # returns [] (today's "empty_content" miss) instead of raising
        # IncompleteChapterError. The raise is for "the reader TOLD us N pages
        # and we got fewer" — a nav failure hasn't told us anything.
        empty = ComixChapterCapture(urls=[], expected_pages=0)
        if not self._start():
            return empty
        self._sync_cf_cookies()
        import base64 as _b64
        import re as _re
        import time as _time

        page = self._page
        if page is None:
            return empty

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
        # Baseline for the per-chapter scramble tally. The session's set spans
        # the whole run (one browser, every chapter), so the count that means
        # anything is the DELTA across this capture.
        scramble_baseline = len(self._scrambled_urls)

        # ── Step 1: ask the reader to preload every page. Must happen BEFORE
        # the chapter navigation below — the store hydrates from localStorage
        # at mount, so a later write wouldn't affect this render. Failure is
        # non-fatal: the per-page scrollIntoView loop still works, just slower.
        preload_all = self._apply_reader_preload_pref()

        # ── Step 2: navigate to chapter and wait for .rpage-page divs.
        try:
            page.goto(
                chapter_url,
                wait_until="domcontentloaded",
                timeout=30000,
            )
        except Exception as e:
            print(
                f"[!] Comix chapter image capture: nav failed for "
                f"{chapter_url}: {type(e).__name__}: {e}",
                flush=True,
            )
            return empty

        # The WAF fires on behavior, and a chapter render is the heaviest thing
        # we do (one navigation plus ~40-80 image loads), so this is where it
        # most often lands. Without the check the interstitial just produced
        # "0 .rpage-page divs" below, which reads as a broken chapter.
        self._enforce_no_waf("the chapter page", chapter_url)

        # Wait for the React app to mount and the chapter API to fire,
        # which populates .rpage-page divs. Poll up to 30 s — most
        # chapters mount in 3-8 s but the CF turnstile / slow networks
        # can push that out.
        # `mount_polls` exists so the failure path can tell "we waited and the
        # reader never mounted" apart from "the budget was already gone before
        # we looked". `deadline` is armed at the top of this method, i.e.
        # BEFORE the preload step, the navigation, and any WAF handoff (which
        # alone may legitimately burn 180s of a 300s budget) — so the loop can
        # break on its very first line having waited nothing at all. The old
        # message claimed "after wait" unconditionally, which in that case was
        # simply false and sent you looking for a render bug.
        page_count = 0
        mount_polls = 0
        mount_started_at = _time.monotonic()
        for _ in range(60):
            if _time.monotonic() > deadline:
                break
            try:
                page_count = page.evaluate(
                    "() => document.querySelectorAll('.rpage-page').length"
                ) or 0
            except Exception:
                page_count = 0
            mount_polls += 1
            if page_count > 0:
                break
            page.wait_for_timeout(500)
        mount_waited_s = _time.monotonic() - mount_started_at

        if page_count == 0:
            # Re-check before blaming the render: a WAF redirect can land here
            # too (it may arrive after the initial navigation settled).
            self._enforce_no_waf("the chapter page", chapter_url)
            # Gather evidence rather than guessing. The chapter-LIST scrape has
            # done this for a while (grep "no chapter rows rendered on page 1")
            # and the search scrape sniffs CF; this path used to assert "React
            # failed to mount or CF re-challenged" without testing either, so a
            # renamed selector was indistinguishable from a transient miss.
            probe: Dict[str, Any] = {}
            try:
                probe = page.evaluate("""() => {
                    const n = (s) => document.querySelectorAll(s).length;
                    return {
                        dataPage: n('[data-page]'),
                        rpageAny: n('[class*="rpage-"]'),
                        imgs: n('img'),
                        title: document.title || '',
                        url: location.href,
                        text: document.body
                            ? document.body.innerText.slice(0, 400) : '',
                    };
                }""") or {}
            except Exception:
                probe = {}
            body_text = str(probe.get("text") or "")
            causes: List[str] = []
            if mount_polls == 0:
                causes.append(
                    f"the {time_budget_s:.0f}s time budget was already spent "
                    f"before the mount wait began (navigation and/or a "
                    f"verification handoff consumed it) — nothing was waited for"
                )
            if probe.get("rpageAny") or probe.get("dataPage"):
                # The reader IS on screen; only our selector missed. This is the
                # analogue of the chapter list's ".mchap-row__primary exists but
                # .mchap-item doesn't" hint.
                causes.append(
                    f"the reader DID mount ([class*=rpage-]="
                    f"{probe.get('rpageAny')}, [data-page]="
                    f"{probe.get('dataPage')}, img={probe.get('imgs')}) but no "
                    f"'.rpage-page' matched — comix most likely renamed the "
                    f"page container; update the selectors in "
                    f"fetch_chapter_images_via_dom"
                )
            if _CF_AVAILABLE and body_text:
                try:
                    if is_cf_challenge(200, body_text):
                        causes.append("the body looks like a Cloudflare challenge")
                except Exception:
                    pass
            if not causes:
                causes.append(
                    "the reader never mounted (no rpage-* nodes at all) — a "
                    "slow/blocked chapter API, or the chapter genuinely has no "
                    "pages"
                )
            snippet = " ".join(body_text.split())[:200]
            print(
                f"[!] Comix: chapter had 0 .rpage-page divs after "
                f"{mount_waited_s:.0f}s / {mount_polls} poll(s). "
                f"{'; '.join(causes)}. "
                f"url={probe.get('url') or chapter_url!r} "
                f"title={probe.get('title')!r} preload_all={preload_all} "
                f"Visible text: {snippet}",
                flush=True,
            )
            return empty

        print(
            f"[*] Comix: chapter has {page_count} pages; capturing "
            f"each via Patchright (<img> src, or canvas pixels if a page "
            f"is tile-scrambled).",
            flush=True,
        )

        # ── Step 3: converge on the whole working set.
        # The probe caps to the first N pages; the download path takes them all.
        # Capping the TARGET SET (rather than breaking out mid-walk like the old
        # loop) is what keeps the probe from ever waiting on the chapter's tail.
        targets = list(range(1, page_count + 1))
        if max_capture_pages is not None:
            targets = targets[: max(0, int(max_capture_pages))]

        # page -> {"type": "img"|"canvas", "url": str|None, "had_img": bool}
        resolved: Dict[int, Dict[str, Any]] = {}
        # page -> last DOM state we saw, for the failure diagnostic. A bare list
        # of page numbers (what this used to print) can't distinguish "never
        # rendered" from "img src was set but hadn't decoded", which is exactly
        # the distinction you need to fix a capture miss without a repro.
        last_state: Dict[int, Any] = {}

        def _classify(st: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            """Decide whether one page's DOM state is capturable.

            Precedence is canvas-then-img, byte-for-byte the old per-page poll's
            rule — see the class docstring for why that ordering is under
            suspicion but deliberately UNCHANGED here.
            """
            if not st or st.get("missing"):
                return None
            has_img = bool(
                st.get("src")
                and st.get("complete")
                and int(st.get("nw") or 0) > 0
            )
            if (
                st.get("hasCanvas")
                and int(st.get("cw") or 0) > 0
                and int(st.get("ch") or 0) > 0
                and not st.get("loading")
            ):
                return {"type": "canvas", "url": None, "had_img": has_img}
            if has_img:
                return {"type": "img", "url": st["src"], "had_img": True}
            return None

        # ONE evaluate reports every pending page. This is the round-trip win:
        # the old loop paid a scroll + up to 40 polls PER PAGE.
        _POLL_JS = """(nums) => nums.map((n) => {
            const el = document.querySelector(
                '.rpage-page[data-page="' + n + '"]');
            if (!el) return {n: n, missing: true};
            const c = el.querySelector('canvas');
            const i = el.querySelector('img');
            return {
                n: n,
                loading: el.classList.contains('is-loading'),
                hasCanvas: !!c,
                cw: c ? c.width : 0,
                ch: c ? c.height : 0,
                src: i ? (i.src || '') : '',
                complete: i ? !!i.complete : false,
                nw: i ? i.naturalWidth : 0,
            };
        })"""

        # instant + center so the IntersectionObserver fires immediately and the
        # neighbours preload too. Only the lowest unresolved page is nudged per
        # round; over rounds that walks naturally down the chapter.
        _SCROLL_JS = (
            "(n) => { const el = document.querySelector("
            "'.rpage-page[data-page=\"' + n + '\"]'); "
            "if (el) el.scrollIntoView("
            "{behavior: 'instant', block: 'center'}); }"
        )

        def _converge(pending: List[int]) -> List[int]:
            """Poll ``pending`` until empty, the deadline passes, or no page has
            resolved for _COMIX_CAPTURE_STALL_S. Returns the still-unresolved.

            Progress on ANY page refreshes the stall clock, which is the whole
            behavioural change: a straggler keeps getting re-examined for as
            long as the chapter is still coming in, instead of being abandoned
            after a private window that expired mid-walk.
            """
            last_progress = _time.monotonic()
            while pending:
                if _time.monotonic() > deadline:
                    print(
                        f"[!] Comix: hit time budget {time_budget_s:.0f}s with "
                        f"{len(pending)} page(s) of {page_count} unresolved — "
                        f"returning what we have.",
                        flush=True,
                    )
                    break
                try:
                    states = page.evaluate(_POLL_JS, pending) or []
                except Exception:
                    states = []
                still: List[int] = []
                progressed = False
                for st in states:
                    try:
                        n = int(st.get("n"))
                    except (TypeError, ValueError):
                        continue
                    last_state[n] = st
                    decision = _classify(st)
                    if decision is None:
                        still.append(n)
                        continue
                    resolved[n] = decision
                    progressed = True
                if not states:
                    # The evaluate itself failed (page navigating, transport
                    # blip). Keep the set intact and let the stall clock decide.
                    still = list(pending)
                pending = still
                if not pending:
                    break
                now = _time.monotonic()
                if progressed:
                    last_progress = now
                elif now - last_progress > _COMIX_CAPTURE_STALL_S:
                    break
                try:
                    page.evaluate(_SCROLL_JS, pending[0])
                except Exception:
                    pass
                page.wait_for_timeout(
                    int(_COMIX_PAGE_POLL_INTERVAL_S * 1000)
                )
            return pending

        def _harvest_canvas(pages: List[int]) -> List[int]:
            """Read canvas pixels for ``pages`` into image_cache under synthetic
            comix-page:// keys. Returns the pages whose read FAILED (caller
            demotes them back to unresolved).

            MUST run before any reload — a reload discards the rendered pixels,
            and unlike an <img> src there is nothing left to re-fetch. comix's
            real /si/ URL would serve the SCRAMBLED bytes, which is the whole
            reason the synthetic key exists.
            """
            failed: List[int] = []
            for p in sorted(pages):
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
                    failed.append(p)
                    continue
                if not data_url or not data_url.startswith("data:image/"):
                    failed.append(p)
                    continue
                try:
                    _hdr, b64 = data_url.split(",", 1)
                    decoded = _b64.b64decode(b64)
                except Exception:
                    failed.append(p)
                    continue
                synthetic_url = f"comix-page://{chap_id}/{p:04d}.webp"
                if _image_cache is not None:
                    _image_cache.cache_image(
                        synthetic_url, decoded, "image/webp",
                    )
                resolved[p]["url"] = synthetic_url
            return failed

        def _pending_canvas() -> List[int]:
            return [
                p for p, d in resolved.items()
                if d["type"] == "canvas" and not d["url"]
            ]

        unresolved = _converge(list(targets))
        for p in _harvest_canvas(_pending_canvas()):
            resolved.pop(p, None)
            unresolved.append(p)

        # ── Step 4: one reload retry for whatever is still missing.
        # This is the handler's own retry policy, the one IncompleteChapterError
        # is documented to sit behind (sites/base.py). Bounded to ONE reload: a
        # page that survives a fresh render plus a full convergence pass is not
        # going to appear on a third try inside the same budget, and the caller
        # has its own 30s/60s inline retries for the transient case.
        #
        # DOWNLOAD PATH ONLY. The probe samples representative pages and already
        # tolerates a partial capture, so completeness buys it nothing — and a
        # reload plus a second convergence would spend its whole 60s budget
        # chasing pages it was never going to score.
        if max_capture_pages is None and unresolved and _time.monotonic() < deadline:
            print(
                f"[*] Comix: {len(unresolved)} page(s) unresolved "
                f"({', '.join(str(p) for p in sorted(unresolved)[:10])}"
                f"{'…' if len(unresolved) > 10 else ''}); reloading the "
                f"chapter once and retrying just those.",
                flush=True,
            )
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
                self._enforce_no_waf("the chapter page", chapter_url)
                # Let the reader re-mount before polling, else the first round
                # sees an empty DOM and burns a stall interval on nothing.
                for _ in range(60):
                    if _time.monotonic() > deadline:
                        break
                    try:
                        if page.evaluate(
                            "() => document.querySelectorAll("
                            "'.rpage-page').length"
                        ):
                            break
                    except Exception:
                        pass
                    page.wait_for_timeout(500)
                unresolved = _converge(sorted(unresolved))
                for p in _harvest_canvas(_pending_canvas()):
                    resolved.pop(p, None)
                    unresolved.append(p)
            except Exception as e:
                print(
                    f"[!] Comix: reload retry failed "
                    f"({type(e).__name__}: {e}); keeping the pages already "
                    f"captured.",
                    flush=True,
                )

        # ── Step 5: assemble in PAGE ORDER. The old loop's append-in-order was
        # only correct because it walked sequentially; convergence resolves
        # pages in whatever order the browser finishes them.
        urls: List[str] = [
            resolved[p]["url"] for p in sorted(resolved) if resolved[p]["url"]
        ]
        # Derive the miss list from the TARGETS rather than trusting what
        # _converge handed back. A state row with an unusable "n" (or a poll
        # returning fewer rows than it was asked about) would otherwise drop the
        # page out of both sets — the raise in get_chapter_images still fires on
        # the count, but the diagnostic would omit the very page you need named.
        unresolved = [
            p for p in targets
            if p not in resolved or not resolved[p]["url"]
        ]
        canvas_count = sum(
            1 for d in resolved.values() if d["type"] == "canvas"
        )
        img_count = sum(1 for d in resolved.values() if d["type"] == "img")
        canvas_with_img = sum(
            1 for d in resolved.values()
            if d["type"] == "canvas" and d["had_img"]
        )
        scrambled_seen = max(
            0, len(self._scrambled_urls) - scramble_baseline
        )

        # THE MEASUREMENT. canvas fired on 6/68 pages of one live chapter and
        # ~8/88 of another (~9% both times) even though comix is believed to
        # have dropped tile-scrambling in 2026-07. Two incompatible readings —
        # real per-page scrambling, or the canvas-first poll winning a race
        # against a perfectly ordinary <img> — and this line separates them:
        #   scrambled=0 AND canvas_with_img == canvas  -> race; prefer <img> and
        #     stop paying a lossy toDataURL re-encode on ~9% of every chapter.
        #   scrambled ≈ canvas                         -> real; canvas is load-
        #     bearing, optimise the encode instead.
        # Behaviour is UNCHANGED until that reads one way or the other.
        print(
            f"[*] Comix capture shapes: img={img_count} canvas={canvas_count} "
            f"({canvas_with_img} of which also had a complete <img>); "
            f"scramble-headered responses this chapter={scrambled_seen}.",
            flush=True,
        )

        capture = ComixChapterCapture(
            urls=urls,
            expected_pages=len(targets),
            canvas_pages=canvas_count,
            img_pages=img_count,
            canvas_with_img=canvas_with_img,
            scrambled_responses=scrambled_seen,
            unresolved={p: last_state.get(p) for p in sorted(unresolved)},
        )

        # Probe-capture path logs its own line (a capped "8/70" is success, not
        # the partial-failure the download summary below would imply).
        if max_capture_pages is not None:
            print(
                f"[*] Comix probe capture: grabbed {len(urls)} page(s) "
                f"(cap {max_capture_pages}) of {page_count} for image-quality "
                f"sampling.",
                flush=True,
            )
            return capture

        if unresolved:
            # Report the last observed DOM state, not just the page number.
            def _describe(p: int) -> str:
                st = last_state.get(p) or {}
                if st.get("missing"):
                    return f"{p}(no .rpage-page div)"
                bits = []
                if st.get("hasCanvas"):
                    bits.append(f"canvas {st.get('cw')}x{st.get('ch')}")
                if st.get("src"):
                    bits.append(
                        f"img complete={bool(st.get('complete'))} "
                        f"naturalWidth={st.get('nw')}"
                    )
                else:
                    bits.append("img src unset")
                if st.get("loading"):
                    bits.append("is-loading")
                return f"{p}({'; '.join(bits)})"

            shown = sorted(unresolved)[:5]
            more = (
                f" (+{len(unresolved) - 5} more)"
                if len(unresolved) > 5 else ""
            )
            print(
                f"[!] Comix capture: {len(urls)}/{len(targets)} pages captured "
                f"({canvas_count} via canvas, {img_count} via <img>). "
                f"{len(unresolved)} page(s) never rendered, even after a reload "
                f"retry: {', '.join(_describe(p) for p in shown)}{more}. "
                f"The caller will treat this chapter as incomplete.",
                flush=True,
            )
        else:
            print(
                f"[*] Comix capture: {len(urls)}/{len(targets)} pages captured "
                f"({canvas_count} via canvas, {img_count} via <img>). "
                f"All pages rendered.",
                flush=True,
            )
        return capture



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

    def fetch_series_html(self, url: str) -> Optional[str]:
        """Bridge facade for the browser-sourced title-page read.

        Budget = one navigation plus a possible interactive solve, since
        _enforce_no_waf runs INSIDE the session method and can legitimately sit
        on a 180s human handoff. Deliberately does NOT swallow exceptions the
        way the search facade does: ComixWafChallengeError must reach
        _waf_recover_once, which turns it into the user-facing remediation.
        Cross-file: _ComixBrowserSession.fetch_series_html.
        """
        try:
            solve_timeout = float(
                os.environ.get(_WAF_SOLVE_TIMEOUT_ENV)
                or _WAF_DEFAULT_SOLVE_TIMEOUT_S
            )
        except (TypeError, ValueError):
            solve_timeout = _WAF_DEFAULT_SOLVE_TIMEOUT_S
        return _comix_call(
            "fetch_series_html",
            url,
            _timeout_s=60.0 + solve_timeout + 60.0,
        )

    def context_cookies(self) -> List[Dict[str, Any]]:
        """Bridge facade for reading the persistent context's cookies.

        Never raises — the only caller uses this to opportunistically warm an
        HTTP session, so a bridge timeout must not become a download failure.
        """
        try:
            return _comix_call("context_cookies", _timeout_s=15.0) or []
        except Exception:
            return []

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
    ) -> ComixChapterCapture:
        """Bridge facade for chapter-page image capture.

        The chapter page's `/api/v1/chapters/{id}` response is signed +
        encrypted (`{"e": "..."}`) and Python can't reproduce/decrypt it, so
        drive the browser to render the chapter and scrape the page URLs. Pages
        are plain <img> webp CDN URLs now (comix dropped the tile-scramble); a
        legacy <canvas> branch remains as a fallback — see
        _ComixBrowserSession.fetch_chapter_images_via_dom for the details.

        Default 300 s budget covers ~126-page chapters. Inner deadline + 30 s
        outer cap matches fetch_chapters_via_dom. Populates
        sites/image_cache.py with page bytes; aio-dl.py:dl_image reads real CDN
        URLs from there, and canvas-captured pages via synthetic
        `comix-page://<chap_id>/<NNNN>.webp` keys.

        ``max_capture_pages`` (default None = every page, the download path)
        limits the capture to the FIRST that many pages — the image-quality
        probe (ComixSiteHandler._probe_chapter_aggregate) passes
        _COMIX_PROBE_PAGE_CAP so one chapter renders in ~5-15 s instead of
        minutes. It also shrinks ``expected_pages`` to match, so a deliberately
        capped probe render is never mistaken for a truncated chapter.

        Returns ComixChapterCapture. Callers that only want the URLs read
        ``.urls``; ``get_chapter_images`` additionally compares against
        ``.expected_pages`` to enforce the no-missing-pages contract.
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
