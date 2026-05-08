#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ------------------------------------------------------------------
# Multi-site comic downloader  →  PDF, EPUB, or CBZ
# -----------------------------------------------------------
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import base64
import glob
import io
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import textwrap
import xml.sax.saxutils
import zipfile
import zlib
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, unquote_to_bytes

from sites import get_handler_by_name, get_handler_for_url
from sites.chapter_merger import group_chapters_for_download
from sites.base import SiteComicContext, IncompleteChapterError


# -----------------------------------------------------------
# Custom exceptions
# -----------------------------------------------------------
class ChapterSkippedError(Exception):
    """Raised by _process_chapter_impl when a chapter cannot complete in one
    attempt — any of:
      - any page failed to download (zero-tolerance: pages_ok < pages_total)
      - watchdog deadline fired (chapter took too long)
      - host poison threshold hit (≥N distinct URLs to one host fully failed)

    Caught by _process_chapter_strict, which performs an inline retry pass
    (long wait + redo the chapter from scratch). After inline retries are
    exhausted, _process_chapter_strict converts this into ChapterAbortedError
    which the main loop treats as a fatal stop.

    Attributes:
        reason:       short tag, one of: 'incomplete', 'time_budget', 'host_poison'
        host:         netloc that triggered the bail (for diagnostic logging)
        pages_ok:     count of pages successfully downloaded before the bail
        pages_total:  total pages the chapter was supposed to have
    """
    def __init__(self, reason: str, host: str = "", pages_ok: int = 0, pages_total: int = 0):
        self.reason = reason
        self.host = host
        self.pages_ok = pages_ok
        self.pages_total = pages_total
        super().__init__(
            f"chapter skipped: reason={reason} host={host or '-'} pages={pages_ok}/{pages_total}"
        )


class ChapterAbortedError(Exception):
    """Raised by _process_chapter_strict after all inline retries are exhausted.
    The main loop catches this and stops the run with a clear error — partial
    output (per-chapter files saved up to this point via --keep-chapters) is
    preserved, but no further chapters are attempted.

    The semantics match the user-requested behavior: never produce partial
    chapter PDFs, retry the whole chapter inline if any page fails, and stop
    cold if the inline retries can't recover.

    Attributes:
        chap, reason, host, pages_ok, pages_total: forwarded from the last
            ChapterSkippedError that triggered the abort.
        attempts: total number of attempts made (1 initial + N inline retries).
    """
    def __init__(self, chap, reason: str, host: str = "", pages_ok: int = 0, pages_total: int = 0, attempts: int = 0):
        self.chap = chap
        self.reason = reason
        self.host = host
        self.pages_ok = pages_ok
        self.pages_total = pages_total
        self.attempts = attempts
        super().__init__(
            f"chapter {chap} aborted after {attempts} attempt(s): "
            f"reason={reason} host={host or '-'} pages={pages_ok}/{pages_total}"
        )


# -----------------------------------------------------------
# Cross-process folder allocation (avoid mixing same-title series)
# -----------------------------------------------------------
class _AIOFileLock:
    """A tiny cross-platform exclusive file lock."""
    def __init__(self, path: str):
        self.path = path
        self._fd = -1

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT)
            if os.name == "nt":
                import msvcrt
                # Ensure at least 1 byte exists so we can lock byte 0
                if os.fstat(self._fd).st_size == 0:
                    os.write(self._fd, b"0")
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_EX)
        except Exception:
            # Best effort — close fd and proceed without lock.
            if self._fd >= 0:
                try:
                    os.close(self._fd)
                except Exception:
                    pass
                self._fd = -1
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fd < 0:
            return
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(self._fd)
        except Exception:
            pass
        self._fd = -1



# -----------------------------------------------------------
# Cross-process coordination (NET vs CPU pipelining, shared cooldown)
# -----------------------------------------------------------
import contextlib as _contextlib
import uuid as _uuid

_COORD = None  # set in main() if enabled
_WORKER_ID = os.getenv("AIO_WORKER_ID", "").strip() or f"pid{os.getpid()}"
_HEARTBEAT_FILE = os.getenv("AIO_HEARTBEAT_FILE", "").strip()
_LAST_HB_WRITE = 0.0

def _hb(phase: str, detail: str = "") -> None:
    """Best-effort heartbeat for the supervisor (cross-process)."""
    global _LAST_HB_WRITE
    if not _HEARTBEAT_FILE:
        return
    now = time.time()
    # Throttle writes to avoid excessive IO
    if now - _LAST_HB_WRITE < 0.5 and phase not in ("start", "done", "error", "killed"):
        return
    _LAST_HB_WRITE = now
    try:
        os.makedirs(os.path.dirname(_HEARTBEAT_FILE) or ".", exist_ok=True)
        payload = {
            "ts": now,
            "pid": os.getpid(),
            "worker_id": _WORKER_ID,
            "phase": phase,
            "detail": (detail or "")[:300],
        }
        with open(_HEARTBEAT_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception:
        pass


class _AIOCoordinator:
    """Coordinates request pacing + NET/CPU phases across processes via file locks."""

    def __init__(self, coord_dir: str, net_min_gap: float = 0.25):
        self.coord_dir = os.path.abspath(coord_dir)
        os.makedirs(self.coord_dir, exist_ok=True)

        self._net_lock = _AIOFileLock(os.path.join(self.coord_dir, "phase_net.lock"))
        self._cpu_lock = _AIOFileLock(os.path.join(self.coord_dir, "phase_cpu.lock"))
        self._state_lock = _AIOFileLock(os.path.join(self.coord_dir, "state.lock"))
        self._state_path = os.path.join(self.coord_dir, "state.json")

        self.net_min_gap = max(0.0, float(net_min_gap or 0.0))

    def _read_state(self) -> Dict[str, Any]:
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {"cooldown_until": 0.0, "last_net_ts": 0.0}

    def _write_state(self, data: Dict[str, Any]) -> None:
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
                    st["cooldown_reason"] = str(reason)[:120]
                self._write_state(st)

    def _wait_for_net_slot(self) -> None:
        """Wait for shared cooldown and min-gap, then reserve a NET slot.

        NOTE: We intentionally do *not* hold the NET phase file lock while sleeping.
        Holding the lock while waiting can starve other workers and looks like a hang.
        """
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

    @_contextlib.contextmanager
    def net_phase(self, label: str = ""):
        # Wait outside the NET phase lock so other processes are not blocked while idling.
        self._wait_for_net_slot()
        with self._net_lock:
            if label:
                _hb("net", label)
            yield

    @_contextlib.contextmanager
    def cpu_phase(self, label: str = ""):
        with self._cpu_lock:
            if label:
                _hb("cpu", label)
            yield


def _net_guard(label: str = ""):
    if _COORD is None:
        return _contextlib.nullcontext()
    return _COORD.net_phase(label=label)


def _cpu_guard(label: str = ""):
    if _COORD is None:
        return _contextlib.nullcontext()
    return _COORD.cpu_phase(label=label)


def _sanitize_folder_component(name: str) -> str:
    # Windows-illegal chars and trim.
    name = re.sub(r'[\\/*?:"<>|]', "", str(name or "")).strip()
    # Keep spaces in folder names for readability; collapse weird whitespace.
    name = re.sub(r"\s+", " ", name)
    # Avoid trailing dots/spaces on Windows
    name = name.rstrip(" .")
    return name or "comic"


def allocate_series_output_dir(title: str, hid: str, root: str = "mangas") -> str:
    """Choose a per-series output folder.

    Normally uses: root/<title>
    If that folder is already claimed by a different hid (or looks non-empty with unknown hid),
    uses: root/<title> (hid=<hid>)

    A hidden marker file stores the hid so multiple runs and multiple processes stay consistent.
    """
    clean_title = re.sub(r"\s*\(hid=[^)]+\)\s*$", "", str(title or "")).strip() or "comic"
    base = _sanitize_folder_component(clean_title)
    os.makedirs(root, exist_ok=True)
    lock_path = os.path.join(root, ".aio_folder_alloc.lock")

    def _marker_path(folder: str) -> str:
        return os.path.join(folder, ".mangafire_hid")

    def _read_marker(folder: str) -> str | None:
        mp = _marker_path(folder)
        try:
            if os.path.exists(mp):
                with open(mp, "r", encoding="utf-8", errors="ignore") as f:
                    return f.read().strip() or None
        except Exception:
            return None
        return None

    def _folder_nonempty(folder: str) -> bool:
        try:
            items = [x for x in os.listdir(folder) if x not in (".mangafire_hid", ".DS_Store")]
            return len(items) > 0
        except Exception:
            return False

    def _write_marker(folder: str):
        try:
            with open(_marker_path(folder), "w", encoding="utf-8") as f:
                f.write(str(hid))
        except Exception:
            pass

    with _AIOFileLock(lock_path):
        preferred = os.path.join(root, base)
        if os.path.exists(preferred):
            existing = _read_marker(preferred)
            if existing == str(hid):
                return preferred
            # If unclaimed AND empty-ish, claim it.
            if existing is None and not _folder_nonempty(preferred):
                _write_marker(preferred)
                return preferred
            # Otherwise collision: add hid suffix.
            candidate_base = _sanitize_folder_component(f"{clean_title} (hid={hid})")
            candidate = os.path.join(root, candidate_base)
            k = 2
            while os.path.exists(candidate):
                ex = _read_marker(candidate)
                if ex == str(hid):
                    return candidate
                candidate = os.path.join(root, f"{candidate_base} ({k})")
                k += 1
            os.makedirs(candidate, exist_ok=True)
            _write_marker(candidate)
            return candidate

        # Preferred does not exist: create and claim it.
        os.makedirs(preferred, exist_ok=True)
        _write_marker(preferred)
        return preferred

# cloudscraper is optional; fall back to requests.Session if unavailable
try:
    import cloudscraper  # type: ignore
except Exception:  # pragma: no cover
    cloudscraper = None

import requests

from PIL import Image, ImageDraw, ImageFont

# Increase PIL decompression bomb limit for large manga pages
# MangaFire often has high-resolution pages that exceed the default limit
Image.MAX_IMAGE_PIXELS = 200_000_000  # 200 megapixels (default is ~89 megapixels)

_VERBOSE = False  # Global flag for standard verbose output
_DEBUG = False  # Global flag for debug-level output


# -----------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------
def log_verbose(*args, **kwargs):
    """Prints if --verbose or --debug is set."""
    if _VERBOSE or _DEBUG:
        print(*args, **kwargs)


def log_debug(*args, **kwargs):
    """Prints only if --debug is set."""
    if _DEBUG:
        print(*args, **kwargs)



def make_request(url: str, scraper):
    """HTTP GET with retries/backoff + cross-process shared cooldown."""
    host = urlparse(url).netloc

    max_retries = int(globals().get("_HTTP_MAX_RETRIES", 6))
    timeout = float(globals().get("_HTTP_TIMEOUT", 30.0))
    base = float(globals().get("_HTTP_BACKOFF_BASE", 1.0))
    cap = float(globals().get("_HTTP_BACKOFF_CAP", 45.0))

    last_exc = None
    for attempt in range(max_retries):
        _hb("request", f"{host} {url}")
        _respect_rate_limit(host)

        try:
            with _net_guard(f"GET {host}"):
                r = scraper.get(url, timeout=timeout)

            if r.status_code >= 400:
                txt = ""
                try:
                    txt = (r.text or "")[:250].lower()
                except Exception:
                    txt = ""
                # Classify the failure so 520-527 (CF origin error) doesn't
                # trigger a multi-minute escalating cooldown the way it used to.
                cls = _classify_response_failure(r.status_code, txt)
                if cls == "rate_limit":
                    retry_after = 0.0
                    try:
                        ra = r.headers.get("Retry-After")
                        if ra:
                            retry_after = float(ra)
                    except Exception:
                        retry_after = 0.0
                    # Bounded cooldown — cap at 12s (was uncapped to 45s).
                    delay = max(3.0, retry_after, min(12.0, base * (2 ** attempt)))
                    delay *= random.uniform(0.85, 1.15)
                    _record_rate_limit(host, delay)
                    _bump_polite_delay(host)
                    if _COORD is not None:
                        _COORD.set_cooldown(delay, reason=f"rate_limit:{r.status_code}")
                    raise requests.exceptions.HTTPError(f"Rate limited ({r.status_code})", response=r)

                if cls == "origin_error":
                    # CF 520-527: must raise so the outer retry loop catches it.
                    # Previously these were lumped under rate_limit (which raises);
                    # without this branch, an HTML error page longer than 100 chars
                    # would slip through the "warning, continuing" path below and
                    # the caller would parse a CF error page as JSON.
                    raise requests.exceptions.HTTPError(
                        f"Origin error ({r.status_code})", response=r
                    )

                if not r.text or len(r.text) < 100:
                    r.raise_for_status()
                log_verbose(
                    f"  Warning: Got status {r.status_code} but response has content, continuing..."
                )

            _cool_polite_delay(host)
            return r

        except requests.exceptions.RequestException as e:
            last_exc = e
            status, snippet = _extract_error_info(e)
            cls = _classify_response_failure(status, snippet)

            # Determine retry behaviour from classification, NOT from the
            # old _is_retryable_error / _looks_like_rate_limit pair which
            # treated 520-527 as rate-limit (causing 5+ min hangs on MangaFire).
            if attempt < max_retries - 1:
                if cls == "rate_limit":
                    delay = max(3.0, min(12.0, base * (2 ** attempt))) * random.uniform(0.85, 1.15)
                    _record_rate_limit(host, delay)
                    _bump_polite_delay(host)
                    if _COORD is not None:
                        _COORD.set_cooldown(delay, reason=f"rate_limit:{status}")
                    time.sleep(delay)
                    continue
                if cls == "origin_error":
                    # Quick fixed retry for CF 520-527 — origin will recover or stay broken.
                    if attempt < min(2, max_retries - 1):
                        time.sleep(1.5 * random.uniform(0.85, 1.15))
                        continue
                    raise
                if cls == "retryable":
                    delay = min(cap, base * (2 ** attempt)) * random.uniform(0.5, 1.5)
                    time.sleep(delay)
                    continue
                # 'permanent' (4xx) → fall through to raise

            raise

    if last_exc:
        raise last_exc
    raise RuntimeError("Request failed without exception")


def parse_size(size_str: str) -> int:
    """Parses a human-readable size string (e.g., '400MB') into bytes."""
    if not size_str:
        return 0
    size_str = size_str.strip().upper()
    match = re.match(r"^([\d.]+)\s*([KMGT]?B?)$", size_str)
    if not match:
        raise ValueError(f"Invalid size format: {size_str}")

    value, unit = match.groups()
    value = float(value)
    unit = unit.replace("B", "")

    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    multiplier = multipliers.get(unit, 1)
    return int(value * multiplier)


def parse_aspect_ratio(spec: str) -> float:
    """Converts 'W:H' or a direct H/W float string to a float ratio (H/W)."""
    if not spec:
        return 0
    if ":" in spec:
        w, h = map(float, spec.split(":"))
        if w == 0:
            return float("inf")  # Avoid division by zero
        return h / w  # Return H/W for calculation
    return float(spec)


def resolve_site_handler(url: str, site_name: str):
    if site_name:
        handler = get_handler_by_name(site_name)
        if not handler:
            sys.exit(f"Unknown site handler: {site_name}")
        return handler

    handler = get_handler_for_url(url)
    if not handler:
        sys.exit(
            "Unable to auto-detect a site handler for the provided URL. "
            "Please specify one with --site."
        )
    return handler


def is_chapter_wanted(chapter_num_float: float, range_spec: str) -> bool:
    """
    Checks if a chapter number falls within a comma-separated range spec.
    Handles both single numbers and 'start-end' ranges with floats.
    """
    for part in range_spec.split(","):
        part = part.strip()
        if "-" in part:
            try:
                start, end = map(float, part.split("-"))
                if start <= chapter_num_float <= end:
                    return True
            except ValueError:
                continue  # Ignore malformed range parts
        else:
            try:
                if chapter_num_float == float(part):
                    return True
            except ValueError:
                continue  # Ignore malformed numbers
    return False


# -----------------------------------------------------------
# Metadata extractor
# -----------------------------------------------------------
# -----------------------------------------------------------
# file helpers
# -----------------------------------------------------------
_RATE_LIMIT_SCHEDULE: Dict[str, float] = {}
_RATE_LIMIT_LOCK = threading.Lock()
_HOST_POLITE_DELAY: Dict[str, float] = {}

# Per-chapter host-failure bookkeeping. Counts *distinct* fully-failed URLs
# per host so we can detect when a host has gone bad mid-chapter.
# Cleared at the start of every chapter via _reset_host_failures_for_chapter().
# Read by _try_download_url and dl_image to fast-fail when threshold is hit.
_HOST_FAIL_COUNT: Dict[str, int] = {}
_HOST_FAIL_URLS: Dict[str, set] = {}
_HOST_FAIL_LOCK = threading.Lock()

# Set by the per-chapter watchdog Timer in _process_chapter when the chapter's
# wall-clock deadline expires. dl_image / _try_download_url check this and
# return early so the chapter aborts within seconds of the deadline.
# None outside the chapter loop (e.g. during cover download).
_CHAPTER_CANCEL: Optional[threading.Event] = None


def _record_rate_limit(host: str, delay: float) -> None:
    if not host or delay <= 0:
        return
    wake_time = time.monotonic() + delay
    with _RATE_LIMIT_LOCK:
        current = _RATE_LIMIT_SCHEDULE.get(host, 0.0)
        if wake_time > current:
            _RATE_LIMIT_SCHEDULE[host] = wake_time


def _bump_polite_delay(host: str, minimum: float = 0.75) -> None:
    """Increase per-host polite delay after a rate-limit hit, so subsequent
    requests pace themselves. Capped at 2s — the previous 8s ceiling let a
    poisoned cooldown chain run for minutes; 2s is enough to slow burstiness
    without stalling the worker. _cool_polite_delay below decays it on success.
    """
    if not host:
        return
    with _RATE_LIMIT_LOCK:
        current = _HOST_POLITE_DELAY.get(host, 0.0)
        baseline = max(minimum, current if current else 0.0)
        new_delay = min(2.0, max(minimum, baseline * 1.5 if baseline else minimum))
        _HOST_POLITE_DELAY[host] = new_delay


def _cool_polite_delay(host: str) -> None:
    if not host:
        return
    with _RATE_LIMIT_LOCK:
        current = _HOST_POLITE_DELAY.get(host, 0.0)
        if not current:
            return
        new_delay = current * 0.7
        if new_delay < 0.2:
            _HOST_POLITE_DELAY.pop(host, None)
        else:
            _HOST_POLITE_DELAY[host] = new_delay


def _respect_rate_limit(host: str) -> None:
    """Sleep before the next request to this host if a cooldown is scheduled
    or a polite delay is active. The per-call wait cap is 8s (was 30s) so the
    chapter watchdog can break long stalls — if a longer cooldown is set,
    callers will re-enter and sleep again until it clears or the chapter is
    cancelled. Bails out immediately if the per-chapter watchdog has fired.
    """
    if not host:
        return
    if _chapter_cancelled():
        return
    with _RATE_LIMIT_LOCK:
        wake_time = _RATE_LIMIT_SCHEDULE.get(host, 0.0)
        polite_delay = _HOST_POLITE_DELAY.get(host, 0.0)
    remaining = wake_time - time.monotonic()
    if remaining > 0:
        wait = min(remaining, 8)
        log_verbose(f"  Waiting {wait:.1f}s for {host} to honor rate limit...")
        time.sleep(wait)
    if polite_delay and polite_delay > 0:
        jitter = min(0.5, polite_delay * 0.25)
        extra = polite_delay + random.uniform(0, jitter)
        log_verbose(f"  Throttling {host} for {extra:.2f}s to avoid CDN slowdowns...")
        time.sleep(extra)


def _extract_error_info(exc: requests.exceptions.RequestException) -> Tuple[Optional[int], str]:
    response = getattr(exc, "response", None)
    status = None
    snippet = ""
    if response is not None:
        status = response.status_code
        try:
            snippet = response.text[:200].lower()
        except Exception:
            snippet = ""
    return status, snippet


def _is_retryable_error(exc: requests.exceptions.RequestException) -> bool:
    if isinstance(
        exc,
        (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
        ),
    ):
        return True
    status, _ = _extract_error_info(exc)
    if status is None:
        return True
    if status >= 500 or status in {429, 408}:
        return True
    return False


# Body-keyword tokens that indicate genuine rate-limiting (429/503 with these
# means "actually rate limited"; without them, 503 is just transient origin/proxy).
# 1015 is Cloudflare's RL error code; "reduce your request rate" / "slowdownread"
# are origin-side hints that have shown up in real responses.
_RL_BODY_KEYWORDS = (
    "reduce your request rate",
    "slowdownread",
    "please slow down",
    "rate limit",
    "error 1015",
    "you are being rate limited",
    "ray id",
    "access denied",
)

# Cloudflare-specific 5xx codes meaning "edge couldn't talk to origin properly":
#   520 = origin returned malformed response
#   521 = origin refused connection
#   522 = origin timed out
#   523 = origin unreachable
#   524 = origin took too long (>100s)
#   525 = SSL handshake failed
#   526 = invalid SSL certificate
#   527 = Railgun error
# These are NOT rate limits — the browser sees them, retries, and most succeed
# on the next try. We classify them separately so they don't trigger escalating
# cooldowns or polite-delay bumps.
_ORIGIN_ERROR_STATUSES = {520, 521, 522, 523, 524, 525, 526, 527}


def _classify_response_failure(status: Optional[int], body_snippet: str) -> str:
    """Classify an HTTP error response so the retry strategy can match the cause.

    Returns one of:
      'rate_limit'   – genuine throttle: long-ish bounded cooldown, polite-delay bump.
      'origin_error' – CF/edge can't reach origin: short fixed retry, no cooldown spam.
      'retryable'    – 5xx/timeout that's worth a normal exponential retry.
      'permanent'    – 4xx (except 429/403-with-RL): no point retrying.

    Cross-file: read by both _try_download_url and make_request. _looks_like_rate_limit
    remains as a thin shim because some callers only need the boolean. Grep
    _classify_response_failure to find every retry decision site.
    """
    body = (body_snippet or "")
    has_rl_keyword = any(k in body for k in _RL_BODY_KEYWORDS)
    if status == 429:
        return "rate_limit"
    if status == 503 and has_rl_keyword:
        return "rate_limit"
    if status == 403 and has_rl_keyword:
        return "rate_limit"
    if status in _ORIGIN_ERROR_STATUSES:
        return "origin_error"
    if status in {500, 502, 503, 504, 408}:
        return "retryable"
    if status is None:
        return "retryable"   # network errors (timeout/connection reset)
    if status >= 500:
        return "retryable"
    return "permanent"


def _looks_like_rate_limit(status: Optional[int], body_snippet: str) -> bool:
    """Backwards-compat boolean shim. Prefer _classify_response_failure for
    new code — this only returns True for the 'rate_limit' class, NOT for
    'origin_error'. Older 5xx codes (500/502/504) used to count here; they
    no longer do (they are 'retryable' now and handled by normal backoff)."""
    return _classify_response_failure(status, body_snippet) == "rate_limit"


def _record_failure(host: str, url: str, cls: str) -> None:
    """Record a fully-failed URL (after retries exhausted) against a host so
    the per-chapter poison threshold can detect a broken host. Counts each
    URL only once per chapter — multiple retry attempts on the same URL
    increment the counter once.

    Called from _try_download_url after the retry loop ends without success.
    Cls is the classification of the *last* failure; we only count network
    failures (origin_error / rate_limit / retryable) — permanent 4xx errors
    don't indicate the host is broken.
    """
    if not host or cls == "permanent":
        return
    with _HOST_FAIL_LOCK:
        seen = _HOST_FAIL_URLS.setdefault(host, set())
        if url not in seen:
            seen.add(url)
            _HOST_FAIL_COUNT[host] = _HOST_FAIL_COUNT.get(host, 0) + 1


def _host_fail_count(host: str) -> int:
    """Distinct URLs that have fully failed against this host this chapter."""
    if not host:
        return 0
    with _HOST_FAIL_LOCK:
        return _HOST_FAIL_COUNT.get(host, 0)


def _reset_host_failures_for_chapter() -> None:
    """Clear per-chapter host-failure state. Called at the top of every chapter
    in _process_chapter so the threshold is scoped per chapter, not per run."""
    with _HOST_FAIL_LOCK:
        _HOST_FAIL_COUNT.clear()
        _HOST_FAIL_URLS.clear()


def _chapter_cancelled() -> bool:
    """True if the current chapter's watchdog has fired or _CHAPTER_CANCEL was
    set explicitly. Returns False outside a chapter (cover download etc.)."""
    return _CHAPTER_CANCEL is not None and _CHAPTER_CANCEL.is_set()


# ── Image format sniffing ────────────────────────────────────────────────────
# Phase A (2026-05-07): callers used to save every download as `.jpg`
# regardless of actual format, so a WebP page was named `0001.jpg`. That broke
# CBZ readers (extension/content mismatch) and prevented byte-preserving CBZ
# from working. We now sniff the format from the first 16 downloaded bytes
# (primary) plus the response Content-Type (fallback), and rename atomically
# via os.replace into `base + ext` after the bytes land. Magic-byte detection
# is primary because CDN proxies frequently misreport Content-Type
# (`image/jpeg` for everything they cache).
_JPEG_MAGIC = b"\xff\xd8"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_GIF_MAGIC = b"GIF8"
# WebP/AVIF/HEIC use ISO BMFF / RIFF containers — checked via byte ranges.


def _content_type_to_ext(content_type: str) -> Optional[str]:
    """Map an `image/*` Content-Type to a file extension. Returns None for
    unrecognized types so the caller falls back to a default. The mapping
    intentionally normalizes `image/jpg` → `.jpg` even though it's not the
    IANA-registered name (some CDNs send it)."""
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/avif": ".avif",
        "image/heic": ".heic",
        "image/heif": ".heic",
        "image/gif": ".gif",
    }.get((content_type or "").strip().lower())


def _sniff_image_extension(head: bytes, content_type: Optional[str] = None) -> str:
    """Return the most accurate file extension (with leading dot) for an image
    given its first ≥12 bytes and an optional Content-Type. Magic bytes are
    primary; Content-Type is consulted only when magic is ambiguous. Falls
    back to `.jpg` so callers always get a usable extension (matches prior
    blanket-`.jpg` behavior for unknown content)."""
    if head:
        if head.startswith(_JPEG_MAGIC):
            return ".jpg"
        if head.startswith(_PNG_MAGIC):
            return ".png"
        if head.startswith(_GIF_MAGIC):
            return ".gif"
        # WebP: bytes 0-3 = 'RIFF', bytes 8-11 = 'WEBP'.
        if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return ".webp"
        # AVIF/HEIC: ISO-BMFF "ftyp" box. Major brand at offset 8-11 tells
        # us the codec family. We only special-case AVIF; HEIC is rare in
        # manga aggregators but recognized so we don't accidentally label
        # it `.jpg`.
        if len(head) >= 12 and head[4:8] == b"ftyp":
            major = head[8:12]
            if major in (b"avif", b"avis"):
                return ".avif"
            if major in (b"heic", b"heix", b"mif1", b"msf1"):
                return ".heic"
    fallback = _content_type_to_ext(
        (content_type or "").split(";", 1)[0]
    )
    return fallback or ".jpg"


def _finalize_pending_image(
    pending_path: str, folder: str, base: str, content_type: Optional[str]
) -> Optional[str]:
    """Sniff a successfully-downloaded pending file's first bytes, atomic-
    rename it to `<folder>/<base><ext>`, and return the final path. Returns
    None if the pending file is missing (caller should treat as failure).
    `os.replace` is atomic on both POSIX and NT when source/dest share a
    volume — pending and final live in the same folder, so this is safe."""
    if not os.path.exists(pending_path):
        return None
    try:
        with open(pending_path, "rb") as fh:
            head = fh.read(32)
    except Exception:
        head = b""
    ext = _sniff_image_extension(head, content_type)
    final_path = os.path.join(folder, base + ext)
    os.replace(pending_path, final_path)
    return final_path


def _try_download_url(
    url, pth, name, scraper, max_retries, retry_delay, timeout=30
) -> Tuple[bool, Optional[requests.exceptions.RequestException], Optional[str]]:
    """Attempts to download a single URL with retries. Returns
    (success, last_error, content_type). content_type is the response's
    Content-Type header on success (used by Phase A's image-extension sniff
    fallback) and None on failure.

    Failure handling is classified (see _classify_response_failure):
      - rate_limit:   short bounded cooldown (3-12s), polite-delay bump,
                      coord cooldown for cross-process. Use the full retry budget.
      - origin_error: CF 520-527 — quick fixed retry (~1.5s ± jitter), max 2 retries,
                      no polite-delay bump, no coord cooldown. The browser sees the
                      same flaky upstream and recovers fast.
      - retryable:    transient 5xx/timeout — normal exponential backoff, capped at 8s.
      - permanent:    no retry, fail fast.

    Fast-fail conditions checked at the top of each retry iteration:
      - per-chapter watchdog timer fired (_CHAPTER_CANCEL.is_set())
      - this host has accumulated >= _CHAPTER_HOST_POISON distinct fully-failed URLs
        within the current chapter
    """
    host = urlparse(url).netloc
    last_error: Optional[requests.exceptions.RequestException] = None
    last_class = "retryable"
    poison_threshold = int(globals().get("_CHAPTER_HOST_POISON", 5))

    for attempt in range(max_retries):
        # Fast-fail: chapter deadline passed, give up everything in flight.
        if _chapter_cancelled():
            return False, last_error, None
        # Fast-fail: this host has shown N fully-failed URLs already this
        # chapter. No point grinding through more retries; the chapter is
        # going to be skipped anyway and the watchdog will see the poisoned
        # host count after this download phase.
        if poison_threshold > 0 and _host_fail_count(host) >= poison_threshold:
            return False, last_error, None

        _hb("download", f"{host} {os.path.basename(name)}")
        _respect_rate_limit(host)

        try:
            with _net_guard(f"IMG {host}"):
                r = scraper.get(url, stream=True, timeout=timeout)
                r.raise_for_status()
                # Capture Content-Type before we stream the body; the header
                # is available immediately after raise_for_status returns.
                content_type = r.headers.get("Content-Type")
                with open(pth, "wb") as fh:
                    for chunk in r.iter_content(131072):
                        if chunk:
                            fh.write(chunk)

            _cool_polite_delay(host)
            return True, None, content_type

        except requests.exceptions.RequestException as e:
            last_error = e
            status, body_snippet = _extract_error_info(e)
            cls = _classify_response_failure(status, body_snippet)
            last_class = cls

            if cls == "rate_limit":
                # Bounded cooldown: 3-12s. The browser-equivalent server response
                # for a real rate-limit is rarely longer than a few seconds.
                cooldown = max(3.0, min(12.0, float(retry_delay) * (attempt + 1)))
                cooldown *= random.uniform(0.85, 1.15)
                log_verbose(
                    f"    Rate-limited by {host or 'remote host'} (status={status}). Cooling down for {cooldown:.1f}s..."
                )
                _record_rate_limit(host, cooldown)
                _bump_polite_delay(host)
                if _COORD is not None:
                    _COORD.set_cooldown(cooldown, reason=f"img_rate_limit:{status}")
                if attempt < max_retries - 1:
                    time.sleep(cooldown)
                    continue
                break

            if cls == "origin_error":
                # CF origin error (520-527): flaky upstream, NOT throttling.
                # Quick fixed retry, no polite-delay bump, no coord cooldown.
                # Cap at 2 retries — origin will either recover quickly or stay broken.
                short = 1.5 * random.uniform(0.85, 1.15)
                log_verbose(
                    f"    Origin error {status} from {host or 'remote host'}; quick retry in {short:.1f}s..."
                )
                if attempt < min(2, max_retries - 1):
                    time.sleep(short)
                    continue
                break

            if cls == "retryable" and attempt < max_retries - 1:
                # Normal transient (5xx/timeout/connection error).
                # Capped exponential backoff.
                delay = min(8.0, float(retry_delay) * (2 ** attempt)) * random.uniform(0.6, 1.4)
                time.sleep(delay)
                continue

            # 'permanent' or out of retries
            break

    # Loop ended without success — record the URL once against the host so
    # the chapter-level poison threshold can fire if many distinct URLs fail.
    _record_failure(host, url, last_class)
    return False, last_error, None


def dl_image(url: str, folder: str, name: str, scraper, cleanup: bool = True) -> str:
    """
    Downloads an image using a sophisticated fallback chain with parallel attempts.
    Returns the file path on success, or None on failure.
    
    Args:
        url: URL to download
        folder: Directory to save the image
        name: Filename for the image
        scraper: HTTP scraper object
        cleanup: If True, clean up failed parallel temp files. If False, preserve them.
    
    Strategy:
    1. Try the first variant (original URL) sequentially with full retries
    2. If it fails, launch all remaining variants in parallel with reduced retries
    3. Return first successful download
    """
    max_retries = 5
    retry_delay = 1.0  # seconds
    parallel_retries = 1  # Reduced retries for parallel attempts
    timeout = 30  # seconds

    os.makedirs(folder, exist_ok=True)

    # Phase A (2026-05-07): callers pass `name` like "5_0001.jpg" by historic
    # convention, but the actual bytes may be webp/png/avif. Strip the
    # extension to get the base, write to a `.pending_<base>` tempfile in the
    # same folder, sniff format from magic + Content-Type once bytes land,
    # then atomic-rename to `<base><real_ext>`. Crash window only leaves
    # `.pending_*` files which the resume globs don't match (safe).
    base, _orig_ext = os.path.splitext(name)
    if not base:
        base = name
    pending_pth = os.path.join(folder, f".pending_{base}")

    if url.startswith("data:"):
        try:
            header, encoded = url.split(",", 1)
        except ValueError:
            log_verbose(f"  Warning: Invalid data URI for {name}")
            return None
        try:
            if ";base64" in header:
                data = base64.b64decode(encoded)
            else:
                data = unquote_to_bytes(encoded)
        except Exception as exc:
            log_verbose(f"  Warning: Failed to decode data URI for {name} ({exc})")
            return None
        # data URI header looks like 'data:image/webp;base64' — extract the
        # MIME segment for the sniff fallback.
        ct = header[5:].split(";", 1)[0].strip() if header.startswith("data:") else ""
        with open(pending_pth, "wb") as fh:
            fh.write(data)
        return _finalize_pending_image(pending_pth, folder, base, ct)

    # Fast-fail: chapter watchdog already fired, or this host has accumulated
    # too many fully-failed URLs this chapter. No point even starting.
    # These checks are no-ops outside the chapter loop (e.g. cover download).
    _host = urlparse(url).netloc
    _poison_threshold = int(globals().get("_CHAPTER_HOST_POISON", 5))
    if _chapter_cancelled():
        return None
    if _poison_threshold > 0 and _host_fail_count(_host) >= _poison_threshold:
        return None

    # 1. Try the original URL first (fast path – succeeds >95% of the time)
    log_debug(f"  Trying URL variant: {os.path.basename(url)}")

    success, first_error, content_type = _try_download_url(
        url, pending_pth, name, scraper, max_retries, retry_delay, timeout
    )
    if success:
        log_debug(f"  Successfully downloaded {os.path.basename(name)} using first variant.")
        return _finalize_pending_image(pending_pth, folder, base, content_type)

    # After the first attempt, re-check fast-fail conditions before generating
    # variants. If we just hit the poison threshold or watchdog, abort the
    # whole 9-variant cascade — that was the original 5-minute hang.
    if _chapter_cancelled():
        return None
    if _poison_threshold > 0 and _host_fail_count(_host) >= _poison_threshold:
        return None

    # 2. First URL failed – now generate fallback variants (lazy, only when needed)
    extensions_to_try = [".webp", ".png", ".jpg", ".jpeg", ".avif"]
    urls_to_try = [url]
    base_url, original_ext = os.path.splitext(url)

    # Extension variants of original URL
    for ext in extensions_to_try:
        urls_to_try.append(base_url + ext)

    # '-m' variant and its extension variants
    modified_base_url = base_url + "-m"
    urls_to_try.append(modified_base_url + original_ext)
    for ext in extensions_to_try:
        urls_to_try.append(modified_base_url + ext)

    # De-duplicate while preserving order, skip the original URL we already tried
    unique_urls_to_try = list(dict.fromkeys(urls_to_try))

    def _should_force_sequential(err: Optional[requests.exceptions.RequestException]) -> bool:
        if not err:
            return False
        if isinstance(
            err,
            (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
            ),
        ):
            return True
        status, snippet = _extract_error_info(err)
        return _looks_like_rate_limit(status, snippet)

    force_sequential_fallback = _should_force_sequential(first_error)

    # 3. First variant failed - try remaining variants (optionally sequentially)
    remaining_urls = unique_urls_to_try[1:]
    if not remaining_urls:
        print(f"  Error: Skipping image {os.path.basename(name)} after trying the only available variant.")
        return None

    if force_sequential_fallback:
        log_verbose(
            f"  First variant failed due to throttling/timeouts. Retrying {len(remaining_urls)} variants sequentially..."
        )
        print(f"  [Fallback] {os.path.basename(name)}: first URL failed (throttle/timeout), trying {len(remaining_urls)} variants sequentially...")
        for alt_url in remaining_urls:
            # Fast-fail between variants. Without this, a poisoned host
            # would grind through all 9 variants × 5 retries each.
            if _chapter_cancelled():
                return None
            if _poison_threshold > 0 and _host_fail_count(urlparse(alt_url).netloc) >= _poison_threshold:
                log_verbose(f"    [Sequential Fallback] Skipping further variants — host poisoned this chapter.")
                return None
            log_debug(f"    [Sequential Fallback] Attempting {os.path.basename(alt_url)}")
            success, _err, alt_content_type = _try_download_url(
                alt_url,
                pending_pth,
                name,
                scraper,
                max_retries,
                retry_delay,
                timeout,
            )
            if success:
                log_verbose(
                    f"  Successfully downloaded {os.path.basename(name)} via sequential fallback variant: {os.path.basename(alt_url)}"
                )
                print(f"  [Fallback] {os.path.basename(name)}: succeeded with variant {os.path.basename(alt_url)}")
                return _finalize_pending_image(pending_pth, folder, base, alt_content_type)

        print(
            f"  Error: Skipping image {os.path.basename(name)} after throttled sequential retries across {len(unique_urls_to_try)} variants."
        )
        return None

    log_verbose(f"  First variant failed. Trying {len(remaining_urls)} remaining variants in parallel...")
    print(f"  [Fallback] {os.path.basename(name)}: first URL failed, trying {len(remaining_urls)} variants in parallel...")

    # Import here to avoid dependency at module level
    import tempfile
    import threading
    
    # Track all temp files created for cleanup
    temp_files_created = []
    temp_files_lock = threading.Lock()
    
    # Track which thread succeeded (if any)
    success_lock = threading.Lock()
    successful_temp_file = [None]  # Use list to allow modification in nested function

    def try_variant(attempt_url, thread_id):
        """Helper function for parallel execution - each thread uses its own temp file"""
        # Fast-fail: skip this variant if the chapter is already being aborted
        # or the target host is poisoned. _try_download_url itself also checks
        # this, but bailing out before tempfile creation saves needless I/O.
        if _chapter_cancelled():
            return None
        if _poison_threshold > 0 and _host_fail_count(urlparse(attempt_url).netloc) >= _poison_threshold:
            return None
        temp_path = None
        try:
            # Create a unique temporary file for this thread. Prefix uses the
            # name (with .jpg-or-no-ext extension preserved) so concurrent
            # workers for different pages don't collide.
            temp_fd, temp_path = tempfile.mkstemp(dir=folder, prefix=f".tmp_{name}_")
            os.close(temp_fd)  # Close the file descriptor, we'll use the path

            # Track this temp file for cleanup
            with temp_files_lock:
                temp_files_created.append(temp_path)

            log_debug(f"    [Parallel] Attempting {os.path.basename(attempt_url)}")
            success, _err, ct = _try_download_url(
                attempt_url,
                temp_path,
                name,
                scraper,
                parallel_retries,
                retry_delay,
                timeout,
            )
            if success:
                # Successfully downloaded to temp file
                log_debug(f"    [Parallel] Success: {os.path.basename(attempt_url)}")

                with success_lock:
                    if successful_temp_file[0] is None:
                        # We're the first successful download. Phase A: also
                        # capture this thread's response Content-Type so the
                        # post-loop sniff has a reliable fallback when magic
                        # bytes are ambiguous.
                        successful_temp_file[0] = (temp_path, attempt_url, ct)
                        return attempt_url

                # Another thread already succeeded, this will be cleaned up later
                return None
            else:
                # Download failed, will be cleaned up later
                return None
        except Exception as e:
            log_debug(f"    [Parallel] Exception for {os.path.basename(attempt_url)}: {e}")
            return None

    # Use ThreadPoolExecutor to try all remaining variants in parallel
    # Limit workers to avoid overwhelming the server
    max_workers = min(len(remaining_urls), 5)
    
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all variant attempts with thread IDs
            future_to_url = {
                executor.submit(try_variant, url, i): url 
                for i, url in enumerate(remaining_urls)
            }
            
            # Wait for first successful result
            for future in as_completed(future_to_url):
                result = future.result()
                if result:
                    # Cancel remaining futures since we found a successful download
                    for f in future_to_url:
                        f.cancel()
                    break
    finally:
        # Clean up ALL temp files except the successful one (unless cleanup is disabled)
        if cleanup:
            successful_path = successful_temp_file[0][0] if successful_temp_file[0] else None

            for temp_path in temp_files_created:
                if temp_path != successful_path:
                    try:
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                            log_debug(f"    [Cleanup] Removed temp file: {os.path.basename(temp_path)}")
                    except Exception as e:
                        log_debug(f"    [Cleanup] Failed to remove temp file {os.path.basename(temp_path)}: {e}")
        else:
            log_debug(f"    [Cleanup] Skipped - preserving {len(temp_files_created)} temp files for debugging")

    # Move successful temp file to final destination (Phase A: with sniff)
    if successful_temp_file[0]:
        temp_path, successful_url, parallel_ct = successful_temp_file[0]
        try:
            # Sniff the winning temp file and pick its real extension. Then
            # atomic-rename into <folder>/<base><ext>. Same-folder rename is
            # atomic on POSIX and NT, so the file appears at its final path
            # with correct extension in one step.
            try:
                with open(temp_path, "rb") as fh:
                    head = fh.read(32)
            except Exception:
                head = b""
            ext = _sniff_image_extension(head, parallel_ct)
            final_pth = os.path.join(folder, base + ext)
            shutil.move(temp_path, final_pth)
            log_verbose(f"  Successfully downloaded {os.path.basename(name)} using variant: {os.path.basename(successful_url)}")
            print(f"  [Fallback] {os.path.basename(name)}: succeeded with variant {os.path.basename(successful_url)}")
            return final_pth
        except Exception as e:
            print(f"  Error: Failed to move temp file: {e}")
            # Clean up the temp file if move failed
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except:
                pass
            return None

    # 4. All attempts failed
    print(
        f"  Error: Skipping image {os.path.basename(name)} after trying all {len(unique_urls_to_try)} URL variants."
    )
    return None


def render_text_to_images(
    paragraphs: List[str],
    folder: str,
    prefix: str,
    title: str = None,
    width: int = 1400,
    height: int = 2000,
    font_size: int = 42,
    start_index: int = 1,
) -> List[str]:
    """
    Renders text paragraphs into JPEG images so that text-based chapters can be
    processed alongside normal image content.
    """

    if not paragraphs and not title:
        return []

    os.makedirs(folder, exist_ok=True)

    font = _load_font(font_size)
    margin = 100
    max_text_width = width - margin * 2
    line_height = _font_line_height(font)
    line_gap = max(8, int(line_height * 0.35))

    def new_canvas():
        img = Image.new("RGB", (width, height), color="white")
        draw = ImageDraw.Draw(img)
        return img, draw

    image, draw = new_canvas()
    y = margin
    page_index = start_index
    page_has_content = False
    output_paths: List[str] = []

    def commit_page():
        nonlocal image, draw, y, page_index, page_has_content
        if not page_has_content:
            return
        out_path = os.path.join(folder, f"{prefix}_{page_index:04d}.jpg")
        image.save(out_path, optimize=True, quality=95)
        output_paths.append(out_path)
        page_index += 1
        image, draw = new_canvas()
        y = margin
        page_has_content = False

    def ensure_space(additional_height: int):
        nonlocal y, page_has_content
        if y + additional_height > height - margin:
            commit_page()

    def add_text_line(text_line: str, fill="black"):
        nonlocal y, page_has_content
        ensure_space(line_height)
        draw.text((margin, y), text_line, font=font, fill=fill)
        y += line_height + line_gap
        page_has_content = True

    if title:
        for line in _wrap_text_line(title, font, max_text_width):
            add_text_line(line)
        y += line_gap

    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            ensure_space(line_height)
            y += line_height  # Blank line separation
            continue
        lines = _wrap_text_line(paragraph, font, max_text_width)
        if not lines:
            continue
        for line in lines:
            add_text_line(line)
        y += line_gap  # Paragraph spacing

    if page_has_content:
        commit_page()

    return output_paths


def write_text_file(
    paragraphs: List[str],
    path: str,
    title: Optional[str] = None,
) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        if title:
            fh.write(title.strip() + "\n\n")
        for para in paragraphs:
            fh.write(para.strip() + "\n")
        fh.write("\n")
    return path


def render_text_to_xhtml(
    paragraphs: List[str],
    path: str,
    title: Optional[str] = None,
    lang: str = "en",
) -> str:
    body_lines = []
    if title:
        body_lines.append(f"<h2>{xml.sax.saxutils.escape(title.strip())}</h2>")
    for para in paragraphs:
        para = para.strip()
        if not para:
            body_lines.append("<p>&nbsp;</p>")
        else:
            body_lines.append(
                f"<p>{xml.sax.saxutils.escape(para)}</p>"
            )
    body_html = "\n        ".join(body_lines) if body_lines else "<p></p>"
    xhtml_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="{lang}">
<head>
    <title>{xml.sax.saxutils.escape(title or "Text")}</title>
    <meta charset="utf-8"/>
    <link rel="stylesheet" type="text/css" href="text.css"/>
</head>
<body>
        {body_html}
</body>
</html>'''
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(xhtml_content)
    return path


def _pdf_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def render_text_to_pdf(
    paragraphs: List[str],
    path: str,
    title: Optional[str] = None,
    font_size: int = 12,
    max_chars_per_line: int = 90,
) -> str:
    page_width = 595  # A4 width in points
    page_height = 842  # A4 height in points
    margin = 72  # 1 inch
    leading = int(font_size * 1.6)
    usable_height = page_height - 2 * margin
    max_lines_per_page = max(1, int(usable_height // leading))

    lines: List[str] = []
    if title:
        lines.extend(textwrap.wrap(title.strip(), max_chars_per_line))
        lines.append("")
    for para in paragraphs:
        para = para.strip()
        if not para:
            lines.append("")
            continue
        lines.extend(textwrap.wrap(para, max_chars_per_line, replace_whitespace=False))
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        lines = [""]

    # Split into pages
    pages = [
        lines[i : i + max_lines_per_page]
        for i in range(0, len(lines), max_lines_per_page)
    ]

    objects: List[Optional[bytes]] = [None]

    def reserve_object() -> int:
        objects.append(None)
        return len(objects) - 1

    def set_object(obj_num: int, data: bytes) -> None:
        objects[obj_num] = data

    catalog_obj = reserve_object()
    pages_obj = reserve_object()
    font_obj = reserve_object()
    set_object(
        font_obj,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    )

    page_objects = []
    for page_lines in pages:
        content_lines = [
            "BT",
            f"/F1 {font_size} Tf",
            f"{leading} TL",
            f"1 0 0 1 {margin} {page_height - margin} Tm",
        ]
        for line in page_lines:
            if not line:
                content_lines.append("T*")
                continue
            escaped = _pdf_escape(line)
            content_lines.append(f"({escaped}) Tj")
            content_lines.append("T*")
        content_lines.append("ET")
        content_stream = "\n".join(content_lines).encode("latin-1", "replace")
        stream_obj = reserve_object()
        stream_header = f"<< /Length {len(content_stream)} >>\nstream\n".encode(
            "latin-1"
        )
        set_object(
            stream_obj,
            stream_header + content_stream + b"\nendstream",
        )

        page_obj = reserve_object()
        page_dict = (
            f"<< /Type /Page /Parent {pages_obj} 0 R "
            f"/MediaBox [0 0 {page_width} {page_height}] "
            f"/Resources << /Font << /F1 {font_obj} 0 R >> >> "
            f"/Contents {stream_obj} 0 R >>"
        ).encode("latin-1")
        set_object(page_obj, page_dict)
        page_objects.append(page_obj)

    kids = " ".join(f"{num} 0 R" for num in page_objects) or ""
    set_object(
        pages_obj,
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_objects)} >>".encode(
            "latin-1"
        ),
    )
    set_object(
        catalog_obj,
        f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode("latin-1"),
    )

    with open(path, "wb") as fh:
        fh.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for idx, obj in enumerate(objects[1:], start=1):
            if obj is None:
                obj = b"<<>>"
            offsets.append(fh.tell())
            fh.write(f"{idx} 0 obj\n".encode("latin-1"))
            fh.write(obj)
            fh.write(b"\nendobj\n")
        xref_pos = fh.tell()
        fh.write(f"xref\n0 {len(objects)}\n".encode("latin-1"))
        fh.write(b"0000000000 65535 f \n")
        for offset in offsets:
            fh.write(f"{offset:010d} 00000 n \n".encode("latin-1"))
        fh.write(
            f"trailer\n<< /Size {len(objects)} /Root {catalog_obj} 0 R >>\n".encode(
                "latin-1"
            )
        )
        fh.write(f"startxref\n{xref_pos}\n%%EOF".encode("latin-1"))

    return path


def _build_images_pdf(
    image_paths: List[str],
    out_path: str,
    source_paths: Optional[List[Optional[str]]] = None,
) -> str:
    """Build a PDF from image files by embedding image data directly.

    For each image the function picks the smallest encoding from up to
    three candidates:

      1. **Original source JPEG bytes** (if *source_paths* is provided and
         the source has the same pixel dimensions as the processed file).
         This is the best option: the server's original compression is
         almost always smaller than a quality-100 re-encode, and it has
         zero generation loss.

      2. **Processed file JPEG bytes** via DCTDecode — the re-encoded file
         that ``save_final_images`` wrote.  Used when the image was resized
         or scaled so the original no longer matches.

      3. **FlateDecode** (zlib on raw pixels) — fallback for non-JPEG files
         (PNG, WebP, etc.).

    Args:
        image_paths:  Processed image files (output of save_final_images).
        out_path:     Where to write the PDF.
        source_paths: Optional parallel list (same length as *image_paths*).
                      Each entry is either the path to the **original
                      downloaded file** for that page, or ``None`` if the
                      page was modified during processing (resized / scaled)
                      and the original bytes can no longer be used.
    """
    # ------------------------------------------------------------------ #
    #  Configurable settings (change these to experiment)                 #
    # ------------------------------------------------------------------ #
    ZLIB_LEVEL = 9          # max compression, lossless quality
    # ------------------------------------------------------------------ #

    _JPEG_MAGIC = b"\xff\xd8"  # first two bytes of every JPEG file

    # Can we use source_paths?  Only if the list was provided and has the
    # same length as image_paths (a length mismatch means some images were
    # skipped during processing, so the indices don't line up).
    use_sources = (
        source_paths is not None and len(source_paths) == len(image_paths)
    )

    # ---- object table (index 0 is the free-head, never used) ----
    objects: List[Optional[bytes]] = [None]

    def _reserve() -> int:
        objects.append(None)
        return len(objects) - 1

    def _set(num: int, data: bytes) -> None:
        objects[num] = data

    catalog_obj = _reserve()
    pages_obj = _reserve()
    page_objs: List[int] = []
    _source_used = 0  # counter for verbose logging

    for i, img_path in enumerate(image_paths):
        # ── Read the processed file once (bytes + dimensions + mode) ──
        # We always need (w, h, proc_mode) for the page MediaBox and to
        # decide between /DCTDecode (JPEG) and /FlateDecode (pixels). We
        # also need proc_bytes when the source can't be embedded directly.
        # Phase 4 audit fix (2026-05-08): combine the dimension probe and
        # the bytes-read into a single open + a single in-memory decode
        # to avoid the prior 2-3 file opens per processed image.
        with open(img_path, "rb") as fh:
            proc_bytes = fh.read()
        with Image.open(io.BytesIO(proc_bytes)) as img:
            w, h = img.size
            proc_mode = img.mode
        proc_is_jpeg = proc_bytes[:2] == _JPEG_MAGIC
        proc_colorspace = "/DeviceGray" if proc_mode == "L" else "/DeviceRGB"

        # ── Candidate 1: original source bytes ──
        # When the image wasn't resized, the original download has zero
        # generation loss. Always prefer it regardless of size. Read the
        # source file ONCE (bytes), then probe dimensions/mode from the
        # in-memory buffer — same single-open pattern as the processed
        # image above.
        source_bytes: Optional[bytes] = None
        source_colorspace: Optional[str] = None
        source_is_nonjpeg = False
        src_raw: Optional[bytes] = None
        src_mode: Optional[str] = None
        if use_sources and source_paths[i]:
            try:
                with open(source_paths[i], "rb") as fh:
                    src_raw = fh.read()
                with Image.open(io.BytesIO(src_raw)) as src:
                    sw, sh = src.size
                    src_mode = src.mode
                # Only usable when pixel dimensions match the processed
                # output (if the image was resized they won't match).
                if (sw, sh) == (w, h):
                    if src_raw[:2] == _JPEG_MAGIC:
                        source_bytes = src_raw
                        source_colorspace = (
                            "/DeviceGray" if src_mode == "L" else "/DeviceRGB"
                        )
                    else:
                        # Non-JPEG source (WebP/PNG) — flag for lossless
                        # pixel embedding via FlateDecode.
                        source_is_nonjpeg = True
            except Exception:
                # Fall through to processed bytes. Drop any partial src_raw
                # we may have captured before the failure so the non-JPEG
                # branch below doesn't try to decode a corrupted/half-read
                # buffer.
                src_raw = None
                source_is_nonjpeg = False

        # ── Pick the best encoding ──
        # Priority: original JPEG > lossless source pixels > processed JPEG > pixel fallback
        if source_bytes is not None:
            # Original JPEG — always prefer (zero generation loss).
            best_bytes = source_bytes
            best_filter = "/DCTDecode"
            best_colorspace = source_colorspace
            _source_used += 1
        elif source_is_nonjpeg and src_raw is not None:
            # Non-JPEG source (WebP/PNG) with matching dimensions —
            # embed raw pixels via FlateDecode for lossless quality.
            # Reuse src_raw captured above instead of re-opening the file.
            with Image.open(io.BytesIO(src_raw)) as src_img:
                if src_img.mode == "L":
                    pixel_img = src_img
                    best_colorspace = "/DeviceGray"
                else:
                    pixel_img = src_img.convert("RGB")
                    best_colorspace = "/DeviceRGB"
                pixel_bytes = pixel_img.tobytes()
            best_bytes = zlib.compress(pixel_bytes, ZLIB_LEVEL)
            best_filter = "/FlateDecode"
            _source_used += 1
        elif proc_is_jpeg:
            # Processed JPEG (re-encode fallback).
            best_bytes = proc_bytes
            best_filter = "/DCTDecode"
            best_colorspace = proc_colorspace
        else:
            # Non-JPEG fallback: decode the processed file to pixels and
            # zlib-compress. Reuse proc_bytes (already in memory) to avoid
            # the second file open the prior implementation did here.
            with Image.open(io.BytesIO(proc_bytes)) as img:
                if img.mode == "L":
                    pixel_img = img
                    best_colorspace = "/DeviceGray"
                else:
                    pixel_img = img.convert("RGB")
                    best_colorspace = "/DeviceRGB"
                pixel_bytes = pixel_img.tobytes()
            best_bytes = zlib.compress(pixel_bytes, ZLIB_LEVEL)
            best_filter = "/FlateDecode"

        # ── Write the image XObject ──
        img_obj = _reserve()
        hdr = (
            f"<< /Type /XObject /Subtype /Image "
            f"/Width {w} /Height {h} "
            f"/ColorSpace {best_colorspace} "
            f"/BitsPerComponent 8 "
            f"/Filter {best_filter} "
            f"/Length {len(best_bytes)} >>\nstream\n"
        ).encode("latin-1")
        _set(img_obj, hdr + best_bytes + b"\nendstream")

        # Content stream: scale image to fill the page
        # 'q' = save state, 'cm' = transform matrix, 'Do' = paint, 'Q' = restore
        content = f"q {w} 0 0 {h} 0 0 cm /Im0 Do Q".encode("latin-1")
        content_obj = _reserve()
        _set(
            content_obj,
            f"<< /Length {len(content)} >>\nstream\n".encode("latin-1")
            + content
            + b"\nendstream",
        )

        # Page object: one image fills the entire page
        page_obj = _reserve()
        _set(
            page_obj,
            (
                f"<< /Type /Page /Parent {pages_obj} 0 R "
                f"/MediaBox [0 0 {w} {h}] "
                f"/Resources << /XObject << /Im0 {img_obj} 0 R >> >> "
                f"/Contents {content_obj} 0 R >>"
            ).encode("latin-1"),
        )
        page_objs.append(page_obj)

    if _source_used:
        log_verbose(
            f"  PDF: embedded original bytes for {_source_used}/{len(image_paths)} pages"
        )

    # Pages tree + Catalog
    kids = " ".join(f"{p} 0 R" for p in page_objs)
    _set(
        pages_obj,
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_objs)} >>".encode("latin-1"),
    )
    _set(
        catalog_obj,
        f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode("latin-1"),
    )

    # ---- write the PDF file ----
    with open(out_path, "wb") as fh:
        fh.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: List[int] = []
        for idx, obj in enumerate(objects[1:], start=1):
            if obj is None:
                obj = b"<<>>"
            offsets.append(fh.tell())
            fh.write(f"{idx} 0 obj\n".encode("latin-1"))
            fh.write(obj)
            fh.write(b"\nendobj\n")
        xref_pos = fh.tell()
        fh.write(f"xref\n0 {len(objects)}\n".encode("latin-1"))
        fh.write(b"0000000000 65535 f \n")
        for offset in offsets:
            fh.write(f"{offset:010d} 00000 n \n".encode("latin-1"))
        fh.write(
            f"trailer\n<< /Size {len(objects)} /Root {catalog_obj} 0 R >>\n".encode(
                "latin-1"
            )
        )
        fh.write(f"startxref\n{xref_pos}\n%%EOF".encode("latin-1"))

    return out_path


def _epub_page_count(entries: List[Dict[str, Any]]) -> int:
    return sum(
        1
        for item in entries
        if isinstance(item, dict)
        and item.get("type") in {"image", "xhtml"}
    )


def _load_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        ("DejaVuSans.ttf", size),
        ("Arial.ttf", size),
        ("Helvetica.ttf", size),
    ]
    for font_name, font_size in candidates:
        try:
            return ImageFont.truetype(font_name, font_size)
        except Exception:
            continue
    return ImageFont.load_default()


def _font_line_height(font: ImageFont.ImageFont) -> int:
    try:
        bbox = font.getbbox("Hy")
        return bbox[3] - bbox[1]
    except Exception:
        return font.getsize("Hy")[1]


def _wrap_text_line(
    text: str, font: ImageFont.ImageFont, max_width: int
) -> List[str]:
    words = text.split()
    if not words:
        return []

    lines: List[str] = []
    current = ""

    for word in words:
        candidate = f"{current} {word}".strip()
        if not candidate:
            continue
        if _measure_text(font, candidate) <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)
            current = ""

        for segment in _split_long_word(word, font, max_width):
            if _measure_text(font, segment) <= max_width and not current:
                current = segment
            else:
                lines.append(segment)
                current = ""

    if current:
        lines.append(current)

    return lines


def _split_long_word(word: str, font: ImageFont.ImageFont, max_width: int) -> List[str]:
    segments: List[str] = []
    buffer = ""
    for ch in word:
        trial = buffer + ch
        if not buffer or _measure_text(font, trial) <= max_width:
            buffer = trial
        else:
            segments.append(buffer)
            buffer = ch
    if buffer:
        segments.append(buffer)
    return segments if segments else [word]


def _measure_text(font: ImageFont.ImageFont, text: str) -> float:
    if hasattr(font, "getlength"):
        return font.getlength(text)
    return font.getsize(text)[0]


def combine_images(images: List[Image.Image], width: int) -> Image.Image:
    """Combines multiple PIL images vertically into a single PIL image."""
    if not images:
        return None
    total_height = sum(img.height for img in images)
    if width <= 0 or total_height <= 0:
        return None

    combined_img = Image.new("RGB", (width, total_height))
    y_offset = 0
    for img in images:
        combined_img.paste(img, (0, y_offset))
        y_offset += img.height
    return combined_img


def process_chapter_images(
    input_paths: List[str], target_w: int, target_h: int
) -> List[Image.Image]:
    """
    Uses a "fill the gap" algorithm to combine and slice images in memory.
    Returns a list of final page images as PIL objects.

    Phase G6 (2026-05-08): the decode + initial-resize pass runs through a
    cpu//2 ThreadPool. PIL.Image.open + .convert + .resize all release the
    GIL during the native libjpeg-turbo / libwebp / LANCZOS work, so worker
    count translates to near-linear speedup. The fill-the-gap assembly that
    follows is order-dependent (output strip N depends on consumed bytes
    from inputs 0..N) so it stays sequential, but it operates on already-
    decoded PIL images and is fast (memcpy-style paste/crop).

    Memory note (Phase 3 audit fix, 2026-05-08): the assembly loop now
    consumes the decode iterator directly (without the prior `list(pool.map)`
    materialization step). Peak in-flight decoded images is bounded by
    workers + a small lookahead buffer rather than sum-of-all. For a
    35-page chapter at ~3 MB/page, peak drops from ~105 MB to ~15 MB.
    The pool is held alive across the consume loop via the with-block
    so workers continue feeding the iterator.
    """
    if not input_paths:
        log_verbose("  Processed into 0 pages in memory.")
        return []

    def _load_one(path: str) -> Optional[Image.Image]:
        try:
            with Image.open(path) as src:
                img = src.convert("RGB")
            if img.width != target_w:
                scale = target_w / img.width
                img = img.resize(
                    (target_w, int(img.height * scale)),
                    Image.LANCZOS,
                )
            return img
        except Exception as e:
            print(f"  Warning: Skipping corrupted image {path}: {e}")
            return None

    final_pages: List[Image.Image] = []

    def _assemble(decoded_iter) -> None:
        """Run the fill-the-gap assembly while the decode iterator is alive.
        Mutates `final_pages` in the enclosing scope. Extracted so the
        single-image and parallel paths share the loop body without
        copying it.
        """
        page_buffer: List[Image.Image] = []
        buffer_height = 0
        for current_image in decoded_iter:
            if current_image is None:
                continue
            while True:
                space_left = target_h - buffer_height
                if current_image.height <= space_left:
                    page_buffer.append(current_image)
                    buffer_height += current_image.height
                    log_debug(
                        f"    Buffering image (fill: {buffer_height}/{target_h})"
                    )
                    break
                else:
                    if space_left > 0:
                        log_debug(
                            f"    Buffer full. Filling gap of {space_left}px."
                        )
                        piece_to_fill = current_image.crop(
                            (0, 0, target_w, space_left)
                        )
                        page_buffer.append(piece_to_fill)
                        current_image = current_image.crop(
                            (0, space_left, target_w, current_image.height)
                        )

                    combined_page = combine_images(page_buffer, target_w)
                    if combined_page:
                        final_pages.append(combined_page)
                        log_debug(
                            f"      Finalized page {len(final_pages)} in memory."
                        )
                    page_buffer, buffer_height = [], 0
        if page_buffer:
            combined_page = combine_images(page_buffer, target_w)
            if combined_page:
                final_pages.append(combined_page)
                log_debug(f"    (END) Finalizing last buffered page in memory.")

    if len(input_paths) > 1:
        cpu = os.cpu_count() or 4
        workers = max(1, min(cpu // 2 or 1, len(input_paths)))
        # `pool.map` returns a lazy iterator that yields results in submission
        # order. Workers run concurrently up to `workers`; consumed results
        # are GC-eligible immediately (no list() retains them). The pool
        # MUST stay alive while we iterate, hence the `with` block wraps
        # the assembly call — exiting the block before the iterator is
        # exhausted would cancel pending futures.
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="img-decode"
        ) as pool:
            _assemble(pool.map(_load_one, input_paths))
    else:
        _assemble(iter([_load_one(input_paths[0])]))

    log_verbose(f"  Processed into {len(final_pages)} pages in memory.")
    return final_pages


def resize_chapter_images(
    input_paths: List[str], target_w: int
) -> List[Image.Image]:
    """Resizes images to a target width and returns PIL objects."""
    output_images = []
    for i, path in enumerate(input_paths):
        try:
            im = Image.open(path).convert("RGB")
            if im.width != target_w:
                scale = target_w / im.width
                im = im.resize(
                    (target_w, int(im.height * scale)), Image.LANCZOS
                )
            output_images.append(im)
            log_debug(f"    Resized image {i+1}/{len(input_paths)} in memory.")
        except Exception as e:
            print(f"  Warning: Could not process image {path}: {e}")
    log_verbose(f"  Resized {len(output_images)} pages in memory.")
    return output_images


def recombine_scaled_images(
    scaled_images: List[Image.Image], recombine_height: int
) -> List[Image.Image]:
    """
    Takes scaled-down images and stacks them vertically to fill the
    original target height, creating 'long strip' pages.
    """
    if not scaled_images:
        return []

    final_strips = []
    page_buffer = []
    buffer_height = 0
    strip_width = scaled_images[0].width

    for img in scaled_images:
        if buffer_height + img.height > recombine_height and page_buffer:
            combined_strip = combine_images(page_buffer, strip_width)
            if combined_strip:
                final_strips.append(combined_strip)
            page_buffer = [img]
            buffer_height = img.height
        else:
            page_buffer.append(img)
            buffer_height += img.height

    if page_buffer:
        combined_strip = combine_images(page_buffer, strip_width)
        if combined_strip:
            final_strips.append(combined_strip)

    log_verbose(
        f"  Re-combined {len(scaled_images)} scaled pages into {len(final_strips)} long strips."
    )
    return final_strips


def save_final_images(
    images: List[Image.Image],
    output_dir: str,
    prefix: str,
    quality: int,
    output_format: str = "auto",
    source_paths: Optional[List[Optional[str]]] = None,
) -> List[str]:
    """Saves a list of final PIL images to disk with format-aware encoding.

    Phase C (2026-05-07): historic behavior was always JPEG at the given
    quality (default 85). For CBZ that meant the image was lossy regardless
    of source format and the user's quality setting was the only knob. The
    new ``output_format`` argument lets the caller request a format that
    matches (and preserves the quality of) the source:

      - "auto": decide per-image from source_paths[i].format. WebP source
        → WebP lossless output (zero generation loss vs decoded WebP);
        JPEG source → JPEG at ``quality`` (typically q≥95 from caller);
        PNG/GIF/other or unknown → PNG (lossless). Falls back to PNG
        when source_paths isn't provided or doesn't line up 1:1.
      - "webp_lossless": every output is WebP-lossless at method=4
        (used by ``auto`` for WebP-source legacy re-encode).
      - "jpeg": legacy behavior, every output is JPEG at ``quality``.
      - "png": every output is PNG (lossless).

    Phase G2 (2026-05-08): WebP-lossless encodes go through a
    ThreadPoolExecutor (cap 4 workers). libwebp releases the GIL during
    the native encode, so worker count translates ~linearly to speedup
    until the physical-core ceiling. This addresses the legacy-path
    "saving 49 final pages: 40s" complaint — with 4 workers the same
    49-page WebP-lossless save lands at ~12s.

    Phase G5 (2026-05-08): JPEG saves now also go through the pool. The
    original "JPEG is fast enough that pool overhead would dominate"
    rationale held for SMALL images (~800×1200 page-per-page), but
    breaks down on the long-strip recombined output that CBZ produces:
    a 1500×7000 stitched JPEG with optimize=True takes ~700ms to encode,
    so 20 sequential = ~14s. libjpeg-turbo (PIL ≥8) releases the GIL
    during JPEG encoding, so the same 4-worker pool drops that to ~3-4s.
    User report 2026-05-08: WebP CBZs already-fast (pooled), JPEG CBZs
    15s vs PDF 1s — fix bridges the gap.

    The PDF path passes ``output_format="jpeg"`` and ``quality=100`` to
    keep the existing PDF re-encode contract unchanged.
    """
    os.makedirs(output_dir, exist_ok=True)
    log_verbose(f"  Saving {len(images)} final pages...")
    use_sources = (
        output_format == "auto"
        and source_paths is not None
        and len(source_paths) == len(images)
    )

    # Build per-image plan: resolve fmt + save_kwargs + final path for each.
    # Done sequentially up-front (no encoding yet, just metadata + format
    # detection via header probes on source_paths). The actual encode/disk-
    # write happens below, optionally through a worker pool.
    plan: List[Tuple[int, Image.Image, str, str, Dict[str, Any]]] = []
    for i, img in enumerate(images):
        fmt = output_format
        if fmt == "auto":
            src_fmt = None
            if use_sources and source_paths[i]:
                try:
                    with Image.open(source_paths[i]) as probe:
                        src_fmt = (probe.format or "").upper()
                except Exception:
                    src_fmt = None
            if src_fmt == "WEBP":
                fmt = "webp_lossless"
            elif src_fmt == "JPEG":
                fmt = "jpeg"
            else:
                # PNG, GIF, missing source — go lossless via PNG.
                fmt = "png"

        if fmt == "webp_lossless":
            ext = ".webp"
            save_kwargs: Dict[str, Any] = dict(
                format="WebP", lossless=True, method=4, quality=100
            )
        elif fmt == "jpeg":
            ext = ".jpg"
            save_kwargs = dict(format="JPEG", optimize=True, quality=quality)
        elif fmt == "png":
            ext = ".png"
            save_kwargs = dict(format="PNG", optimize=True)
        else:
            raise ValueError(f"unknown output_format: {fmt}")

        out_path = os.path.join(output_dir, f"{prefix}_{i+1:04d}{ext}")
        plan.append((i, img, out_path, fmt, save_kwargs))

    output_paths: List[Optional[str]] = [None] * len(plan)

    def _save_one(entry):
        idx, src_img, dst, fmt_local, save_kw = entry
        # WebP lossless does not accept RGBA in some PIL builds; convert
        # to a clean RGB/L mode before save. Same for JPEG.
        if fmt_local.startswith("webp_lossless") and src_img.mode not in ("RGB", "L"):
            src_img = src_img.convert("RGB")
        elif fmt_local == "jpeg" and src_img.mode not in ("RGB", "L"):
            src_img = src_img.convert("RGB")
        src_img.save(dst, **save_kw)
        log_debug(f"    Saved -> {os.path.basename(dst)}")
        return idx, dst

    # Pool for both WebP-lossless AND JPEG saves on multi-page chapters.
    # Gating on len(plan) > 1 alone is correct: PIL.Image.save with libjpeg-
    # turbo / libwebp both release the GIL during native encode, so any list
    # with ≥2 images benefits from at least 2 workers in flight. PNG-only
    # plans (rare — only happens when source format is unknown) stay in the
    # pool too; libpng's GIL story is less clear but pool overhead at this
    # scale is dominated by encode time anyway.
    use_pool = len(plan) > 1
    if use_pool:
        # Worker count tuned for the long-strip stitched output that CBZ
        # produces (1500×~12000 RGB buffer per worker + encoder scratch,
        # ~80 MB resident per worker). Cap at HALF of available cores —
        # leaves headroom for whatever else the user is doing (browser,
        # IDE, the orchestrator's other in-flight chapters) and keeps
        # peak memory bounded (12-core box → 6 workers ≈ 480 MB peak;
        # 24-core → 12 workers ≈ 960 MB). min(.., len) avoids spinning
        # up idle workers for short page lists. The same cap works for
        # both WebP-lossless and JPEG paths since memory is dominated
        # by the decoded RGB buffer, not the encoder state.
        cpu = os.cpu_count() or 4
        half_cores = max(1, cpu // 2)
        workers = max(1, min(half_cores, len(plan)))
        # Thread-name prefix reflects the dominant format in the plan so log
        # output stays readable; doesn't change behavior.
        prefix = "webp-encode" if any(
            entry[3].startswith("webp_lossless") for entry in plan
        ) else "img-encode"
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix=prefix
        ) as pool:
            futures = [pool.submit(_save_one, entry) for entry in plan]
            for fut in as_completed(futures):
                idx, dst = fut.result()
                output_paths[idx] = dst
    else:
        for entry in plan:
            idx, dst = _save_one(entry)
            output_paths[idx] = dst

    return output_paths  # type: ignore[return-value]


# -----------------------------------------------------------
# Builders (PDF, EPUB, CBZ)
# -----------------------------------------------------------
def _media(path: str):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    return "image/jpeg"


def build_comic_info_xml(
    title: str,
    comic_info: Dict,
    publishers: List[str],
    lang: str,
    page_count: int,
) -> str:
    """Generates the ComicInfo.xml string for CBZ files."""

    def escape(s):
        return xml.sax.saxutils.escape(s) if s else ""

    authors = ", ".join(comic_info.get("authors", []))
    artists = ", ".join(comic_info.get("artists", []))
    publisher = ", ".join(publishers)
    description = comic_info.get("desc", "")

    tags = []
    for key in ["genres", "theme", "format"]:
        if comic_info.get(key):
            tags.extend(comic_info[key])
    genre = ", ".join(set(tags))

    xml_template = f'''<?xml version="1.0" encoding="utf-8"?>
<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
    <Title>{escape(title)}</Title>
    <Series>{escape(title)}</Series>
    <Summary>{escape(description)}</Summary>
    <Writer>{escape(authors)}</Writer>
    <Penciller>{escape(artists)}</Penciller>
    <Publisher>{escape(publisher)}</Publisher>
    <Genre>{escape(genre)}</Genre>
    <LanguageISO>{escape(lang)}</LanguageISO>
    <PageCount>{page_count}</PageCount>
    <ScanInformation>{escape(publisher)}</ScanInformation>
</ComicInfo>
'''
    return xml_template


def build_cbz(
    slices: List[str],
    out_path: str,
    title: str,
    comic_info: Dict,
    publishers: List[str],
    lang: str,
):
    """Builds a CBZ file from a list of image slices with metadata."""
    xml_content = build_comic_info_xml(
        title, comic_info, publishers, lang, len(slices)
    )
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as zf:
        for i, image_path in enumerate(slices):
            arcname = f"{i:04d}{os.path.splitext(image_path)[1]}"
            zf.write(image_path, arcname)
        zf.writestr("ComicInfo.xml", xml_content, compress_type=zipfile.ZIP_DEFLATED)
    print(f"CBZ saved → {os.path.basename(out_path)}")


def build_cbz_from_content(
    content: List[Dict[str, Any]],
    out_path: str,
    title: str,
    comic_info: Dict,
    publishers: List[str],
    lang: str,
):
    """Builds a CBZ from chapter_content items of type 'image' or 'cbz_cache'.

    Phase D (2026-05-07): chapter_content can carry per-chapter cached
    .cbz archives produced by the new caching layer (Phase B fast-path
    and the legacy-encode flow both write `processed_tdir/{n}.cbz`).
    'cbz_cache' entries are member-copied into the destination archive
    via zipfile.read/writestr — no decode, no re-zip past the container
    framing — preserving byte-perfect content. 'image' entries continue
    to work for back-compat with code paths that haven't been ported
    (e.g. the EPUB cover-prepend and pre-Phase-D resume cases).

    The series-level ComicInfo.xml is written once at the end; member
    copies skip any per-chapter ComicInfo.xml so we don't get duplicate
    entries that confuse readers.
    """
    page_count = 0
    for item in content:
        t = item.get("type")
        if t == "image":
            page_count += 1
        elif t == "cbz_cache":
            try:
                with zipfile.ZipFile(item["path"], "r") as zin:
                    page_count += sum(
                        1 for info in zin.infolist()
                        if info.filename != "ComicInfo.xml"
                    )
            except Exception:
                # Cache file unreadable — skip its page contribution. The
                # later assembly loop will also fail to open it and write
                # zero entries; user gets an empty CBZ they can debug.
                pass
    xml_content = build_comic_info_xml(
        title, comic_info, publishers, lang, page_count
    )

    idx = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as zout:
        for item in content:
            t = item.get("type")
            if t == "image":
                ext = os.path.splitext(item["path"])[1]
                zout.write(item["path"], f"{idx:04d}{ext}")
                idx += 1
            elif t == "cbz_cache":
                try:
                    with zipfile.ZipFile(item["path"], "r") as zin:
                        for info in zin.infolist():
                            if info.filename == "ComicInfo.xml":
                                continue
                            ext = os.path.splitext(info.filename)[1]
                            zout.writestr(
                                f"{idx:04d}{ext}", zin.read(info)
                            )
                            idx += 1
                except Exception as exc:
                    log_verbose(
                        f"  Warning: cbz_cache at {item.get('path')!r} unreadable: {exc}"
                    )
        zout.writestr(
            "ComicInfo.xml", xml_content, compress_type=zipfile.ZIP_DEFLATED
        )
    print(f"CBZ saved → {os.path.basename(out_path)}")


def build_epub(
    items: List[Dict[str, Any]],
    out_path: str,
    title: str,
    lang: str,
    layout: str,
    comic_info: Dict,
    publishers: List[str],
    cover_metadata_path: str = None,
    chapter_markers: List[Dict] = None,
):
    assert layout in ("page", "vertical")

    # --- Create a temporary directory for EPUB contents ---
    temp_dir = f"temp_epub_{comic_info['hid']}"
    epub_dir = os.path.join(temp_dir, "EPUB")
    images_dir = os.path.join(epub_dir, "images")
    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(os.path.join(temp_dir, "META-INF"), exist_ok=True)

    # --- 1. mimetype file ---
    with open(os.path.join(temp_dir, "mimetype"), "w") as f:
        f.write("application/epub+zip")

    # --- 2. container.xml ---
    container_xml = '''<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
    <rootfiles>
        <rootfile full-path="EPUB/content.opf" media-type="application/oebps-package+xml"/>
    </rootfiles>
</container>'''
    with open(os.path.join(temp_dir, "META-INF", "container.xml"), "w") as f:
        f.write(container_xml)

    # --- 3. content.opf (Package Document) ---
    manifest_items = []
    spine_items = []
    metadata_items = []

    # --- Viewport & Styling ---
    view_w, view_h = 1200, 1920
    first_image = next(
        (item for item in items if item.get("type") == "image"), None
    )
    if first_image:
        try:
            with Image.open(first_image["path"]) as img:
                view_w, view_h = img.size
        except Exception:
            pass
    viewport_meta = (
        f'<meta name="viewport" content="width={view_w}, height={view_h}"/>'
    )

    style_content = '''@charset "UTF-8";
body, html { padding: 0; margin: 0; height: 100%; width: 100%; text-align: center; }
svg, img { max-width: 100vw; max-height: 100vh; object-fit: contain; display: block; margin: auto; }'''
    style_path = os.path.join(epub_dir, "style.css")
    with open(style_path, "w") as f:
        f.write(style_content)
    manifest_items.append('<item id="css" href="style.css" media-type="text/css"/>')

    text_style_content = '''@charset "UTF-8";
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    margin: 1.75em;
    line-height: 1.5;
    color: #111;
}
h1, h2, h3 {
    margin: 0 0 0.6em 0;
}
p {
    margin: 0 0 0.8em 0;
    text-align: justify;
}
'''
    text_style_path = os.path.join(epub_dir, "text.css")
    with open(text_style_path, "w") as f:
        f.write(text_style_content)
    manifest_items.append('<item id="text_css" href="text.css" media-type="text/css"/>')

    nav_style_content = '''
html, body { height: 100%; margin: 0; padding: 0; }
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background-color: #fff; color: #000;
    padding: 2em;
    box-sizing: border-box;
    text-align: left;
    -webkit-column-count: 3;
    -moz-column-count: 3;
    column-count: 3;
    -webkit-column-gap: 2em;
    -moz-column-gap: 2em;
    column-gap: 2em;
}
h1 {
    text-align: center;
    -webkit-column-span: all;
    column-span: all;
    margin-top: 0;
}
ol {
    list-style-type: none;
    padding: 0;
    margin: 0;
}
li {
    padding: 0.1em 0;
    -webkit-column-break-inside: avoid;
    page-break-inside: avoid;
    break-inside: avoid-column;
}
a { text-decoration: none; color: #005a9c; }
a:hover, a:active { text-decoration: underline; }
'''
    nav_style_path = os.path.join(epub_dir, "nav_style.css")
    with open(nav_style_path, "w") as f:
        f.write(nav_style_content)
    manifest_items.append(
        '<item id="nav_css" href="nav_style.css" media-type="text/css"/>'
    )

    # --- Cover ---
    if cover_metadata_path and os.path.exists(cover_metadata_path):
        try:
            with Image.open(cover_metadata_path) as img:
                if img.mode != "RGB":
                    img = img.convert("RGB")
                cover_path_in_epub = os.path.join(images_dir, "cover.jpg")
                img.save(cover_path_in_epub, "jpeg", quality=90)

            manifest_items.append(
                '<item id="cover-image" href="images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>'
            )
            metadata_items.append('<meta name="cover" content="cover-image"/>')
            cover_html_content = f'''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
    <title>Cover</title>
    {viewport_meta}
    <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
    <img src="images/cover.jpg" alt="Cover"/>
</body>
</html>'''
            with open(os.path.join(epub_dir, "cover.xhtml"), "w") as f:
                f.write(cover_html_content)
            manifest_items.append(
                '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>'
            )
            spine_items.append('<itemref idref="cover"/>')
        except Exception as e:
            log_verbose(f"  Warning: Could not process cover image: {e}")

    # --- Content Pages ---
    page_docs = []
    image_counter = 0
    text_counter = 0

    for item in items:
        item_type = item.get("type")
        if item_type == "image":
            image_path = item["path"]
            img_ext = os.path.splitext(image_path)[1]
            img_filename = f"img_{image_counter}{img_ext}"
            shutil.copy(image_path, os.path.join(images_dir, img_filename))
            manifest_items.append(
                f'<item id="img_{image_counter}" href="images/{img_filename}" media-type="{_media(image_path)}"/>'
            )

            page_index = len(page_docs)
            page_filename = f"page_{page_index}.xhtml"
            page_html_content = f'''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}">
<head>
    <title>{title} - Page {page_index + 1}</title>
    <meta charset="utf-8"/>
    {viewport_meta}
    <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
    <img src="images/{img_filename}" alt="Page {page_index + 1}"/>
</body>
</html>'''
            with open(os.path.join(epub_dir, page_filename), "w") as f:
                f.write(page_html_content)
            manifest_items.append(
                f'<item id="page_{page_index}" href="{page_filename}" media-type="application/xhtml+xml"/>'
            )
            spine_items.append(f'<itemref idref="page_{page_index}"/>')
            page_docs.append({"href": page_filename})
            image_counter += 1
        elif item_type == "xhtml":
            source_path = item["path"]
            basename = os.path.basename(source_path)
            if not basename.lower().endswith(".xhtml"):
                basename = f"text_{text_counter}.xhtml"
            dest_path = os.path.join(epub_dir, basename)
            shutil.copy(source_path, dest_path)
            item_id = f"text_{text_counter}"
            manifest_items.append(
                f'<item id="{item_id}" href="{basename}" media-type="application/xhtml+xml"/>'
            )
            spine_items.append(f'<itemref idref="{item_id}"/>')
            page_docs.append({"href": basename})
            text_counter += 1

    # --- Table of Contents (Navigation Document) ---
    # This is identified by the "nav" property in the manifest and used by the
    # reader's UI. It is not part of the linear reading flow, which solves
    # the problem of it being cut off by the fixed-layout viewport.
    if chapter_markers:
        nav_content = f'''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
    <title>Table of Contents</title>
    <link rel="stylesheet" type="text/css" href="nav_style.css"/>
</head>
<body>
    <nav epub:type="toc">
        <h1>Table of Contents</h1>
        <ol>
'''
        for marker in chapter_markers:
            page_index = marker["page_index"]
            if page_index < len(page_docs):
                ch_title = f"Chapter {marker['ch']['chap']}"
                nav_target = page_docs[page_index]["href"]
                nav_content += f'<li><a href="{nav_target}">{xml.sax.saxutils.escape(ch_title)}</a></li>'
        nav_content += '''
        </ol>
    </nav>
</body>
</html>'''
        with open(os.path.join(epub_dir, "nav.xhtml"), "w") as f:
            f.write(nav_content)
        manifest_items.append(
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        )

    # --- Build content.opf ---
    from datetime import datetime, timezone

    modified_timestamp = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    # --- Metadata ---
    metadata_items.append(
        f'<dc:identifier id="bookid">series-{comic_info["hid"]}</dc:identifier>'
    )
    metadata_items.append(
        f"<dc:title>{xml.sax.saxutils.escape(title)}</dc:title>"
    )
    metadata_items.append(f"<dc:language>{lang}</dc:language>")
    metadata_items.append(
        f'<meta property="dcterms:modified">{modified_timestamp}</meta>'
    )

    if comic_info.get("authors"):
        for author in comic_info["authors"]:
            metadata_items.append(
                f"<dc:creator>{xml.sax.saxutils.escape(author)}</dc:creator>"
            )
    if comic_info.get("artists"):
        for artist in comic_info["artists"]:
            metadata_items.append(
                f"<dc:contributor>{xml.sax.saxutils.escape(artist)}</dc:contributor>"
            )
    if publishers:
        for publisher in publishers:
            metadata_items.append(
                f"<dc:publisher>{xml.sax.saxutils.escape(publisher)}</dc:publisher>"
            )
    if comic_info.get("desc"):
        metadata_items.append(
            f'<dc:description>{xml.sax.saxutils.escape(comic_info["desc"])}</dc:description>'
        )
    tags = []
    for key in ["genres", "theme", "format"]:
        if comic_info.get(key):
            tags.extend(comic_info[key])
    for tag in set(tags):
        metadata_items.append(
            f"<dc:subject>{xml.sax.saxutils.escape(tag)}</dc:subject>"
        )

    has_text_pages = any(item.get("type") == "xhtml" for item in items)
    rendition_spread = "none"
    if has_text_pages:
        rendition_layout = "reflowable"
        rendition_flow = "auto"
    else:
        rendition_layout = "pre-paginated"
        rendition_flow = "scrolled-continuous" if layout == "vertical" else "paginated"
    metadata_items.append(
        f'<meta property="rendition:layout">{rendition_layout}</meta>'
    )
    metadata_items.append(
        f'<meta property="rendition:spread">{rendition_spread}</meta>'
    )
    metadata_items.append(
        f'<meta property="rendition:flow">{rendition_flow}</meta>'
    )

    # Precompute joined XML fragments to avoid backslashes inside f-string
    # expressions (needed for Python 3.7–3.11 compatibility).
    metadata_xml = "\n        ".join(metadata_items)
    manifest_xml = "\n        ".join(manifest_items)
    spine_xml = "\n        ".join(spine_items)

    package_document = f'''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="3.0" prefix="rendition: http://www.idpf.org/vocab/rendition/#">
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:opf="http://www.idpf.org/2007/opf">
        {metadata_xml}
    </metadata>
    <manifest>
        {manifest_xml}
    </manifest>
    <spine>
        {spine_xml}
    </spine>
</package>'''
    with open(os.path.join(epub_dir, "content.opf"), "w") as f:
        f.write(package_document)

    # --- Create the EPUB file (zip archive) ---
    # Image extensions that are already compressed and won't benefit from deflate
    _STORED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"}
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(
            os.path.join(temp_dir, "mimetype"),
            "mimetype",
            compress_type=zipfile.ZIP_STORED,
        )
        for root, _, files in os.walk(temp_dir):
            for file in files:
                if file == "mimetype":
                    continue
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, temp_dir)
                # Skip deflate for images – they're already compressed
                ext = os.path.splitext(file)[1].lower()
                comp = zipfile.ZIP_STORED if ext in _STORED_EXTS else zipfile.ZIP_DEFLATED
                zf.write(file_path, arcname, compress_type=comp)

    # Clean up temp directory with retry logic for file handle issues
    try:
        shutil.rmtree(temp_dir)
    except OSError:
        # If rmtree fails, try again with ignore_errors after brief delay
        import time
        time.sleep(0.1)  # Brief delay to allow file handles to close
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass  # Ignore cleanup errors - EPUB file was successfully created

    print(f"EPUB saved \u2192 {os.path.basename(out_path)}")

def merge_pdf_files(input_paths, out_path, metadata):
    """
    Cross-version PDF merge:
    - pypdf >= 5: use PdfWriter.append
    - older pypdf: use PdfWriter + PdfReader pages
    - very old pypdf: fall back to PdfMerger (if available)
    Always writes to a binary file handle.
    """
    # 1) Try PdfWriter-first path (works on pypdf >= 5 and many older versions)
    try:
        from pypdf import PdfWriter, PdfReader
        writer = PdfWriter()
        if hasattr(writer, "append"):
            for p in input_paths:
                writer.append(p)
        else:
            # Older writer: add pages manually
            for p in input_paths:
                reader = PdfReader(p)
                for page in reader.pages:
                    writer.add_page(page)
        if metadata:
            writer.add_metadata(metadata)
        with open(out_path, "wb") as f:
            writer.write(f)
        try:
            writer.close()
        except Exception:
            pass
        return
    except Exception:
        pass

    # 2) Fallback: PdfMerger (available in older pypdf versions)
    try:
        from pypdf import PdfMerger
        merger = PdfMerger()
        for p in input_paths:
            merger.append(p)
        if metadata:
            merger.add_metadata(metadata)
        with open(out_path, "wb") as f:
            merger.write(f)
        merger.close()
        return
    except Exception as e:
        raise RuntimeError(
            "PDF merge failed with both PdfWriter and PdfMerger."
        ) from e

def build_book_part(
    args,
    base_filename,
    comic_data,
    book_content,
    book_chapters,
    book_scan_groups,
    original_cover_path,
    epub_markers=None,
):
    """Builds and saves a single part of a split book."""
    if not book_content:
        return

    start_chap = book_chapters[0]["chap"]
    end_chap = book_chapters[-1]["chap"]
    part_suffix = f"Ch {format_chap_for_filename(start_chap)}-{format_chap_for_filename(end_chap)}"
    part_filename = join_name(base_filename, part_suffix)
    # Write parts into the same output directory as the main run.
    out_dir = getattr(args, "output_dir", "mangas")
    os.makedirs(out_dir, exist_ok=True)
    title = comic_data["title"]
    part_title = f"{title} ({part_suffix})"

    if args.format == "pdf":
        final_path = os.path.join(out_dir, f"{part_filename}.pdf")
        pdf_inputs = [
            item["path"]
            for item in book_content
            if item.get("type") == "pdf"
        ]
        if pdf_inputs:
            with _cpu_guard('merge_pdf'):
                merge_pdf_files(
                pdf_inputs,
                final_path,
                {
                    "/Title": part_title,
                    "/Author": ", ".join(comic_data.get("authors", [])),
                },
            )
            print(f"PDF part saved → {os.path.basename(final_path)}")
        for path in pdf_inputs:
            try:
                os.remove(path)
            except OSError:
                pass

    elif args.format == "epub":
        final_path = os.path.join(out_dir, f"{part_filename}.epub")
        with _cpu_guard('build_epub'):
            build_epub(
            book_content,
            final_path,
            part_title,
            args.language,
            args.epub_layout,
            comic_data,
            list(book_scan_groups),
            original_cover_path,
            chapter_markers=epub_markers,
        )
    elif args.format == "cbz":
        final_path = os.path.join(out_dir, f"{part_filename}.cbz")
        # Phase D (2026-05-07): book_content can carry both 'image' and
        # 'cbz_cache' entries. The wrapper member-copies cached entries
        # without decode and falls back to file writes for legacy 'image'
        # entries (e.g. the cover-prepend at line ~3899).
        with _cpu_guard('build_cbz'):
            build_cbz_from_content(
                book_content,
                final_path,
                part_title,
                comic_data,
                list(book_scan_groups),
                args.language,
            )


# -----------------------------------------------------------
# clean helper
# -----------------------------------------------------------
def rm_tree(path):
    log_verbose(f"  Cleaning up temporary directory: {path}")
    shutil.rmtree(path, ignore_errors=True)


def get_processing_params(args, calculated_width, calculated_aspect_ratio):
    """Creates a dictionary of parameters that affect image processing.
    
    These are used to check if a resumed download is compatible — if any of
    these changed, the tmp directory is cleaned and a fresh start happens.
    Only include params that affect the actual image data on disk.
    """
    return {
        "width": calculated_width,
        "aspect_ratio": calculated_aspect_ratio,
        "quality": args.quality,
        "scaling": args.scaling,
        "chapters": args.chapters,
        "group": args.group,
        "mix_by_upvote": args.mix_by_upvote,
        "no_partials": args.no_partials,
        "no_processing": args.no_processing,
    }


def get_behavior_params(args):
    """Creates a dictionary of all non-processing parameters.

    These don't affect image data so changing them shouldn't force a
    re-download. They're saved to run_params.json so --restore-parameters
    can restore the full set of flags (keep_chapters, no_final_file, etc.).

    Cross-file: must include every CLI flag the user might want preserved
    on resume. Adding a new flag elsewhere in the codebase? Audit this
    list — restore-parameters silently drops any field absent from here.
    """
    return {
        "keep_chapters": args.keep_chapters,
        "no_final_file": args.no_final_file,
        "keep_images": args.keep_images,
        "no_cleanup": args.no_cleanup,
        "split": args.split,
        "language": args.language,
        "site": args.site,
        "cookies": args.cookies,
        "image_workers": args.image_workers,
        "http_timeout": args.http_timeout,
        "http_max_retries": args.http_max_retries,
        "http_backoff_base": args.http_backoff_base,
        "http_backoff_cap": args.http_backoff_cap,
        "no_retry_missed_chapters": args.no_retry_missed_chapters,
        "missed_retries": args.missed_retries,
        # Multi-source fallback (added 2026-05-07). When the user resumes
        # via --restore-parameters from a tmp_<hid>/ folder, these come
        # back so the downloader continues with the same multi-source
        # behavior the original run had — without forcing the user to
        # re-toggle in the UI on every resume.
        "multi_source": args.multi_source,
        "multi_source_quality_min": args.multi_source_quality_min,
        # Chapter-collapse toggle (2026-05-08). Affects whether split-cluster
        # chapters (e.g. 1.1/1.2/1.3/1.4 with no integer 1) merge into one
        # combined output, whether redundant duplicate uploads are dropped,
        # and the search-display "X main / Y entries" diagnostic counts. Must
        # be persisted so a resumed run doesn't surprise-switch behavior.
        # See sites/chapter_merger.py:group_chapters_for_download for rules.
        "collapse_splits": args.collapse_splits,
        # Inter-chapter image-download prefetch worker count (Phase G7, 2026-05-08).
        # See _start_image_prefetch for the orchestration. Persisted so a
        # resumed run preserves the user's bandwidth-budget choice. Sentinel
        # -1 = match args.image_workers; 0 = off; N>0 = explicit count.
        "prefetch_image_workers": getattr(args, "prefetch_image_workers", -1),
    }


# -----------------------------------------------------------
# main
# -----------------------------------------------------------
# ------------------------------------------------------------------
# Standalone final-file builder (from already-downloaded chapter PDFs)
# ------------------------------------------------------------------
_CHAPTER_PDF_NAME_RE = re.compile(r"^(?P<prefix>.+?)\s+Ch\s+(?P<label>.+?)\.pdf$", re.IGNORECASE)

def _chapter_label_sort_key(label: str):
    """Stable numeric-ish ordering for chapter labels like '8', '8~5', '8.5', '10', '10~1'.

    Full chapters come before partials: 8 < 8~5 < 9.
    """
    s = (label or "").strip()
    if not s:
        return (10**9, 1, 0, "")
    s_norm = s.replace("~", ".")
    m = re.match(r"^(\d+)(?:\.(\d+))?", s_norm)
    if not m:
        return (10**9, 1, 0, s_norm.lower())
    main = int(m.group(1))
    sub = m.group(2)
    if sub is None:
        return (main, 0, 0, s_norm.lower())
    try:
        sub_i = int(sub)
    except Exception:
        sub_i = 0
    return (main, 1, sub_i, s_norm.lower())

def build_final_pdf_from_chapter_folder(folder: str, verbose: bool = False) -> int:
    """Build final PDF(s) inside `folder` by merging chapter PDFs already saved there.

    Expects filenames like:
      '<Series Title> Ch 1.pdf', '<Series Title> Ch 1~1.pdf', etc.

    If multiple different '<Series Title>' prefixes exist in the folder, builds one final PDF per prefix.
    Returns the number of final PDFs built.
    """
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"Folder not found: {folder}")

    pdf_paths = [p for p in glob.glob(os.path.join(folder, "*.pdf")) if os.path.isfile(p)]
    groups: Dict[str, List[Tuple[Tuple[int,int,int,str], str, str]]] = {}
    for pth in pdf_paths:
        base = os.path.basename(pth)
        m = _CHAPTER_PDF_NAME_RE.match(base)
        if not m:
            continue
        prefix = (m.group("prefix") or "").strip()
        label = (m.group("label") or "").strip()
        if not prefix:
            continue
        key = _chapter_label_sort_key(label)
        groups.setdefault(prefix, []).append((key, pth, label))

    if not groups:
        print(f"[!] No per-chapter PDFs found in: {folder}")
        print("    Expected names like: 'Series Title Ch 1.pdf'")
        return 0

    built = 0
    for prefix, items in sorted(groups.items(), key=lambda kv: kv[0].lower()):
        items.sort(key=lambda t: t[0])
        inputs = [pth for _, pth, _ in items]
        out_path = os.path.join(folder, f"{prefix}.pdf")
        tmp_out = out_path + ".tmp"

        # Avoid accidentally including an existing final file.
        inputs = [p for p in inputs if os.path.abspath(p) != os.path.abspath(out_path)]
        if not inputs:
            continue

        if verbose:
            print(f"[*] Building final PDF from {len(inputs)} chapter file(s) for: {prefix}")

        meta = {"/Title": prefix}
        merge_pdf_files(inputs, tmp_out, meta)
        os.replace(tmp_out, out_path)

        print(f"PDF saved → {os.path.basename(out_path)}")
        built += 1

    return built

def _validate_build_final_cli(p: argparse.ArgumentParser, argv: List[str]) -> None:
    """Enforce '--build-final-file' is used alone (plus optional verbosity flags)."""
    allowed = {"--build-final-file", "-v", "--verbose", "-d", "--debug"}
    for a in argv[1:]:
        if a == "--":
            break
        if a.startswith("-") and (a not in allowed):
            p.error("--build-final-file must be used on its own (optionally with -v/--verbose or -d/--debug).")



# -----------------------------------------------------------
# VRF prefetch – overlap next-chapter VRF capture with
#                current-chapter image downloads.
#
# How it works:
#   After get_chapter_images(ch_N) returns the image URLs,
#   we start a background thread that calls ensure_vrf() for
#   ch_N+1.  The VRF generator's internal single-worker executor
#   serializes Playwright operations, so this is safe.  By the
#   time _process_chapter(ch_N+1) calls ensure_vrf(), the token
#   is already cached → instant return.
#
# Why this is safe with --jobs:
#   --jobs spawns separate subprocesses, each with their own
#   Playwright browser and VRF generator.  This prefetch only
#   touches the current process's VRF generator.
# -----------------------------------------------------------
_vrf_prefetch_thread: Optional[threading.Thread] = None


def _start_vrf_prefetch(next_chapter: Optional[Dict], handler) -> None:
    """Fire-and-forget VRF capture for the next chapter (MangaFire only).

    If the handler isn't MangaFire, or the chapter dict is missing the
    required fields, this silently does nothing.  If the prefetch fails
    for any reason, the normal VRF path in get_chapter_images() will
    handle it as usual – no chapter is ever skipped because of a
    prefetch failure.
    """
    global _vrf_prefetch_thread

    if next_chapter is None:
        return

    # Only relevant for MangaFire (only site that uses VRF).
    if getattr(handler, "name", "") != "mangafire":
        return

    chapter_id = next_chapter.get("hid")
    chapter_url = next_chapter.get("url")
    if not chapter_id or not chapter_url:
        return

    # Import the VRF generator at runtime – same pattern mangafire.py uses.
    try:
        from sites.mangafire_vrf_simple import get_vrf_generator
    except Exception:
        return

    ajax_path = f"/ajax/read/chapter/{chapter_id}"
    chap_label = next_chapter.get("chap", "?")

    def _prefetch():
        try:
            vrf_gen = get_vrf_generator()
            vrf_gen.ensure_vrf(ajax_path, page_url=chapter_url, init_url=chapter_url)
            log_verbose(f"  [VRF Prefetch] Cached VRF for upcoming chapter {chap_label}")
        except Exception as e:
            # Not a problem – the normal path will capture it.
            log_verbose(f"  [VRF Prefetch] Failed for chapter {chap_label} (will retry normally): {e}")

    # If a previous prefetch is still in flight, let it finish first so we
    # don't pile up threads.  In practice this join returns almost instantly
    # because the prefetch runs on the VRF executor which serializes work.
    if _vrf_prefetch_thread is not None and _vrf_prefetch_thread.is_alive():
        _vrf_prefetch_thread.join(timeout=120)

    _vrf_prefetch_thread = threading.Thread(target=_prefetch, daemon=True, name="VRF-Prefetch")
    _vrf_prefetch_thread.start()


# -----------------------------------------------------------
# Inter-chapter image-download prefetch (Phase G7, 2026-05-08)
# -----------------------------------------------------------
# While the main thread is encoding/processing chapter N (CPU-bound), a
# background thread downloads chapter N+1's images (network I/O-bound).
# Once N's processing is done, the main thread picks up N+1 and finds the
# files already on disk — Phase 2 short-circuits and we go straight to
# processing.
#
# Coordination (no shared in-memory queue, no IPC):
#   - Prefetch worker writes a `.download_prefetched` marker into N+1's
#     tdir on full success. On partial failure it wipes tdir so main does
#     a clean re-download.
#   - Main's _process_chapter_impl sees the marker → skips the rm_tree at
#     start, then in Phase 2 resolves each download_task to its on-disk
#     prefetched file instead of re-fetching.
#   - Phase 1 (handler.get_chapter_images) still runs in main on every
#     chapter — needed for media_entries (text_blocks etc.). For mangafire
#     this is ~0.5-1s with the cached VRF; cheap enough that we don't try
#     to share metadata via sidecar JSON between threads.
#
# Gated by --prefetch-image-workers (default -1 → match --image-workers).
# Set to 0 for full opt-out when the CDN is rate-limiting and the extra
# concurrent burst hurts more than the overlap helps; set to a smaller
# positive number to keep prefetch on but with a lighter footprint.
#
# Single in-flight prefetch — main consumes (joins) before firing the next
# one, so `_image_prefetch_thread` only ever holds one outstanding task.
_image_prefetch_lock = threading.Lock()
_image_prefetch_thread: Optional[threading.Thread] = None
_image_prefetch_target_chap: Optional[str] = None


def _start_image_prefetch(
    next_chapter: Optional[Dict[str, Any]],
    target_tdir: str,
    scraper,
    handler,
    image_workers: int,
) -> None:
    """Fire-and-forget background download for next_chapter's images. On
    success, writes target_tdir/.download_prefetched so the main thread's
    next iteration can detect and skip its own Phase 2 download.

    Honors split-cluster collapse: if next_chapter carries `_merged_parts`
    (set by group_chapters_for_download for rule-5 clusters), the worker
    fetches each part's media_entries and concatenates them in order —
    matching what _process_chapter_impl would have done synchronously.
    """
    global _image_prefetch_thread, _image_prefetch_target_chap

    if next_chapter is None:
        return
    chap_label = str(next_chapter.get("chap", "?"))
    if not chap_label or chap_label == "?":
        return

    def _worker() -> None:
        try:
            # ── Phase 1: media_entries (URL list) ──
            merged_parts = next_chapter.get("_merged_parts")
            if merged_parts:
                media_entries: List[Any] = []
                for part in merged_parts:
                    try:
                        part_entries = handler.get_chapter_images(
                            part, scraper, make_request
                        ) or []
                        media_entries.extend(part_entries)
                    except Exception as exc:
                        log_verbose(
                            f"  [Img Prefetch] Ch {chap_label} part fetch failed: {exc}"
                        )
                        return
            else:
                try:
                    media_entries = handler.get_chapter_images(
                        next_chapter, scraper, make_request
                    ) or []
                except Exception as exc:
                    log_verbose(
                        f"  [Img Prefetch] Ch {chap_label} get_chapter_images failed: {exc}"
                    )
                    return

            if not media_entries:
                return

            os.makedirs(target_tdir, exist_ok=True)

            # ── Classify entries (mirrors main's Phase 1 logic) ──
            download_tasks: List[Tuple[int, str, str, str]] = []
            page_counter = 1
            for entry in media_entries:
                if isinstance(entry, dict):
                    entry_type = entry.get("type")
                    if entry_type == "text":
                        # Text blocks are re-extracted by main's own Phase 1;
                        # the prefetch only persists image bytes.
                        continue
                    if entry_type == "binary_image":
                        blob = entry.get("data")
                        if not blob:
                            continue
                        explicit_ext = entry.get("extension")
                        if explicit_ext:
                            ext = (
                                explicit_ext
                                if explicit_ext.startswith(".")
                                else "." + explicit_ext
                            )
                        else:
                            ext = _sniff_image_extension(
                                blob[:32]
                                if isinstance(blob, (bytes, bytearray))
                                else b"",
                                entry.get("content_type"),
                            )
                        custom_name = entry.get("name")
                        filename = (
                            custom_name
                            if custom_name
                            else f"{chap_label}_{page_counter:04d}{ext}"
                        )
                        pth = os.path.join(target_tdir, filename)
                        try:
                            with open(pth, "wb") as fh:
                                fh.write(blob)
                        except OSError:
                            pass
                        page_counter += 1
                        continue
                full_url = entry if isinstance(entry, str) else entry.get("url")
                if not full_url:
                    continue
                # Filename uses ".jpg" placeholder; dl_image's Phase A sniff
                # gives the file its real extension after bytes land.
                filename = f"{chap_label}_{page_counter:04d}.jpg"
                download_tasks.append((page_counter, full_url, target_tdir, filename))
                page_counter += 1

            if not download_tasks:
                # Pure binary_image chapter — write marker so main skips
                # Phase 2 anyway (there'd be nothing to download).
                _write_prefetched_marker(target_tdir)
                return

            # ── Phase 2: parallel download ──
            workers = max(1, min(image_workers, len(download_tasks)))
            failed = 0
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix=f"img-prefetch-{chap_label}"
            ) as pool:
                futures = [
                    pool.submit(dl_image, url, folder, name, scraper, True)
                    for _, url, folder, name in download_tasks
                ]
                for fut in as_completed(futures):
                    try:
                        if not fut.result():
                            failed += 1
                    except Exception:
                        failed += 1

            if failed == 0:
                _write_prefetched_marker(target_tdir)
                log_verbose(
                    f"  [Img Prefetch] Ch {chap_label} ready ({len(download_tasks)} imgs)"
                )
            else:
                # Partial failure → wipe so main starts fresh. Don't leave a
                # half-populated tdir that main's marker check would skip into.
                log_verbose(
                    f"  [Img Prefetch] Ch {chap_label} partial fail "
                    f"({failed}/{len(download_tasks)}) — discarding"
                )
                try:
                    rm_tree(target_tdir)
                except Exception:
                    pass
        except Exception as exc:
            log_verbose(f"  [Img Prefetch] Ch {chap_label} unexpected error: {exc}")

    with _image_prefetch_lock:
        # If a prior prefetch is somehow still in flight (chapter loop should
        # have consumed it), let it finish before firing the next one. Keeps
        # us bounded at one outstanding background task at any time.
        if _image_prefetch_thread is not None and _image_prefetch_thread.is_alive():
            log_verbose("  [Img Prefetch] Waiting for prior prefetch to finish")
            _image_prefetch_thread.join(timeout=120)
        _image_prefetch_target_chap = chap_label
        t = threading.Thread(
            target=_worker, daemon=True, name=f"Img-Prefetch-{chap_label}"
        )
        _image_prefetch_thread = t
        t.start()


def _write_prefetched_marker(tdir: str) -> None:
    """Write the success-marker that main's _process_chapter_impl checks for
    before deciding whether to wipe tdir + re-fetch. Safe to call on already-
    prefetched tdirs (write is idempotent)."""
    try:
        with open(os.path.join(tdir, ".download_prefetched"), "w") as fh:
            pass
    except OSError:
        pass


def _consume_image_prefetch(chap_label: str) -> None:
    """Block until any in-flight prefetch for chap_label finishes, then
    clear the module-level slot. Idempotent — call at the start of each
    chapter's processing. The prefetch's outputs are picked up via the
    .download_prefetched marker (filesystem-mediated, not in-memory)."""
    global _image_prefetch_thread, _image_prefetch_target_chap
    with _image_prefetch_lock:
        thread = _image_prefetch_thread
        target = _image_prefetch_target_chap
    if thread is None or target != str(chap_label):
        return
    if thread.is_alive():
        log_verbose(f"  Waiting for image prefetch of Ch {chap_label}...")
        thread.join(timeout=300)
    with _image_prefetch_lock:
        if _image_prefetch_target_chap == str(chap_label):
            _image_prefetch_thread = None
            _image_prefetch_target_chap = None


def main():
    p = argparse.ArgumentParser("comic downloader")
    p.add_argument("comic_url", nargs="*", help="One or more comic/manga URLs")
    p.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Download multiple URLs concurrently using separate processes (safe with Playwright). "
             "When multiple URLs are provided, up to this many downloads run at once.",
    )
    p.add_argument(
        "--prompt-urls",
        action="store_true",
        help="Prompt for multiple URLs on stdin (one per line). Finish with an empty line.",
    )

    p.add_argument(
        "--coord-dir",
        default=os.getenv("AIO_COORD_DIR", os.path.join("mangas", ".aio_coord")),
        help="Directory for cross-process coordination state/locks (default: mangas/.aio_coord).",
    )
    p.add_argument(
        "--net-min-gap",
        type=float,
        default=float(os.getenv("AIO_NET_MIN_GAP", "0.25")),
        help="Minimum delay (seconds) between network request starts across processes (default: 0.25).",
    )
    p.add_argument(
        "--job-stall-timeout",
        type=int,
        default=int(os.getenv("AIO_JOB_STALL_TIMEOUT", "900")),
        help="In batch mode, kill+retry a worker if it hasn't updated its heartbeat in this many seconds (default: 900).",
    )
    p.add_argument(
        "--job-hard-timeout",
        type=int,
        default=int(os.getenv("AIO_JOB_HARD_TIMEOUT", "0")),
        help="In batch mode, kill+retry a worker if total runtime exceeds this many seconds (0 disables).",
    )
    p.add_argument(
        "--job-retries",
        type=int,
        default=int(os.getenv("AIO_JOB_RETRIES", "3")),
        help="In batch mode, retry a failed/stalled URL this many times before giving up (default: 3).",
    )
    p.add_argument(
        "--job-spawn-gap",
        type=float,
        default=float(os.getenv("AIO_JOB_SPAWN_GAP", "1.5")),
        help="Delay between launching worker processes to avoid bursty request patterns (default: 1.5s).",
    )
    p.add_argument(
        "--http-timeout",
        type=float,
        default=float(os.getenv("AIO_HTTP_TIMEOUT", "30")),
        help="HTTP timeout in seconds for HTML/AJAX requests (default: 30).",
    )
    p.add_argument(
        "--http-max-retries",
        type=int,
        default=int(os.getenv("AIO_HTTP_MAX_RETRIES", "6")),
        help="Max retries for HTML/AJAX requests (default: 6).",
    )
    p.add_argument(
        "--http-backoff-base",
        type=float,
        default=float(os.getenv("AIO_HTTP_BACKOFF_BASE", "1.0")),
        help="Base seconds for exponential backoff (default: 1.0).",
    )
    p.add_argument(
        "--http-backoff-cap",
        type=float,
        default=float(os.getenv("AIO_HTTP_BACKOFF_CAP", "45")),
        help="Max seconds for backoff sleep (default: 45).",
    )
    p.add_argument(
        "--image-workers",
        type=int,
        default=int(os.getenv("AIO_IMAGE_WORKERS", "3")),
        help="Number of parallel threads for downloading images within a single chapter (default: 3). "
             "Set to 1 to download images one at a time (old behaviour).",
    )

    p.add_argument(
        "--site",
        type=str,
        default=None,
        help="Explicitly select the site handler (auto-detected by URL when omitted).",
    )
    # ── Cross-site search (Phase 1a per snappy-forging-waffle.md) ──
    # See sites/search_orchestrator.py for ranking; aio_search_cli.py for
    # the per-handler scraper factory and JSON/auto-pick branching.
    p.add_argument(
        "--search",
        type=str,
        default=None,
        metavar="QUERY",
        help="Search across all search-capable sites for a manga title and "
             "print ranked candidates as JSON. Without --auto-pick, exits after "
             "printing. With --auto-pick, picks the top result and downloads it.",
    )
    p.add_argument(
        "--auto-pick",
        action="store_true",
        help="With --search: select the top-ranked candidate and run the "
             "normal download pipeline against its URL.",
    )
    p.add_argument(
        "--search-language",
        type=str,
        default=None,
        help="Language filter for --search (default: --language, or 'en'). "
             "Use 'all' to disable. Site-specific: MangaDex applies it as "
             "availableTranslatedLanguage; other sites mostly ignore it.",
    )
    p.add_argument(
        "--search-parallelism",
        type=int,
        default=6,
        help="Number of sites probed in parallel for --search (default: 6).",
    )
    p.add_argument(
        "--search-timeout",
        type=float,
        default=20.0,
        help="Per-site search timeout in seconds (default: 20.0). Sized for "
             "MangaFire's Playwright bridge: ~3s browser warmup + ~3-4s page "
             "navigation with networkidle + up to 12s capture_search retry "
             "loop. Pure-HTTP handlers complete in <2s. Slow sites self-select "
             "out and the probe-failure cache suppresses them for 1h after "
             "2 timeouts.",
    )
    p.add_argument(
        "--search-min-match",
        type=float,
        default=0.55,
        help="Drop search hits below this rapidfuzz WRatio similarity "
             "(0.0-1.0, default 0.55). Lower = looser, more false positives.",
    )
    p.add_argument(
        "--search-json",
        action="store_true",
        help="Force JSON output for --search even when --auto-pick is set "
             "(prints candidates to stdout, picks winner internally). Useful "
             "for UI integrations that want to display the candidate list "
             "while still proceeding with a download.",
    )
    p.add_argument(
        "--multi-source",
        action="store_true",
        help="Enable cross-site multi-source mode. Works with --search "
             "--auto-pick OR with a direct URL. Pre-fetches chapter lists "
             "from alternative sources and uses them for per-chapter download "
             "fallback when the primary source fails (e.g., CDN 520 errors). "
             "Alternatives are filtered to high-seed-quality sites by default "
             "(see --multi-source-quality-min) — this keeps unknown-quality / "
             "foreign-language Madara extras out of the fallback rotation.",
    )
    p.add_argument(
        "--multi-source-quality-min",
        type=float,
        default=0.65,
        help="Minimum seed_quality (or measured img_quality_score) for a "
             "source to be eligible as a multi-source alternative. Default "
             "0.65 excludes unknown-quality sites (default 0.50) which are "
             "mostly foreign-language Madara/MangaThemesia extras. Set lower "
             "(e.g., 0.4) to opt those back in if you want broader fallback "
             "coverage and don't mind language drift.",
    )
    p.add_argument(
        "--seeded-only",
        action="store_true",
        help="Restrict --search fan-out (and the multi-source title-search "
             "for --multi-source on a direct URL) to handlers explicitly "
             "listed in sites/quality_seed.json. Skips the ~250 Madara/"
             "MangaThemesia extras that default to seed=0.50 — most of "
             "those are foreign-language and contribute mostly noise to "
             "rankings. Significantly faster (typically halves search wall "
             "time on popular queries) at the cost of dropping niche sites "
             "that aren't in the curated list.",
    )
    p.add_argument(
        "--prefetch-image-workers",
        type=int,
        default=-1,
        help="Number of parallel workers for the inter-chapter image prefetch. "
             "Default -1 = match --image-workers (typical 12). Set to 0 to "
             "disable prefetch entirely. Positive N = use exactly N workers, "
             "regardless of --image-workers. Useful when the upstream CDN is "
             "rate-limiting (Cloudflare 5xx storms) — drop to 4 or 0 so the "
             "extra concurrent burst from N+1's downloads doesn't compound "
             "throttling. While prefetch is active a background thread "
             "downloads chapter N+1's images while the main thread encodes "
             "chapter N (typical 2-5s wall-clock saved per chapter on "
             "mangafire-style long-strip CBZ runs). 0 falls back to fully-"
             "sequential download → process → next-download.",
    )
    p.add_argument(
        "--no-collapse-splits",
        dest="collapse_splits",
        action="store_false",
        default=True,
        help="Disable split-cluster collapse in --multi-source coverage "
             "diagnostics. By default (collapse ON), decimal sub-chapters "
             "X.1/X.2/X.3 with no integer X are reported as ONE main "
             "chapter for the per-source 'effective' count — fixes the "
             "misleading 362-vs-119 display where one aggregator splits "
             "each chapter into 4 decimal entries. Pass this flag if your "
             "series legitimately uses decimal numbering (some webnovel "
             "adaptations / episodic releases) and you want each decimal "
             "counted as its own chapter. Affects only the displayed "
             "counts; the per-chapter download alternatives in the "
             "chapter_map are unchanged either way.",
    )
    p.add_argument(
        "--multi-source-prefetched",
        default=None,
        help="Path to a JSON file with pre-discovered alternative sources "
             "for --multi-source on a direct URL. Skips the cross-site "
             "search step (find_alternatives_for_direct_url) and uses the "
             "listed sources directly — saves ~80s when the UI's Search "
             "tab already discovered the alternatives in a recent session. "
             "JSON shape: {\"primary\": {\"site\", \"url\"}, "
             "\"alternatives\": [{\"site\", \"url\"}, ...], \"title\": ...}. "
             "Quietly falls back to the search path if the file is missing/"
             "malformed.",
    )
    p.add_argument("--cookies", default="")
    p.add_argument(
        "--group",
        nargs="+",
        default=[],
        help="One or more preferred scanlation groups, in order of priority. "
        'Can be a single quoted string with commas (e.g., "A, B") '
        'or multiple arguments (e.g., "A" "B").',
    )
    p.add_argument(
        "--mix-by-upvote",
        action="store_true",
        help="When multiple --group args are used, ignore priority and pick the "
        "version with the most upvotes from any of the specified groups.",
    )
    p.add_argument(
        "--no-partials",
        action="store_true",
        help="Skip chapters with partial numbers (e.g., 1.5, 60.1).",
    )
    p.add_argument("--chapters", default="all")
    p.add_argument(
        "--list-chapters",
        action="store_true",
        help="Fetch the chapter list and series metadata as JSON, then exit. No downloading.",
    )


    p.add_argument(
        "--no-retry-missed-chapters",
        action="store_true",
        help="Disable end-of-run retry for chapters that failed to download/process.",
    )
    p.add_argument(
        "--missed-retries",
        type=int,
        default=2,
        help="Number of retry attempts per missed chapter at the end of the run (default: 2).",
    )
    p.add_argument(
        "--missed-log",
        default=None,
        help="Optional path for the temporary missed-chapter log (default: tmp_<hid>/missed_chapters.json).",
    )
    # ── Per-chapter zero-tolerance + inline retry + hard abort knobs ──
    # The script never produces a partial chapter PDF: any missing page →
    # _process_chapter_strict retries the whole chapter inline → hard abort
    # if all inline retries fail. See ChapterSkippedError, ChapterAbortedError.
    p.add_argument(
        "--chapter-deadline-seconds",
        type=float,
        default=float(os.getenv("AIO_CHAPTER_DEADLINE", "90")),
        help="Per-chapter wall-clock budget. Chapters exceeding this trigger "
             "the inline retry pass (doubled on the end-of-run retry pass). "
             "Set 0 to disable. Default: 90.",
    )
    p.add_argument(
        "--chapter-host-poison-threshold",
        type=int,
        default=int(os.getenv("AIO_CHAPTER_HOST_POISON", "5")),
        help="Treat the chapter as failed if N distinct URLs to the same host "
             "fully fail during one chapter (so we don't grind through all "
             "the variants for every page). Set 0 to disable. Default: 5.",
    )
    p.add_argument(
        "--inline-chapter-retries",
        type=int,
        default=int(os.getenv("AIO_INLINE_CHAPTER_RETRIES", "2")),
        help="If a chapter has any missing page after Phase 2, retry the whole "
             "chapter inline (long backoff between attempts). After this many "
             "retries with a missing page, the run aborts with a fatal error. "
             "Set 0 to abort on the first failed chapter. Default: 2.",
    )
    p.add_argument(
        "--inline-chapter-backoff",
        type=float,
        default=float(os.getenv("AIO_INLINE_CHAPTER_BACKOFF", "30")),
        help="Base wait (seconds) between inline chapter retries. Doubles each "
             "retry: 30s, 60s, 120s, ... Gives the upstream CDN time to "
             "recover before we hit the same URLs again. Default: 30.",
    )
    p.add_argument("--language", default="en")
    p.add_argument(
        "--format", choices=["pdf", "epub", "cbz", "none"], default="epub"
    )
    p.add_argument(
        "--epub-layout", choices=["page", "vertical"], default="vertical"
    )
    p.add_argument(
        "--width",
        type=int,
        default=None,
        help="Base width to process images at (px). Defaults vary by format.",
    )
    p.add_argument(
        "--aspect-ratio",
        type=str,
        default=None,
        help="Target W:H ratio for processing (e.g., '4:3'). Not used for PDF.",
    )
    p.add_argument(
        "--quality",
        type=int,
        default=85,
        choices=range(1, 101),
        metavar="[1-100]",
        help="Final JPEG quality for saved images (default: 85).",
    )
    p.add_argument(
        "--scaling",
        type=int,
        default=100,
        choices=range(1, 101),
        metavar="[1-100]",
        help="Scale final image resolution. For EPUB/CBZ, re-combines scaled pages.",
    )
    p.add_argument(
        "--split",
        default=None,
        help='Split into parts by size (e.g., "400MB") or chapter count (e.g., "10ch").',
    )
    p.add_argument(
        "--restore-parameters",
        action="store_true",
        help="Restore processing settings from the temp folder for re-assembly. "
        "Requires setting a new --format.",
    )
    p.add_argument(
        "--keep-images",
        action="store_true",
        help="Keep the original, unprocessed images in a structured folder.",
    )
    p.add_argument(
        "--keep-chapters",
        action="store_true",
        help="Additionally, save a separate file for each chapter.",
    )
    p.add_argument(
        "--no-final-file",
        action="store_true",
        help="With --keep-chapters, skip building the combined series file at the end.",
    )
    p.add_argument(
        "--build-final-file",
        action="store_true",
        help="Standalone mode (no downloading): build a combined PDF from existing chapter PDFs "
             "in the given folder path(s). Each folder should contain chapter files like 'Title Ch 1.pdf'.",
    )
    p.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Do not delete the temporary processing directory on completion.",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable detailed, step-by-step logging.",
    )
    p.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Enable highly detailed debug-level logging for image processing.",
    )
    p.add_argument(
        "--no-processing",
        action="store_true",
        help="Skip all image post-processing (resize, recombine, scaling). "
        "Builds formats directly from the raw downloaded images.",
    )
    p.add_argument(
        "--no-cbz-preserve-originals",
        action="store_true",
        help="Force CBZ to decode/re-encode every page instead of preserving "
        "original wire bytes. Default: when --format cbz with --scaling 100 "
        "and no --width / --aspect-ratio / --quality override, the wire bytes "
        "are written into the archive untouched (lossless, fastest, smallest). "
        "This flag forces the legacy decode/recombine/re-encode path even "
        "when no transform was requested.",
    )
    args = p.parse_args()
    # -----------------------------
    # Argument sanity checks / modes
    # -----------------------------
    if args.no_final_file and (not args.keep_chapters):
        p.error("--no-final-file requires --keep-chapters.")

    # --search is checked before --list-chapters / build-final-file because it
    # resolves the URL, and the downstream modes' "URL required" check would
    # otherwise fire before search runs.
    if getattr(args, "search", None):
        # --search mode: query is the input, URL is the output.
        # With --auto-pick: search resolves to a single URL that falls into the
        # normal single-URL flow (which then honors --list-chapters etc.).
        # Without --auto-pick: print JSON and exit; downstream flags ignored.
        if args.comic_url:
            p.error("--search and a positional URL are mutually exclusive.")
        if getattr(args, "prompt_urls", False):
            p.error("--search cannot be combined with --prompt-urls.")
        if args.build_final_file:
            p.error("--search cannot be combined with --build-final-file.")
        if getattr(args, "list_chapters", False) and not getattr(args, "auto_pick", False):
            p.error("--search --list-chapters requires --auto-pick (search resolves the URL first).")
    elif args.build_final_file:
        _validate_build_final_cli(p, sys.argv)
        if getattr(args, "prompt_urls", False):
            p.error("--build-final-file cannot be used with --prompt-urls.")
        if not args.comic_url:
            p.error("--build-final-file requires one or more folder paths as positional arguments.")
    elif getattr(args, "list_chapters", False):
        # --list-chapters mode: need exactly one URL, nothing else matters
        if not args.comic_url or len(args.comic_url) != 1:
            p.error("--list-chapters requires exactly one URL.")
    else:
        # In normal download mode, require at least one URL unless prompt mode is enabled.
        if (not getattr(args, "prompt_urls", False)) and (not args.comic_url):
            p.error("You must provide at least one URL (or use --prompt-urls).")

    # Apply tunables to module globals (used by make_request / dl_image)
    globals()["_HTTP_TIMEOUT"] = float(getattr(args, "http_timeout", 30.0))
    globals()["_HTTP_MAX_RETRIES"] = int(getattr(args, "http_max_retries", 6))
    globals()["_HTTP_BACKOFF_BASE"] = float(getattr(args, "http_backoff_base", 1.0))
    globals()["_HTTP_BACKOFF_CAP"] = float(getattr(args, "http_backoff_cap", 45.0))
    # Per-chapter strict-mode tunables — read inside _try_download_url, dl_image,
    # _process_chapter (watchdog), and _process_chapter_strict (inline retry).
    # The script is "all or nothing" per chapter: any missing page triggers an
    # inline retry; after _INLINE_CHAPTER_RETRIES exhausted, the run aborts.
    globals()["_CHAPTER_DEADLINE"] = float(getattr(args, "chapter_deadline_seconds", 90.0))
    globals()["_CHAPTER_HOST_POISON"] = int(getattr(args, "chapter_host_poison_threshold", 5))
    globals()["_INLINE_CHAPTER_RETRIES"] = int(getattr(args, "inline_chapter_retries", 2))
    globals()["_INLINE_CHAPTER_BACKOFF"] = float(getattr(args, "inline_chapter_backoff", 30.0))

    # Coordinator setup (cross-process NET/CPU pipelining)
    coord_dir = os.getenv("AIO_COORD_DIR", "").strip() or getattr(args, "coord_dir", "")
    coord_enabled = os.getenv("AIO_COORD_ENABLED", "").strip() not in ("", "0", "false", "False")
    if coord_enabled and coord_dir:
        try:
            globals()["_COORD"] = _AIOCoordinator(coord_dir=coord_dir, net_min_gap=float(getattr(args, "net_min_gap", 0.25)))
        except Exception:
            globals()["_COORD"] = None

    _hb("start", "parsed_args")

    # ------------------------------------------------------------------
    # --search: cross-site search mode (Phase 1a per snappy-forging-waffle.md)
    # ------------------------------------------------------------------
    # Without --auto-pick: print JSON candidates, exit cleanly.
    # With --auto-pick: replace args.comic_url with the winner URL and fall
    # through into the normal single-URL flow below. The search resolves to
    # one URL — multi-URL/--prompt-urls modes are blocked at validation above.
    # Closure-scope multi-source state. Populated when --search --multi-source
    # --auto-pick is set: dict mapping chapter_num_float → list of alternative
    # source dicts (each with handler/scraper/context/chapter). Consumed by
    # _process_chapter_strict for per-chapter fallback. Empty/None means
    # single-source mode (existing behavior unchanged).
    _multi_source_alternatives: Dict[float, List[Dict[str, Any]]] = {}

    if getattr(args, "search", None):
        from aio_search_cli import run_search_mode, take_latest_multi_source_state
        winner_url = run_search_mode(
            args,
            make_request=make_request,
            record_rate_limit=_record_rate_limit,
        )
        if winner_url is None:
            # JSON-only mode: candidates printed, exit cleanly.
            return
        # --auto-pick: continue with the chosen URL.
        args.comic_url = [winner_url]
        # Pick up the multi-source state, if any, before main() proceeds.
        _ms_state = take_latest_multi_source_state()
        if _ms_state and _ms_state.get("alternatives_by_chap_num"):
            _multi_source_alternatives = _ms_state["alternatives_by_chap_num"]
            n_alts = sum(len(v) for v in _multi_source_alternatives.values())
            n_chapters_with_alts = len(_multi_source_alternatives)
            print(
                f"[*] Multi-source ON: {n_chapters_with_alts} chapters have "
                f"alternative sources ({n_alts} total fallback paths)",
                file=sys.stderr,
            )

    # ------------------------------------------------------------------
    # Multi-URL / multi-job runner
    # ------------------------------------------------------------------
    urls: List[str] = list(args.comic_url) if isinstance(args.comic_url, list) else [str(args.comic_url)]
    # Standalone mode: build a final PDF from already-downloaded chapter PDFs in folder(s).
    if args.build_final_file:
        built_any = 0
        for folder in urls:
            if str(folder).lower().startswith("http://") or str(folder).lower().startswith("https://"):
                p.error("--build-final-file expects folder paths, not URLs.")
            try:
                built_any += build_final_pdf_from_chapter_folder(folder, verbose=bool(args.verbose))
            except Exception as e:
                print(f"[!] Failed to build final file for '{folder}': {e}")
        if built_any == 0:
            print("[!] No final files were built.")
        return
    if args.prompt_urls:
        # If prompt mode is enabled, read additional URLs from stdin.
        print("[*] Paste one or more URLs (one per line). Submit an empty line to start.")
        while True:
            try:
                line = input().strip()
            except EOFError:
                break
            if not line:
                break
            urls.append(line)

    # Basic validation
    urls = [u for u in urls if u]
    if not urls:
        sys.exit("No URL provided. Pass a URL or use --prompt-urls.")

        # If multiple URLs were provided, run them sequentially (jobs=1) or concurrently (jobs>1)
    if len(urls) > 1:
        jobs = max(1, int(getattr(args, "jobs", 1) or 1))
        job_retries = max(0, int(getattr(args, "job_retries", 3) or 3))
        stall_timeout = max(30, int(getattr(args, "job_stall_timeout", 900) or 900))
        hard_timeout = max(0, int(getattr(args, "job_hard_timeout", 0) or 0))
        spawn_gap = float(getattr(args, "job_spawn_gap", 1.5) or 1.5)

        coord_dir = os.getenv("AIO_COORD_DIR", "").strip() or getattr(args, "coord_dir", "") or os.path.join("mangas", ".aio_coord")
        coord_dir = os.path.abspath(coord_dir)
        hb_dir = os.path.join(coord_dir, "heartbeats")
        os.makedirs(hb_dir, exist_ok=True)

        failures_path = os.path.join(coord_dir, "batch_failures.json")

        print(f"[*] Starting {len(urls)} downloads with up to {jobs} worker(s)...")
        print(f"[*] Coordinator dir: {coord_dir}")

        orig_argv = sys.argv[1:]
        url_set = set(urls)

        child_base: List[str] = []
        skip_next = False
        for tok in orig_argv:
            if skip_next:
                skip_next = False
                continue
            if tok in url_set:
                continue
            if tok == "--jobs":
                skip_next = True
                continue
            if tok.startswith("--jobs="):
                continue
            if tok in ("--prompt-urls", "--prompt_urls"):
                continue
            child_base.append(tok)

        if "--coord-dir" not in " ".join(child_base):
            child_base.extend(["--coord-dir", coord_dir])
        if "--net-min-gap" not in " ".join(child_base):
            child_base.extend(["--net-min-gap", str(getattr(args, "net_min_gap", 0.25))])

        def _load_failures() -> Dict[str, Any]:
            try:
                with open(failures_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, dict) else {}
            except Exception:
                return {}

        def _save_failures(data: Dict[str, Any]) -> None:
            try:
                tmp = failures_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp, failures_path)
            except Exception:
                pass

        failures_db = _load_failures()
        failures_db.setdefault("failed", [])
        failures_db.setdefault("attempts", {})

        queue: List[Dict[str, Any]] = [{"url": u, "attempt": int(failures_db["attempts"].get(u, 0))} for u in urls]
        running: Dict[int, Dict[str, Any]] = {}

        def _spawn(job: Dict[str, Any]):
            worker_id = _uuid.uuid4().hex[:10]
            hb_path = os.path.join(hb_dir, f"{worker_id}.json")
            env = os.environ.copy()
            env["AIO_COORD_DIR"] = coord_dir
            env["AIO_COORD_ENABLED"] = "1" if jobs > 1 else "0"
            env["AIO_WORKER_ID"] = worker_id
            env["AIO_HEARTBEAT_FILE"] = hb_path
            env["AIO_TARGET_URL"] = job["url"]

            cmd = [sys.executable, sys.argv[0], *child_base, job["url"]]
            p = subprocess.Popen(cmd, env=env)
            running[p.pid] = {
                "p": p,
                "job": job,
                "worker_id": worker_id,
                "hb": hb_path,
                "start": time.time(),
            }
            time.sleep(max(0.0, spawn_gap))

        def _read_hb(path: str) -> Optional[Dict[str, Any]]:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, dict) else None
            except Exception:
                return None

        completed = 0
        while queue or running:
            while queue and len(running) < jobs:
                _spawn(queue.pop(0))

            now = time.time()

            for pid, info in list(running.items()):
                p = info["p"]
                rc = p.poll()
                job = info["job"]
                hb_path = info["hb"]
                started = info["start"]

                if hard_timeout and (now - started) > hard_timeout and rc is None:
                    print(f"[!] Hard timeout. Killing worker pid={pid} for URL: {job['url']}")
                    try:
                        p.kill()
                    except Exception:
                        pass
                    rc = -9

                hb = _read_hb(hb_path)
                last_ts = float(hb.get("ts", 0.0)) if hb else 0.0
                if rc is None and last_ts and (now - last_ts) > stall_timeout:
                    print(f"[!] Stall detected (> {stall_timeout}s). Killing worker pid={pid} for URL: {job['url']}")
                    try:
                        p.kill()
                    except Exception:
                        pass
                    rc = -9

                if rc is None:
                    continue

                running.pop(pid, None)
                try:
                    if os.path.exists(hb_path):
                        os.remove(hb_path)
                except Exception:
                    pass

                if rc == 0:
                    completed += 1
                    print(f"[*] Completed ({completed}/{len(urls)}): {job['url']}")
                    continue

                job["attempt"] = int(job.get("attempt", 0)) + 1
                failures_db["attempts"][job["url"]] = job["attempt"]
                if job["attempt"] <= job_retries:
                    print(f"[!] Worker failed (rc={rc}) for URL: {job['url']} → retry {job['attempt']}/{job_retries}")
                    queue.append(job)
                else:
                    print(f"[!] Giving up after {job_retries} retries: {job['url']}")
                    failures_db["failed"].append({"url": job["url"], "rc": rc, "attempts": job["attempt"]})

                _save_failures(failures_db)

            if queue or running:
                time.sleep(0.25)

        if failures_db.get("failed"):
            print(f"[!] Batch finished with failures. See: {failures_path}")
            if completed == 0:
                sys.exit(1)
        return

    # Single-URL mode: unwrap the list into a string for the rest of the script.
    args.comic_url = urls[0]

    handler = resolve_site_handler(args.comic_url, args.site)
    if not handler:
        sys.exit("Unable to resolve site handler. Use --site to specify explicitly.")

    # Process the group argument to handle comma-separated strings
    if args.group:
        # Flatten the list of strings, splitting each by comma, and stripping whitespace.
        args.group = [
            g.strip()
            for group_string in args.group
            for g in group_string.split(",")
        ]

    global _VERBOSE, _DEBUG
    _VERBOSE = args.verbose
    _DEBUG = args.debug

    # Create HTTP session:
    # - Prefer cloudscraper on Python >= 3.7
    # - On Python < 3.7 or any init error, fall back to requests.Session
    use_cloudscraper = cloudscraper is not None and sys.version_info >= (3, 7)
    if use_cloudscraper:
        try:
            scraper = cloudscraper.create_scraper(
                browser={
                    "browser": "chrome",
                    "platform": "darwin",
                    "mobile": False,
                }
            )
        except Exception as e:
            log_verbose(
                f"  Warning: cloudscraper init failed ({e}). "
                "Falling back to requests.Session()"
            )
            scraper = requests.Session()
    else:
        scraper = requests.Session()
    if args.cookies:
        scraper.cookies.update(
            dict(kv.split("=", 1) for kv in args.cookies.split(";") if "=" in kv)
        )
    handler.configure_session(scraper, args)

    try:
        context: SiteComicContext = handler.fetch_comic_context(
            args.comic_url, scraper, make_request
        )
    except Exception as e:
        if isinstance(e, SystemExit):
            raise
        sys.exit(f"Failed to fetch comic data: {e}")

    comic_data = context.comic
    hid, title = context.identifier, context.title

    # Defensive cleanup: in some setups the title string may already include
    # a suffix like "(hid=xxxx)". We always want the folder/file naming base
    # to exclude that suffix.
    title = re.sub(r"\s*\(hid=[^)]+\)\s*$", "", str(title or "")).strip() or "comic"
    print(f"{title} (hid={hid})")

    main_tmp_dir = os.path.abspath(f"tmp_{hid}")

    if args.restore_parameters:
        params_path = os.path.join(main_tmp_dir, "run_params.json")
        print(f"Attempting to restore parameters from: {params_path}")

        if not os.path.exists(params_path):
            sys.exit(
                f"Error: --restore-parameters failed. File not found: {params_path}\n"
                "Please run the script once without this flag to download content first."
            )

        # Store the format from the new command line, as requested
        new_format = args.format
        new_epub_layout = args.epub_layout

        try:
            with open(params_path, "r") as f:
                restored_params = json.load(f)

            # Update the args namespace with the restored parameters
            for key, value in restored_params.items():
                setattr(args, key, value)

            # Crucially, apply the new format settings
            args.format = new_format
            args.epub_layout = new_epub_layout

            print("  Successfully restored parameters. The following settings will be used:")
            log_verbose(f"    - Chapters: {args.chapters}")
            log_verbose(f"    - Group(s): {args.group}")
            log_verbose(f"    - Width: {args.width}")
            log_verbose(f"    - Aspect Ratio: {args.aspect_ratio}")
            log_verbose(f"    - Scaling: {args.scaling}%")
            log_verbose(f"    - Quality: {args.quality}")
            log_verbose(f"    - Keep Chapters: {args.keep_chapters}")
            log_verbose(f"    - No Final File: {args.no_final_file}")
            log_verbose(f"    - Language: {args.language}")
            log_verbose(f"    - Site: {args.site}")
            print(f"  New output format will be: {args.format.upper()}")

        except (json.JSONDecodeError, TypeError) as e:
            sys.exit(f"Error: Could not parse parameters file at {params_path}: {e}")

    split_size_bytes = 0
    split_chapter_count = 0
    if args.split:
        if args.split.lower().endswith("ch"):
            try:
                split_chapter_count = int(args.split[:-2])
            except ValueError:
                sys.exit("Invalid chapter count for --split (e.g., '10ch').")
        else:
            try:
                split_size_bytes = parse_size(args.split)
            except ValueError as e:
                sys.exit(e)

    # Phase B (2026-05-07): snapshot which CLI flags the user explicitly set
    # BEFORE format-defaulting fills in `width` / `aspect_ratio` from the
    # format-specific defaults below. The CBZ fast-path uses these booleans
    # to detect "user wants the wire bytes verbatim" vs "user asked for a
    # transform." `--width` / `--aspect-ratio` argparse-default to None so
    # `is None` is the user-set test; `--quality` defaults to 85 so we sniff
    # sys.argv for it instead.
    args._user_set_width = args.width is not None
    args._user_set_aspect_ratio = args.aspect_ratio is not None
    # Phase G4 (2026-05-08): --quality 100 means "highest quality, no
    # tradeoffs" — exactly what the fast-path provides. Treating it as a
    # transform-request would force CBZ into the legacy decode/recombine/
    # re-encode path, defeating the byte-preservation. The UI's Settings
    # quality slider defaults to 100, so without this guard EVERY
    # UI-spawned CBZ download fell into legacy. Only quality < 100 now
    # signals "user wants smaller/lossy."
    args._user_set_quality = (
        any(
            a == "--quality" or a.startswith("--quality=")
            for a in sys.argv[1:]
        )
        and args.quality < 100
    )

    width = args.width
    aspect_ratio_str = args.aspect_ratio

    if args.no_processing:
        # No processing: ignore aspect/width/scaling messages and recombine logic.
        aspect_ratio_str = None
        log_verbose(
            "No-processing: raw images will be packaged as-is. "
            "Skipping resize, recombine, and scaling."
        )

    if args.format == "epub":
        if args.epub_layout == "page":
            if width is None:
                width = 1500
            if aspect_ratio_str is None:
                aspect_ratio_str = "2.5"
        else:  # vertical
            if width is None:
                width = 2000
            if aspect_ratio_str is None:
                aspect_ratio_str = "4:3"
    elif args.format == "cbz":
        if width is None:
            width = 1500
        if aspect_ratio_str is None:
            aspect_ratio_str = "2.5"
    elif args.format == "pdf":
        if width is None:
            width = 1500
        aspect_ratio_str = None
    elif args.format == "none":
        if width is None:
            width = 1500
        aspect_ratio_str = None
        args.keep_images = True

    recombine_target_height = 0
    if not args.no_processing and aspect_ratio_str:
        ratio = parse_aspect_ratio(aspect_ratio_str)
        recombine_target_height = int(width * ratio)
        log_verbose(
            f"  Processing images at {width}px width, aspect ratio {aspect_ratio_str} (~{recombine_target_height}px height)"
        )
    elif not args.no_processing:
        log_verbose(
            f"  Processing images at {width}px width (original aspect ratio)"
        )

    scale_factor = args.scaling / 100.0
    if not args.no_processing and scale_factor != 1.0:
        log_verbose(
            f"  Final images will be scaled to {args.scaling}% of this size."
        )

    extra_metadata = handler.extract_additional_metadata(context)
    if extra_metadata:
        comic_data.update(extra_metadata)
        log_verbose("  Extracted metadata (Authors, Artists, Genres, etc.)")

    def sanitize_filename(name: str) -> str:
        """Sanitize a filename component for Windows and remove underscores.
        Keeps spaces for readability (and for your no-underscore preference).
        """
        s = re.sub(r'[\\/*?:"<>|]', "", str(name or ""))
        # Remove underscores in the *output* filenames (replace with spaces).
        s = s.replace("_", " ")
        # Collapse whitespace and trim.
        s = re.sub(r"\s+", " ", s).strip()
        # Windows: avoid trailing dots/spaces.
        s = s.rstrip(" .")
        return s

    def join_name(*parts: str) -> str:
        s = " ".join([p for p in parts if p])
        s = re.sub(r"\s+", " ", s).strip()
        s = s.rstrip(" .")
        return s

    _DECIMAL_DOT_LAST_RE = re.compile(r'(\d)\.(\d)(?!.*\d\.\d)')  # last digit.dot.digit
    _KNOWN_EXTS = {".pdf", ".cbz", ".epub", ".zip", ".png", ".jpg", ".jpeg", ".webp"}

    def format_chap_for_filename(chap) -> str:
        """Format chapter label for filenames so lexical sort matches chapter order.

        - Keeps the original chapter number for logic/selection.
        - Replaces a decimal dot with '~' so '1' sorts before '1~1'.
        - If a full filename is passed in, only touches the chapter-number portion after the chapter marker.
        - Avoids treating decimal chapters like '8.5' as having an extension ('.5').
        """
        s = str(chap).strip()

        # Only treat trailing '.ext' as a real extension for known file types (e.g. '.pdf').
        stem, ext = os.path.splitext(s)
        if ext.lower() not in _KNOWN_EXTS:
            stem, ext = s, ""

        # The output naming uses " Ch " (no underscores).
        marker = " Ch "
        i = stem.rfind(marker)
        if i != -1:
            prefix = stem[: i + len(marker)]
            chap_part = stem[i + len(marker) :]
            chap_part = _DECIMAL_DOT_LAST_RE.sub(r"\1~\2", chap_part, count=1)
            return prefix + chap_part + ext

        # Otherwise, treat input as just the chapter label.
        stem = _DECIMAL_DOT_LAST_RE.sub(r"\1~\2", stem, count=1)
        return stem + ext

    safe_title = sanitize_filename(title) or "comic"
    # Provider/site label intentionally omitted from filenames for cleaner names.
    base_filename = safe_title
    if args.group:
        safe_group = sanitize_filename(" ".join(args.group))
        base_filename = join_name(base_filename, safe_group)

    pool = handler.get_chapters(context, scraper, args.language, make_request)

    # ── Direct-URL multi-source: find alternatives for fallback ──
    # When --multi-source is set and we got here via a direct URL (not via
    # --search --auto-pick which already populated _multi_source_alternatives),
    # search for the series title across other handlers and pre-fetch their
    # chapter lists so per-chapter fallback in _process_chapter_strict has
    # alternatives to try. Skipped when --list-chapters is set (read-only
    # mode, no downloads happening, alternatives discovery would just waste
    # time).
    if (
        getattr(args, "multi_source", False)
        and not getattr(args, "search", None)
        and not getattr(args, "list_chapters", False)
        and not _multi_source_alternatives  # not already populated
    ):
        # Fix B (2026-05-07): when the UI passes --multi-source-prefetched, use
        # the JSON-listed alts instead of running cross-site search again. The
        # search-tab download path writes this file just before spawning aio-dl
        # so we skip the redundant ~80s search. Falls back to search if the
        # file is missing or malformed (defensive — doesn't fail the download).
        prefetched_path = getattr(args, "multi_source_prefetched", None)
        try:
            if prefetched_path:
                from aio_search_cli import build_alternatives_from_prefetched
                _ms_alts = build_alternatives_from_prefetched(
                    prefetched_path=prefetched_path,
                    primary_handler=handler,
                    primary_context=context,
                    primary_chapters=pool,
                    args=args,
                    make_request=make_request,
                    on_status=lambda m: print(m, file=sys.stderr),
                )
            else:
                from aio_search_cli import find_alternatives_for_direct_url
                _ms_alts = find_alternatives_for_direct_url(
                    primary_url=args.comic_url,
                    primary_handler=handler,
                    primary_context=context,
                    primary_chapters=pool,
                    args=args,
                    make_request=make_request,
                    record_rate_limit=_record_rate_limit,
                    on_status=lambda m: print(m, file=sys.stderr),
                )
            if _ms_alts:
                _multi_source_alternatives = _ms_alts
                n_alts = sum(len(v) for v in _multi_source_alternatives.values())
                n_chapters_with_alts = len(_multi_source_alternatives)
                print(
                    f"[*] Multi-source ON: {n_chapters_with_alts} chapters have "
                    f"alternative sources ({n_alts} total fallback paths)",
                    file=sys.stderr,
                )
        except Exception as exc:
            # Don't let alternatives discovery block the main download. If it
            # fails, the user gets standard single-source behavior.
            print(
                f"[!] Multi-source alternatives discovery failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    # ── --list-chapters: print metadata + chapter list as JSON, then exit ──
    # Used by the UI to check for new chapters without downloading anything.
    # Only needs the page HTML + chapter list API call — no image VRF, no downloads.
    # IMPORTANT: This runs BEFORE allocate_series_output_dir so it doesn't
    # create empty folders in mangas/ just for checking.
    if getattr(args, "list_chapters", False):
        # Deduplicate chapter numbers (pool may have multiple versions per chapter)
        seen_nums = set()
        unique_chapters = []
        for ch in pool:
            num = ch.get("chap")
            if num is not None and num not in seen_nums:
                seen_nums.add(num)
                unique_chapters.append(num)
        # Sort numerically
        try:
            unique_chapters.sort(key=lambda x: float(x))
        except (ValueError, TypeError):
            pass

        result = {
            "hid": hid,
            "title": title,
            "url": args.comic_url,
            "site": handler.name,
            "status": comic_data.get("status"),
            "authors": comic_data.get("authors", []),
            "cover": comic_data.get("cover"),
            "genres": comic_data.get("genres", []),
            "total": len(unique_chapters),
            "chapters": unique_chapters,
        }
        print(json.dumps(result))
        sys.exit(0)

    # Output goes into a per-title folder under ./mangas (title, with hid only on collision)
    out_dir = allocate_series_output_dir(title, hid, root="mangas")
    setattr(args, "output_dir", out_dir)

    # --- Chapter Selection Logic ---
    log_verbose("Filtering chapters based on preferences...")

    # 1. Group all available chapter versions by chapter number
    chapters_by_num = {}
    for ch in pool:
        num_str = ch.get("chap")
        if num_str is None:
            continue

        # Coerce to string upfront — handlers are inconsistent: most produce
        # str ("4", "4.5") but mangathemesia (and any subclass like rizzcomic
        # registered via mangathemesia_sites.py) emits float. Without this
        # normalization, the .lower() oneshot check below crashes on float
        # ('float' has no .lower()), and dict-bucketing under both "4" and 4.0
        # would split a single chapter into two buckets when handlers mix.
        # `:g` formats 4.0 as "4" and 4.5 as "4.5", matching str-producers.
        if isinstance(num_str, (int, float)):
            num_str = f"{num_str:g}"
        else:
            num_str = str(num_str)

        # Treat "Oneshot" as Chapter 1
        if num_str and num_str.lower() in ("oneshot", "one-shot"):
            num_str = "1"

        try:
            float(num_str)
            if num_str not in chapters_by_num:
                chapters_by_num[num_str] = []
            chapters_by_num[num_str].append(ch)
        except (ValueError, TypeError):
            log_verbose(f"  Skipping chapter with invalid number: {num_str}")
            continue

    # 2. For each chapter number, select the best version
    best_chapters = []
    sorted_chap_nums = sorted(chapters_by_num.keys(), key=float)
    for num in sorted_chap_nums:
        versions = chapters_by_num[num]
        best_version = handler.select_best_chapter_version(
            versions, args.group, args.mix_by_upvote, log_debug_fn=log_debug
        )
        if best_version:
            best_chapters.append(best_version)

    # 3. Apply filters to the final list
    chapters = best_chapters

    # ──────────────────────────────────────────────────────────────────
    # Phase 8 (2026-05-08): apply collapse-splits grouping BEFORE --chapters
    # / --no-partials filters so user-facing chapter numbers correspond to
    # post-collapse labels. Without this, `--chapters 1` against a source
    # that delivers {1.1, 1.2, 1.3, 1.4} (no integer 1) would filter to
    # empty BEFORE the cluster could collapse into a "Ch 1" group.
    #
    # See sites/chapter_merger.py:group_chapters_for_download for the full
    # 6-rule cluster table:
    #   - Rule 5: sequential X.1/X.2/.../X.n cluster → combined Ch X
    #   - Rule 3: integer X + splits → keep only X
    #   - Rule 6: scattered decimals → keep one labeled X
    #   - Rules 1/2/4: integers and true partials preserved
    #
    # Multi-part groups (rule 5 only) get a synthesized chapter dict
    # carrying `_merged_parts`; _process_chapter_impl detects this and
    # fetches each part's images in order. Output filename uses
    # group.label so the user sees "Title Ch 1.pdf".
    collapse_splits_enabled = bool(getattr(args, "collapse_splits", True))
    groups = group_chapters_for_download(chapters, collapse_splits=collapse_splits_enabled)
    grouped_chapters: List[Dict[str, Any]] = []
    for group in groups:
        if len(group.parts) == 1:
            ch = group.parts[0]
            # Override chap label only when the group label differs from the
            # part's original (rule 6 case: scattered decimals labeled as
            # the integer floor for filename consistency). Other rules where
            # len(parts)==1 already match by construction (rules 1, 2, 4).
            if str(ch.get("chap")) != group.label:
                ch = {**ch, "chap": group.label}
            grouped_chapters.append(ch)
        else:
            # Rule 5: combined cluster. Synthesize a chapter dict from
            # parts[0]'s metadata (scanlator/group/upload date all carry
            # over) but with chap=group.label and _merged_parts set so
            # _process_chapter_impl pre-fetches every part's image stream.
            grouped_chapters.append({
                **group.parts[0],
                "chap": group.label,
                "_merged_parts": group.parts,
            })
    if collapse_splits_enabled and len(grouped_chapters) != len(chapters):
        log_verbose(
            f"  collapse-splits: {len(chapters)} entries → {len(grouped_chapters)} groups"
        )
    chapters = grouped_chapters
    # ──────────────────────────────────────────────────────────────────

    if args.no_partials:
        original_count = len(chapters)
        chapters = [
            c
            for c in chapters
            if float(c["chap"]) == int(float(c["chap"]))
        ]
        log_verbose(
            f"  --no-partials: Filtered out {original_count - len(chapters)} partial chapters."
        )

    if args.chapters.lower() != "all":
        # Check for negative indexing (e.g. "-1" for last chapter, "-3" for last 3)
        is_negative_index = False
        try:
            if args.chapters.strip().startswith("-") and "," not in args.chapters:
                # Check if it's a valid integer (e.g. -1, -5)
                # Note: This might conflict with actual negative chapter numbers (e.g. -12),
                # but those are rare. We prioritize the "last N" semantics here.
                val = int(args.chapters)
                if val < 0:
                    chapters = chapters[val:]
                    is_negative_index = True
                    log_verbose(
                        f"  --chapters '{args.chapters}': Interpreted as last {-val} chapters. Selected {len(chapters)} chapters."
                    )
        except ValueError:
            pass

        if not is_negative_index:
            chapters = [
                c
                for c in chapters
                if is_chapter_wanted(float(c["chap"]), args.chapters)
            ]
            log_verbose(
                f"  --chapters '{args.chapters}': Filtered list down to {len(chapters)} chapters."
            )

    if not chapters:
        sys.exit("No chapters selected.")
    # Always print the final chapter count so the UI can show a progress bar.
    # This is parsed by the Electron app to determine the total for the
    # "Chapter X/Y" progress indicator (regex: /Selected \d+ chapters/).
    print(f"  Selected {len(chapters)} chapters.")
    # --- End of Chapter Selection Logic ---

    # Ensure output folder exists (shared by chapter files, final book, and split parts)
    out_dir = getattr(args, "output_dir", "mangas")
    os.makedirs(out_dir, exist_ok=True)

    resume_mode = False
    params_path = os.path.join(main_tmp_dir, "run_params.json")
    current_processing = get_processing_params(args, width, aspect_ratio_str)
    current_behavior = get_behavior_params(args)
    # The full params dict: processing params + behavior params.
    # Processing params are used for the resume match check (if they differ,
    # the images on disk are incompatible and we must start fresh).
    # Behavior params are saved alongside so --restore-parameters can
    # restore the full set of flags (keep_chapters, no_final_file, etc.).
    current_params = {**current_processing, **current_behavior}

    if os.path.isdir(main_tmp_dir):
        print("Temporary directory found. Checking for resume compatibility...")
        if os.path.exists(params_path):
            try:
                with open(params_path, "r") as f:
                    old_params = json.load(f)
                # Only compare processing-relevant keys for resume compatibility.
                # Behavior params (keep_chapters, no_final_file, etc.) can change
                # without invalidating the downloaded images.
                old_processing = {k: old_params.get(k) for k in current_processing}
                if old_processing == current_processing:
                    print("  Parameters match. Resuming download.")
                    resume_mode = True
                else:
                    print(
                        "  Mismatched parameters. Cleaning up and starting fresh."
                    )
                    rm_tree(main_tmp_dir)
            except (json.JSONDecodeError, TypeError):
                print(
                    "  Could not read parameters file. Cleaning up and starting fresh."
                )
                rm_tree(main_tmp_dir)
        else:
            print(
                "  No parameters file found. Cleaning up and starting fresh."
            )
            rm_tree(main_tmp_dir)

    if not resume_mode:
        os.makedirs(main_tmp_dir, exist_ok=True)
        with open(params_path, "w") as f:
            json.dump(current_params, f, indent=4)

    # Save UI metadata (URL, format, title) separately from processing params.
    # This file is read by the Electron UI to auto-fill the URL when resuming,
    # so you don't have to re-enter it manually.
    # Written on EVERY run (not just new ones) to keep the URL current.
    meta_path = os.path.join(main_tmp_dir, "run_meta.json")
    try:
        os.makedirs(main_tmp_dir, exist_ok=True)
        meta_data = {
            "url": args.comic_url,
            "format": args.format,
            "title": title,
            "hid": hid,
        }
        with open(meta_path, "w") as f:
            json.dump(meta_data, f, indent=4)
    except OSError:
        pass  # Non-critical — resume still works via history lookup

    current_book_content = []
    current_book_chapters = []
    current_book_scan_groups = set()
    current_book_size = 0
    current_epub_markers = []

    original_cover_path = None
    if args.format in ["epub", "cbz"]:
        # Prefer handler's extracted cover first (more reliable, can be customized per-site)
        # Fall back to og:image only if handler didn't provide one
        cover_url = comic_data.get("cover") or comic_data.get("thumb")
        if not cover_url and context.soup:
            cover_tag = context.soup.find("meta", property="og:image")
            if cover_tag and cover_tag.get("content"):
                cover_url = cover_tag["content"]
        if cover_url:
            original_cover_path = dl_image(
                cover_url, main_tmp_dir, "cover_orig.jpg", scraper, cleanup=not args.no_cleanup
            )
            if args.format == "cbz" and original_cover_path:
                current_book_content.append(
                    {"type": "image", "path": original_cover_path}
                )
                current_book_size += os.path.getsize(original_cover_path)

    # --- Missed chapter logging + end-of-run retries ---
    retry_missed = not getattr(args, 'no_retry_missed_chapters', False)
    missed_retries = max(0, int(getattr(args, 'missed_retries', 2) or 0))
    missed_log_path = getattr(args, 'missed_log', None) or os.path.join(main_tmp_dir, 'missed_chapters.json')
    missed_entries: List[Dict[str, Any]] = []

    if retry_missed and missed_retries > 0 and (split_size_bytes > 0 or split_chapter_count > 0):
        print('[*] Note: --split is disabled while missed-chapter retry is enabled (to keep output ordering correct).')
        split_size_bytes = 0
        split_chapter_count = 0

    def _chapter_key(ch: Dict[str, Any]) -> str:
        v = ch.get('id') or ch.get('chapter_id') or ch.get('url') or ch.get('chap')
        return str(v)

    def _load_missed() -> List[Dict[str, Any]]:
        try:
            if os.path.exists(missed_log_path):
                with open(missed_log_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        return []

    def _save_missed(entries: List[Dict[str, Any]]) -> None:
        try:
            os.makedirs(os.path.dirname(missed_log_path) or '.', exist_ok=True)
            with open(missed_log_path, 'w', encoding='utf-8') as f:
                json.dump(entries, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _record_missed(ch: Dict[str, Any], grp_name: str, reason: str, err: str, *, insert_list_index: int, insert_chapter_index: int, insert_marker_index: int, insert_page_index: int, host: str = "", pages_ok: int = 0, pages_total: int = 0) -> None:
        entry = {
            'key': _chapter_key(ch),
            'ch': ch,
            'chap': ch.get('chap'),
            'url': ch.get('url'),
            'group': grp_name,
            'reason': reason,
            'error': (str(err) if err else '')[:500],
            # Diagnostic fields used by the end-of-run timing summary so the user
            # can see *which* host caused which chapter's failure. Backwards
            # compatible: older tools that read missed_chapters.json ignore
            # unknown fields. Default to empty/0 for entries from places that
            # don't have the data (e.g. exception path before the watchdog ran).
            'host': str(host or ''),
            'pages_ok': int(pages_ok or 0),
            'pages_total': int(pages_total or 0),
            'insert_list_index': int(insert_list_index),
            'insert_chapter_index': int(insert_chapter_index),
            'insert_marker_index': int(insert_marker_index),
            'insert_page_index': int(insert_page_index),
        }
        missed_entries.append(entry)
        _save_missed(missed_entries)

    def _process_chapter_impl(ch: Dict[str, Any], *, force_redownload: bool = False, next_chapter: Optional[Dict[str, Any]] = None, is_alt_source: bool = False):
        # Implementation body. _process_chapter() (defined below) wraps this with
        # the per-chapter watchdog timer + host-failure reset so we can fast-fail
        # a chapter if the source CDN goes flaky. ChapterSkippedError raised in
        # here propagates up to the chapter loop in main(), which records it via
        # _record_missed and continues.
        #
        # is_alt_source: True when _process_chapter_strict has rebound
        # handler/scraper to an alternative source (Phase 4b multi-source
        # fallback). When set, the inter-chapter image prefetch (Phase G7)
        # is suppressed because next_chapter is from the PRIMARY source's
        # chapter list — alt_handler.get_chapter_images(primary_chapter)
        # would silently fail and waste a worker pool. The prefetch only
        # makes sense on the primary path; subsequent chapters resume on
        # the primary (strict wrapper restores it via finally).
        n = ch["chap"]
        grp_name = handler.get_group_name(ch)
        tdir = os.path.join(main_tmp_dir, f"ch_{n}")
        processed_tdir = os.path.join(tdir, "processed")
        chapter_content = []
        chapter_content_size = 0
        process_this_chapter = True

        # Use a different marker when skipping processing
        marker_name = (
            ".download_complete" if args.no_processing else ".processed_complete"
        )
        marker_path = os.path.join(tdir, marker_name)

        if force_redownload:
            # Force a clean re-download/re-process for this chapter (used by end-of-run retries)
            if os.path.isdir(tdir):
                rm_tree(tdir)
            process_this_chapter = True


        if resume_mode and os.path.exists(marker_path):
            print(f"\nChapter {n} (already processed, collecting files)")
            if args.format in {"epub", "none"}:
                log_verbose(
                    "  Resume mode not supported for this format; re-processing."
                )
                rm_tree(tdir)
            elif args.format == "pdf":
                cached_pdf_path = os.path.join(processed_tdir, f"{n}.pdf")
                if os.path.exists(cached_pdf_path) and os.path.getsize(cached_pdf_path) > 0:
                    # Fast resume: the chapter was already fully processed; just reuse the cached PDF.
                    process_this_chapter = False
                    chapter_content = [{"type": "pdf", "path": cached_pdf_path}]
                    chapter_content_size = os.path.getsize(cached_pdf_path)
                else:
                    log_verbose(
                        f"  Resume marker found but cached PDF missing for Ch {n}; re-processing."
                    )
                    rm_tree(tdir)
            elif args.format == "cbz":
                # Phase D (2026-05-07): mirror PDF's cached-output resume.
                # If processed_tdir/{n}.cbz exists from a prior run, just
                # surface a cbz_cache reference and skip rebuild. The
                # final-assembly wrapper member-copies its entries.
                cached_cbz_path = os.path.join(processed_tdir, f"{n}.cbz")
                if os.path.exists(cached_cbz_path) and os.path.getsize(cached_cbz_path) > 0:
                    process_this_chapter = False
                    chapter_content = [{"type": "cbz_cache", "path": cached_cbz_path}]
                    chapter_content_size = os.path.getsize(cached_cbz_path)
                else:
                    # No cached archive — fall back to the broadened image
                    # globs (legacy resume path, e.g. for users upgrading
                    # from a pre-Phase-D run).
                    _IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif")
                    if args.no_processing:
                        raw_images = []
                        for ext in _IMG_EXTS:
                            raw_images.extend(
                                glob.glob(os.path.join(tdir, f"{n}_*{ext}"))
                            )
                        try:
                            source_images = sorted(
                                raw_images,
                                key=lambda p: int(
                                    os.path.splitext(os.path.basename(p))[0]
                                    .split("_")[-1]
                                ),
                            )
                        except Exception:
                            source_images = sorted(raw_images)
                    else:
                        processed_images = []
                        for ext in _IMG_EXTS:
                            processed_images.extend(
                                glob.glob(os.path.join(processed_tdir, f"*{ext}"))
                            )
                        source_images = sorted(processed_images)

                    if not source_images:
                        log_verbose(
                            f"  Warning: Found process marker for Ch {n} but no images. Re-processing."
                        )
                        rm_tree(tdir)
                    else:
                        process_this_chapter = False
                        chapter_content = [
                            {"type": "image", "path": p} for p in source_images
                        ]
                        chapter_content_size = sum(
                            os.path.getsize(p) for p in source_images
                        )
            else:
                # Phase A (2026-05-07): downloads now land with their actual
                # extensions (.webp/.png/.avif/.gif), so resume globs can no
                # longer assume `.jpg`. This branch covers any non-pdf,
                # non-cbz format that still uses the image-glob resume path
                # (kept for back-compat with formats added later).
                _IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif")
                if args.no_processing:
                    raw_images = []
                    for ext in _IMG_EXTS:
                        raw_images.extend(
                            glob.glob(os.path.join(tdir, f"{n}_*{ext}"))
                        )
                    try:
                        source_images = sorted(
                            raw_images,
                            key=lambda p: int(
                                os.path.splitext(os.path.basename(p))[0]
                                .split("_")[-1]
                            ),
                        )
                    except Exception:
                        source_images = sorted(raw_images)
                else:
                    processed_images = []
                    for ext in _IMG_EXTS:
                        processed_images.extend(
                            glob.glob(os.path.join(processed_tdir, f"*{ext}"))
                        )
                    source_images = sorted(processed_images)

                if not source_images:
                    log_verbose(
                        f"  Warning: Found process marker for Ch {n} but no images. Re-processing."
                    )
                    rm_tree(tdir)
                    # process_this_chapter remains True
                else:
                    process_this_chapter = False
                    chapter_content = [
                        {"type": "image", "path": p} for p in source_images
                    ]
                    chapter_content_size = sum(
                        os.path.getsize(p) for p in source_images
                    )

        if process_this_chapter:
            # Phase G7 (2026-05-08): consume any in-flight prefetch for this
            # chapter before deciding whether to wipe tdir. The prefetch may
            # have already downloaded everything into tdir + written
            # `.download_prefetched`. Without the join we'd race the wipe
            # against the prefetch worker writing files.
            _consume_image_prefetch(n)
            prefetch_marker_path = os.path.join(tdir, ".download_prefetched")
            if force_redownload:
                # Inline retry / explicit redo: discard whatever was there,
                # including any prefetched bytes (the same sources may have
                # been the failure root cause).
                prefetch_hit = False
                if os.path.isdir(tdir):
                    rm_tree(tdir)
            else:
                prefetch_hit = os.path.exists(prefetch_marker_path)
                if prefetch_hit:
                    log_verbose(f"  [Img Prefetch] Using prefetched downloads for Ch {n}")
                elif os.path.isdir(tdir):
                    log_verbose(
                        f"  Found incomplete temporary directory for Ch {n}. Cleaning before re-download."
                    )
                    rm_tree(tdir)

            print(f"\nChapter {n} ({grp_name or 'No Group'})")
            _t0_vrf = time.monotonic()
            # Phase 8 (2026-05-08): split-cluster collapse — when this chapter
            # was synthesized by group_chapters_for_download from multiple
            # parts (rule 5: X.1/X.2/X.3/X.4 with no integer X), `_merged_parts`
            # carries the original chapter dicts. Fetch each part's media
            # entries in order and concatenate, so downstream processing sees
            # ONE long chapter with all parts' pages stitched together. The
            # chap label was already replaced with group.label at synthesis
            # time, so tdir / output filename use the floor (e.g., "1") not
            # any individual part's label.
            merged_parts = ch.get("_merged_parts")
            if merged_parts:
                media_entries = []
                for part_idx, part in enumerate(merged_parts):
                    part_label = part.get("chap")
                    log_verbose(
                        f"  [collapse-splits] part {part_idx + 1}/{len(merged_parts)} (chap {part_label})"
                    )
                    try:
                        part_entries = handler.get_chapter_images(
                            part, scraper, make_request
                        ) or []
                    except IncompleteChapterError as ice:
                        # Handler did its own retries and still couldn't get all
                        # pages — convert to ChapterSkippedError so the strict
                        # wrapper's alt-source fallback / inline retry path
                        # picks it up the same as a Phase-2 download failure.
                        if os.path.isdir(tdir):
                            rm_tree(tdir)
                        raise ChapterSkippedError(
                            reason=ice.reason,
                            host=ice.host,
                            pages_ok=ice.pages_ok,
                            pages_total=ice.pages_total,
                        ) from ice
                    except Exception as exc:
                        # Re-raise as ChapterSkippedError so the strict wrapper
                        # treats this as a normal chapter failure (alt-source
                        # fallback / inline retry / hard abort if exhausted).
                        # Without this, a transient get_chapter_images error
                        # on one part would hard-fail the whole combined
                        # chapter outside the strict-wrapper retry envelope.
                        raise ChapterSkippedError(
                            reason=f"merged_part_fetch_failed:{type(exc).__name__}",
                            host="",
                            pages_ok=0,
                            pages_total=0,
                        ) from exc
                    media_entries.extend(part_entries)
            else:
                try:
                    media_entries = handler.get_chapter_images(
                        ch, scraper, make_request
                    ) or []
                except IncompleteChapterError as ice:
                    # Handler exhausted its own retry policy without getting
                    # all pages — same conversion as the merged-parts branch
                    # above. Wipes tdir so any partial state doesn't get
                    # picked up by the next attempt's resume check.
                    if os.path.isdir(tdir):
                        rm_tree(tdir)
                    raise ChapterSkippedError(
                        reason=ice.reason,
                        host=ice.host,
                        pages_ok=ice.pages_ok,
                        pages_total=ice.pages_total,
                    ) from ice
            _timing["vrf"] += time.monotonic() - _t0_vrf

            # --- VRF pipelining: start capturing the next chapter's VRF
            #     while this chapter's images download in parallel. ---
            _start_vrf_prefetch(next_chapter, handler)
            raw_image_paths: List[str] = []
            text_blocks: List[Dict[str, Any]] = []
            page_counter = 1
            log_verbose(
                f"  Fetching {len(media_entries)} media item(s)..."
            )

            # -----------------------------------------------------------
            # Phase 1: Scan entries – handle text/binary immediately,
            #          queue URL-based images for parallel download.
            # -----------------------------------------------------------
            # Create download folder once upfront so parallel dl_image
            # calls don't all race through os.makedirs().
            os.makedirs(tdir, exist_ok=True)
            # Each item in download_tasks is (page_index, url, folder, filename)
            # page_index lets us put results back in the right order later.
            download_tasks: List[Tuple[int, str, str, str]] = []
            # immediate_images stores (page_index, path) for binary/data entries
            immediate_images: List[Tuple[int, str]] = []

            for entry in media_entries:
                if isinstance(entry, dict):
                    entry_type = entry.get("type")
                    if entry_type == "text":
                        paragraphs = entry.get("paragraphs", [])
                        title_text = entry.get("title") or ch.get("title")
                        if paragraphs or title_text:
                            text_blocks.append(
                                {
                                    "paragraphs": paragraphs,
                                    "title": title_text,
                                }
                            )
                        continue
                    if entry_type == "binary_image":
                        blob = entry.get("data")
                        if not blob:
                            continue
                        # Phase A (2026-05-07): if the handler provided an
                        # explicit extension, trust it. Otherwise sniff from
                        # blob magic + the optional content-type hint, falling
                        # back to .jpg only when nothing matches. Same logic
                        # as dl_image so binary_image entries don't bypass the
                        # CBZ byte-preservation guarantee.
                        explicit_ext = entry.get("extension")
                        if explicit_ext:
                            ext = explicit_ext if explicit_ext.startswith(".") else "." + explicit_ext
                        else:
                            ext = _sniff_image_extension(
                                blob[:32] if isinstance(blob, (bytes, bytearray)) else b"",
                                entry.get("content_type"),
                            )
                        custom_name = entry.get("name")
                        filename = (
                            custom_name
                            if custom_name
                            else f"{n}_{page_counter:04d}{ext}"
                        )
                        pth = os.path.join(tdir, filename)
                        with open(pth, "wb") as fh:
                            fh.write(blob)
                        immediate_images.append((page_counter, pth))
                        page_counter += 1
                        continue

                full_url = entry if isinstance(entry, str) else entry.get("url")
                if not full_url:
                    continue
                filename = f"{n}_{page_counter:04d}.jpg"
                download_tasks.append((page_counter, full_url, tdir, filename))
                page_counter += 1

            # -----------------------------------------------------------
            # Phase 2: Download all URL-based images in parallel.
            # -----------------------------------------------------------
            # Pre-create the download folder so parallel workers don't
            # race on os.makedirs for the same directory (avoids Windows race).
            os.makedirs(tdir, exist_ok=True)
            # downloaded_images stores (page_index, path_or_None) results
            downloaded_images: List[Tuple[int, Optional[str]]] = []
            image_workers = max(1, getattr(args, "image_workers", 3))

            _t0_dl = time.monotonic()
            if download_tasks:
                if prefetch_hit:
                    # Files already on disk from the inter-chapter prefetch;
                    # resolve actual filenames since dl_image's Phase A sniff
                    # may have rewritten the extension (e.g. placeholder
                    # "{n}_0001.jpg" → "{n}_0001.webp" after the bytes landed).
                    log_verbose(
                        f"  Using prefetched files for {len(download_tasks)} image(s)..."
                    )
                    try:
                        existing = os.listdir(download_tasks[0][2])
                    except OSError:
                        existing = []
                    for task_page_idx, task_url, task_folder, task_filename in download_tasks:
                        base, _ = os.path.splitext(task_filename)
                        prefix = base + "."
                        # Pick the matching real file (skip hidden markers
                        # like .download_prefetched / .pending_*).
                        match = next(
                            (
                                os.path.join(task_folder, fn)
                                for fn in existing
                                if fn.startswith(prefix)
                                and not fn.startswith(".")
                            ),
                            None,
                        )
                        downloaded_images.append((task_page_idx, match))
                elif image_workers > 1 and len(download_tasks) > 1:
                    log_verbose(
                        f"  Downloading {len(download_tasks)} image(s) with {min(image_workers, len(download_tasks))} parallel workers..."
                    )
                    with ThreadPoolExecutor(max_workers=min(image_workers, len(download_tasks))) as img_pool:
                        future_to_page = {
                            img_pool.submit(
                                dl_image,
                                task_url,
                                task_folder,
                                task_filename,
                                scraper,
                                not args.no_cleanup,
                            ): task_page_idx
                            for task_page_idx, task_url, task_folder, task_filename in download_tasks
                        }
                        for future in as_completed(future_to_page):
                            pg_idx = future_to_page[future]
                            try:
                                result_path = future.result()
                            except Exception as e:
                                log_verbose(f"  Warning: Image page {pg_idx} raised exception: {e}")
                                result_path = None
                            downloaded_images.append((pg_idx, result_path))
                else:
                    # Sequential fallback (image_workers=1 or only 1 image)
                    for task_page_idx, task_url, task_folder, task_filename in download_tasks:
                        result_path = dl_image(
                            task_url,
                            task_folder,
                            task_filename,
                            scraper,
                            cleanup=not args.no_cleanup,
                        )
                        downloaded_images.append((task_page_idx, result_path))

            # -----------------------------------------------------------
            # Phase 3: Merge results back in page order.
            # -----------------------------------------------------------
            all_images = immediate_images + [
                (pg, p) for pg, p in downloaded_images if p
            ]
            all_images.sort(key=lambda x: x[0])
            raw_image_paths = [p for _, p in all_images]
            _timing["download"] += time.monotonic() - _t0_dl

            # ── Per-chapter zero-tolerance check ──
            # After Phase 2 (downloads), the chapter is treated as failed if
            # ANY page is missing. The strict wrapper (_process_chapter_strict)
            # catches the resulting ChapterSkippedError and performs an inline
            # retry — clean restart of the chapter after a long backoff to let
            # a flaky CDN recover. If the inline retries also can't get every
            # page, the wrapper raises ChapterAbortedError → run stops.
            #
            # We never produce partial chapter PDFs. That was the bug the user
            # hit on Record of Ragnarok: 4-of-65 pages failed but a 61-page PDF
            # was saved with silent gaps. Now: chapter is all-or-nothing.
            #
            # Reason precedence: 'incomplete' < 'time_budget' < 'host_poison'.
            # The most informative reason takes priority for the diagnostic
            # log line and the timing summary block.
            pages_total = len(download_tasks) + len(immediate_images)
            pages_ok = sum(1 for _, p in downloaded_images if p) + len(immediate_images)
            poison_threshold = int(globals().get("_CHAPTER_HOST_POISON", 5))
            poisoned_hosts: List[str] = []
            if poison_threshold > 0:
                with _HOST_FAIL_LOCK:
                    poisoned_hosts = [h for h, c in _HOST_FAIL_COUNT.items() if c >= poison_threshold]
            deadline_hit = _chapter_cancelled()
            incomplete = (pages_total > 0 and pages_ok < pages_total)
            if incomplete or deadline_hit or poisoned_hosts:
                # host_blame fallback chain. download_tasks is empty for
                # handlers that return all binary_image entries (e.g.
                # MangaDex's resilient pipeline that pre-fetches blobs at
                # the handler level). Fall back to the first media_entries
                # URL, then the chapter's source URL, so the diagnostic
                # log line always shows a concrete host instead of '-'.
                def _resolve_host_blame() -> str:
                    if download_tasks:
                        try:
                            return urlparse(download_tasks[0][1]).netloc
                        except Exception:
                            pass
                    if media_entries:
                        first = media_entries[0]
                        if isinstance(first, dict):
                            url = first.get("url")
                            if url:
                                try:
                                    return urlparse(url).netloc
                                except Exception:
                                    pass
                        elif isinstance(first, str):
                            try:
                                return urlparse(first).netloc
                            except Exception:
                                pass
                    chap_url = ch.get("url")
                    if chap_url:
                        try:
                            return urlparse(str(chap_url)).netloc
                        except Exception:
                            pass
                    return ""
                if poisoned_hosts:
                    reason = "host_poison"
                    host_blame = poisoned_hosts[0]
                elif deadline_hit:
                    reason = "time_budget"
                    host_blame = _resolve_host_blame()
                else:
                    reason = "incomplete"
                    host_blame = _resolve_host_blame()
                print(
                    f"  [!] Chapter {n} incomplete: {pages_ok}/{pages_total} pages "
                    f"(reason={reason}, host={host_blame or '-'}). Will inline-retry."
                )
                # Wipe partial chapter dir so the inline retry starts fresh.
                rm_tree(tdir)
                raise ChapterSkippedError(
                    reason=reason,
                    host=host_blame,
                    pages_ok=pages_ok,
                    pages_total=pages_total,
                )

            if not raw_image_paths and not text_blocks:
                print(
                    f"  Warning: No media downloaded for Chapter {n}. Skipping."
                )
                return None, grp_name, n, 0

            if args.keep_images and raw_image_paths:
                # When --keep-images is enabled, keep raw downloads inside the manga's
                # output folder to avoid mixing different series in the same directory.
                dest_dir = os.path.join(out_dir, "images", f"Chapter_{n}")
                log_verbose(f"  Copying original images to: {dest_dir}")
                # Python 3.7 doesn't support dirs_exist_ok. Fallback if needed.
                try:
                    shutil.copytree(tdir, dest_dir, dirs_exist_ok=True)
                except TypeError:
                    if os.path.exists(dest_dir):
                        # Emulate dirs_exist_ok=True
                        for root, dirs, files in os.walk(tdir):
                            rel = os.path.relpath(root, tdir)
                            target = (
                                os.path.join(dest_dir, rel)
                                if rel != "."
                                else dest_dir
                            )
                            os.makedirs(target, exist_ok=True)
                            for fname in files:
                                shutil.copy2(
                                    os.path.join(root, fname),
                                    os.path.join(target, fname),
                                )
                    else:
                        shutil.copytree(tdir, dest_dir)

            _t0_proc = time.monotonic()
            os.makedirs(processed_tdir, exist_ok=True)

            # Phase G7 (2026-05-08): kick off image prefetch for next_chapter
            # NOW — after this chapter's downloads + validation succeeded,
            # before the CPU-bound processing/encoding begins. While the
            # main thread is decoding/scaling/saving this chapter's images,
            # the prefetch worker downloads next_chapter's images in
            # parallel. _process_chapter_impl's next iteration consumes
            # the prefetch via the .download_prefetched marker.
            #
            # Worker count: --prefetch-image-workers (default -1 = match
            # --image-workers). 0 disables. Positive N = exact count
            # regardless of main pool size, useful when the user wants
            # fewer concurrent connections during prefetch than during
            # the in-band download (e.g. main=12 prefetch=4 to avoid
            # CDN-throttle compounding without giving up the overlap).
            #
            # Skipped on:
            #   - prefetch_image_workers <= 0 (user opt-out)
            #   - force_redownload=True (inline retry — don't fire side
            #     work that the retry path will also fire)
            #   - next_chapter is None (last chapter in the run)
            #   - next_chapter is already fully processed (resume case;
            #     prefetching a cached chapter would just download bytes
            #     into a tdir whose `.processed_complete` marker already
            #     short-circuits the next iteration)
            prefetch_workers_raw = getattr(args, "prefetch_image_workers", -1)
            if prefetch_workers_raw is None:
                prefetch_workers_raw = -1
            if prefetch_workers_raw < 0:
                effective_prefetch_workers = image_workers
            else:
                effective_prefetch_workers = int(prefetch_workers_raw)
            if (
                effective_prefetch_workers > 0
                and next_chapter is not None
                and not force_redownload
                and not is_alt_source
            ):
                next_n = next_chapter.get("chap")
                if next_n is not None:
                    next_tdir = os.path.join(main_tmp_dir, f"ch_{next_n}")
                    next_marker_name = (
                        ".download_complete" if args.no_processing else ".processed_complete"
                    )
                    next_already_cached = os.path.exists(
                        os.path.join(next_tdir, next_marker_name)
                    )
                    if not next_already_cached:
                        _start_image_prefetch(
                            next_chapter, next_tdir, scraper, handler,
                            effective_prefetch_workers,
                        )

            chapter_content = []

            processed_page_images: List[str] = []
            # For PDF format, track which images were NOT modified during
            # processing so _build_images_pdf can embed the original
            # download bytes (smaller and better quality than re-encoding).
            _pdf_source_paths: Optional[List[Optional[str]]] = None

            if raw_image_paths:
                # Phase B (2026-05-07): CBZ fast-path. When the user is on
                # default --scaling 100 with no width/aspect/quality override,
                # we put the original wire bytes straight into the archive —
                # no PIL decode, no recombine, no JPEG re-encode. The archive
                # layer (build_cbz / build_cbz_from_content) preserves
                # per-file extensions on the arcname, so .webp downloads stay
                # .webp, .png stay .png, etc. Phase A made raw_image_paths
                # land with correct extensions which is what makes this work.
                cbz_fast_path = (
                    args.format == "cbz"
                    and not args.no_processing
                    and not getattr(args, "no_cbz_preserve_originals", False)
                    and scale_factor == 1.0
                    and not getattr(args, "_user_set_width", False)
                    and not getattr(args, "_user_set_aspect_ratio", False)
                    and not getattr(args, "_user_set_quality", False)
                )
                if cbz_fast_path:
                    processed_page_images = list(raw_image_paths)
                    log_verbose(
                        f"  CBZ fast-path: preserving original bytes for "
                        f"{len(raw_image_paths)} pages"
                    )
                elif args.no_processing:
                    processed_page_images = list(raw_image_paths)
                    # --no-processing: every image is the original download.
                    if args.format == "pdf":
                        _pdf_source_paths = list(raw_image_paths)
                else:
                    log_verbose(
                        f"  Processing {len(raw_image_paths)} downloaded images..."
                    )
                    if args.format == "cbz" or (
                        args.format == "epub" and not text_blocks
                    ):
                        pages_in_memory = process_chapter_images(
                            raw_image_paths, width, recombine_target_height
                        )
                    elif args.format == "pdf":
                        # PDF MediaBox is per-page, so every image keeps its
                        # original dimensions — including double-page spreads,
                        # which display at full source resolution. --width is
                        # therefore ignored on the PDF path.
                        #
                        # _pdf_source_paths runs in parallel with pages_in_memory
                        # so _build_images_pdf can embed the original wire bytes
                        # via /DCTDecode (zero generation loss) instead of
                        # re-encoding from PIL pixels. The scale_factor != 1.0
                        # branch below nulls out source_paths if --scaling forces
                        # a resize, falling back to a quality-100 re-encode.
                        pages_in_memory = []
                        _pdf_source_paths = []
                        for path in raw_image_paths:
                            try:
                                im = Image.open(path)
                                if im.mode not in ("RGB", "L"):
                                    im = im.convert("RGB")
                                _pdf_source_paths.append(path)
                                pages_in_memory.append(im)
                            except Exception as e:
                                print(f"  Warning: Could not process image {path}: {e}")
                        log_verbose(f"  Loaded {len(pages_in_memory)} pages in memory.")
                    else:
                        pages_in_memory = resize_chapter_images(
                            raw_image_paths, width
                        )

                    log_verbose(f"  Applying {args.scaling}% scaling...")
                    if scale_factor == 1.0:
                        scaled_images_in_mem = pages_in_memory
                    else:
                        scaled_images_in_mem = []
                        with _cpu_guard("scale_images"):
                            for idx_img, img in enumerate(pages_in_memory):
                                if idx_img % 8 == 0:
                                    _hb("cpu", f"scaling {idx_img+1}/{len(pages_in_memory)}")
                                scaled_images_in_mem.append(
                                    img.resize(
                                        (
                                            int(img.width * scale_factor),
                                            int(img.height * scale_factor),
                                        ),
                                        Image.LANCZOS,
                                    )
                                )

                    # If scaling changed dimensions, original bytes no
                    # longer match → clear all source paths.
                    if scale_factor != 1.0 and _pdf_source_paths is not None:
                        _pdf_source_paths = [None] * len(_pdf_source_paths)

                    images_to_save = scaled_images_in_mem
                    if (
                        args.scaling < 100
                        and args.format in ["epub", "cbz"]
                        and recombine_target_height > 0
                    ):
                        images_to_save = recombine_scaled_images(
                            scaled_images_in_mem, recombine_target_height
                        )

                    # Phase C (2026-05-07): when the user did NOT explicitly
                    # set --quality, CBZ uplifts the default 85 → 95 so the
                    # legacy re-encode path doesn't silently degrade quality.
                    # PDF still forces 100 (its lossless-bytes path covers
                    # most cases anyway). User-set --quality always wins.
                    if args.format == "pdf":
                        _save_quality = 100
                    elif (
                        args.format == "cbz"
                        and not getattr(args, "_user_set_quality", False)
                    ):
                        _save_quality = 95
                    else:
                        _save_quality = args.quality

                    # Phase C: pick output format. PDF's _build_images_pdf
                    # consumes JPEG-quality re-encodes, so PDF stays "jpeg".
                    # CBZ asks for "auto" which maps each output to its
                    # source format (WebP→WebP-lossless, JPEG→JPEG q=95,
                    # else PNG). When recombination drew from multiple
                    # inputs (1:N mapping), source_paths is None and "auto"
                    # falls to PNG — but if every source was WebP we can
                    # safely keep the lossless promise via webp_lossless.
                    if args.format == "pdf":
                        _output_format = "jpeg"
                        _src_paths_for_save = None
                    elif args.format == "cbz":
                        if len(images_to_save) == len(raw_image_paths):
                            _output_format = "auto"
                            _src_paths_for_save = list(raw_image_paths)
                        elif raw_image_paths and all(
                            os.path.splitext(p)[1].lower() == ".webp"
                            for p in raw_image_paths
                        ):
                            _output_format = "webp_lossless"
                            _src_paths_for_save = None
                        else:
                            _output_format = "auto"
                            _src_paths_for_save = None
                    else:
                        # EPUB and "none" keep the legacy JPEG-only behavior.
                        _output_format = "jpeg"
                        _src_paths_for_save = None

                    # Phase G6 (2026-05-08): skip the save_final_images JPEG
                    # re-encode for the PDF byte-passthrough case. When
                    # --scaling is 100 and the per-page raw bytes are still
                    # available (_pdf_source_paths populated, no None entries),
                    # _build_images_pdf will read pixel dimensions directly
                    # from the source files and embed source bytes via
                    # /DCTDecode (zero generation loss). The legacy path
                    # would write a quality-100 JPEG re-encode for every page
                    # whose bytes are then ignored — pure waste at ~1-3s per
                    # 30-page chapter. processed_page_images becomes the same
                    # list as _pdf_source_paths so _build_images_pdf opens
                    # those files for both the dim check AND the source-bytes
                    # branch, hitting the (sw, sh) == (w, h) shortcut every
                    # iteration.
                    pdf_byte_passthrough = (
                        args.format == "pdf"
                        and scale_factor == 1.0
                        and _pdf_source_paths is not None
                        and len(_pdf_source_paths) == len(images_to_save)
                        and all(p is not None for p in _pdf_source_paths)
                    )
                    if pdf_byte_passthrough:
                        log_verbose(
                            f"  PDF byte-passthrough: skipping {len(images_to_save)} "
                            f"JPEG re-encodes (sources embed via /DCTDecode)"
                        )
                        processed_page_images = list(_pdf_source_paths)
                    else:
                        processed_page_images = save_final_images(
                            images_to_save,
                            processed_tdir,
                            f"p_{n}",
                            _save_quality,
                            output_format=_output_format,
                            source_paths=_src_paths_for_save,
                        )

            if args.format == "cbz":
                for idx, block in enumerate(text_blocks):
                    text_paths = render_text_to_images(
                        block["paragraphs"],
                        processed_tdir,
                        f"{n}_text_{idx:02d}",
                        title=block.get("title") or ch.get("title"),
                        start_index=len(processed_page_images) + 1,
                    )
                    processed_page_images.extend(text_paths)

                # Phase D (2026-05-07): build a per-chapter cached .cbz so
                # resume can short-circuit the rebuild AND so the final-
                # assembly wrapper can member-copy it (no decode, no re-zip)
                # into the series-wide archive. Mirrors PDF's
                # processed_tdir/{n}.pdf cache. Replaces chapter_content
                # with a single cbz_cache entry pointing at the new archive.
                if processed_page_images:
                    cached_cbz_path = os.path.join(processed_tdir, f"{n}.cbz")
                    with zipfile.ZipFile(
                        cached_cbz_path, "w", zipfile.ZIP_STORED
                    ) as zf:
                        for i, p in enumerate(processed_page_images):
                            zf.write(p, f"{i:04d}{os.path.splitext(p)[1]}")
                    chapter_content = [
                        {"type": "cbz_cache", "path": cached_cbz_path}
                    ]
                else:
                    chapter_content = []
            elif args.format == "epub":
                chapter_content = [
                    {"type": "image", "path": p} for p in processed_page_images
                ]
                for idx, block in enumerate(text_blocks):
                    xhtml_path = os.path.join(
                        processed_tdir, f"{n}_text_{idx:02d}.xhtml"
                    )
                    render_text_to_xhtml(
                        block["paragraphs"],
                        xhtml_path,
                        block.get("title") or ch.get("title"),
                        args.language,
                    )
                    chapter_content.append(
                        {
                            "type": "xhtml",
                            "path": xhtml_path,
                            "title": block.get("title"),
                        }
                    )
            elif args.format == "pdf":
                pdf_parts: List[str] = []
                if processed_page_images:
                    image_pdf_path = os.path.join(
                        processed_tdir, f"{n}_images.pdf"
                    )
                    _build_images_pdf(
                        processed_page_images,
                        image_pdf_path,
                        source_paths=_pdf_source_paths,
                    )
                    pdf_parts.append(image_pdf_path)
                for idx, block in enumerate(text_blocks):
                    pdf_path = os.path.join(
                        processed_tdir, f"{n}_text_{idx:02d}.pdf"
                    )
                    render_text_to_pdf(
                        block["paragraphs"],
                        pdf_path,
                        block.get("title") or ch.get("title"),
                    )
                    pdf_parts.append(pdf_path)

                if pdf_parts:
                    # Keep a canonical per-chapter PDF inside the chapter temp folder so resume can
                    # "collect" it instantly without re-processing.
                    cached_pdf_path = os.path.join(processed_tdir, f"{n}.pdf")

                    if len(pdf_parts) == 1:
                        src_pdf = pdf_parts[0]
                        if os.path.abspath(src_pdf) != os.path.abspath(cached_pdf_path):
                            try:
                                os.replace(src_pdf, cached_pdf_path)
                            except OSError:
                                shutil.copy2(src_pdf, cached_pdf_path)
                        final_pdf_path = cached_pdf_path
                    else:
                        final_pdf_path = cached_pdf_path
                        with _cpu_guard('merge_pdf'):
                            merge_pdf_files(pdf_parts, final_pdf_path, None)
                        # Remove intermediate PDF parts to save disk.
                        for part_path in pdf_parts:
                            try:
                                os.remove(part_path)
                            except OSError:
                                pass

                    chapter_content = [
                        {"type": "pdf", "path": final_pdf_path}
                    ]

            elif args.format == "none":
                if text_blocks:
                    combined_paragraphs: List[str] = []
                    for idx, block in enumerate(text_blocks):
                        if idx == 0 and block.get("title"):
                            combined_paragraphs.append(block["title"])
                        combined_paragraphs.extend(block["paragraphs"])
                        combined_paragraphs.append("")
                    txt_path = os.path.join(processed_tdir, f"{n}.txt")
                    write_text_file(combined_paragraphs, txt_path)
                    chapter_content.append(
                        {"type": "text_file", "path": txt_path}
                    )
                # keep_images already preserved raw downloads
            else:
                chapter_content = [
                    {"type": "image", "path": p} for p in processed_page_images
                ]

            if chapter_content:
                with open(marker_path, "w") as f:
                    pass
            chapter_content_size = sum(
                os.path.getsize(item["path"])
                for item in chapter_content
                if isinstance(item, dict)
                and item.get("path")
                and os.path.exists(item["path"])
            )
            _timing["processing"] += time.monotonic() - _t0_proc
        if not chapter_content:
            return None, grp_name, n, 0

        if args.keep_chapters:
            ch_suffix = f"Ch {format_chap_for_filename(n)}"
            ch_filename = f"{join_name(base_filename, ch_suffix)}.{args.format}"
            ch_out_path = os.path.join(out_dir, ch_filename)
            ch_title = f"{title} ({ch_suffix})"
            log_verbose(f"  Saving individual chapter file...")

            if args.format == "epub":
                chapter_marker = [{"ch": ch, "page_index": 0}]
                with _cpu_guard('build_epub'):
                    build_epub(
                    chapter_content,
                    ch_out_path,
                    ch_title,
                    args.language,
                    args.epub_layout,
                    comic_data,
                    [grp_name] if grp_name else [],
                    original_cover_path,
                    chapter_markers=chapter_marker,
                )
            elif args.format == "cbz":
                # Phase D (2026-05-07): when chapter_content is a single
                # cbz_cache entry, just copy the cached .cbz to the user-
                # visible per-chapter file. Mirrors PDF's flow at the
                # parallel `elif args.format == "pdf"` block below — the
                # cache is byte-identical to what build_cbz would have
                # written, so the copy is correct AND skips a re-zip.
                if (
                    chapter_content
                    and chapter_content[0].get("type") == "cbz_cache"
                ):
                    src_cbz = chapter_content[0]["path"]
                    try:
                        if (not os.path.exists(ch_out_path)) or (
                            os.path.getsize(ch_out_path)
                            != os.path.getsize(src_cbz)
                        ):
                            shutil.copy2(src_cbz, ch_out_path)
                    except OSError:
                        shutil.copy2(src_cbz, ch_out_path)
                    print(f"CBZ saved → {os.path.basename(ch_out_path)}")
                else:
                    # Legacy back-compat: chapter_content carries 'image'
                    # entries (pre-Phase-D code path). Build directly.
                    cbz_images = [
                        item["path"]
                        for item in chapter_content
                        if item.get("type") == "image"
                    ]
                    with _cpu_guard('build_cbz'):
                        build_cbz(
                            cbz_images,
                            ch_out_path,
                            ch_title,
                            comic_data,
                            [grp_name] if grp_name else [],
                            args.language,
                        )
            elif args.format == "pdf":
                if chapter_content:
                    src_pdf = chapter_content[0]["path"]
                    try:
                        if (not os.path.exists(ch_out_path)) or (os.path.getsize(ch_out_path) != os.path.getsize(src_pdf)):
                            shutil.copy2(src_pdf, ch_out_path)
                    except OSError:
                        # If size checks fail for any reason, fall back to copying.
                        shutil.copy2(src_pdf, ch_out_path)
                print(f"PDF Chapter saved → {os.path.basename(ch_out_path)}")

        return chapter_content, grp_name, n, chapter_content_size


    def _process_chapter(ch: Dict[str, Any], *, force_redownload: bool = False, next_chapter: Optional[Dict[str, Any]] = None, is_alt_source: bool = False):
        """Wrapper around _process_chapter_impl that arms the per-chapter
        watchdog (deadline timer + host-failure reset) and tears it down on
        exit, even if _process_chapter_impl raises (ChapterSkippedError or any
        other exception).

        Why a wrapper: the impl is large and has many return paths. Putting
        the try/finally here avoids indenting hundreds of lines of existing
        download/processing logic. Cross-file: dl_image and _try_download_url
        check _CHAPTER_CANCEL / _host_fail_count which we own here.

        is_alt_source plumbs through to _process_chapter_impl so the inter-
        chapter image prefetch is suppressed during multi-source fallback
        attempts (where handler/scraper have been rebound to an alt and
        next_chapter is still in the primary's chapter list).
        """
        global _CHAPTER_CANCEL
        # Reset per-chapter host-failure tally so the poison threshold is
        # scoped per chapter, not per run.
        _reset_host_failures_for_chapter()
        _CHAPTER_CANCEL = threading.Event()

        deadline_s = float(globals().get("_CHAPTER_DEADLINE", 90.0))
        if force_redownload and deadline_s > 0:
            # End-of-run retry pass: give the chapter more headroom on its
            # second chance. Origin is often less flaky a few minutes later.
            deadline_s *= 2.0

        timer: Optional[threading.Timer] = None
        if deadline_s > 0:
            timer = threading.Timer(deadline_s, _CHAPTER_CANCEL.set)
            timer.daemon = True
            timer.start()
        try:
            return _process_chapter_impl(ch, force_redownload=force_redownload, next_chapter=next_chapter, is_alt_source=is_alt_source)
        finally:
            if timer is not None:
                timer.cancel()
            # Clear the global so dl_image calls outside a chapter (e.g. the
            # cover image on the next run, or a follow-up batch URL) don't
            # see a stale set Event that would make them fast-fail.
            _CHAPTER_CANCEL = None


    def _process_chapter_strict(ch: Dict[str, Any], *, force_redownload: bool = False, next_chapter: Optional[Dict[str, Any]] = None):
        """Outer wrapper: zero-tolerance + per-chapter source fallback + inline
        retry + hard abort on exhaustion.

        Phase 4b adds per-chapter source fallback: when the primary source
        fails, try alternative sources (from the multi-source alignment) BEFORE
        engaging the slow inline-retry. The handler/scraper/context are swapped
        in via nonlocal for the duration of an alternative attempt, then
        restored. Avoids waiting 30-60s on inline-retry when an alternative
        source has the chapter and would succeed in seconds.

        Behavior the user explicitly asked for after seeing partial PDFs:
          1. Never accept a partial chapter (any missing page → fallback or retry).
          2. Try alternative sources first (cheap — no sleep).
          3. If all sources fail: long inline retry on primary (CDN recovery).
          4. If inline retries don't recover: stop the run with a clear error.

        Backoff schedule: --inline-chapter-backoff seconds, doubling each
        retry. With defaults (base=30s, retries=2): waits 30s, then 60s.

        Cross-file: cooperates with _process_chapter (watchdog wrapper) which
        bounds each individual attempt's wall-clock time. Multi-source state
        comes from `_multi_source_alternatives` populated in main() when
        --search --multi-source --auto-pick is set.
        """
        nonlocal handler, scraper, context, comic_data
        primary_state = (handler, scraper, context, comic_data)
        n_for_log = ch.get("chap")
        max_retries = int(globals().get("_INLINE_CHAPTER_RETRIES", 2))
        base_backoff = float(globals().get("_INLINE_CHAPTER_BACKOFF", 30.0))

        # Try primary source first.
        try:
            redo_primary = bool(force_redownload)
            return _process_chapter(ch, force_redownload=redo_primary, next_chapter=next_chapter)
        except ChapterSkippedError as cse_primary:
            primary_err: ChapterSkippedError = cse_primary

        # Phase 4b: try alternative sources before the inline-retry sleep.
        # Look up alternatives by chapter number (float). Numeric extraction
        # mirrors chapter_merger._extract_chapter_num.
        alts: List[Dict[str, Any]] = []
        try:
            chap_str = str(ch.get("chap") or "").strip()
            m = re.search(r"(\d+(?:\.\d+)?)", chap_str)
            if m:
                chap_float = float(m.group(1))
                alts = list(_multi_source_alternatives.get(chap_float, []))
        except (TypeError, ValueError):
            alts = []

        if alts:
            print(
                f"  [Multi-source] Chapter {n_for_log} failed on {primary_state[0].name}; "
                f"trying {len(alts)} alternative source(s) before inline-retry..."
            )
            for alt in alts:
                alt_handler = alt.get("handler") or get_handler_by_name(alt.get("site", ""))
                alt_scraper = alt.get("scraper")
                alt_context = alt.get("context")
                alt_chapter = alt.get("chapter")
                if not (alt_handler and alt_scraper and alt_context and alt_chapter):
                    continue
                # Swap the closure-scope state so _process_chapter_impl
                # (which reads handler/scraper/context from outer scope) uses
                # the alternative source. Restore in finally so subsequent
                # chapters see the primary by default.
                handler = alt_handler
                scraper = alt_scraper
                context = alt_context
                comic_data = context.comic if context is not None else primary_state[3]
                try:
                    print(f"    [Multi-source] -> {alt_handler.name}")
                    # force_redownload=True on alternatives so the primary's
                    # failed tdir is wiped; otherwise the marker check would
                    # see the partial state from the failed primary attempt.
                    # is_alt_source=True suppresses the Phase G7 inter-chapter
                    # prefetch — next_chapter is from the PRIMARY chapter list,
                    # so alt_handler.get_chapter_images(next_chapter) would
                    # silently fail and waste a worker pool. Subsequent
                    # chapters resume on the primary anyway (strict wrapper's
                    # finally restores primary state).
                    return _process_chapter(
                        alt_chapter, force_redownload=True, next_chapter=next_chapter, is_alt_source=True
                    )
                except ChapterSkippedError as cse_alt:
                    print(
                        f"    [Multi-source] {alt_handler.name} also failed "
                        f"({cse_alt.reason}); trying next..."
                    )
                    continue
                finally:
                    # Always restore primary state so the next chapter starts
                    # on the primary source (the alignment anchor).
                    handler, scraper, context, comic_data = primary_state

        # All alternatives failed (or none available). Fall back to the
        # existing inline-retry on the primary source.
        last_err: ChapterSkippedError = primary_err
        for retry_attempt in range(max_retries):
            wait = base_backoff * (2 ** retry_attempt)
            print(
                f"  [!] Chapter {n_for_log} long retry {retry_attempt + 1}/{max_retries}: "
                f"waiting {wait:.0f}s for {last_err.host or 'upstream'} to recover, "
                f"then redownloading chapter inline..."
            )
            waited = 0.0
            while waited < wait:
                chunk = min(2.0, wait - waited)
                time.sleep(chunk)
                waited += chunk
            try:
                return _process_chapter(ch, force_redownload=True, next_chapter=next_chapter)
            except ChapterSkippedError as cse_retry:
                last_err = cse_retry
                continue
        # Out of inline retries — hard abort.
        raise ChapterAbortedError(
            chap=n_for_log,
            reason=last_err.reason,
            host=last_err.host,
            pages_ok=last_err.pages_ok,
            pages_total=last_err.pages_total,
            attempts=max_retries + 1 + len(alts),
        ) from last_err


    # --- Timing accumulators (monotonic clock – essentially zero overhead) ---
    _timing = {"vrf": 0.0, "download": 0.0, "processing": 0.0}
    _timing_total_start = time.monotonic()
    # Running counter for EPUB page indices – avoids re-scanning
    # the ever-growing current_book_content list every chapter.
    _running_page_count = 0
    # When a chapter can't be recovered after inline retries, _process_chapter_strict
    # raises ChapterAbortedError. We set this flag so the end-of-run retry pass
    # (which is for *resumable* missed entries) is skipped — we already gave up
    # via inline retries; running a second mass-retry would be redundant and
    # would obscure the abort message in the timing summary.
    aborted_remaining = False
    aborted_chapter: Optional[Dict[str, Any]] = None

    for ch_idx, ch in enumerate(chapters):
        grp_name = handler.get_group_name(ch)
        insert_list_index = len(current_book_content)
        insert_chapter_index = len(current_book_chapters)
        insert_marker_index = len(current_epub_markers)
        insert_page_index = _running_page_count if args.format == 'epub' else 0
        # Look ahead so _process_chapter can prefetch the next chapter's VRF
        # while downloading images for this one.
        next_ch = chapters[ch_idx + 1] if ch_idx + 1 < len(chapters) else None
        try:
            chapter_content, grp_name, n, chapter_content_size = _process_chapter_strict(ch, next_chapter=next_ch)
        except ChapterAbortedError as cae:
            # Hard abort: a chapter could not be downloaded fully even after
            # inline retries. The user explicitly asked for this — refuse to
            # produce a partial book, refuse to silently move on, just stop.
            print(
                f"\n[!] FATAL: Chapter {cae.chap} could not be downloaded after "
                f"{cae.attempts} attempt(s)."
            )
            print(
                f"    {cae.pages_ok}/{cae.pages_total} pages succeeded, "
                f"reason={cae.reason}, host={cae.host or '-'}."
            )
            print(
                f"    Aborting run. Chapters successfully saved before this point "
                f"are kept (per-chapter files via --keep-chapters)."
            )
            _record_missed(
                ch, grp_name, f"aborted:{cae.reason}", str(cae),
                insert_list_index=insert_list_index,
                insert_chapter_index=insert_chapter_index,
                insert_marker_index=insert_marker_index,
                insert_page_index=insert_page_index,
                host=cae.host,
                pages_ok=cae.pages_ok,
                pages_total=cae.pages_total,
            )
            aborted_remaining = True
            aborted_chapter = ch
            # Record un-attempted chapters too, so the timing summary shows
            # the full damage. These don't get retried at end-of-run because
            # of the aborted_remaining flag.
            for j in range(ch_idx + 1, len(chapters)):
                skipped_ch = chapters[j]
                _record_missed(
                    skipped_ch,
                    handler.get_group_name(skipped_ch),
                    'not_attempted_after_abort',
                    f"main pass aborted at chapter {cae.chap}",
                    insert_list_index=len(current_book_content),
                    insert_chapter_index=len(current_book_chapters),
                    insert_marker_index=len(current_epub_markers),
                    insert_page_index=_running_page_count if args.format == 'epub' else 0,
                    host=cae.host,
                    pages_ok=0,
                    pages_total=0,
                )
            break
        except Exception as e:
            _record_missed(ch, grp_name, 'exception', repr(e), insert_list_index=insert_list_index, insert_chapter_index=insert_chapter_index, insert_marker_index=insert_marker_index, insert_page_index=insert_page_index)
            continue

        if not chapter_content:
            _record_missed(ch, grp_name, 'empty_content', 'No downloadable content', insert_list_index=insert_list_index, insert_chapter_index=insert_chapter_index, insert_marker_index=insert_marker_index, insert_page_index=insert_page_index)
            continue

        should_split_by_size = (
            split_size_bytes > 0
            and current_book_content
            and current_book_size + chapter_content_size > split_size_bytes
        )
        should_split_by_chapters = (
            split_chapter_count > 0
            and len(current_book_chapters) >= split_chapter_count
        )

        if should_split_by_size or should_split_by_chapters:
            build_book_part(
                args,
                base_filename,
                comic_data,
                current_book_content,
                current_book_chapters,
                current_book_scan_groups,
                original_cover_path,
                epub_markers=current_epub_markers,
            )
            current_book_content = []
            current_book_chapters = []
            current_book_scan_groups = set()
            current_book_size = 0
            current_epub_markers = []
            _running_page_count = 0

        if args.format == 'epub':
            start_page_index = _running_page_count
        current_book_content.extend(chapter_content)
        current_book_chapters.append(ch)
        if grp_name:
            current_book_scan_groups.add(grp_name)
        current_book_size += chapter_content_size
        chapter_page_count = _epub_page_count(chapter_content) if args.format == 'epub' else 0
        if chapter_page_count > 0:
            current_epub_markers.append({'ch': ch, 'page_index': start_page_index})
        _running_page_count += chapter_page_count

    # Retry missed chapters at the end. Skip if the run was aborted by
    # ChapterAbortedError — the strict-mode inline retry already gave up,
    # and a second mass-retry would just produce more failure noise without
    # changing the outcome (and would obscure the FATAL message in the log).
    # Missed entries from non-fatal paths (the residual 'exception' /
    # 'empty_content' branches) still get this final pass.
    if retry_missed and missed_entries and missed_retries > 0 and not aborted_remaining:
        print(f"\n[*] Missed {len(missed_entries)} chapter(s). Retrying at the end...")
        missed_entries.sort(key=lambda e: (int(e.get('insert_chapter_index', 0)), int(e.get('insert_list_index', 0))))
        remaining: List[Dict[str, Any]] = []
        content_shift_items = 0
        chapter_shift = 0
        marker_shift = 0
        page_shift = 0

        for entry in missed_entries:
            ch_retry = entry.get('ch') or {}
            grp_name_retry = entry.get('group') or handler.get_group_name(ch_retry)
            ok = False
            last_err = ''
            for attempt in range(1, missed_retries + 1):
                try:
                    # Route through the strict wrapper so the retry pass
                    # benefits from the multi-source alt-source fallback —
                    # chapters that needed multi-source most (the missed
                    # ones) couldn't use it on the original pass when the
                    # retry called _process_chapter directly.
                    # ChapterAbortedError is a normal-flow signal here (all
                    # alts + inline retries exhausted); treat it the same as
                    # a regular failure so we just append to `remaining` and
                    # surface it via the missed_chapters.json log.
                    chapter_content, grp_name_retry, n, chapter_content_size = _process_chapter_strict(ch_retry, force_redownload=True)
                    if chapter_content:
                        ok = True
                        break
                    last_err = 'No downloadable content'
                except ChapterAbortedError as cae:
                    last_err = f"aborted after {cae.attempts} attempt(s): {cae.reason} ({cae.host or 'unknown host'})"
                except Exception as e:
                    last_err = repr(e)
                sleep_s = min(60.0, (2 ** attempt)) + random.uniform(0.0, 1.25)
                log_verbose(f"  Retry backoff: sleeping {sleep_s:.1f}s (attempt {attempt}/{missed_retries})")
                time.sleep(sleep_s)

            if not ok:
                entry['error'] = (last_err or entry.get('error') or '')[:500]
                remaining.append(entry)
                continue

            insert_at = int(entry.get('insert_list_index', 0)) + content_shift_items
            chap_insert_at = int(entry.get('insert_chapter_index', 0)) + chapter_shift
            marker_insert_at = int(entry.get('insert_marker_index', 0)) + marker_shift
            page_insert_at = int(entry.get('insert_page_index', 0)) + page_shift

            delta_pages = _epub_page_count(chapter_content) if args.format == 'epub' else 0
            if args.format == 'epub' and delta_pages > 0:
                for m in current_epub_markers:
                    if int(m.get('page_index', 0) or 0) >= page_insert_at:
                        m['page_index'] = int(m.get('page_index', 0) or 0) + delta_pages

            current_book_content[insert_at:insert_at] = chapter_content
            current_book_chapters.insert(chap_insert_at, ch_retry)
            if grp_name_retry:
                current_book_scan_groups.add(grp_name_retry)
            current_book_size += chapter_content_size
            if args.format == 'epub' and delta_pages > 0:
                current_epub_markers.insert(marker_insert_at, {'ch': ch_retry, 'page_index': page_insert_at})

            content_shift_items += len(chapter_content)
            chapter_shift += 1
            marker_shift += 1
            page_shift += delta_pages
            print(f"  [+] Recovered chapter {n}")

        missed_entries = remaining
        _save_missed(missed_entries)
        if missed_entries:
            print(f"[!] Still missed {len(missed_entries)} chapter(s). A log was saved to: {missed_log_path}")
            try:
                out_log = os.path.join(out_dir, f"{base_filename} (missed chapters).json")
                shutil.copy(missed_log_path, out_log)
            except Exception:
                pass
        else:
            try:
                os.remove(missed_log_path)
            except Exception:
                pass

    if current_book_content and not aborted_remaining:
        if args.no_final_file:
            print("\nSkipping final file build (--no-final-file).")
        elif args.format == "none":
            pass
        elif split_size_bytes > 0 or split_chapter_count > 0:
            build_book_part(
                args,
                base_filename,
                comic_data,
                current_book_content,
                current_book_chapters,
                current_book_scan_groups,
                original_cover_path,
                epub_markers=current_epub_markers,
            )
        else:
            print("\nBuilding final file...")
            final_path = os.path.join(out_dir, f"{base_filename}.{args.format}")
            if args.format == "epub":
                with _cpu_guard('build_epub'):
                    build_epub(
                    current_book_content,
                    final_path,
                    title,
                    args.language,
                    args.epub_layout,
                    comic_data,
                    list(current_book_scan_groups),
                    original_cover_path,
                    chapter_markers=current_epub_markers,
                )
            elif args.format == "cbz":
                # Phase D (2026-05-07): use the wrapper so cbz_cache entries
                # produced during chapter processing get member-copied into
                # the final archive instead of being silently dropped by the
                # old type=="image" filter.
                with _cpu_guard('build_cbz'):
                    build_cbz_from_content(
                        current_book_content,
                        final_path,
                        title,
                        comic_data,
                        list(current_book_scan_groups),
                        args.language,
                    )
            elif args.format == "pdf":
                pdf_inputs = [
                    item["path"]
                    for item in current_book_content
                    if item.get("type") == "pdf"
                ]
                if pdf_inputs:
                    with _cpu_guard('merge_pdf'):
                        merge_pdf_files(
                        pdf_inputs,
                        final_path,
                        {
                            "/Title": title,
                            "/Author": ", ".join(comic_data.get("authors", [])),
                        },
                    )
                    print(f"PDF saved → {os.path.basename(final_path)}")
                for item in current_book_content:
                    if item.get("type") == "pdf" and item.get("path"):
                        try:
                            os.remove(item["path"])
                        except OSError:
                            pass

    # --- Save series metadata for the UI's update-checking feature ---
    # .aio_series.json is written to the output folder (alongside PDFs) and
    # survives cleanup. It stores the source URL, downloaded chapters, and
    # series info so the UI can later check for new chapters without the
    # user having to re-enter the URL.
    try:
        series_meta_path = os.path.join(out_dir, ".aio_series.json")

        # Figure out which chapters were actually downloaded successfully.
        # Start with all chapters we attempted, then subtract any that are
        # still in the missed list after retries.
        downloaded_nums = set(str(ch["chap"]) for ch in chapters)
        still_missed_nums = set(str(e["chap"]) for e in missed_entries) if missed_entries else set()
        actually_downloaded = sorted(downloaded_nums - still_missed_nums, key=lambda x: float(x))

        # If a previous .aio_series.json exists (from an earlier download),
        # merge the chapter lists so partial/split downloads accumulate.
        existing_meta = {}
        if os.path.isfile(series_meta_path):
            try:
                with open(series_meta_path, "r", encoding="utf-8") as f:
                    existing_meta = json.load(f)
            except Exception:
                pass

        prev_downloaded = set(existing_meta.get("chapters_downloaded", []))
        merged_downloaded = sorted(
            prev_downloaded | set(actually_downloaded),
            key=lambda x: float(x),
        )

        series_meta = {
            "url": args.comic_url,
            "hid": hid,
            "title": title,
            "site": handler.name,
            "format": args.format,
            "language": args.language,
            "status": comic_data.get("status"),
            "authors": comic_data.get("authors", []),
            "cover": comic_data.get("cover"),
            "genres": comic_data.get("genres", []),
            "chapters_downloaded": merged_downloaded,
            "total_available_at_download": len(pool),
            "last_downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        with open(series_meta_path, "w", encoding="utf-8") as f:
            json.dump(series_meta, f, indent=2)
    except Exception as e:
        log_verbose(f"  Warning: Failed to write .aio_series.json: {e}")

    # --- Timing summary ---
    _timing_total = time.monotonic() - _timing_total_start
    def _fmt_time(s: float) -> str:
        if s >= 60:
            m, sec = divmod(s, 60)
            return f"{int(m)}m {sec:.1f}s"
        return f"{s:.1f}s"

    _timing_other = max(0.0, _timing_total - _timing["vrf"] - _timing["download"] - _timing["processing"])
    print(f"\n--- Timing Summary ---")
    print(f"  VRF / image URLs : {_fmt_time(_timing['vrf'])}")
    print(f"  Image download   : {_fmt_time(_timing['download'])}")
    print(f"  Processing       : {_fmt_time(_timing['processing'])}")
    print(f"  Other (overhead) : {_fmt_time(_timing_other)}")
    print(f"  Total            : {_fmt_time(_timing_total)}")

    # --- Skipped chapters report ---
    # Printed AFTER the timing summary but BEFORE 'Done.' so the Electron
    # parser sees: '--- Timing Summary ---' (phase=finishing) → this block →
    # 'Done.' (phase=done). User explicitly asked this to live next to the
    # timing summary so a broken site is obvious at a glance.
    print(f"\n--- Skipped Chapters ---")
    if not missed_entries:
        total_attempted = len(chapters)
        print(f"  None — all {total_attempted} chapter(s) downloaded successfully.")
    else:
        # Sort by chapter number for readability. Chapters with non-numeric
        # labels go last in stable order.
        def _chap_sort_key(e):
            try:
                return (0, float(e.get('chap') or 0))
            except (TypeError, ValueError):
                return (1, str(e.get('chap') or ''))
        srt = sorted(missed_entries, key=_chap_sort_key)
        if aborted_remaining:
            heading = (
                f"Run ABORTED at chapter "
                f"{aborted_chapter.get('chap') if aborted_chapter else '?'}. "
                f"{len(srt)} chapter(s) were not completed:"
            )
        else:
            heading = f"{len(srt)} chapter(s) failed after end-of-run retries:"
        print(f"  {heading}")
        for e in srt:
            chap_label = str(e.get('chap') or '?')
            reason = str(e.get('reason') or '?')
            host = str(e.get('host') or '-') or '-'
            ok = e.get('pages_ok')
            tot = e.get('pages_total')
            pages_part = (
                f"pages={ok}/{tot} ok"
                if isinstance(ok, int) and isinstance(tot, int) and tot > 0
                else ""
            )
            err_text = (e.get('error') or '').strip().replace('\n', ' ').replace('\r', ' ')
            if err_text and len(err_text) > 80:
                err_text = err_text[:80] + '…'
            err_part = f"err={err_text}" if err_text else ""
            extras = "  ".join(p for p in (pages_part, err_part) if p)
            print(f"    Ch {chap_label:<8} reason={reason:<24} host={host:<24} {extras}".rstrip())
        try:
            out_log_hint = os.path.join(out_dir, f"{base_filename} (missed chapters).json")
            print(f"  Detailed log: {out_log_hint}")
        except Exception:
            pass

    if aborted_remaining:
        # Don't wipe tmp_dir on abort — completed chapter dirs are useful for
        # a resume run, and the partial chapter dir was already wiped by the
        # impl/strict wrappers. Tell the user where to look. Exit non-zero so
        # the Electron UI's downloader.js sees a failure (it reads exit code).
        print(f"\nABORTED. Run stopped because chapter {aborted_chapter.get('chap') if aborted_chapter else '?'} could not be downloaded.")
        print(f"Per-chapter PDFs saved before this point are kept in: {out_dir}")
        print(f"Temporary files kept at: {main_tmp_dir}")
        sys.exit(1)
    elif not args.no_cleanup:
        rm_tree(main_tmp_dir)
        print("\nDone.")
    else:
        print(f"\nDone. Temporary files kept at: {main_tmp_dir}")


if __name__ == "__main__":
    try:
        main()
        _hb("done", "ok")
    except SystemExit:
        raise
    except KeyboardInterrupt:
        _hb("error", "keyboard_interrupt")
        raise
    except Exception as e:
        _hb("error", str(e))
        raise
