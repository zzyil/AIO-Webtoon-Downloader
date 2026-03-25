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


# ----------------------------- Playwright import -----------------------------


try:
    from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False


_BASE_URL = "https://mangafire.to"


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

        self._vrf_cache: Dict[str, str] = {}
        self._vrf_meta: Dict[str, Dict[str, Any]] = {}

        self._op_lock = threading.Lock()
        atexit.register(self.close)

    # ----------------------------- logging -----------------------------

    def _log(self, msg: str) -> None:
        print(f"[VRF {_pid_tid()} {_now()}] {msg}")

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

    # ----------------------------- init -----------------------------

    def _initialize(self, init_url: Optional[str] = None) -> None:
        if self._initialized:
            return

        self._log("initializing Playwright…")
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self._HEADLESS)
        self._context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        )

        # Faster VRF capture: no images/fonts/media.
        try:
            def _route_handler(route, request):
                rt = getattr(request, "resource_type", "")
                if rt in ("image", "media", "font"):
                    return route.abort()
                return route.continue_()

            self._context.route("**/*", _route_handler)
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
                resp = self._page.goto(init_url, wait_until="domcontentloaded", timeout=60000)
            status = resp.status if resp is not None else None
            self._log(f"warm-up done status={status} url={self._page_url()}")
            if self._POST_NAV_SETTLE_MS:
                self._page.wait_for_timeout(self._POST_NAV_SETTLE_MS)
        except Exception as e:
            self._log(f"warm-up navigation warning: {_classify_exc(e)}: {_short(e)}")

        self._initialized = True

    def _recreate_page(self, reason: str = "") -> None:
        if self._context is None:
            return
        try:
            if self._page is not None:
                try:
                    self._page.close()
                except Exception:
                    pass
            self._page = self._context.new_page()
            if hasattr(self, "_handle_request"):
                self._page.on("request", self._handle_request)
            if hasattr(self, "_handle_response"):
                self._page.on("response", self._handle_response)
            if reason:
                self._log(f"recreated page ({reason})")
        except Exception as e:
            self._log(f"page recreate warning: {_classify_exc(e)}: {_short(e)}")

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

            def _pred(req) -> bool:
                try:
                    u = req.url
                except Exception:
                    return False
                if "vrf=" not in u:
                    return False
                try:
                    return urlparse(u).path == expected_ajax_path
                except Exception:
                    return False

            for i in range(1, self._NAV_RETRIES + 1):
                info = _AttemptInfo(
                    attempt=i,
                    total=self._NAV_RETRIES,
                    stage="goto",
                    page_url=page_url,
                    expected_path=expected_ajax_path,
                    pre_url=self._page_url(),
                )
                try:
                    # Cross-process serialization (NET lock + cooldown/gap).
                    with _net_guard("vrf:navigate"):
                        # Start request expectation *before* goto so we never miss the XHR.
                        try:
                            with self._page.expect_request(_pred, timeout=self._WAIT_REQ_MS) as req_info:
                                resp = self._page.goto(page_url, wait_until="commit", timeout=self._DOM_TIMEOUT_MS)
                            req = req_info.value
                            # ensure cache has it (handler should, but belt & suspenders)
                            try:
                                u = req.url
                                parsed = urlparse(u)
                                q = parse_qs(parsed.query)
                                vrf = (q.get("vrf") or [""])[0]
                                if vrf:
                                    self._vrf_cache[parsed.path] = vrf
                            except Exception:
                                pass
                        except Exception as e:
                            # Expectation timed out or goto raised. Either way, we may have captured via handler.
                            resp = None
                            last_exc = e

                        info.goto_status = resp.status if resp is not None else None
                        info.post_url = self._page_url()
                        self._log_attempt(info)

                        if self._POST_NAV_SETTLE_MS:
                            self._page.wait_for_timeout(self._POST_NAV_SETTLE_MS)

                    if expected_ajax_path in self._vrf_cache:
                        self._log(f"success: captured expected VRF for {expected_ajax_path}")
                        return

                    # No exception, but still no VRF.
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
                    self._sleep_retry(i)

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
    def close(self) -> None:
        with self._op_lock:
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
        # Clear any stale running event loop ONLY before first init.
        # Playwright's Sync API checks asyncio.get_running_loop() and refuses
        # to start if one is found.  Once Playwright is running it manages
        # the running-loop reference itself, so we must not touch it again.
        try:
            asyncio._set_running_loop(None)
        except Exception:
            pass
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
    global _VRF_GEN
    try:
        if _VRF_GEN is not None:
            _vrf_call("close")
    except Exception:
        pass
    try:
        _VRF_EXECUTOR.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass


import atexit as _atexit
_atexit.register(_shutdown_vrf_bridge)
