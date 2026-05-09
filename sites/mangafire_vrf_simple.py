"""MangaFire VRF token capture using Playwright (Sync API).

This module captures the `vrf=` query parameter from the same network requests
your browser makes after navigating to reader pages.

v11 fixes (from your log):
- Playwright Python **does not** have `Page.wait_for_request()` (Node has it). Use
  `Page.expect_request()` instead.
- Remove NameError: `net_phase` (use the module's cross-process net guard).
- Make `ensure_vrf()` / `generate_vrf()` safe if called inside an asyncio loop
  by executing Playwright sync work in a dedicated helper thread.
"""

from __future__ import annotations

import atexit
import asyncio
import concurrent.futures
import contextlib
import json
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse


# ----------------------------- cross-process coordination -----------------------------


class _AIOFileLock:
    """A tiny cross-process lock (best-effort).

    This is intentionally small and dependency-free; it only needs to serialize
    Playwright *navigation* across downloader workers.
    """

    def __init__(self, lock_path: str):
        self.lock_path = lock_path
        self._fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.lock_path) or ".", exist_ok=True)
        self._fh = open(self.lock_path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            # best-effort: if locking fails, we still continue
            pass
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._fh:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            if self._fh:
                self._fh.close()
        except Exception:
            pass
        self._fh = None


class _AIOCoordinator:
    """Shared cooldown + a NET phase lock.

    - NET lock serializes Playwright navigation across processes.
    - State.json shares a cooldown window + min-gap so other workers slow down.
    """

    def __init__(self, coord_dir: str, net_min_gap: float = 0.25):
        self.coord_dir = os.path.abspath(coord_dir)
        os.makedirs(self.coord_dir, exist_ok=True)
        self.net_min_gap = max(0.0, float(net_min_gap or 0.0))
        self._net_lock = _AIOFileLock(os.path.join(self.coord_dir, "phase_vrf.lock"))
        self._state_lock = _AIOFileLock(os.path.join(self.coord_dir, "state.lock"))
        self._state_path = os.path.join(self.coord_dir, "state.json")

    def _read_state(self) -> dict:
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {"cooldown_until": 0.0, "last_net_ts": 0.0}

    def _write_state(self, data: dict) -> None:
        try:
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self._state_path)
        except Exception:
            pass

    def set_cooldown(self, seconds: float, reason: str = "") -> None:
        if seconds <= 0:
            return
        until = time.time() + float(seconds)
        with self._state_lock:
            st = self._read_state()
            if until > float(st.get("cooldown_until", 0.0) or 0.0):
                st["cooldown_until"] = until
                if reason:
                    st["cooldown_reason"] = str(reason)[:160]
                self._write_state(st)

    def _wait_ready_unlocked(self) -> None:
        while True:
            with self._state_lock:
                st = self._read_state()
                until = float(st.get("cooldown_until", 0.0) or 0.0)
                last_ts = float(st.get("last_net_ts", 0.0) or 0.0)

            now = time.time()
            wait_cd = max(0.0, until - now)
            wait_gap = 0.0
            if self.net_min_gap > 0 and last_ts > 0:
                wait_gap = max(0.0, (last_ts + self.net_min_gap) - now)

            wait = max(wait_cd, wait_gap)
            if wait <= 0:
                break
            time.sleep(min(wait, 1.0))

        with self._state_lock:
            st = self._read_state()
            st["last_net_ts"] = time.time()
            self._write_state(st)

    @contextlib.contextmanager
    def net_phase(self, label: str = ""):
        # Wait outside the phase lock so other processes are not blocked while idling.
        self._wait_ready_unlocked()
        with self._net_lock:
            yield


_COORD: Optional[_AIOCoordinator] = None


def _init_coord() -> None:
    global _COORD
    coord_dir = os.getenv("AIO_COORD_DIR", "").strip()
    enabled = os.getenv("AIO_COORD_ENABLED", "").strip() not in ("", "0", "false", "False")
    if enabled and coord_dir:
        try:
            _COORD = _AIOCoordinator(
                coord_dir=coord_dir,
                net_min_gap=float(os.getenv("AIO_NET_MIN_GAP", "0.25")),
            )
        except Exception:
            _COORD = None


_init_coord()


def _net_guard(label: str = ""):
    if _COORD is None:
        return contextlib.nullcontext()
    return _COORD.net_phase(label)


# ----------------------------- Patchright import -----------------------------
# Patchright is a drop-in for Playwright Sync API (same imports, same Browser/
# Context/Page classes, same `python -m <pkg> install chromium` CLI). It patches
# CDP-leak fingerprints (navigator.webdriver, etc.) at the protocol level —
# vanilla Playwright is detected by Cloudflare on aggregator sites, which
# triggers redirect-to-homepage on rapid sequential chapter navigations.
#
# We keep the module variables called "PLAYWRIGHT_*" because the public-facing
# constant (PLAYWRIGHT_AVAILABLE) is consumed by aio_search_cli.py and was the
# pre-existing flag — renaming it would cascade through callers without
# functional benefit. Treat "playwright" in identifiers as a generic stand-in
# for "headless browser driver" from here on.


try:
    from patchright.sync_api import Browser, BrowserContext, Page, sync_playwright

    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False


_BASE_URL = "https://mangafire.to"

# Resource types needed for VRF capture. Everything else is blocked
# to speed up navigation (no stylesheets, images, fonts, media, etc.)
_VRF_ALLOWED_TYPES = {"document", "script", "xhr", "fetch"}


# ----------------------------- stealth + persistence config -----------------------------
# Persistent browser state (cookies + localStorage) lives here. cf_clearance
# from Cloudflare lasts 30min–several hours; reusing it across runs eliminates
# the warmup challenge. Path uses ~/.aio-dl/cache/ which is the same root the
# --multi-source-prefetched flag writes to (see aio-dl.py argparse).
_DEFAULT_STATE_DIR = os.path.expanduser(
    os.path.join("~", ".aio-dl", "cache", "mangafire")
)
_DEFAULT_STATE_PATH = os.path.join(_DEFAULT_STATE_DIR, "storage_state.json")
# Reject saved state older than this (Cloudflare cookies have already
# expired naturally; loading them just confuses fingerprint heuristics).
_STATE_MAX_AGE_SECONDS = 24 * 3600

# UA + viewport. We initially tried a mobile UA (Pixel 7) per a Tachiyomi-
# documented MangaFire CF workaround, but mobile clients trigger MangaFire's
# server-side mobile detection (Sec-CH-UA-Mobile hint + UA sniffing), which
# serves a stripped-down homepage that doesn't load the jQuery bundle the
# typeahead search relies on. capture_search then crashes with "window.jQuery
# is not a function" even after wait_for_function reports jQuery as defined,
# because the mobile bundle's jQuery handle is in a different scope or
# unloaded between the wait and the trigger.
#
# Patchright's CDP-leak patches plus persistent storage_state cf_clearance
# are doing the Cloudflare-evasion heavy lifting; the mobile UA was belt-
# and-suspenders that turned out to break typeahead. Sticking with a current
# desktop Chrome UA gives Cloudflare the same fingerprint a real desktop
# browser would and lets MangaFire serve the full desktop bundle.
_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_DESKTOP_VIEWPORT = {"width": 1280, "height": 800}

# Reference-only: a stealth init script we previously layered on top of
# Patchright. We removed it (see _initialize for the rationale) because
# Object.defineProperty leaves a detectable signature
# (Object.getOwnPropertyDescriptor(navigator, 'webdriver').get returns a
# function rather than undefined). Patchright handles this at the protocol
# level without that side effect. Keeping the constant here documents the
# decision and makes A/B re-enabling trivial if a different target site
# needs additional stealth that Patchright doesn't cover.
_STEALTH_INIT_SCRIPT_UNUSED = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
window.chrome = window.chrome || { runtime: {} };
""".strip()

# Throttle for in-flight `_save_storage_state(throttle=True)`. Saves are
# triggered every captured VRF; this keeps file I/O at ≤ once per minute.
# Forced saves (warmup-end, close()) bypass the throttle.
_STATE_SAVE_THROTTLE_SECONDS = 60.0


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _now() -> str:
    return time.strftime("%H:%M:%S")


def _pid_tid() -> str:
    return f"pid={os.getpid()} tid={threading.get_ident()}"


def _short(s: str, n: int = 220) -> str:
    s = str(s).replace("\r", " ").replace("\n", " ")
    return s[:n] + ("…" if len(s) > n else "")


def _classify_exc(e: BaseException) -> str:
    msg = str(e).lower()
    if "target page, context or browser has been closed" in msg:
        return "TargetClosed"
    if "interrupted by another navigation" in msg:
        return "InterruptedByNavigation"
    if "timeout" in msg:
        return "Timeout"
    if "net::err_aborted" in msg or "err_aborted" in msg:
        return "ErrAborted"
    if "frame was detached" in msg:
        return "FrameDetached"
    return e.__class__.__name__


@dataclass
class _AttemptInfo:
    attempt: int
    total: int
    stage: str
    page_url: str
    expected_path: str
    pre_url: str = ""
    post_url: str = ""
    goto_status: Optional[int] = None
    error_class: str = ""
    error_msg: str = ""


class SimpleMangaFireVRFGenerator:
    """Captures VRF tokens by listening to browser network requests."""

    def __init__(self) -> None:
        if not PLAYWRIGHT_AVAILABLE:
            raise ImportError(
                "Playwright is required. Install with: pip install playwright && playwright install chromium"
            )

        self._NAV_RETRIES = _env_int("MANGAFIRE_VRF_NAV_RETRIES", 4)
        self._RETRY_DELAY = _env_float("MANGAFIRE_VRF_RETRY_DELAY", 0.5)
        self._RETRY_CAP = _env_float("MANGAFIRE_VRF_RETRY_CAP", 5.0)
        self._RETRY_JITTER = _env_float("MANGAFIRE_VRF_RETRY_JITTER", 0.25)
        self._DOM_TIMEOUT_MS = _env_int("MANGAFIRE_VRF_NAV_TIMEOUT_MS", 45000)
        self._WAIT_REQ_MS = _env_int("MANGAFIRE_VRF_WAIT_REQUEST_MS", 12000)
        self._POST_NAV_SETTLE_MS = _env_int("MANGAFIRE_VRF_POST_NAV_SETTLE_MS", 350)
        self._HEADLESS = os.getenv("MANGAFIRE_VRF_HEADLESS", "1").strip() not in ("0", "false", "False")

        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._initialized = False
        self._warmup_url: Optional[str] = None

        self._vrf_cache: Dict[str, str] = {}
        self._vrf_meta: Dict[str, Dict[str, Any]] = {}

        # Throttle for incremental storage_state writes. See _save_storage_state.
        self._last_state_save_ts: float = 0.0

        # Layer-4 (in-page fetch) circuit breaker. The first call to
        # _capture_via_inpage_fetch sets this to True; on subsequent calls,
        # if a previous attempt has failed (_inpage_fetch_failed), the
        # method returns False immediately without burning the 2s poll
        # budget. Empirically MangaFire's hook is chapter-scoped — it only
        # fires for the chapter the page was loaded with, so the warmup
        # chapter benefits but every subsequent chapter wastes 2s. Once we
        # know it's chapter-scoped (one failure proves it), short-circuit.
        self._inpage_fetch_attempted: bool = False
        self._inpage_fetch_failed: bool = False

        self._op_lock = threading.Lock()
        atexit.register(self.close)

    # ----------------------------- logging -----------------------------

    def _log(self, msg: str) -> None:
        # stderr, not stdout — diagnostic output must not pollute JSON
        # consumers (aio-dl.py --search emits JSON on stdout).
        import sys as _sys
        print(f"[VRF {_pid_tid()} {_now()}] {msg}", file=_sys.stderr)

    def _route_handler(self, route, request):
        """Resource-type filter applied at the context level. Allows only
        document/script/xhr/fetch — saves ~200-500ms per navigation in the
        VRF capture path. capture_series_meta unroutes this temporarily
        because rendering a series page reliably needs stylesheets too."""
        rt = getattr(request, "resource_type", "")
        if rt in _VRF_ALLOWED_TYPES:
            return route.continue_()
        return route.abort()

    def _page_url(self) -> str:
        try:
            return (self._page.url if self._page else "<no-page>") or "<empty>"
        except Exception:
            return "<unavailable>"

    def _log_attempt(self, info: _AttemptInfo) -> None:
        base = (
            f"attempt {info.attempt}/{info.total} stage={info.stage} "
            f"expected={info.expected_path} page={info.page_url}"
        )
        extras: List[str] = []
        if info.pre_url:
            extras.append(f"pre_url={info.pre_url}")
        if info.post_url:
            extras.append(f"post_url={info.post_url}")
        if info.goto_status is not None:
            extras.append(f"goto_status={info.goto_status}")
        if info.error_class:
            extras.append(f"err={info.error_class}")
        if info.error_msg:
            extras.append(f"msg={_short(info.error_msg)}")
        if extras:
            base += " | " + " ".join(extras)
        self._log(base)

    def _sleep_retry(self, attempt_index: int) -> None:
        base = max(0.0, float(self._RETRY_DELAY or 0.0))
        cap = max(base, float(self._RETRY_CAP or 5.0))
        delay = min(cap, base * (2 ** max(0, attempt_index - 1)))
        if self._RETRY_JITTER:
            delay += random.uniform(0.0, float(self._RETRY_JITTER))
        delay = max(0.0, delay)
        if delay:
            self._log(f"sleeping {delay:.1f}s before retry…")
            time.sleep(delay)

    @staticmethod
    def _is_homepage_landing(url: Optional[str], target_host: str) -> bool:
        """True iff `url` is the bare site root for `target_host` — i.e., the
        page is currently at `https://mangafire.to/` (or its `www.` variant)
        with no chapter path. Signals MangaFire's anti-bot redirect that
        bounces rapid sequential chapter-N → chapter-N+1 navigations back to
        the homepage. The page is in a clean baseline state in that case,
        so the next retry's goto from `/` to the chapter URL typically
        succeeds — no need to wait the usual jitter sleep.
        """
        if not url or not target_host:
            return False
        if url.startswith("<"):
            return False  # _page_url's sentinels: "<no-page>", "<empty>", "<unavailable>"
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        if not parsed.netloc:
            return False
        host_match = (
            parsed.netloc == target_host
            or parsed.netloc.endswith("." + target_host)
        )
        if not host_match:
            return False
        return parsed.path in ("", "/")

    # ----------------------------- init -----------------------------

    def _initialize(self, init_url: Optional[str] = None) -> None:
        if self._initialized:
            return

        self._log("initializing Patchright…")
        self._playwright = sync_playwright().start()
        # Extra args reduce Chromium startup time and memory when we only
        # need it for network request interception (VRF capture).
        _LAUNCH_ARGS = [
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-default-apps",
        ]
        self._browser = self._playwright.chromium.launch(
            headless=self._HEADLESS,
            args=_LAUNCH_ARGS,
        )

        # Build context kwargs. Desktop Chrome UA — see the comment above
        # _DESKTOP_UA for why we're not using mobile. storage_state restores
        # cf_clearance + other cookies from a previous run so the Cloudflare
        # challenge cycle is skipped on warmup.
        context_kwargs: Dict[str, Any] = {
            "user_agent": _DESKTOP_UA,
            "viewport": _DESKTOP_VIEWPORT,
            "locale": "en-US",
        }
        storage_path = self._load_storage_state_path()
        if storage_path:
            context_kwargs["storage_state"] = storage_path
            self._log(f"loading saved storage state from {storage_path}")

        try:
            self._context = self._browser.new_context(**context_kwargs)
        except Exception as e:
            # If the file is corrupt / schema-mismatched / Patchright rejects
            # it for any reason, fall back to a clean context. Don't fail the
            # run over a stale cookie cache.
            self._log(f"context init with storage state failed ({_short(e)}); retrying clean")
            context_kwargs.pop("storage_state", None)
            self._context = self._browser.new_context(**context_kwargs)

        # NOTE: we DON'T install a JS-level stealth init script here. Patchright
        # handles webdriver/CDP-leak fingerprinting at the protocol level
        # (invisible to detection scripts), but a JS-level Object.defineProperty
        # over navigator.webdriver leaves a detectable signature —
        # `Object.getOwnPropertyDescriptor(navigator, 'webdriver').get` returns
        # a function instead of undefined, and modern anti-bot scripts use that
        # to detect tampering. On MangaFire specifically, an earlier version of
        # this code installed such a script; the side effect was that MangaFire
        # poisoned window.jQuery as a defense (it'd be defined but not callable),
        # which broke capture_search's typeahead. Trust Patchright; don't layer
        # JS overrides on top of its protocol patches.

        # Faster VRF capture: only allow resource types needed for VRF.
        # document = page HTML, script = JS that computes VRF,
        # xhr/fetch = AJAX requests that carry VRF tokens.
        # Blocking stylesheets, images, fonts, media etc. saves 200-500ms per navigation.
        # The handler is saved as an instance attribute so capture_series_meta
        # can temporarily unroute it (series page rendering needs more resource
        # types than VRF capture does).
        try:
            self._context.route("**/*", self._route_handler)
        except Exception:
            pass

        self._page = self._context.new_page()

        def handle_request(request) -> None:
            try:
                url = request.url
            except Exception:
                return
            if "vrf=" not in url:
                return
            parsed = urlparse(url)
            q = parse_qs(parsed.query)
            vrfs = q.get("vrf") or []
            if not vrfs:
                return
            vrf = vrfs[0]
            path = parsed.path
            self._vrf_cache[path] = vrf
            self._vrf_meta[path] = {
                "ts": time.time(),
                "full_url": url,
                "page_url": self._page_url(),
            }
            self._log(f"captured {path} vrf={vrf[:30]}…")
            # Throttled background save — keeps the on-disk state warm in
            # case of crash/interruption mid-download. The throttle inside
            # _save_storage_state means this is a no-op most of the time.
            try:
                self._save_storage_state(throttle=True)
            except Exception:
                pass  # Belt+suspenders; _save_storage_state already swallows.
        def handle_response(resp) -> None:
            # Soft-block signals: share a cooldown.
            try:
                st = getattr(resp, "status", None)
                if st in (403, 429, 503) and _COORD is not None:
                    _COORD.set_cooldown(12.0, reason=f"playwright_resp:{st}")
            except Exception:
                pass

        self._handle_request = handle_request
        self._handle_response = handle_response
        self._page.on("request", handle_request)
        self._page.on("response", handle_response)

        if not init_url:
            init_url = f"{_BASE_URL}/"

        self._log(f"warm-up navigate: {init_url}")
        try:
            with _net_guard("vrf:warmup"):
                resp = self._page.goto(init_url, wait_until="commit", timeout=60000)
            status = resp.status if resp is not None else None
            self._log(f"warm-up done status={status} url={self._page_url()}")
            if self._POST_NAV_SETTLE_MS:
                self._page.wait_for_timeout(self._POST_NAV_SETTLE_MS)
        except Exception as e:
            self._log(f"warm-up navigation warning: {_classify_exc(e)}: {_short(e)}")

        # Force-save the post-warmup state — even if this run fails downstream,
        # the next run benefits from a fresh cf_clearance cookie. Bypasses the
        # throttle since this is the first successful state we've reached.
        self._save_storage_state(throttle=False)

        self._warmup_url = init_url
        self._initialized = True

    # ----------------------------- storage-state persistence -----------------------------

    def _save_storage_state(self, throttle: bool = True) -> None:
        """Persist the current browser context's cookies + localStorage to
        disk so subsequent runs warm up without re-running Cloudflare's
        challenge.

        Called from:
          - `_initialize` after warmup (throttle=False, force first save)
          - `handle_request` after each captured VRF (throttle=True)
          - `close` on shutdown (throttle=False, final save)

        With throttle=True, no-op if last save was within
        _STATE_SAVE_THROTTLE_SECONDS (default 60s). With throttle=False,
        always writes. Errors are logged but never raised — losing a save
        is not fatal.
        """
        if self._context is None:
            return
        if throttle:
            if time.time() - self._last_state_save_ts < _STATE_SAVE_THROTTLE_SECONDS:
                return
        try:
            os.makedirs(_DEFAULT_STATE_DIR, exist_ok=True)
            # Patchright's storage_state(path=...) writes the JSON atomically
            # via a temp-file-then-rename internally, so a crash mid-write
            # leaves the previous file intact.
            self._context.storage_state(path=_DEFAULT_STATE_PATH)
            self._last_state_save_ts = time.time()
        except Exception as e:
            # Patchright/Playwright sync objects are bound to the thread
            # that created them. There are two atexit hooks (one on the
            # _VRFBridge, one on this instance — see __init__) which can
            # race: the bridge's hook runs close() on the owner thread
            # successfully; the instance hook then re-runs close() on the
            # main thread and Patchright raises a "different thread" error.
            # The save already happened during the bridge's close, so this
            # second-call failure is benign — silence it.
            msg = str(e)
            if "different thread" in msg or "switch to" in msg:
                return
            self._log(f"storage state save warning: {_short(e)}")

    def _load_storage_state_path(self) -> Optional[str]:
        """Return the on-disk storage-state path if it exists and is younger
        than `_STATE_MAX_AGE_SECONDS`, else None.

        Doesn't validate the JSON — Patchright will reject malformed files
        and `_initialize`'s try/except falls back to a clean context.
        """
        try:
            if not os.path.exists(_DEFAULT_STATE_PATH):
                return None
            age = time.time() - os.path.getmtime(_DEFAULT_STATE_PATH)
            if age > _STATE_MAX_AGE_SECONDS:
                self._log(
                    f"saved storage state too old ({age/3600:.1f}h > "
                    f"{_STATE_MAX_AGE_SECONDS/3600:.0f}h); ignoring"
                )
                return None
            return _DEFAULT_STATE_PATH
        except Exception:
            return None

    # ----------------------------- page lifecycle -----------------------------

    def _recreate_page(self, reason: str = "") -> None:
        if self._context is None:
            return
        try:
            if self._page is not None:
                # Remove route intercepts before closing to avoid EPIPE
                try:
                    self._context.unroute_all()
                except Exception:
                    pass
                try:
                    self._page.close()
                except Exception:
                    pass
            self._page = self._context.new_page()

            # Re-register the route handler on the context (unroute_all removed it)
            try:
                self._context.route("**/*", self._route_handler)
            except Exception:
                pass

            if hasattr(self, "_handle_request"):
                self._page.on("request", self._handle_request)
            if hasattr(self, "_handle_response"):
                self._page.on("response", self._handle_response)
            if reason:
                self._log(f"recreated page ({reason})")
        except Exception as e:
            self._log(f"page recreate warning: {_classify_exc(e)}: {_short(e)}")

    # ----------------------------- capture-loop helpers -----------------------------

    def _poll_for_vrf(self, expected_path: str, timeout_ms: int = 3000, interval_ms: int = 100) -> bool:
        """Wait up to ``timeout_ms`` for the request handler to populate
        ``_vrf_cache[expected_path]``. Returns True if captured, False on
        timeout.

        Cheaper than ``expect_request`` because we're not building a
        Playwright-side waiter — the request handler already populates the
        cache from incoming network events. We just pump the page event
        loop with short sleeps so it can dispatch them.

        Used in place of ``expect_request(_pred, timeout=12s)`` in the
        navigation loop. The 12s wait was the dominant cost of every failed
        attempt; this caps the wait at ~3s and bails immediately when we
        detect a homepage redirect (chapter AJAX won't fire there).
        """
        if expected_path in self._vrf_cache:
            return True
        if self._page is None:
            return False
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            try:
                self._page.wait_for_timeout(interval_ms)
            except Exception:
                # Page may have closed under us (e.g. _recreate_page); just
                # return whatever we have in cache.
                return expected_path in self._vrf_cache
            if expected_path in self._vrf_cache:
                return True
        return False

    def _reset_mangafire_cookies(self) -> None:
        """Clear mangafire.to cookies. Called when consecutive
        redirect-to-homepage attempts suggest Cloudflare has flagged the
        session — dropping the bad cf_clearance forces a fresh challenge,
        which often clears faster than waiting out the rate-limit window.

        Best-effort; logged on failure but never raised. Patchright
        signature varies across versions: newer takes a ``domain=`` filter,
        older clears all. We try filtered first, fall back to clear-all.
        """
        if self._context is None:
            return
        try:
            try:
                self._context.clear_cookies(domain="mangafire.to")
            except TypeError:
                self._context.clear_cookies()
        except Exception as e:
            self._log(f"cookie reset warning: {_short(e)}")

    # Path filter for the Layer-4 in-page fetch fast path. Only chapter-
    # payload paths qualify; the chapter-list endpoint is captured during
    # warmup and other paths might not be hooked the same way.
    _CHAPTER_PAYLOAD_PATH_RE = re.compile(r"^/ajax/read/chapter/\d+$")

    def _capture_via_inpage_fetch(self, expected_ajax_path: str) -> bool:
        """Layer 4: fast path that avoids per-chapter navigation.

        MangaFire's chapter pages load a heavily-obfuscated JS bundle (from
        boringegotistical.com — the suspicious-named domain is deliberately
        misleading) that hooks `fetch`/`XMLHttpRequest` and injects ?vrf=…
        into AJAX URLs. The token format is a deterministic encoder over
        (session-secret, path) — the chapter-list and chapter-payload
        VRFs share a long prefix and differ only in the suffix that
        encodes the path.

        That means the hook is almost certainly session-wide: any AJAX from
        the warm chapter page (regardless of which chapter was originally
        loaded) gets a valid VRF computed for the requested path. We
        exploit this by firing fetch() from the page context for an
        arbitrary chapter ID; the hook computes and injects the VRF; our
        request handler picks it up.

        Worst case (chapter-scoped hook, hook checks initiator stack, etc.):
        the request goes out without VRF or returns 404, _poll_for_vrf
        times out, and we return False. Caller falls back to the navigation
        path. No regression.
        """
        # Sanity guards
        if not self._initialized or self._page is None or self._context is None:
            return False
        if expected_ajax_path in self._vrf_cache:
            return True

        # Limit scope: only chapter-payload paths. Other AJAX paths might
        # not be hooked by the obfuscated bundle.
        if not self._CHAPTER_PAYLOAD_PATH_RE.match(expected_ajax_path):
            return False

        # Circuit breaker: if a previous in-page fetch on THIS page failed,
        # the hook is chapter-scoped (verified empirically — MangaFire's
        # obfuscated bundle only injects vrf= for the current chapter URL).
        # Don't burn another 2s polling for nothing. The flag clears on
        # _recreate_page (new page = new hook = worth retrying once).
        if self._inpage_fetch_failed:
            return False

        self._log(f"in-page fetch attempt: {expected_ajax_path}")
        self._inpage_fetch_attempted = True
        try:
            # Fire fetch from page context. The .catch() drops any network
            # error so we don't propagate it back to Python — we don't need
            # the response body, only for the request to leave the page so
            # our handler captures the URL (with the hook-injected vrf=).
            self._page.evaluate(
                "(p) => { try { fetch(p, { credentials: 'include' }).catch(() => null); } catch (e) {} }",
                expected_ajax_path,
            )
        except Exception as e:
            self._log(f"in-page fetch error: {_short(e)}")
            self._inpage_fetch_failed = True
            return False

        # Poll briefly. If the hook is session-wide, the request fires
        # within a few hundred ms; longer timeouts just delay the fallback.
        # 2s gives margin for slow hooks/event loops.
        captured = self._poll_for_vrf(expected_ajax_path, timeout_ms=2000)
        if captured:
            self._log(f"in-page fetch SUCCEEDED for {expected_ajax_path}")
        else:
            self._log(
                "in-page fetch did not capture; hook is chapter-scoped — "
                "disabling in-page fetch for the rest of this session "
                "(short-circuits future calls; saves 2s/chapter)"
            )
            self._inpage_fetch_failed = True
        return captured

    # ----------------------------- core capture -----------------------------

    def _navigate_and_capture(self, page_url: str, expected_ajax_path: str, init_url: Optional[str] = None) -> None:
        if expected_ajax_path in self._vrf_cache:
            return

        with self._op_lock:
            if not self._initialized:
                self._initialize(init_url or page_url)
            if expected_ajax_path in self._vrf_cache:
                return

            assert self._page is not None
            last_exc: Optional[BaseException] = None

            # If warmup already navigated to the target page, the page's
            # JS is still loading. Poll briefly for the VRF XHR to fire
            # from the ongoing warmup load instead of re-navigating to the
            # same URL. Uses the same _poll_for_vrf helper as the main
            # loop — _pred / expect_request are no longer needed since the
            # request handler installed in _initialize already populates
            # _vrf_cache from any matching network event.
            if self._warmup_url and self._warmup_url == page_url:
                self._warmup_url = None  # only try once
                self._poll_for_vrf(expected_ajax_path, timeout_ms=3000)
                if expected_ajax_path in self._vrf_cache:
                    self._log(f"captured VRF during warmup for {expected_ajax_path}")
                    return

            # ── Layer 4: in-page fetch fast path ──
            # Try firing fetch() from the warm page context BEFORE re-
            # navigating. The chapter page's obfuscated network hook
            # (boringegotistical.com bundle) injects the VRF into outbound
            # AJAX. Token-format evidence (chapter-list and chapter-payload
            # VRFs share a long prefix, differ only in the suffix encoding
            # the path) strongly suggests the hook is session-wide and will
            # produce valid VRFs for any chapter ID, not just the one the
            # page was originally loaded for.
            #
            # If the hook turns out to be chapter-scoped or initiator-
            # checked, _capture_via_inpage_fetch returns False quickly (2s
            # poll budget) and we fall through to the navigation loop —
            # exactly the same path we had before this optimization.
            if self._capture_via_inpage_fetch(expected_ajax_path):
                return

            # Target host for the redirect-to-homepage detector below — we
            # extract it from the chapter URL once and reuse it on each
            # retry. Falls back to "mangafire.to" if the URL is malformed
            # (defensive; shouldn't happen since callers pass full URLs).
            try:
                _retry_target_host = urlparse(page_url).netloc or "mangafire.to"
            except Exception:
                _retry_target_host = "mangafire.to"

            # Track consecutive homepage-redirect attempts. Used to escalate
            # the retry action: 1st bounce = immediate retry; 2nd+ bounce =
            # clear cookies (fresh CF challenge); 3+ on right page but no
            # VRF = recreate page (stuck JS/dead context).
            homepage_streak = 0

            for i in range(1, self._NAV_RETRIES + 1):
                if expected_ajax_path in self._vrf_cache:
                    self._log(f"success: captured expected VRF for {expected_ajax_path}")
                    return
                info = _AttemptInfo(
                    attempt=i,
                    total=self._NAV_RETRIES,
                    stage="goto",
                    page_url=page_url,
                    expected_path=expected_ajax_path,
                    pre_url=self._page_url(),
                )
                # Will be set inside the try block; the retry-action
                # selector at the bottom of the loop reads this without
                # re-checking _page_url() (defense against the page state
                # changing between the navigation and the retry decision).
                on_homepage = False
                try:
                    # Cross-process serialization (NET lock + cooldown/gap).
                    with _net_guard("vrf:navigate"):
                        # Plain goto — no expect_request wrapper. Previously
                        # we wrapped this in `expect_request(_pred, 12s)`, but
                        # a redirect-to-homepage means the expected XHR
                        # never fires AND we burn the full 12s timeout per
                        # failed attempt. Now: do the goto, immediately
                        # check the post-nav URL, and either bail (homepage)
                        # or briefly poll the cache (right page, AJAX
                        # imminent). Worst-case wasted attempt: ~3s.
                        try:
                            resp = self._page.goto(
                                page_url,
                                wait_until="commit",
                                timeout=self._DOM_TIMEOUT_MS,
                            )
                        except Exception as e:
                            resp = None
                            last_exc = e

                        info.goto_status = resp.status if resp is not None else None
                        info.post_url = self._page_url()
                        self._log_attempt(info)

                        on_homepage = self._is_homepage_landing(
                            info.post_url, _retry_target_host
                        )
                        if on_homepage:
                            homepage_streak += 1
                            # Don't poll — chapter AJAX won't fire from `/`.
                            # The retry selector below decides escalation.
                        else:
                            homepage_streak = 0
                            # Right page; the request handler should populate
                            # the cache as soon as the page's JS triggers the
                            # AJAX. 3s covers the typical case (~500ms-1s)
                            # with margin for slow pages. The request
                            # handler in _initialize is what actually fills
                            # _vrf_cache; this just yields the event loop.
                            self._poll_for_vrf(
                                expected_ajax_path, timeout_ms=3000
                            )

                    if expected_ajax_path in self._vrf_cache:
                        self._log(f"success: captured expected VRF for {expected_ajax_path}")
                        return

                    # No exception, but still no VRF. Distinguish the two
                    # failure modes in the log so a reader can tell them
                    # apart at a glance.
                    if on_homepage:
                        self._log_attempt(
                            _AttemptInfo(
                                attempt=i,
                                total=self._NAV_RETRIES,
                                stage="redirect-to-homepage",
                                page_url=page_url,
                                expected_path=expected_ajax_path,
                                pre_url=info.pre_url,
                                post_url=info.post_url,
                            )
                        )
                    else:
                        self._log_attempt(
                            _AttemptInfo(
                                attempt=i,
                                total=self._NAV_RETRIES,
                                stage="no-vrf-after-nav",
                                page_url=page_url,
                                expected_path=expected_ajax_path,
                                pre_url=info.pre_url,
                                post_url=info.post_url,
                            )
                        )

                except BaseException as e:
                    last_exc = e
                    # Sometimes a redirect interrupts goto; clear page and retry quickly.
                    msg = str(e)
                    if ("InterruptedByNavigation" in msg) or ("interrupted by another navigation" in msg.lower()):
                        self._recreate_page("InterruptedByNavigation")
                        if i < self._NAV_RETRIES:
                            d = 0.2 + random.uniform(0.0, 0.2)
                            self._log(f"fast retry sleep {d:.1f}s after InterruptedByNavigation…")
                            time.sleep(d)
                            continue

                    self._log_attempt(
                        _AttemptInfo(
                            attempt=i,
                            total=self._NAV_RETRIES,
                            stage="goto-exception",
                            page_url=page_url,
                            expected_path=expected_ajax_path,
                            pre_url=info.pre_url,
                            post_url=self._page_url(),
                            error_class=_classify_exc(e),
                            error_msg=str(e),
                        )
                    )

                if expected_ajax_path in self._vrf_cache:
                    self._log(f"success: captured expected VRF for {expected_ajax_path}")
                    return

                if i < self._NAV_RETRIES:
                    # Retry-action selection by failure mode. Three actions,
                    # ordered by aggressiveness:
                    #
                    #   1. Homepage-bounce (1st time): immediate retry, no
                    #      sleep. Page is at a clean baseline; next goto
                    #      typically lands.
                    #   2. Homepage-bounce (2nd+ consecutive): clear cookies
                    #      to force a fresh CF challenge. The current
                    #      cf_clearance is flagged; new challenge usually
                    #      passes faster than waiting out the rate-limit.
                    #   3. Right-page-no-VRF on attempt 3+: recreate the
                    #      page. The page context is likely stuck (slow JS
                    #      that never fires the AJAX). New page = clean
                    #      slate. Then sleep with backoff.
                    #   4. Otherwise: normal exponential backoff sleep.
                    if on_homepage:
                        if homepage_streak >= 2:
                            self._reset_mangafire_cookies()
                            self._log(
                                f"redirect-to-homepage streak={homepage_streak}; "
                                f"cleared cookies, immediate retry"
                            )
                        else:
                            self._log(
                                "redirect-to-homepage detected; immediate retry "
                                "(skipping jitter sleep — page already at clean baseline)"
                            )
                    elif i >= 3:
                        self._recreate_page("persistent-no-vrf-after-3-attempts")
                        self._sleep_retry(i)
                    else:
                        self._sleep_retry(i)

            # One last check — the background request handler may have
            # captured the VRF during the final retry's exception path or
            # right after the last sleep.  This prevents a spurious error.
            if expected_ajax_path in self._vrf_cache:
                self._log(f"success: late capture of VRF for {expected_ajax_path}")
                return

            recent_paths = list(self._vrf_cache.keys())[-8:]
            summary = (
                f"Could not capture VRF for {expected_ajax_path} after {self._NAV_RETRIES} attempt(s). "
                f"page_now={self._page_url()} cache_size={len(self._vrf_cache)} recent_paths={recent_paths}"
            )
            if last_exc is not None:
                summary += f" last_error={_classify_exc(last_exc)}:{_short(last_exc)}"
            raise RuntimeError(summary)

    # ----------------------------- public API -----------------------------

    def ensure_vrf(
        self,
        url_path: str,
        *,
        page_url: Optional[str] = None,
        init_url: Optional[str] = None,
        max_attempts: Optional[int] = None,
        retry_backoff: Optional[float] = None,
        retry_cap: Optional[float] = None,
        retry_jitter: Optional[float] = None,
        nav_timeout_ms: Optional[int] = None,
        wait_request_ms: Optional[int] = None,
    ) -> str:
        """Ensure a VRF token for a specific AJAX path is present in the cache.

        Typical callers:
        - Chapter list endpoint:  url_path = /ajax/read/{hid}/chapter/{lang}
          Provide init_url pointing to a reader URL that triggers the request, e.g.
          https://mangafire.to/read/manga.{hid}/{lang}/chapter-1

        - Chapter payload endpoint: url_path = /ajax/read/chapter/{chapter_id}
          Provide page_url as the reader URL for that chapter; VRF is chapter-specific.

        This function is sync-only; when used from an asyncio application, callers should
        go through the module's get_vrf_generator() (which returns a thread-bridge).
        """
        s = (str(url_path or "").strip() or "")
        if not s.startswith("/"):
            s = "/" + s

        # Temporary per-call overrides (restore no matter what).
        old = (
            self._NAV_RETRIES,
            self._RETRY_DELAY,
            self._RETRY_CAP,
            self._RETRY_JITTER,
            self._DOM_TIMEOUT_MS,
            self._WAIT_REQ_MS,
        )
        try:
            if max_attempts is not None:
                self._NAV_RETRIES = max(1, int(max_attempts))
            if retry_backoff is not None:
                self._RETRY_DELAY = max(0.05, float(retry_backoff))
            if retry_cap is not None:
                self._RETRY_CAP = max(0.1, float(retry_cap))
            if retry_jitter is not None:
                self._RETRY_JITTER = max(0.0, float(retry_jitter))
            if nav_timeout_ms is not None:
                self._DOM_TIMEOUT_MS = max(1000, int(nav_timeout_ms))
            if wait_request_ms is not None:
                self._WAIT_REQ_MS = max(500, int(wait_request_ms))

            if s in self._vrf_cache:
                return self._vrf_cache[s]

            # Chapter payloads are chapter-specific; require navigation to the chapter reader page.
            if re.match(r"^/ajax/read/chapter/\d+$", s):
                if not page_url:
                    raise RuntimeError(
                        f"VRF for {s} is chapter-specific; provide page_url (the chapter reader URL) to capture it."
                    )
                self._navigate_and_capture(page_url, s, init_url=init_url or page_url)
                if s in self._vrf_cache:
                    return self._vrf_cache[s]
                raise RuntimeError(f"VRF capture failed for {s}. cache_size={len(self._vrf_cache)}")

            # For other endpoints, best effort: navigate to a page that triggers the request.
            trigger = page_url or init_url
            if trigger:
                self._navigate_and_capture(trigger, s, init_url=init_url or trigger)
                if s in self._vrf_cache:
                    return self._vrf_cache[s]

            # Final fallback: use generate_vrf which may know how to trigger-capture specific endpoints.
            return self.generate_vrf(s, init_url=init_url or trigger)
        finally:
            (
                self._NAV_RETRIES,
                self._RETRY_DELAY,
                self._RETRY_CAP,
                self._RETRY_JITTER,
                self._DOM_TIMEOUT_MS,
                self._WAIT_REQ_MS,
            ) = old
    def generate_vrf(self, url_path: str, init_url: Optional[str] = None) -> str:
        """Generate (capture) a VRF token for a non-chapter-specific endpoint.

        This is mainly used for the chapter-list endpoint:
          /ajax/read/{hid}/chapter/{lang}

        If init_url is provided, it should be a reader URL that naturally triggers the
        desired AJAX request so we can capture the VRF from the outgoing request.
        """
        s = (str(url_path or "").strip() or "")
        if not s.startswith("/"):
            s = "/" + s

        if s in self._vrf_cache:
            return self._vrf_cache[s]

        # Chapter payload tokens cannot be generated without navigation to that specific chapter.
        if re.match(r"^/ajax/read/chapter/\d+$", s):
            raise RuntimeError(
                f"Could not generate VRF for {s}. This endpoint is chapter-specific; "
                "call ensure_vrf(url_path, page_url=CHAPTER_URL) first."
            )

        match = re.match(r"^/ajax/read/([^/]+)/chapter/([^/]+)$", s)
        if match:
            hid = match.group(1)
            lang = match.group(2)

            trigger = init_url
            if not trigger:
                # Reasonable default; may redirect, but usually enough to trigger the XHR.
                trigger = f"{_BASE_URL}/read/manga.{hid}/{lang}/chapter-1"

            self._log(f"trigger-capture via {trigger}")
            self._navigate_and_capture(trigger, s, init_url=trigger)
            if s in self._vrf_cache:
                return self._vrf_cache[s]

            raise RuntimeError(
                f"Could not capture VRF for {s} (chapter list). cache_size={len(self._vrf_cache)}"
            )

        raise RuntimeError(f"Could not generate VRF for {s}. No cached token available.")
    # ----------------------------- search capture -----------------------------
    # MangaFire's /filter endpoint is CF-WAF-blocked for HTTP scrapers (cloudscraper,
    # curl_cffi-with-Chrome-impersonation all return 403; verified 2026-05-06).
    # The site's own typeahead bypasses this: jQuery in mangafire's JS bundle binds a
    # keyup handler to .search-inner input[name=keyword] that fires
    # GET /ajax/manga/search?keyword=&vrf=<TOKEN>, which DOES pass CF and returns
    # JSON-wrapped HTML cards for the top 5-6 matches.
    #
    # Recipe (mirrors keiyoushi/extensions-source PR #11396):
    #   1. Navigate to /home (or /) to load the JS bundle and pass any CF challenge.
    #   2. Use jQuery to set the input's value AND trigger 'keyup' — page.fill()
    #      doesn't fire the keyup event so the typeahead doesn't activate.
    #   3. Wait briefly for the XHR response; capture body via page.expect_response.
    #   4. Parse the JSON envelope's result.html for <a class="unit" href="/manga/..."> cards.
    #
    # Reuses the existing browser/context/page across calls (one Chromium per process,
    # warmed up on first capture). Subsequent searches reuse the warm context so they
    # cost ~1-2s each instead of the ~3-5s cold start.
    def capture_search(
        self,
        query: str,
        *,
        timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Drive the typeahead and capture /ajax/manga/search?keyword=&vrf= response.

        Returns a dict shaped like the upstream JSON: {status: 200, result: {count, html, linkMore}}.
        Caller is responsible for parsing result.html (cards with /manga/<slug>.<id> hrefs).
        Raises RuntimeError on capture failure.
        """
        if not query or not query.strip():
            raise RuntimeError("capture_search: query is empty")
        # Default 12s gives the setInterval typeahead-trigger ~12 chances at
        # 1s intervals. The typeahead occasionally rejects the first trigger
        # (especially for some specific titles like 'Witch Hat Atelier' where
        # something in mangafire's debounce/cache stack short-circuits the
        # initial keyup); retry-firing reliably catches those within 4-5
        # iterations. Bumped from 8s after observing ~20% intermittent failures
        # at the 8s budget.
        wait_ms = int(timeout_ms or _env_int("MANGAFIRE_SEARCH_TIMEOUT_MS", 12000))
        # Sanitize the query for jQuery embedding: the call site passes user input
        # directly into a JS string. We disallow quotes and backslashes which would
        # break out of the literal — anything else is fine for typeahead matching.
        clean = re.sub(r"[\"\\\\]", " ", query.strip())

        with self._op_lock:
            if not self._initialized:
                self._initialize(f"{_BASE_URL}/")

            assert self._page is not None
            # Always re-navigate to homepage with wait_until='networkidle'
            # before driving the typeahead. wait_until='load' was racing the
            # keyup handler binding for some queries — the bundle runs scripts
            # in deferred chunks and the typeahead's debounce wires up after
            # an additional setTimeout that 'load' doesn't wait for. 'networkidle'
            # (500ms of no network activity) reliably catches that final tick.
            # Cost is ~500ms extra per first-call vs 'load' — acceptable for
            # the reliability win.
            try:
                with _net_guard("search:nav-home"):
                    self._page.goto(
                        f"{_BASE_URL}/",
                        wait_until="networkidle",
                        timeout=self._DOM_TIMEOUT_MS,
                    )
            except Exception as e:
                self._log(f"search nav warning: {_classify_exc(e)}: {_short(e)}")

            # Wait for the search input element to be present in the DOM. The
            # keyup-handler binding is checked indirectly via the setInterval
            # in the trigger script below — if the first keyup is missed
            # (handler not yet bound), the 1s ticker keeps firing and
            # eventually one fires after binding completes.
            #
            # Why we no longer poll for jQuery state: empirically, MangaFire's
            # bundle leaves window.jQuery in an unstable state during page
            # load — `typeof window.jQuery !== 'undefined'` returns true at
            # one moment but `window.jQuery(...)` throws "is not a function"
            # the next moment, even with no navigation in between. Some bundle
            # script appears to set/clear window.jQuery as part of its
            # initialization. The DOM-event approach below sidesteps this
            # entirely — we don't call jQuery, we dispatch native events on
            # the input element. jQuery's $.on('keyup') is implemented via
            # addEventListener under the hood, so dispatched native events
            # reach the typeahead handler regardless of whether window.jQuery
            # is currently callable.
            try:
                self._page.wait_for_function(
                    """() => !!document.querySelector(".search-inner input[name=keyword]")""",
                    timeout=min(self._DOM_TIMEOUT_MS, 5000),
                )
            except Exception as e:
                raise RuntimeError(f"capture_search: search input never appeared: {_classify_exc(e)}")

            def _is_search_xhr(resp) -> bool:
                try:
                    return "/ajax/manga/search" in resp.url
                except Exception:
                    return False

            # Capture body via response listener — not via expect_response.
            # The context's route handler does route.continue_() for XHR which
            # makes the resp.text() call AFTER the with-block fail with
            # "Network.getResponseBody: No resource with given identifier found"
            # because the renderer has already discarded the body buffer.
            # Reading inside the response event handler (synchronous from CDP's
            # perspective) catches the body while it's still alive.
            captured: Dict[str, Any] = {"body": None, "status": None, "url": None}
            search_event = threading.Event()

            def _on_resp(resp):
                try:
                    if "/ajax/manga/search" in resp.url and not search_event.is_set():
                        try:
                            captured["body"] = resp.text()
                            captured["status"] = resp.status
                            captured["url"] = resp.url
                        except Exception as e:
                            captured["err"] = _short(e)
                        finally:
                            search_event.set()
                except Exception:
                    pass

            self._page.on("response", _on_resp)
            try:
                self._log(f"capture_search: triggering typeahead for '{clean[:40]}'")
                # Mirror Tachiyomi's PR #11396 strategy: use setInterval inside
                # the page so triggers keep firing even if the first one is
                # ignored (the typeahead skips when input value hasn't changed,
                # or when a debounce window is still open). The interval is
                # cleared on the page side once we get a response — but we
                # also clear it from Python via a final evaluate to be safe.
                # Focus the input first — typeahead handlers may check
                # document.activeElement and skip events when the input isn't
                # focused. Click is the most browser-realistic focus.
                try:
                    self._page.click(".search-inner input[name=keyword]")
                except Exception:
                    pass
                eval_result = self._page.evaluate(
                    """(q) => {
                        try {
                            const el = document.querySelector('.search-inner input[name=keyword]');
                            if (!el) return 'err: search input not found';
                            window.__aioMfTickerId && clearInterval(window.__aioMfTickerId);
                            // Set value via the native HTMLInputElement.value
                            // setter so any framework that wraps the property
                            // (React, Vue, jQuery's own input cache) sees the
                            // change and emits 'input'/'keyup' as if the user
                            // typed. Toggling a trailing space ensures each
                            // tick presents a different value, defeating any
                            // de-dupe in the typeahead's debounce.
                            const valSetter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value'
                            ).set;
                            let toggle = false;
                            const fire = () => {
                                toggle = !toggle;
                                const v = q + (toggle ? ' ' : '');
                                valSetter.call(el, v);
                                // Native events. jQuery's $.on('keyup') uses
                                // addEventListener internally, so dispatched
                                // native events reach jQuery-bound handlers
                                // even when window.jQuery is currently
                                // unstable/non-callable. 'input' event is
                                // belt-and-suspenders for any handler bound
                                // to it instead of keyup.
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new KeyboardEvent('keyup', {
                                    bubbles: true, cancelable: true, key: 'a'
                                }));
                            };
                            fire();
                            window.__aioMfTickerId = setInterval(fire, 1000);
                            return 'ok';
                        } catch (e) { return 'err:' + (e && e.message || String(e)); }
                    }""",
                    clean,
                )
                self._log(f"capture_search: native trigger returned {eval_result!r}")
                # Poll the page event loop while waiting for the response.
                deadline = time.monotonic() + wait_ms / 1000.0
                tick_count = 0
                while not search_event.is_set() and time.monotonic() < deadline:
                    self._page.wait_for_timeout(100)
                    tick_count += 1
                self._log(f"capture_search: polling loop done, ticks={tick_count}, captured={search_event.is_set()}, url={captured.get('url')}, err={captured.get('err')}")
            finally:
                try:
                    # Stop the interval ticker so it doesn't keep firing AJAX
                    # while the next capture_search runs (or the page navigates).
                    self._page.evaluate(
                        "() => { window.__aioMfTickerId && clearInterval(window.__aioMfTickerId); window.__aioMfTickerId = null; }"
                    )
                except Exception:
                    pass
                try:
                    self._page.remove_listener("response", _on_resp)
                except Exception:
                    pass

            if not search_event.is_set():
                raise RuntimeError(f"capture_search timed out after {wait_ms}ms waiting for /ajax/manga/search")

            if captured.get("err"):
                raise RuntimeError(f"capture_search body read failed: {captured['err']}")
            status = captured.get("status")
            body = captured.get("body") or ""
            if status != 200:
                raise RuntimeError(f"search response status {status}")

            try:
                data = json.loads(body)
            except Exception as e:
                raise RuntimeError(f"capture_search: response not JSON: {_short(e)}")

            if not isinstance(data, dict):
                raise RuntimeError(f"capture_search: unexpected payload shape: {type(data).__name__}")
            if data.get("status") != 200:
                raise RuntimeError(f"capture_search: payload status={data.get('status')} body={_short(body)}")
            return data

    # ----------------------------- URL-mode metadata capture -----------------
    # capture_series_meta: navigate a MangaFire series URL via the persistent
    # browser and scrape the title/cover/chapter-count without going through
    # the autocomplete typeahead. Used by aio_search_cli.run_search_mode when
    # the user supplies --search "<mangafire_url>" — bypasses the unreliable
    # typeahead so MangaFire is GUARANTEED to participate in cross-site
    # comparison even when its keyword search would have failed.
    #
    # Cross-file: the synthesized SearchHit is wired into search_all via the
    # seed_hits parameter (sites/search_orchestrator.py).
    def capture_chapter_count(self, manga_id: str, language: str = "en") -> Optional[int]:
        """Get the latest chapter number from MangaFire's VRF-protected
        chapter-list AJAX endpoint.

        Reliable: this is the same endpoint `MangaFireSiteHandler.get_chapters`
        uses for chapter-image downloads — it's what powers the user's actual
        downloads, so it works even when the typeahead and series-page nav
        both fail. The VRF capture happens via Playwright navigation to a
        reader URL (which triggers the AJAX automatically), and we listen
        for the response on that page to grab the body.

        Returns max chapter number (int) or None if capture failed.
        """
        if not manga_id:
            return None
        ajax_path = f"/ajax/read/{manga_id}/chapter/{language}"
        reader_url = f"{_BASE_URL}/read/manga.{manga_id}/{language}/chapter-1"

        with self._op_lock:
            if not self._initialized:
                self._initialize(f"{_BASE_URL}/")
            assert self._page is not None

            # Capture the response body in a listener (route.continue_ on the
            # XHR makes resp.text() unreliable after the navigation completes).
            captured: Dict[str, Any] = {"body": None, "status": None}
            captured_event = threading.Event()

            def _on_resp(resp):
                try:
                    if ajax_path in resp.url and not captured_event.is_set():
                        try:
                            captured["body"] = resp.text()
                            captured["status"] = resp.status
                        except Exception as e:
                            captured["err"] = _short(e)
                        finally:
                            captured_event.set()
                except Exception:
                    pass

            self._page.on("response", _on_resp)
            try:
                # The reader URL passes CF reliably (it's been the user's
                # working download path for years). Navigation triggers the
                # chapter-list AJAX during page load.
                try:
                    with _net_guard("series:chapters-nav"):
                        self._page.goto(
                            reader_url,
                            wait_until="commit",
                            timeout=self._DOM_TIMEOUT_MS,
                        )
                except Exception as e:
                    self._log(
                        f"capture_chapter_count: reader nav warning: {_classify_exc(e)}: {_short(e)}"
                    )
                # Poll for response. ~10s budget; the AJAX usually fires
                # within 1-2s of page load.
                deadline = time.monotonic() + 10.0
                while not captured_event.is_set() and time.monotonic() < deadline:
                    self._page.wait_for_timeout(100)
            finally:
                try:
                    self._page.remove_listener("response", _on_resp)
                except Exception:
                    pass

            if not captured_event.is_set():
                self._log(f"capture_chapter_count: no AJAX response within budget")
                return None
            body = captured.get("body") or ""
            status = captured.get("status")
            if status != 200 or not body:
                self._log(f"capture_chapter_count: bad response status={status}")
                return None
            try:
                data = json.loads(body)
            except Exception as e:
                self._log(f"capture_chapter_count: non-JSON body: {_short(e)}")
                return None

            # Response shape: {"status":200,"result":{"html":"<a data-id=... data-number='N' ...> ..."}}
            html_content = None
            result = data.get("result")
            if isinstance(result, dict):
                html_content = result.get("html") or result.get("result") or result.get("data")
            elif isinstance(result, str):
                html_content = result
            if not html_content:
                return None
            try:
                from bs4 import BeautifulSoup
            except ImportError:
                return None
            soup = BeautifulSoup(html_content, "html.parser")
            nums: List[float] = []
            for a in soup.select("a[data-id]"):
                num_str = a.get("data-number") or a.get("data-num") or a.get("data-chapter")
                if not num_str:
                    continue
                try:
                    nums.append(float(num_str))
                except ValueError:
                    continue
            if not nums:
                return None
            self._log(f"capture_chapter_count: max={int(max(nums))} (over {len(nums)} entries)")
            return int(max(nums))

    @staticmethod
    def _slug_to_query(series_url: str) -> Optional[str]:
        """Extract a search-query candidate from a /manga/<slug>.<id> URL.

        MangaFire slugs sometimes carry a trailing duplicated-char suffix to
        disambiguate (e.g., 'eleceed' → 'eleceedd' when there's a slug
        collision). The trailing duplicate doesn't help fuzzy match, so we
        strip it. Dashes become spaces. Title-case the result.
        """
        try:
            parsed = urlparse(series_url)
        except Exception:
            return None
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 2 or parts[0] != "manga":
            return None
        slug_with_id = parts[1]
        # Format: <slug>.<id>
        slug = slug_with_id.split(".")[0]
        if not slug:
            return None
        # Strip trailing duplicate-char disambiguator if present.
        # Examples: 'eleceedd' → 'eleceed', 'witch-hat-atelierr' → 'witch-hat-atelier'.
        # Heuristic: if the last char appears twice and removing one yields
        # something with at least 3 chars, strip it.
        if len(slug) >= 4 and slug[-1] == slug[-2]:
            stripped = slug[:-1]
            if len(stripped) >= 3:
                slug = stripped
        return slug.replace("-", " ").strip().title() or None

    def _lookup_via_typeahead(
        self, series_url: str, query: str
    ) -> Optional[Dict[str, Any]]:
        """Run the typeahead and find the entry whose URL matches series_url.

        Returns the meta dict on match, None if the typeahead succeeded but
        didn't include our URL (caller should fall back to series-page nav).
        Raises on typeahead failure (caller will catch and fall back).
        """
        # Reuse capture_search; it acquires the op_lock too, so we must call
        # it WITHOUT holding op_lock here. capture_series_meta hasn't acquired
        # the lock yet (we're still in stage 1 before any lock-acquire), so
        # we're safe to call.
        data = self.capture_search(query)
        result = (data or {}).get("result") or {}
        html = result.get("html") or ""
        if not html:
            return None
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return None
        soup = BeautifulSoup(html, "html.parser")
        # Normalize the input URL for comparison: strip query/fragment, ensure
        # leading slash. Match against href on autocomplete cards.
        target_path = urlparse(series_url).path
        for a in soup.select("a.unit[href]"):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            if href.split("?")[0].split("#")[0] != target_path:
                continue
            # Match!
            title_node = a.select_one(".info h6, .info .title, h6, h3")
            title = title_node.get_text(strip=True) if title_node else None
            if not title:
                return None
            cover: Optional[str] = None
            img = a.select_one(".poster img, img")
            if img is not None:
                src = img.get("data-src") or img.get("src")
                if src:
                    cover = src if src.startswith("http") else f"{_BASE_URL}{src}"
            chapter_count: Optional[int] = None
            for span in a.select(".info span"):
                m = re.match(r"(?:Chap|Chapter)\s*([\d.]+)", span.get_text(strip=True), re.I)
                if m:
                    try:
                        chapter_count = int(float(m.group(1)))
                    except ValueError:
                        pass
                    break
            self._log(
                f"capture_series_meta(typeahead): title={title!r} chN={chapter_count}"
            )
            return {
                "title": title,
                "cover": cover,
                "chapter_count": chapter_count,
                "final_url": series_url,
            }
        return None

    def capture_series_meta(self, series_url: str) -> Dict[str, Any]:
        """Get title + cover + chapter_count for a MangaFire series URL.

        Three escalating strategies because CF aggressively bounces direct
        series-page navigation (~80% redirect-to-homepage rate even with
        cf_clearance set, verified empirically):

        1. **Slug-derived typeahead lookup.** Extract slug from URL, run
           capture_search with the slug-as-query, find the autocomplete entry
           whose URL matches the input. Returns accurate title + chN when it
           works. Note: slug ≠ displayed title for licensed series (e.g.,
           'mato-seihei-no-slavee' → query 'Mato Seihei No Slave' → returns
           card titled 'Chained Soldier'); the typeahead's alt-title matching
           handles this correctly.
        2. **Series-page navigation.** Navigate to the URL and scrape the h1.
           Unreliable (CF bounces) but covers cases where typeahead fails.
        3. **Slug-derived fallback.** When both fail, return the slug-derived
           query as the title with chapter_count=None. Guarantees the URL
           gets included as a MangaFire seed in the orchestrator's candidate
           list — the user always sees their URL alongside other sites'
           results, even when we couldn't enrich it. Cross-site merge still
           works via alt-title matching (the slug is usually the romaji,
           which appears in MangaDex's alt-titles).

        Returns a dict with keys: title, cover, chapter_count, final_url.
        Never raises — always provides at least a slug-derived title so the
        seed-hit path doesn't fail. Logs the strategy that succeeded.
        """
        if not series_url or "mangafire.to" not in series_url:
            raise RuntimeError(f"capture_series_meta: not a MangaFire URL: {series_url}")

        # If the caller passed any `/read/<slug>...` URL (chapter URL,
        # truncated /read/<slug> form that the user might paste, or the
        # synthetic /read/manga.<id>/<lang>/chapter-N form constructed
        # internally), rewrite it to the series URL (/manga/<slug>) BEFORE
        # any navigation stage. Otherwise the series-page parser (looking
        # for h1[itemprop=name]) 3-retries against a non-series page and
        # burns ~15s before falling back to the slug-derived title — the
        # dominant cost of a MangaFire download once VRF capture is fast.
        #
        # Match captures everything after /read/ up to the first /, ?, or #.
        # That's the slug (e.g. munou-na-nanaa.nzmj) — same value MangaFire's
        # /manga/<slug> page expects. Any /lang/chapter-N tail is discarded.
        _read_url_match = re.match(
            r"^https?://(?:www\.)?mangafire\.to/read/([^/?#]+)",
            series_url,
            re.IGNORECASE,
        )
        if _read_url_match:
            rewritten = f"{_BASE_URL}/manga/{_read_url_match.group(1)}"
            self._log(
                f"capture_series_meta: rewriting reader URL to series URL: "
                f"{series_url} -> {rewritten}"
            )
            series_url = rewritten

        slug_query = self._slug_to_query(series_url)

        # Stage 1: typeahead lookup using the slug as the query.
        if slug_query:
            try:
                ta_meta = self._lookup_via_typeahead(series_url, slug_query)
                if ta_meta is not None:
                    return ta_meta
            except Exception as e:
                self._log(f"typeahead lookup failed: {_classify_exc(e)}: {_short(e)}; falling back to series-page nav")

        with self._op_lock:
            if not self._initialized:
                self._initialize(f"{_BASE_URL}/")
            assert self._page is not None

            # Series-page rendering needs more resource types than VRF
            # capture: the SPA's redirect-to-homepage logic triggers when
            # critical resources fail to load. Drop the route filter for
            # this call so the page can fully load (CSS, images, etc.),
            # then restore it before returning so subsequent VRF calls
            # keep their fast-load behavior.
            unrouted = False
            try:
                self._context.unroute_all()
                unrouted = True
            except Exception as e:
                self._log(f"unroute warning: {_classify_exc(e)}: {_short(e)}")

            try:
                # Pre-step: load the homepage with networkidle so CF's JS
                # challenge runs to completion and cf_clearance gets set in
                # the browser cookies. Without this, the FIRST goto to the
                # series URL hits CF's anti-bot redirect-to-homepage flow
                # (no cf_clearance yet → bounce). The warmup in _initialize
                # uses wait_until='commit' which is too fast for cf_clearance
                # establishment but is correct for VRF capture, so we do an
                # extra full-load pass here.
                try:
                    with _net_guard("series:warmup"):
                        self._page.goto(
                            f"{_BASE_URL}/",
                            wait_until="networkidle",
                            timeout=self._DOM_TIMEOUT_MS,
                        )
                    # Extra settle for the cf_clearance cookie to land.
                    self._page.wait_for_timeout(800)
                except Exception as e:
                    self._log(f"series:warmup pre-load warning: {_classify_exc(e)}: {_short(e)}")

                last_err: Optional[Exception] = None
                meta: Optional[Dict[str, Any]] = None
                for attempt in range(1, 4):
                    try:
                        with _net_guard("series:nav"):
                            self._page.goto(
                                series_url,
                                wait_until="networkidle",
                                timeout=self._DOM_TIMEOUT_MS,
                            )
                    except Exception as e:
                        last_err = e
                        self._log(
                            f"series:nav attempt {attempt} raised: {_classify_exc(e)}: {_short(e)}"
                        )
                        time.sleep(0.5 * attempt)
                        continue

                    final_url = self._page_url()
                    parsed_final = urlparse(final_url)
                    if parsed_final.path in ("", "/"):
                        # Redirected to homepage despite the warmup — likely
                        # the CF JS just installed cf_clearance during this
                        # navigation; one more retry should hit the cleared
                        # path.
                        self._log(
                            f"series:nav attempt {attempt} landed on homepage; retrying"
                        )
                        time.sleep(1.0)
                        continue
                    try:
                        meta = self._extract_series_meta(series_url)
                        break
                    except RuntimeError as e:
                        last_err = e
                        self._log(
                            f"series:nav attempt {attempt} extract failed: {_short(e)}"
                        )
                        continue

                if meta is not None:
                    return meta
                self._log(
                    f"capture_series_meta: series-page nav failed after retries (last_err={last_err}); "
                    "using slug-derived fallback"
                )
            finally:
                if unrouted:
                    try:
                        self._context.route("**/*", self._route_handler)
                    except Exception:
                        pass

        # Stage 3: both typeahead and series-page nav failed. Build the
        # slug-derived fallback. Title is approximate (slug → romaji-ish)
        # but it's enough to merge the seed into the right candidate via
        # alt-title matching at the orchestrator level. We then try a
        # last-resort chapter-count probe via the VRF-protected AJAX
        # endpoint (same one that powers the user's actual downloads, so
        # it's the most reliable path we have).
        chapter_count: Optional[int] = self._try_chapter_count_via_vrf(series_url)
        return {
            "title": slug_query or "MangaFire series",
            "cover": None,
            "chapter_count": chapter_count,
            "final_url": series_url,
        }

    def _try_chapter_count_via_vrf(self, series_url: str) -> Optional[int]:
        """Last-resort chapter-count probe for capture_series_meta. Extracts
        manga_id from the URL and calls capture_chapter_count. Wrapped in
        try/except so any failure just leaves chN=None (the user still gets
        the URL-mode seed hit, just without a chN signal for cross-site DMCA
        detection)."""
        try:
            parsed = urlparse(series_url)
        except Exception:
            return None
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 2 or parts[0] != "manga":
            return None
        slug_with_id = parts[1]
        if "." not in slug_with_id:
            return None
        manga_id = slug_with_id.split(".")[-1]
        if not manga_id:
            return None
        try:
            return self.capture_chapter_count(manga_id)
        except Exception as e:
            self._log(f"_try_chapter_count_via_vrf failed: {_classify_exc(e)}: {_short(e)}")
            return None

    def _extract_series_meta(self, series_url: str) -> Dict[str, Any]:
        """Scrape the rendered series page DOM for title/cover/chapter_count.
        Called from capture_series_meta after navigation. Separated so the
        unroute/re-route lifecycle stays in one place."""
        assert self._page is not None

        final_url = self._page_url()

        # Title — the canonical h1 with itemprop=name. If missing, the
        # page redirected to homepage (invalid slug) or failed to render.
        title: Optional[str] = None
        try:
            title_el = self._page.query_selector("h1[itemprop='name']")
            if title_el:
                title = (title_el.text_content() or "").strip()
        except Exception:
            title = None
        if not title:
            # Series page didn't render the h1 — likely invalid URL.
            raise RuntimeError(
                f"capture_series_meta: no title found on {final_url} "
                "(URL may not be a valid MangaFire series page)"
            )

        # Cover — series page exposes the full-size cover at .poster img.
        cover: Optional[str] = None
        try:
            cover_el = self._page.query_selector(".poster img[itemprop='image']")
            if cover_el:
                cover = cover_el.get_attribute("src")
        except Exception:
            cover = None

        # Chapter count — regex over the rendered page text. The chapter
        # list is rendered client-side via AJAX after networkidle; once
        # present, every chapter row contributes a "Chap N" label so the
        # max across matches is the latest chapter number. More resilient
        # than depending on a specific [data-number] selector that the
        # site has churned through.
        chapter_count: Optional[int] = None
        try:
            body_text = self._page.evaluate("() => document.body.innerText")
            if isinstance(body_text, str):
                nums: List[float] = []
                for m in re.finditer(r"Chap(?:ter)?\s*([\d.]+)", body_text, re.I):
                    try:
                        nums.append(float(m.group(1)))
                    except ValueError:
                        continue
                if nums:
                    chapter_count = int(max(nums))
        except Exception:
            chapter_count = None

        self._log(
            f"capture_series_meta: title={title!r} cover={'yes' if cover else 'no'} "
            f"chapter_count={chapter_count}"
        )
        return {
            "title": title,
            "cover": cover,
            "chapter_count": chapter_count,
            "final_url": final_url,
        }

    def close(self) -> None:
        with self._op_lock:
            # ── Step 0: Final storage-state save BEFORE teardown ──
            # We have to capture cookies/localStorage while the context is
            # still alive. Bypass throttle — this is the run's final save and
            # we want it even if the throttle hasn't elapsed since the last.
            try:
                self._save_storage_state(throttle=False)
            except Exception:
                pass  # _save_storage_state already swallows; this is paranoia.

            # ── Step 1: Remove all route intercepts FIRST ──
            # This prevents the EPIPE error. Without this, closing the
            # page/context can trigger pending route handlers that try to
            # write (route.continue_/route.abort) through the Playwright
            # pipe after the browser process has already started shutting
            # down. Unrouting first tells Playwright "stop intercepting
            # requests" so nothing writes to a dead pipe.
            try:
                if self._context:
                    self._context.unroute_all()
            except Exception:
                pass

            # ── Step 2: Close page, context, browser, playwright (in order) ──
            try:
                if self._page:
                    self._page.close()
            except Exception:
                pass
            self._page = None

            try:
                if self._context:
                    self._context.close()
            except Exception:
                pass
            self._context = None

            try:
                if self._browser:
                    self._browser.close()
            except Exception:
                pass
            self._browser = None

            try:
                if self._playwright:
                    self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

            self._initialized = False
            self._warmup_url = None

    def dump_state(self) -> None:
        with self._op_lock:
            recent = list(self._vrf_meta.items())[-8:]
            self._log(
                f"state: initialized={self._initialized} cache_size={len(self._vrf_cache)} page={self._page_url()}"
            )
            for path, meta in recent:
                self._log(
                    f"  cached: {path} ts={meta.get('ts')} from_page={meta.get('page_url')} full={_short(meta.get('full_url',''))}"
                )


# ----------------------------- generator singleton + asyncio-safe bridge -----------------------------



# ---------------------------------------------------------------------------
# VRF generator access
# ---------------------------------------------------------------------------
# Playwright Sync API objects must be used from the same thread they were
# created in, and Playwright will raise if Sync API is used from inside an
# asyncio event loop. To make this robust (and faster by reusing one browser
# per process), we route all VRF work through a dedicated background thread.

import concurrent.futures as _futures

_VRF_EXECUTOR: _futures.ThreadPoolExecutor = _futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="MF-VRF"
)
_VRF_GEN = None  # created lazily inside the VRF thread


def _vrf_thread_entry(fn_name: str, args: tuple, kwargs: dict):
    global _VRF_GEN
    if _VRF_GEN is None:
        _VRF_GEN = SimpleMangaFireVRFGenerator()
    fn = getattr(_VRF_GEN, fn_name)
    return fn(*args, **kwargs)


def _vrf_call(fn_name: str, *args, **kwargs):
    fut = _VRF_EXECUTOR.submit(_vrf_thread_entry, fn_name, args, kwargs)
    return fut.result()


class _VRFBridge:
    def ensure_vrf(
        self,
        expected_path: str,
        page_url: str | None = None,
        init_url: str | None = None,
        *,
        max_attempts: int = 4,
        retry_backoff: float = 0.5,
    ) -> str:
        return _vrf_call(
            "ensure_vrf",
            expected_path,
            page_url=page_url,
            init_url=init_url,
            max_attempts=max_attempts,
            retry_backoff=retry_backoff,
        )

    def generate_vrf(self, url_path: str, init_url: str | None = None) -> str:
        return _vrf_call("generate_vrf", url_path, init_url=init_url)

    def capture_search(self, query: str, *, timeout_ms: int | None = None) -> dict:
        """Run the typeahead-driven search via the persistent browser. Returns
        the upstream JSON envelope; caller parses result.html. Used by
        MangaFireSiteHandler.search() to bypass the CF-WAF block on /filter."""
        return _vrf_call("capture_search", query, timeout_ms=timeout_ms)

    def capture_series_meta(self, series_url: str) -> dict:
        """Scrape title + cover + chapter count from a MangaFire series URL.
        Used by URL-mode --search to guarantee MangaFire participation when
        the typeahead would fail. See SimpleMangaFireVRFGenerator.capture_series_meta."""
        return _vrf_call("capture_series_meta", series_url)

    def capture_chapter_count(self, manga_id: str, language: str = "en") -> Optional[int]:
        """Get latest chapter number via the VRF-protected chapter-list
        endpoint. Reliable fallback when typeahead can't return chN.
        See SimpleMangaFireVRFGenerator.capture_chapter_count."""
        return _vrf_call("capture_chapter_count", manga_id, language=language)

    # Back-compat: some callers expect a dump_state() method.
    def dump_state(self) -> None:
        try:
            _vrf_call("dump_state")
        except Exception:
            pass

    def debug_state(self) -> str:
        return _vrf_call("debug_state")

    def close(self) -> None:
        try:
            _vrf_call("close")
        except Exception:
            pass


_VRF_BRIDGE = _VRFBridge()


def get_vrf_generator() -> _VRFBridge:
    """Return a process-local VRF helper.

    All Playwright operations happen on a single background thread so callers
    can remain fully synchronous.
    """
    return _VRF_BRIDGE


def generate_vrf_token(url_path: str) -> str:
    """Backward-compatible helper."""
    return get_vrf_generator().generate_vrf(url_path)


def _shutdown_vrf_bridge():
    # Best-effort: close the browser/context and stop the worker thread.
    # Use a timeout so this never hangs during Python interpreter shutdown.
    global _VRF_GEN
    try:
        if _VRF_GEN is not None:
            fut = _VRF_EXECUTOR.submit(_vrf_thread_entry, "close", (), {})
            fut.result(timeout=5.0)  # give it 5 seconds max
    except Exception:
        pass
    try:
        _VRF_EXECUTOR.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass


import atexit as _atexit
_atexit.register(_shutdown_vrf_bridge)