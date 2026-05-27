#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ------------------------------------------------------------------
# Multi-site comic downloader  →  PDF, EPUB, or CBZ
# -----------------------------------------------------------
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import base64
import glob
import hashlib
import io
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys

# Force UTF-8 on stdio before anything prints. When Electron spawns this
# script (UI-source/electron/downloader.js:585, searcher.js:160,
# main.js:195/878), stdio is a pipe, not a TTY, so Python falls back to
# locale.getpreferredencoding(False) → cp1252 on default Western Windows
# (ACP=1252). Many log lines use → ─ — × ≥ which cp1252 can't encode,
# crashing the run with UnicodeEncodeError mid-listing. errors='replace'
# keeps it crash-proof for any future char. No-op on UTF-8-ACP boxes
# (Win11 Beta UTF-8 mode) and real terminals (PEP 528 WriteConsoleW).
# Grep target: UnicodeEncodeError reconfigure
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import threading
import time
import textwrap
import xml.sax.saxutils
import zipfile
import zlib
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, unquote_to_bytes

from aio_config import (
    CANONICAL_HID_MARKER,
    DEFAULT_OUTPUT_DIR,
    ignored_library_filenames,
    read_hid_marker,
    resolve_output_dir,
    write_hid_marker,
)
from sites import get_handler_by_name, get_handler_for_url
from sites.chapter_merger import group_chapters_for_download
from sites.base import SiteComicContext, IncompleteChapterError
from sites._image_io import (
    sniff_image_extension as _sniff_image_extension,
    finalize_pending_image as _finalize_pending_image,
    JPEG_MAGIC as _JPEG_MAGIC,
    PNG_MAGIC as _PNG_MAGIC,
    GIF_MAGIC as _GIF_MAGIC,
    content_type_to_ext as _content_type_to_ext,
)


# -----------------------------------------------------------
# Custom exceptions
# -----------------------------------------------------------
class ChapterSkippedError(Exception):
    """Raised by _process_chapter_impl when a chapter cannot complete in one
    attempt — any of:
      - any page failed to download (zero-tolerance: pages_ok < pages_total)
      - watchdog deadline fired (chapter took too long)
      - host poison threshold hit (≥N distinct URLs to one host fully failed)
      - ghost chapter (every page returns an identical structural error response
        — uniform status + uniform body size — indicating the chapter doesn't
        exist on the primary source despite being listed in the chapter index;
        canonical example: mangafire "chapter 0" placeholder entries whose
        image URLs all return the same 5051-byte 403 template body)

    Caught by _process_chapter_strict, which performs an inline retry pass
    (long wait + redo the chapter from scratch). After inline retries are
    exhausted, _process_chapter_strict converts this into ChapterAbortedError
    which the main loop treats as a fatal stop — EXCEPT for ghost_chapter,
    which short-circuits inline-retry (a structural failure won't fix itself
    after 30s of sleep) and raises ChapterGhostError instead, which the main
    loop catches as skip-and-continue.

    Attributes:
        reason:       short tag, one of: 'incomplete', 'time_budget',
                      'host_poison', 'ghost_chapter'
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


class ChapterGhostError(Exception):
    """Raised by _process_chapter_strict when the primary source returned a
    'ghost chapter' signature (every page failed with an identical error
    response: same status, same body-size bucket) AND no alternative source
    could deliver the chapter. Distinct from ChapterAbortedError on purpose:
    the main loop treats this as skip-and-continue, NOT abort.

    Why a distinct exception (not just another ChapterSkippedError reason):
    a ghost signature on the primary means the chapter is structurally absent
    there — VRF token rotated to a different URL space, soft-launched
    placeholder, CDN URL signed for a chapter that was unpublished, etc. No
    amount of inline retry will help, because the response template that
    every page returns won't change after a 30s/60s sleep (mangafire's
    5051-byte CF 'access denied' is the canonical case — see the 2026-05-27
    Shangri-La Frontier failure for the original observed pattern). Aborting
    the whole run on a single fake chapter punishes 290 valid chapters for
    one structural mismatch we can prove is structural. Recording as missed
    + continuing preserves the all-or-nothing guarantee at the CHAPTER level
    (we never produced partial PDF output for it) while not sacrificing the
    run-level coverage.

    The caller-loop path:
        ChapterSkippedError(reason='ghost_chapter') (one attempt)
          → _process_chapter_strict tries multi-source alts (might recover)
          → if every alt also fails: raise ChapterGhostError (no inline-retry)
          → main for-loop's except clause: _record_missed + continue

    Attributes:
        chap:         chapter label (from ch.get('chap'))
        host:         netloc whose responses formed the ghost signature
        pages_total:  total pages the chapter was supposed to have
        primary_only: True if the alignment data shows no non-primary source
                      listed this chapter number (strong "ghost" corroboration);
                      False otherwise; None if multi-source isn't enabled and
                      the cross-source check couldn't be evaluated. Surfaced
                      so the log line tells the user WHICH ghost-chapter shape
                      this was (primary-only is the canonical "fake placeholder"
                      pattern; non-primary-only just means the alt sources
                      also couldn't deliver).
    """
    def __init__(self, chap, host: str = "", pages_total: int = 0, primary_only: Optional[bool] = None):
        self.chap = chap
        self.host = host
        self.pages_total = pages_total
        self.primary_only = primary_only
        super().__init__(
            f"chapter {chap} ghost: host={host or '-'} pages={pages_total} "
            f"primary_only={primary_only}"
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


def allocate_series_output_dir(title: str, hid: str, root: str = DEFAULT_OUTPUT_DIR) -> str:
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

    def _read_marker(folder: str) -> str | None:
        return read_hid_marker(folder)

    def _folder_nonempty(folder: str) -> bool:
        try:
            ignored = ignored_library_filenames()
            items = [x for x in os.listdir(folder) if x not in ignored]
            return len(items) > 0
        except Exception:
            return False

    def _write_marker(folder: str):
        write_hid_marker(folder, str(hid))

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

# pyvips powers the lossy-WebP save fast path inside save_final_images. libvips
# streams rows directly from a PIL.Image.tobytes() buffer through libwebp,
# skipping the PIL→libwebp glue layer that costs ~2x on 1500x3750 stitched
# LineWebtoon pages. Bench numbers on the 12-core test box:
#   pil-webp-q85-m2   : 4.24 s / 94 pages parallel x6
#   pyvips-webp-q85-e2: 2.75 s / 94 pages parallel x6   ← 54% faster, same output bytes
# (see bench/results.csv 2026-05-15). If pyvips or its libvips DLL bundle
# can't load (older Windows w/o pyvips-binary wheel, ARM Linux without libvips,
# etc.) we silently fall back to PIL — output bytes are byte-identical-size
# and SSIM-identical because both call the same libwebp under the hood.
try:
    import pyvips  # type: ignore
    _HAS_PYVIPS = True
except Exception:  # pragma: no cover
    pyvips = None  # type: ignore
    _HAS_PYVIPS = False

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

                if cls == "retryable":
                    # 5xx server errors (500/502/503/504) and 408 timeouts. The
                    # response body is always an error page or maintenance HTML
                    # — never useful payload — so the body-size threshold below
                    # is a trap: a 503 with a >100-char MangaDex maintenance
                    # page slipped through as a "successful" response and
                    # fetch_comic_context's .json() blew up with the cryptic
                    # "Expecting value: line 1 column 1 (char 0)" the user
                    # reported on 2026-05-16 (the chapter-5 follow-up where
                    # the API was 503'ing). Raise here so the outer retry
                    # loop engages with exponential backoff — same response
                    # class as origin_error, just a different status band.
                    # Symmetric with the origin_error branch above.
                    raise requests.exceptions.HTTPError(
                        f"Retryable server error ({r.status_code})", response=r
                    )

                # cls == "permanent" (4xx with no rate-limit keyword). Some APIs
                # return structured 4xx with JSON bodies the caller wants to
                # inspect (MangaDex's API does this for client-side validation
                # errors), so when the body has content we surface the response
                # rather than raising. Tiny-body 4xx fails fast — there's
                # nothing to inspect.
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


_CHAPTER_SPEC_NUM_RE = re.compile(
    r"\b(?:chapter|chap|ch)\.?\s*[-_:]?\s*(-?\d+(?:\.\d+)?)\b",
    re.I,
)


def _parse_chapter_spec_number(value: str) -> Optional[float]:
    text = (value or "").strip().strip("'\"")
    if not text:
        return None
    if text.lower() in {"oneshot", "one-shot"}:
        return 1.0
    try:
        return float(text)
    except ValueError:
        pass
    match = _CHAPTER_SPEC_NUM_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def is_chapter_wanted(chapter_num_float: float, range_spec: str) -> bool:
    """
    Checks if a chapter number falls within a comma-separated range spec.
    Handles both single numbers and 'start-end' ranges with floats.
    """
    for part in range_spec.split(","):
        part = part.strip()
        if "-" in part:
            try:
                raw_start, raw_end = part.split("-", 1)
                start = _parse_chapter_spec_number(raw_start)
                end = _parse_chapter_spec_number(raw_end)
                if start is None or end is None:
                    raise ValueError
                if start <= chapter_num_float <= end:
                    return True
            except ValueError:
                pass  # Maybe this was "chapter-1", not a range.

        parsed = _parse_chapter_spec_number(part)
        if parsed is not None and chapter_num_float == parsed:
            return True
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

# Per-chapter response-signature accumulator for ghost-chapter detection.
# Each entry is (status_code, body_size) — raw response bytes, NOT bucketed.
# Initially designed with 64-byte bucketing for fuzzy match, but real CF
# error responses are byte-identical even when Ray IDs differ (Ray IDs are
# fixed-format 16-char hex strings, so the BYTE length is constant across
# responses with different Ray IDs). The 64-byte bucketing introduced
# false-negatives when responses straddled bucket boundaries (e.g. 5051
# vs 5060 split between buckets 4992 and 5056); exact-match is both more
# correct AND simpler. If future handlers produce ghost patterns with
# genuinely-variable body lengths, we can revisit with a tolerance-based
# detector — but for the mangafire case (the canonical 5051-byte CF 403
# placeholder) exact match is provably right.
#
# Read by _is_ghost_chapter_signature at chapter-failure time. Cleared at
# the start of every chapter via _reset_host_failures_for_chapter() so the
# detector is scoped per chapter — a prior chapter's ghosts must not
# poison the next chapter's classification.
#
# Cross-file: written from sites/base.py:fast_download_images via the
# record_host_failure callback (aio-dl.py:_record_failure now accepts a
# signature kwarg), and from _try_download_url's failure path at the
# bottom of the retry loop.
_CHAPTER_FAIL_SIGNATURES: List[Tuple[Optional[int], int]] = []
_CHAPTER_FAIL_SIG_LOCK = threading.Lock()

# Phase D (2026-05-13): per-host concurrency cap that dials DOWN on
# confirmed CDN failures during a run. Distinct from _HOST_FAIL_COUNT
# (per-chapter, drives the chapter-skip threshold) — this lives across
# the whole run so a CDN that 520s on chapter 5 stays capped through
# chapter 50. Reset only by _reset_host_concurrency_caps at run start.
# Floor is 1; we never reduce below "one request at a time" because
# concurrency reduction is a coarse control and the polite-delay
# machinery handles fine-grained request pacing separately.
_HOST_CONCURRENCY_CAP: Dict[str, int] = {}
_HOST_CAP_LOCK = threading.Lock()

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


def _record_failure(
    host: str,
    url: str,
    cls: str,
    *,
    status: Optional[int] = None,
    body_size: Optional[int] = None,
) -> None:
    """Record a fully-failed URL (after retries exhausted) against a host so
    the per-chapter poison threshold can detect a broken host. Counts each
    URL only once per chapter — multiple retry attempts on the same URL
    increment the counter once.

    Called from _try_download_url after the retry loop ends without success,
    and from sites/base.py:fast_download_images via the record_host_failure
    callback when curl_cffi exhausts its 2 attempts.

    `cls` is the classification of the *last* failure; we only count network
    failures (origin_error / rate_limit / retryable) — permanent 4xx errors
    don't indicate the host is broken. NOTE on mangafire's ghost-chapter 403s:
    those classify as 'rate_limit' (the 5051-byte body contains 'cloudflare'
    /'ray id' rate-limit keywords per _classify_response_failure ~line 819),
    so they DO get counted here and DO trip _HOST_FAIL_COUNT. That's
    intentional — the chapter-poison threshold and the ghost-signature
    detector are complementary: poison says "many distinct URLs failed,"
    ghost says "every failure looks structurally identical." Ghost takes
    precedence in the reason ladder (~line 7110) because it's the more
    specific signal.

    Phase D (2026-05-13): also feeds _record_host_failure_for_backoff,
    which dials down the per-host concurrency cap on rate_limit/retryable
    failures. The backoff cap is per-run (not per-chapter like the URL
    counter here) and is independent of the chapter-poison threshold —
    it makes the in-flight fetch lighter BEFORE the threshold trips a
    chapter abort.

    `status` and `body_size`, when provided, feed the ghost-chapter
    signature accumulator (_CHAPTER_FAIL_SIGNATURES) so a uniform
    "every page returned identical structural error" pattern can be
    detected. Optional/keyword-only so existing call sites that don't
    have response metadata (the failing-without-a-response exception
    path in fast_download_images) keep working unchanged. Recording is
    independent of `cls == "permanent"` early-return: even permanent
    4xx failures contribute to ghost detection if they came with a
    body (the host-fail count guard stays gated on non-permanent only).
    """
    # Signature recording happens BEFORE the cls=="permanent" gate so that
    # uniform 4xx ghost responses (e.g. true 403 placeholder pages whose
    # body lacks rate-limit keywords) still feed the detector. Without
    # this, a "pure 403" ghost would never be classified as such — only
    # the rate-limit-classified ones would.
    if body_size is not None:
        _record_failure_signature(status, body_size)
    if not host or cls == "permanent":
        return
    with _HOST_FAIL_LOCK:
        seen = _HOST_FAIL_URLS.setdefault(host, set())
        if url not in seen:
            seen.add(url)
            _HOST_FAIL_COUNT[host] = _HOST_FAIL_COUNT.get(host, 0) + 1
    # Dial down concurrency for subsequent fetches against this host.
    # Outside the _HOST_FAIL_LOCK because _record_host_failure_for_backoff
    # uses its own _HOST_CAP_LOCK — separate locks prevent contention.
    _record_host_failure_for_backoff(host, cls)


def _record_failure_signature(status: Optional[int], body_size: int) -> None:
    """Append one failed-URL response signature to the per-chapter
    accumulator for ghost-chapter detection. Stored as raw (status,
    body_size) — exact-match comparison at detection time. See the
    _CHAPTER_FAIL_SIGNATURES module-level comment for why we don't bucket.

    Negative or None body_size is treated as 0 (the exception path passes
    no body when the request never produced a response). Zero-byte
    signatures still contribute to the uniformity check, which is correct:
    a chapter where every page exception'd with the same error class is
    also structurally broken even if there's no body to fingerprint.

    No-op outside a chapter (when _CHAPTER_FAIL_SIG_LOCK has just been
    cleared) — the lock and list are process-wide but only meaningful
    inside _process_chapter; callers outside that scope just contribute
    noise that the next _reset_host_failures_for_chapter wipes.
    """
    sz = max(0, int(body_size)) if body_size is not None else 0
    with _CHAPTER_FAIL_SIG_LOCK:
        _CHAPTER_FAIL_SIGNATURES.append((status, sz))


def _is_ghost_chapter_signature(
    *,
    pages_ok: int,
    pages_total: int,
    primary_only: Optional[bool] = None,
) -> bool:
    """Return True iff this chapter's failure pattern matches a ghost.

    A "ghost chapter" is a chapter listed in the source's chapter index
    whose image URLs all return the same structural error response —
    indicating the chapter doesn't actually have images on the source,
    not that the CDN is having a moment. Canonical example: mangafire
    "chapter 0" placeholder entries where every page returns a 5051-byte
    CF 403 (same status + same byte-bucket = uniform signature).

    Detection signal set:
      1. pages_ok == 0 (literally nothing succeeded — real transient
         failures rarely take ALL pages down; usually at least one slips
         through). Hard requirement.
      2. pages_total >= threshold. Default 5; lowered to 3 when
         primary_only is True (the cross-source alignment showed no other
         site lists this chapter number, which is independent corroboration
         for "this is fake / soft-launched"). Don't false-positive on
         legit 1-2 page placeholders.
      3. len(set(signatures)) == 1. The smoking gun: every failure was
         the EXACT same (status, body_size). Real CDN issues vary in body
         length because error pages have varying request-context lines
         (Ray IDs, timestamps, paths) — but the Ray-ID-bearing parts of
         CF templates are fixed-format strings, so the BYTE length stays
         constant across responses with different Ray IDs. Identical
         signatures across many pages = "the server is intentionally
         returning a fixed response template" = structural.
      4. The single signature's status is a 4xx (400 <= status < 500).
         5xx errors and pure network failures (status=None from
         timeouts/connection-reset) are inherently transient — the host
         is sick, not lying about chapter existence — and need the normal
         host_poison → inline-retry → abort path. 4xx is the discriminator
         that says "the server is intentionally rejecting this URL,"
         which IS the ghost shape. The mangafire ghost case is 403; a
         future "404 placeholder" handler would also fit.
      5. len(signatures) >= max(3, pages_total // 2). Don't trip on
         fewer than 3 recorded signatures (statistical floor) and require
         at least half the chapter's pages have contributed a signature
         (so we don't ghost-classify a chapter that mostly succeeded but
         then deadline-cancelled). pages_total // 2 caps at 3 minimum to
         keep small chapters checkable.

    Cross-source quorum (Idea B from the design brainstorm) is folded
    into the threshold knob (rule 2) — primary_only=True lowers the
    pages_total floor from 5 to 3, making detection slightly more
    aggressive when we have independent evidence the chapter is fake.
    primary_only=None (multi-source disabled, can't evaluate) treats
    the chapter as not-primary-only (use the default threshold of 5).

    Cross-file: called from _process_chapter_impl's reason-determination
    block (~line 7105) BEFORE host_poison/time_budget/incomplete checks
    so a uniform-signature failure is classified as 'ghost_chapter' even
    when the host-poison threshold also tripped. Both are real signals
    about the same failure; ghost is the more specific (and actionable)
    one.
    """
    if pages_ok != 0:
        return False
    pages_floor = 3 if primary_only is True else 5
    if pages_total < pages_floor:
        return False
    with _CHAPTER_FAIL_SIG_LOCK:
        sigs = list(_CHAPTER_FAIL_SIGNATURES)
    if not sigs:
        return False
    sample_floor = max(3, pages_total // 2)
    if len(sigs) < sample_floor:
        return False
    sig_set = set(sigs)
    if len(sig_set) != 1:
        return False
    # 4xx-only gate (rule 4 in the docstring). 5xx and None (timeouts /
    # network errors) are transient host-level issues that must NOT classify
    # as ghost — they need the existing host_poison → inline-retry → abort
    # path. Without this gate, a true host outage (every request times out
    # → every signature is (None, 0)) would silently skip the ENTIRE
    # chapter queue chapter-by-chapter, wasting an hour before the user
    # realizes the host is down. 4xx is the actionable shape: server is
    # intentionally rejecting THESE URLs, not failing globally.
    (only_status, _only_size) = next(iter(sig_set))
    if only_status is None or not (400 <= only_status < 500):
        return False
    return True


def _host_fail_count(host: str) -> int:
    """Distinct URLs that have fully failed against this host this chapter."""
    if not host:
        return 0
    with _HOST_FAIL_LOCK:
        return _HOST_FAIL_COUNT.get(host, 0)


def _reset_host_failures_for_chapter() -> None:
    """Clear per-chapter host-failure state. Called at the top of every chapter
    in _process_chapter so the threshold is scoped per chapter, not per run.

    Also clears _CHAPTER_FAIL_SIGNATURES so the ghost-chapter detector starts
    fresh for each chapter — without this, a previous chapter's ghost
    signatures would persist and false-positive subsequent chapters that
    happen to share the same body-size bucket on partial failures.
    """
    with _HOST_FAIL_LOCK:
        _HOST_FAIL_COUNT.clear()
        _HOST_FAIL_URLS.clear()
    with _CHAPTER_FAIL_SIG_LOCK:
        _CHAPTER_FAIL_SIGNATURES.clear()


def _record_host_failure_for_backoff(host: str, cls: str) -> None:
    """Reduce _HOST_CONCURRENCY_CAP[host] in response to a confirmed failure.

    Called from _record_failure right after the URL bookkeeping. Floor is 1.
    Class behavior:
      - rate_limit:   cap //= 2  (aggressive — server is mad at request rate)
      - retryable:    cap -= 1   (we got unlucky; light decrement)
      - origin_error: no-op      (CF 520-527 — upstream sickness; concurrency
                                  reduction doesn't help and may slow recovery
                                  when origin comes back)
      - permanent:    no-op      (4xx — already filtered by _record_failure)

    Cross-process backoff (sibling worker processes via _COORD) is NOT
    triggered from here — that path goes through _record_rate_limit which
    is hit by the in-band retry logic. The concurrency cap is purely
    local to this process.

    No-op for empty host (defensive — _record_failure already guards but
    we re-check for cheap insurance)."""
    if cls in ("permanent", "origin_error"):
        return
    if not host:
        return
    with _HOST_CAP_LOCK:
        # Default base is 8 (matches --image-concurrency default). First
        # failure for this host: start from 8, then reduce per the class.
        # If user passed --image-concurrency 4 and we've already capped to
        # 3 from prior failures, _effective_concurrency picks min(4, 3) = 3.
        current = _HOST_CONCURRENCY_CAP.get(host, 8)
        if cls == "rate_limit":
            new_cap = max(1, current // 2)
        elif cls == "retryable":
            new_cap = max(1, current - 1)
        else:
            return
        if new_cap < current:
            _HOST_CONCURRENCY_CAP[host] = new_cap
            log_verbose(
                f"  [Backoff] Reducing {host} concurrency: {current} -> "
                f"{new_cap} (reason={cls})"
            )


def _effective_concurrency(host: str, base: int) -> int:
    """Return min(base, _HOST_CONCURRENCY_CAP[host]) when capped; else base.

    When the cap hasn't been touched (healthy CDN), returns base unchanged
    — zero overhead. Callers must invoke this immediately before
    constructing an asyncio.Semaphore or ThreadPoolExecutor. The host
    is derived from download_tasks[0][1]'s netloc (single-host-per-chapter
    assumption — true in 99%+ cases). Empty host returns base unchanged."""
    if not host:
        return base
    with _HOST_CAP_LOCK:
        cap = _HOST_CONCURRENCY_CAP.get(host)
    return min(base, cap) if cap is not None else base


def _reset_host_concurrency_caps() -> None:
    """Clear per-run concurrency caps. Called by _apply_runtime_tunables at
    run start so each run begins with fresh CDN trust. The polite-delay
    decay (_cool_polite_delay) handles fine-grained request-pacing recovery
    within a run; concurrency stays capped for the rest of the run once
    reduced, intentionally — a CDN that 520s once is likely to 520 again."""
    with _HOST_CAP_LOCK:
        _HOST_CONCURRENCY_CAP.clear()


def _chapter_cancelled() -> bool:
    """True if the current chapter's watchdog has fired or _CHAPTER_CANCEL was
    set explicitly. Returns False outside a chapter (cover download etc.)."""
    return _CHAPTER_CANCEL is not None and _CHAPTER_CANCEL.is_set()


# Image format sniffing + atomic-rename helpers live in sites/_image_io.py
# (extracted 2026-05-09 so sites/mangafire.py:fast_download_images can reuse
# them without circular-importing aio-dl). Aliased above as
# _sniff_image_extension / _finalize_pending_image / _JPEG_MAGIC / etc.


def _try_download_url(
    url, pth, name, scraper, max_retries, retry_delay, timeout=30,
    stop_event: Optional[threading.Event] = None,
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
      - stop_event was set by the caller (parallel-variant winner notifies losers
        to abort their in-flight downloads — see dl_image's parallel block).

    `stop_event` is also polled inside the chunk read loop so a worker mid-
    download can stop within ~one chunk (~131 KB) of another variant winning,
    instead of finishing its full body and getting cleaned up afterward.
    """
    host = urlparse(url).netloc
    last_error: Optional[requests.exceptions.RequestException] = None
    last_class = "retryable"
    # Track the last response's status code + full body length for the
    # ghost-chapter signature accumulator. _extract_error_info only returns
    # a 200-char snippet; for the ghost detector we need the FULL body
    # length (the discriminator for mangafire's 5051-byte uniform 403s).
    # Captured per iteration so the LAST iteration's values are what we
    # record at the failure point — matching the existing last_class
    # convention. Both default to None outside the response path so a
    # network-only failure (Timeout, ConnectionError with no response
    # attached) forwards (None, None) and the detector treats it as a
    # zero-bucket signature.
    last_status: Optional[int] = None
    last_body_size: Optional[int] = None
    poison_threshold = int(globals().get("_CHAPTER_HOST_POISON", 5))

    for attempt in range(max_retries):
        # Fast-fail: chapter deadline passed, give up everything in flight.
        if _chapter_cancelled():
            return False, last_error, None
        # Fast-fail: another parallel variant already succeeded.
        if stop_event is not None and stop_event.is_set():
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
                        # Mid-download abort path. requests honors r.close()
                        # by terminating the underlying socket — the iter_content
                        # generator raises StopIteration on next pull, but we
                        # break out first so callers see a clean (False, ...)
                        # result instead of an exception.
                        if stop_event is not None and stop_event.is_set():
                            try:
                                r.close()
                            except Exception:
                                pass
                            return False, last_error, None
                        if chunk:
                            fh.write(chunk)

            _cool_polite_delay(host)
            return True, None, content_type

        except requests.exceptions.RequestException as e:
            last_error = e
            status, body_snippet = _extract_error_info(e)
            cls = _classify_response_failure(status, body_snippet)
            last_class = cls
            last_status = status
            # Capture full body length for ghost-chapter signature. text[:200]
            # was already read by _extract_error_info, so the response body
            # is already drained — len(response.text) reads from the cached
            # buffer (no extra network I/O). Network-only errors (Timeout,
            # ConnectionError) have no response attached; leave last_body_size
            # as None in that case so the detector treats it as zero-bucket.
            try:
                resp = getattr(e, "response", None)
                if resp is not None:
                    last_body_size = len(resp.text or "")
            except Exception:
                # Defensive: any exception reading body text shouldn't
                # affect the retry decision. Leave last_body_size as
                # whatever the previous iteration set it to.
                pass

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
    # status + body_size feed the ghost-chapter signature accumulator (uniform
    # signatures across all chapter pages → ghost_chapter reason).
    _record_failure(
        host, url, last_class,
        status=last_status,
        body_size=last_body_size,
    )
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

    # Browser-byte-capture cache check (sites/image_cache). Some site
    # handlers (comix.to) capture image response bodies via Patchright's
    # response listener during their chapter scrape, because the CDN's
    # signed tokens expire within ~minute-scale TTL and the HTTP fetch
    # below would 404 by the time it tries. When the cache has the bytes
    # we write them directly to the pending tempfile and skip every HTTP
    # path entirely — including the _host_fail_count and watchdog checks
    # below, because we're not touching the CDN at all. Cross-file:
    # sites/comix.py:_ComixBrowserSession._start attaches the session-
    # level response listener that populates the cache; sites/image_cache.py
    # owns the dict + locks and handles TTL/size-based eviction (no
    # per-scrape clear — that broke under the prefetch chain, see the
    # module docstring there).
    try:
        from sites import image_cache as _ic
        _cached = _ic.get_cached_image(url)
    except Exception:
        _cached = None
    if _cached is not None:
        _body, _ct = _cached
        try:
            with open(pending_pth, "wb") as fh:
                fh.write(_body)
            log_debug(
                f"  Used cached bytes for {os.path.basename(name)} "
                f"(browser-capture cache hit, {len(_body)} bytes, "
                f"content_type={_ct or 'unknown'})"
            )
            return _finalize_pending_image(pending_pth, folder, base, _ct)
        except Exception as _e:
            log_verbose(
                f"  Cache write failed for {os.path.basename(name)} "
                f"({_e}); falling through to HTTP fetch."
            )
            # Fall through to existing HTTP path — defensive, the
            # write should almost never fail (we own the folder).

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

    # Parallel-variant early-stop signal. Set by the first worker that crosses
    # the success line below; polled by every other worker's _try_download_url
    # call inside its chunk-read loop. Without this, future.cancel() in the
    # orchestrator below is a no-op for already-running tasks and the losing
    # workers all complete their full downloads — the temp files get deleted
    # anyway, but the bandwidth is wasted (8x redundancy on a slow CDN).
    stop_event = threading.Event()

    def try_variant(attempt_url, thread_id):
        """Helper function for parallel execution - each thread uses its own temp file"""
        # Fast-fail: skip this variant if the chapter is already being aborted
        # or the target host is poisoned. _try_download_url itself also checks
        # this, but bailing out before tempfile creation saves needless I/O.
        if _chapter_cancelled():
            return None
        if stop_event.is_set():
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
                stop_event=stop_event,
            )
            if success:
                # Successfully downloaded to temp file
                log_debug(f"    [Parallel] Success: {os.path.basename(attempt_url)}")

                with success_lock:
                    if successful_temp_file[0] is None:
                        # We're the first successful download. Phase A: also
                        # capture this thread's response Content-Type so the
                        # post-loop sniff has a reliable fallback when magic
                        # bytes are ambiguous. Setting the stop_event under
                        # success_lock guarantees only one worker ever signals,
                        # and that the signal is visible before the lock release
                        # so any worker that just finished a chunk reads `set`
                        # on its next loop iteration.
                        successful_temp_file[0] = (temp_path, attempt_url, ct)
                        stop_event.set()
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
                    # Belt-and-suspenders: the winning try_variant has already
                    # set stop_event under success_lock, but set again here so
                    # we don't depend on that ordering. future.cancel() only
                    # works for queued (not-yet-started) tasks; running ones
                    # rely on stop_event polling inside _try_download_url.
                    stop_event.set()
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
    *,
    webp_source_is_lossy: bool = False,
) -> List[str]:
    """Saves a list of final PIL images to disk with format-aware encoding.

    Phase C (2026-05-07): historic behavior was always JPEG at the given
    quality (default 85). For CBZ that meant the image was lossy regardless
    of source format and the user's quality setting was the only knob. The
    new ``output_format`` argument lets the caller request a format that
    matches (and preserves the quality of) the source:

      - "auto": decide per-image from source_paths[i].format. WebP source
        → WebP-lossless **by default** (preserves natively-WebP sites like
        Atsumaru), or WebP lossy q85 when the caller sets
        ``webp_source_is_lossy=True`` (Phase H, see below); JPEG source →
        JPEG at ``quality`` (typically q≥95 from caller); PNG/GIF/other or
        unknown → PNG (lossless). Falls back to PNG when source_paths isn't
        provided or doesn't line up 1:1.
      - "webp_lossless": every output is WebP-lossless at method=4. The
        auto-mode default for WebP source when ``webp_source_is_lossy=False``.
        Callers can also pick this explicitly.
      - "webp_q85": every output is lossy WebP q85 method=2. The auto-mode
        choice for WebP source when ``webp_source_is_lossy=True``. Also
        usable as an explicit output_format. Routed through pyvips when
        available (~2x faster than PIL at the same settings); falls back
        to PIL when pyvips can't load.
      - "jpeg": legacy behavior, every output is JPEG at ``quality``.
      - "png": every output is PNG (lossless).

    The ``webp_source_is_lossy`` keyword-only hint tells auto-mode that any
    WebP source it probes is already a lossy q85 from our own
    --webtoon-recompress step (LineWebtoon-specific, see
    recompress_chapter_images_to_webp at ~line 2300). Default False so
    sites that ship native WebP (Atsumaru, etc.) get lossless preserve
    behavior unchanged. Cross-file: set at the CBZ caller around
    ~line 6890 to ``args.webtoon_recompress and handler.name == 'linewebtoon'``.

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

    Phase H (2026-05-16): user reported 65 m 20 s Processing for 6
    chapters with --webtoon-recompress on. Code trace pinpointed this
    function: the pre-Phase-H auto-mode mapped WEBP source →
    webp_lossless (method=4, lossless=True, quality=100), which encodes
    each 1500×3750 stitched page in ~2-3s at ~2.85 MB per page. With
    --webtoon-recompress the source WebPs are already lossy q85, so the
    lossless wrapper was wasting both wall time AND disk (the resulting
    CBZ is ~262 MB / chapter instead of ~30 MB at matched q85). Bench
    on 94 Eleceed Ch.380 pages at 6 parallel workers:
        pil-webp-lossless-m4 (BASELINE):  67.6 s   261.8 MB  SSIM 1.0
        pyvips-webp-q85-e2 (Phase H):      2.75 s   30.0 MB  SSIM 0.99415
            → 25x faster, 8.7x smaller, q85 indistinguishable on phone.
    The pyvips path is preferred when available; PIL fallback at q85 m2
    is still 16x over baseline. See bench/webtoon_encode_bench.py +
    bench/results.csv.

    SCOPING (2026-05-16 follow-up, per user): the q85 mapping fires ONLY
    when ``webp_source_is_lossy=True`` is passed. Sites that ship native
    lossless or near-lossless WebP (Atsumaru is the canonical case;
    MangaDex and others also serve WebP) keep the original
    "WEBP → webp_lossless" mapping so their CBZs aren't silently
    re-encoded at q85. The flag is set at the LineWebtoon + recompress
    call site (search for ``_webp_source_is_lossy`` in this file).

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
                # Phase H (2026-05-16, scoped follow-up): pick lossy q85 only
                # when the caller signals the source is already lossy (i.e.,
                # came from our own --webtoon-recompress step on LineWebtoon).
                # Default stays "webp_lossless" so natively-WebP sites like
                # Atsumaru, MangaDex, etc. don't get silently degraded —
                # their WebPs are at the publisher's chosen quality and
                # losslessly preserving them is the right call. The hint is
                # plumbed in at the CBZ caller (~line 6890).
                fmt = "webp_q85" if webp_source_is_lossy else "webp_lossless"
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
        elif fmt == "webp_q85":
            ext = ".webp"
            # Phase H: lossy WebP q85, libwebp method/effort=2. Sweet spot
            # from bench/results.csv 2026-05-15 — 16x faster than the old
            # lossless path on PIL alone, 25x with pyvips. SSIM 0.99415 vs
            # lossless reference, indistinguishable on phone-screen viewing
            # per existing --webtoon-recompress quality contract.
            # _save_one dispatches to pyvips when available; the kwargs
            # below are also valid for PIL.Image.save when pyvips isn't.
            save_kwargs = dict(format="WebP", quality=85, method=2)
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
        # to a clean RGB/L mode before save. Same for JPEG and the new
        # Phase H webp_q85 path (which also routes through libwebp).
        if fmt_local.startswith("webp") and src_img.mode not in ("RGB", "L"):
            src_img = src_img.convert("RGB")
        elif fmt_local == "jpeg" and src_img.mode not in ("RGB", "L"):
            src_img = src_img.convert("RGB")

        # Phase H (2026-05-16): the lossy webp_q85 path prefers pyvips when
        # the optional dep loaded at import time. libvips streams rows of
        # the PIL buffer through libwebp without building an intermediate
        # RGB array, ~2x faster than PIL.Image.save at the same q/method
        # settings on 1500x3750 stitched LineWebtoon pages (bench/results.csv
        # 2026-05-15: pil-webp-q85-m2=4.24s vs pyvips-webp-q85-e2=2.75s on
        # 94 pages parallel x6). Output bytes are size-identical and SSIM-
        # identical because both call the same libwebp encoder. Fallback
        # is the legacy PIL path so users on platforms without a pyvips
        # wheel (uncommon: pyvips-binary wheels cover win/mac/linux x86_64
        # and arm64) still get the 16x A1 win.
        if fmt_local == "webp_q85" and _HAS_PYVIPS:
            # PIL.Image.tobytes("raw","RGB") returns row-major R0G0B0R1G1B1...
            # which is exactly what pyvips.Image.new_from_memory wants for
            # bands=3 format="uchar". No numpy import needed on the hot path.
            w, h = src_img.size
            buf = src_img.tobytes()
            v = pyvips.Image.new_from_memory(buf, w, h, 3, "uchar")
            v.webpsave(
                str(dst),
                Q=save_kw["quality"],
                effort=save_kw["method"],
            )
        else:
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
            entry[3].startswith("webp") for entry in plan
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
# WebP recompression (LINE Webtoon, opt-in via --webtoon-recompress)
# -----------------------------------------------------------

def recompress_chapter_images_to_webp(
    raw_paths: List[str],
    quality: int,
    method: int,
) -> List[str]:
    """Re-encode source images to lossy WebP at the given quality and
    encoder method, replacing files in place.

    Used by the LINE Webtoon pipeline (handler.name == 'linewebtoon') to
    convert webtoons.com's CDN-served archival images (~200-700 KB/page
    JPEG at q98-99, or historically multi-MB lossless PNG) to storage-
    optimized WebP (~80-130 KB/page) before the CBZ fast-path or EPUB
    packager consumes raw_paths.

    Per-file behavior:
      - Already .webp: passthrough (already in target format). Skipping
        avoids generation loss from decode → re-encode. webtoons.com
        doesn't serve .webp today, but the check is cheap and future-
        proofs the path.
      - Anything else PIL can decode (.png, .jpg/.jpeg, .gif, .avif via
        plugin, etc.): decoded, saved as WebP at quality + method, then
        the original is os.remove'd. Returns the new .webp path in the
        same slot of the output list.
      - Decode failures (corrupt files, UnidentifiedImageError,
        DecompressionBomb): logged via log_verbose, original path kept.
        Caller still packages the chapter with that page's original
        bytes.

    Eligibility design (2026-05-16): the older `_is_recompress_eligible`
    predicate gated JPEG re-encoding behind an estimated-quality + BPP
    threshold, intended to skip already-small dialogue panels. That
    skip created a downstream bug: when 5+/83 pages in a chapter stayed
    .jpg (some panels below BPP threshold), the slow-path 1:N
    `all(.webp)` check at save_final_images failed and the final output
    fell back to lossless PNG, producing 130 MB CBZs on Eleceed Ch 25+.
    The user's call: "compress everything." Simplicity wins; tiny JPEGs
    re-encode to similar-sized WebPs with negligible generation loss
    (q98 JPEG → q85 WebP on already-tiny content), and the all-WebP
    invariant downstream is preserved.

    Concurrency: cpu // 2 workers, matching save_final_images (lines
    ~2360). libwebp releases the GIL during native encode so per-image
    saves run in parallel.

    Atomicity: <base>.webp is written first; only on success do we
    os.remove the original. A crash mid-conversion can leave .webp next
    to the old ext — the next inline retry wipes the chapter dir
    (_process_chapter_strict ~line 5518) so leftover state is self-healing.

    Cross-file: read by _process_chapter_impl ~line 6800 (between the
    --keep-images copytree and the processed_tdir setup); the result
    becomes raw_image_paths for the rest of the chapter pipeline. CBZ
    fast-path (~line 6900) and EPUB chapter_content build honor per-file
    extensions via os.path.splitext, so .webp arcnames flow through.
    Resume gating: webtoon_recompress / _quality / _method are in
    _RESUME_GATING_DESTS — changing any invalidates the on-disk images.
    """
    if not raw_paths:
        return list(raw_paths)

    def _convert_one(entry: Tuple[int, str]) -> Tuple[int, str]:
        idx, src = entry
        # Already in target format: leave alone to avoid generation loss
        # from a decode → re-encode round trip. webtoons.com doesn't serve
        # .webp today, but other sites (Atsumaru, MangaDex) do — relevant
        # if --webtoon-recompress is ever applied outside LineWebtoon.
        if os.path.splitext(src)[1].lower() == ".webp":
            log_debug(
                f"    Recompress skip (already .webp): {os.path.basename(src)}"
            )
            return idx, src

        base, _ = os.path.splitext(src)
        dst = base + ".webp"
        try:
            with Image.open(src) as im:
                # WebP encode wants RGB or L; webtoon pages are color so
                # almost always already RGB, but be defensive against PNG
                # palette / RGBA modes from older site formats.
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                im.save(
                    dst,
                    format="WebP",
                    quality=quality,
                    method=method,
                )
        except Exception as e:
            # PIL.UnidentifiedImageError ⊂ OSError; DecompressionBombError ⊂
            # Exception. Broad catch keeps a single corrupt page from
            # aborting the whole chapter — we keep the original instead.
            log_verbose(
                f"  Warning: WebP recompress failed for "
                f"{os.path.basename(src)}: {e}. Keeping original."
            )
            try:
                os.remove(dst)
            except OSError:
                pass
            return idx, src

        try:
            os.remove(src)
        except OSError as e:
            # The new .webp is fine; we just couldn't delete the old file
            # (locked by AV, OneDrive sync, etc). Return the .webp anyway;
            # the leftover original gets wiped on the next chapter-dir reset.
            log_debug(
                f"    Recompress: kept original alongside webp ({e}): "
                f"{os.path.basename(src)}"
            )
        return idx, dst

    cpu = os.cpu_count() or 4
    half_cores = max(1, cpu // 2)
    workers = max(1, min(half_cores, len(raw_paths)))

    out: List[Optional[str]] = [None] * len(raw_paths)
    with _cpu_guard("recompress_webp"):
        if workers == 1 or len(raw_paths) == 1:
            for entry in enumerate(raw_paths):
                idx, dst = _convert_one(entry)
                out[idx] = dst
                if idx % 8 == 0:
                    _hb("cpu", f"recompress {idx+1}/{len(raw_paths)}")
        else:
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="webp-recompress"
            ) as pool:
                # pool.map preserves submission order; consumed iteratively
                # so memory stays bounded. _hb every 8 keeps the per-chapter
                # watchdog satisfied on long chapters (60+ pages).
                for idx, dst in pool.map(
                    _convert_one, list(enumerate(raw_paths))
                ):
                    out[idx] = dst
                    if idx % 8 == 0:
                        _hb("cpu", f"recompress {idx+1}/{len(raw_paths)}")

    # No None entries possible by construction (every _convert_one returns
    # idx, str); the cast keeps mypy quiet.
    return [p for p in out if p is not None]


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


# -----------------------------------------------------------
# Komikku-mode helpers (--komikku, see Komikku LocalSource format)
# -----------------------------------------------------------
# These three helpers exist exclusively to produce Komikku/Mihon/Tachiyomi-
# compatible per-chapter CBZ output. They are zero-cost on non-Komikku runs
# (never called). Cross-file coupling: the call sites live in main() inside
# the cbz-cache creation block (grep 'cached_cbz_path = os.path.join') and
# the --keep-chapters destination filename build (grep 'ch_suffix = f"Ch ').
# See plan file at C:\Users\legoc\.claude\plans\we-will-be-making-idempotent-parnas.md

def _komikku_status_to_digit(status_str: Optional[str]) -> str:
    """Map a per-handler status string to Komikku's 0-6 enum digit (string).

    Spec §6.1: details.json `status` field is a JSON string containing one
    digit. 0=Unknown, 1=Ongoing, 2=Completed, 3=Licensed, 4=Publishing
    finished, 5=Cancelled, 6=On hiatus. Komikku tolerates out-of-range
    integers by collapsing to 0.

    Source-side strings are normalized to lowercase. Variants per
    sites/*.py: "Ongoing"/"Releasing" → 1, "Completed"/"Finished" → 2,
    "Licensed" → 3, "Cancelled" → 5, "Hiatus"/"On Hiatus" → 6. Unknown
    or empty falls through to "0".
    """
    if not status_str:
        return "0"
    s = str(status_str).strip().lower()
    if s in ("ongoing", "releasing", "publishing", "active"):
        return "1"
    if s in ("completed", "finished", "complete", "ended"):
        return "2"
    if s == "licensed":
        return "3"
    if s in ("publishing finished", "publishingfinished"):
        return "4"
    if s in ("cancelled", "canceled", "dropped", "discontinued"):
        return "5"
    if s in ("hiatus", "on hiatus", "on_hiatus", "onhiatus", "paused"):
        return "6"
    # MangaPark `uploadStatus` returns "pending" for scheduled-but-not-yet-
    # started series. Explicit Unknown — don't let a future refactor lump it
    # in with "ongoing" by accident.
    if s == "pending":
        return "0"
    return "0"


def build_per_chapter_comic_info_xml(
    series_title: str,
    chapter_title: Optional[str],
    chapter_num: Any,
    volume: Optional[Any],
    scanlator: Optional[str],
    web_url: Optional[str],
    uploaded_epoch: Optional[Any],
    comic_info: Dict,
    publishers: List[str],
    lang: str,
    page_count: int,
) -> str:
    """Per-chapter ComicInfo.xml string for Komikku-mode CBZs.

    Spec §6.2: Komikku v1.13.5+ reads <Number>/<Title>/<Translator>/<Series>
    from a ComicInfo.xml at the archive root and these OVERRIDE filename-
    derived metadata. <Year>/<Month>/<Day> compose to SChapter.date_upload
    (falls back to file mtime if absent — so we omit the tags when the
    handler didn't supply an upload epoch).

    Empty/None fields are omitted entirely (not emitted as empty tags) so
    Komikku falls back cleanly to ChapterRecognition where we don't have
    data — vs. an empty <Title/> which would suppress the regex.
    """
    def escape(s):
        return xml.sax.saxutils.escape(str(s)) if s not in (None, "") else ""

    authors = ", ".join(comic_info.get("authors", []) or [])
    artists = ", ".join(comic_info.get("artists", []) or [])
    publisher = ", ".join(publishers or [])
    description = comic_info.get("desc", "") or ""

    tags: List[str] = []
    for key in ("genres", "theme", "format"):
        if comic_info.get(key):
            tags.extend(comic_info[key])
    # Sorted for stable XML output (test/diff friendly); set() dedupes.
    genre = ", ".join(sorted(set(tags))) if tags else ""

    # Year/Month/Day from uploaded epoch. Many handlers store 0 as a
    # sentinel for "unknown" (e.g. mangafire.py); treat 0 as missing.
    # Use time.gmtime (UTC) — Komikku doesn't care about TZ; mtime
    # fallback would itself be filesystem-local anyway.
    year = month = day = None
    if uploaded_epoch:
        try:
            epoch_int = int(uploaded_epoch)
            if epoch_int > 0:
                tm = time.gmtime(epoch_int)
                year, month, day = tm.tm_year, tm.tm_mon, tm.tm_mday
        except (TypeError, ValueError, OverflowError, OSError):
            # OSError on Windows for epochs outside 1970-3000 range.
            pass

    # Render <Number> as plain decimal — strip trailing ".0" on integers.
    num_str = ""
    if chapter_num not in (None, ""):
        try:
            nf = float(chapter_num)
            num_str = str(int(nf)) if nf.is_integer() else f"{nf:g}"
        except (TypeError, ValueError):
            num_str = str(chapter_num)

    vol_str = ""
    if volume not in (None, "", 0, "0"):
        try:
            vf = float(volume)
            vol_str = str(int(vf)) if vf.is_integer() else f"{vf:g}"
        except (TypeError, ValueError):
            vol_str = str(volume)

    lines: List[str] = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
        f'  <Series>{escape(series_title)}</Series>',
    ]
    if chapter_title:
        lines.append(f'  <Title>{escape(chapter_title)}</Title>')
    if num_str:
        lines.append(f'  <Number>{escape(num_str)}</Number>')
    if vol_str:
        lines.append(f'  <Volume>{escape(vol_str)}</Volume>')
    if description:
        lines.append(f'  <Summary>{escape(description)}</Summary>')
    if authors:
        lines.append(f'  <Writer>{escape(authors)}</Writer>')
    if artists:
        lines.append(f'  <Penciller>{escape(artists)}</Penciller>')
    if publisher:
        lines.append(f'  <Publisher>{escape(publisher)}</Publisher>')
    if scanlator:
        lines.append(f'  <Translator>{escape(scanlator)}</Translator>')
    if genre:
        lines.append(f'  <Genre>{escape(genre)}</Genre>')
    if web_url:
        lines.append(f'  <Web>{escape(web_url)}</Web>')
    if lang:
        lines.append(f'  <LanguageISO>{escape(lang)}</LanguageISO>')
    if year is not None:
        lines.append(f'  <Year>{year}</Year>')
        lines.append(f'  <Month>{month}</Month>')
        lines.append(f'  <Day>{day}</Day>')
    lines.append(f'  <PageCount>{int(page_count) if page_count else 0}</PageCount>')
    lines.append('</ComicInfo>')
    return "\n".join(lines) + "\n"


def _komikku_chapter_filename(chap: Any, vol: Any, title: Optional[str]) -> str:
    """Build a Komikku-friendly chapter filename: Vol.{vv} Ch.{ccc} - {title}.cbz.

    Spec §8 + recommendation 7: this layout is parsed correctly by Mihon's
    ChapterRecognition regex set (vol/ch prefixes stripped, decimal numbers
    preserved) AND remains readable inside any file manager. ComicInfo.xml
    <Number>/<Title> override these on read, so the filename is mostly
    cosmetic — but it should still be parseable for cross-reader fallback.

    - Volume: omit the `Vol.{vv} ` prefix when missing/0; otherwise 2-digit
      zero-pad on integer parts.
    - Chapter: integer part zero-pad to 3 digits (5 → "005", 100 → "100",
      1200 → "1200"). Decimal portion kept verbatim (5.5 → "005.5") —
      crucially NOT subjected to format_chap_for_filename's '~' substitution
      which would break ChapterRecognition's decimal parser.
    - Title: appended only if non-empty AND distinct from the chap label
      itself (some handlers set ch["title"] == str(ch["chap"])).
    """
    # Chapter number → padded label
    chap_label = ""
    try:
        cf = float(chap)
        int_part = int(cf)
        if cf.is_integer():
            chap_label = f"{int_part:03d}" if int_part < 1000 else str(int_part)
        else:
            # Strip Python's float repr trailing noise: 5.5 → "5.5", 12.1 → "12.1".
            # Format with %g then split, in case repr gives 5.500000000000001
            # (rare but real on some platforms).
            formatted = f"{cf:g}"  # e.g. "5.5", "12.1", "5"
            if "." in formatted:
                int_token, frac_token = formatted.split(".", 1)
                int_for_pad = abs(int(int_token))
                int_str = (
                    f"{int_for_pad:03d}" if int_for_pad < 1000 else str(int_for_pad)
                )
                if int_token.startswith("-"):
                    int_str = "-" + int_str
                chap_label = f"{int_str}.{frac_token}"
            else:
                chap_label = (
                    f"{int_part:03d}" if int_part < 1000 else str(int_part)
                )
    except (TypeError, ValueError):
        # Non-numeric chap (e.g. "Prologue", "Extra"). Sanitize for filesystem
        # safety but keep the original token — ChapterRecognition will fail
        # to extract a number and Komikku will sort the chapter to the bottom
        # (chapter_number = -1.0), which is correct for non-numeric chapters.
        chap_label = _sanitize_folder_component(str(chap or "")) or "000"

    # Volume → padded prefix or empty
    vol_prefix = ""
    if vol not in (None, "", 0, "0"):
        try:
            vf = float(vol)
            vint = int(vf)
            if vf.is_integer():
                vol_prefix = f"Vol.{vint:02d} "
            else:
                vol_prefix = f"Vol.{vf:g} "
        except (TypeError, ValueError):
            v_sanitized = _sanitize_folder_component(str(vol))
            if v_sanitized:
                vol_prefix = f"Vol.{v_sanitized} "

    # Title suffix → "" or " - {title}"
    title_suffix = ""
    if title:
        t_raw = str(title).strip()
        # Skip when the title duplicates the chap number in any obvious form.
        # Compare against str(chap), the padded label, and the bare-int form.
        try:
            bare_int = str(int(float(chap)))
        except (TypeError, ValueError):
            bare_int = ""
        skip_set = {str(chap or "").strip(), chap_label, bare_int}
        if t_raw and t_raw not in skip_set:
            t_clean = _sanitize_folder_component(t_raw)
            if t_clean and t_clean not in skip_set:
                title_suffix = f" - {t_clean}"

    return f"{vol_prefix}Ch.{chap_label}{title_suffix}.cbz"


def build_cbz(
    slices: List[str],
    out_path: str,
    title: str,
    comic_info: Dict,
    publishers: List[str],
    lang: str,
    chapter_comic_info_xml: Optional[str] = None,
):
    """Builds a CBZ file from a list of image slices with metadata.

    chapter_comic_info_xml: when provided, used in place of the series-level
    ComicInfo.xml that build_comic_info_xml would generate. Used by the
    legacy --keep-chapters fallback path in --komikku mode to inject the
    per-chapter ComicInfo.xml (the cbz_cache fast-path embeds the same XML
    at cache-creation time; this is the slow-path equivalent for pre-Phase-D
    resumes where chapter_content carries 'image' entries instead of
    'cbz_cache' entries).
    """
    xml_content = chapter_comic_info_xml or build_comic_info_xml(
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

    # Clean up temp directory with retry logic for file handle issues.
    # On Windows, AV scanners (Defender, etc.) often hold a read handle to
    # files we just wrote for 500ms-2s after the writer closes. The previous
    # 100ms single retry was below that window — leftovers like
    # `temp_epub_<hid>/` would silently accumulate across runs (search the
    # `.gitignore` for the matching pattern). Backoff covers the AV window
    # in the common case; if all retries still fail we surface a warning so
    # the user knows there's stray cleanup to do, instead of failing silent.
    _cleanup_attempts = (0.0, 0.25, 0.5, 1.0)
    for _delay in _cleanup_attempts:
        if _delay:
            time.sleep(_delay)
        try:
            shutil.rmtree(temp_dir)
        except FileNotFoundError:
            break  # already gone — success
        except OSError:
            continue
        else:
            break
    else:
        # All retries exhausted. Try ignore_errors as a final attempt and log
        # whatever's left so the user can clear it manually.
        shutil.rmtree(temp_dir, ignore_errors=True)
        if os.path.exists(temp_dir):
            log_verbose(
                f"  Warning: could not remove EPUB temp dir {temp_dir!r} "
                f"after {len(_cleanup_attempts)} retries — leftover files "
                f"need manual cleanup. EPUB itself was saved successfully."
            )

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
    out_dir = getattr(args, "output_dir", DEFAULT_OUTPUT_DIR)
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
        active_out_dir = getattr(args, "epub_dir", None) or out_dir
        os.makedirs(active_out_dir, exist_ok=True)
        final_path = os.path.join(active_out_dir, f"{part_filename}.epub")
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


# ──────────────────────────────────────────────────────────────────
# Resume parameter persistence
# ──────────────────────────────────────────────────────────────────
# Single source of truth for what gets written to tmp_<hid>/run_params.json
# and what determines whether a tmp folder is resume-compatible.
#
# Mental model:
# - Every argparse dest auto-saves to run_params.json by default,
#   EXCEPT those listed in _RESUME_TRANSIENT_DESTS (mode flags,
#   one-shot inputs, orchestrator-only knobs). New flags are
#   preserved across resume without any list-maintenance.
# - The _RESUME_GATING_DESTS subset (image-affecting params) is
#   hashed via gating_hash(); the hash is saved alongside the dict.
#   A resume is allowed iff the saved hash matches the current
#   run's hash — meaning the on-disk images are compatible with
#   the current invocation's image-pipeline parameters.
# - Restored values are setattr'd onto args; cached tunables are
#   then re-seeded via _apply_runtime_tunables(args) so the
#   runtime honors the JSON values (not argparse defaults).
# - _validate_resume_categories runs once at startup to catch
#   typos in the dest sets and category overlaps.
#
# Cross-file: read by main()'s resume-check + save block (search
# `current_params = get_resumable_params`) and the
# --restore-parameters block (search `if args.restore_parameters:`).
# UI-source/electron/downloader.js builds the resume CLI as just
# ["--restore-parameters", "--format", fmt, "--verbose", url] — so
# every per-download setting must come from the JSON, not the CLI.

# Dests that GATE resume compatibility. If any of these changes
# between the original run and the current invocation, the on-disk
# images are invalidated and the tmp folder is wiped. These are all
# the params that affect the actual image data: dimensions, quality,
# chapter selection, group filter, processing on/off.
#
# width/aspect_ratio are saved as their RESOLVED (post-format-default)
# values, not raw args.width / args.aspect_ratio (which may be None).
# The resolution happens at line ~4280 in main() before save.
_RESUME_GATING_DESTS = frozenset({
    "width", "aspect_ratio",
    "quality", "scaling", "chapters", "group",
    "mix_by_upvote", "no_group_fallback", "no_partials",
    "download_volumes", "collapse_splits", "no_processing",
    # Phase 1 (2026-05-11): LINE Webtoon WebP recompression. Changing any
    # of these between runs invalidates the on-disk images because the
    # conversion deletes the original PNG/JPEG bytes. See
    # recompress_chapter_images_to_webp() and the call site near line 5559.
    "webtoon_recompress",
    "webtoon_recompress_quality",
    "webtoon_recompress_method",
    # Komikku-mode (Komikku LocalSource format, 2026-05-12): the cbz_cache CBZ at
    # processed_tdir/{n}.cbz either contains the per-chapter ComicInfo.xml
    # or doesn't, depending on Komikku-mode at create time. Flipping the
    # toggle between runs must invalidate the cache so resumed chapters
    # don't end up half-Komikku. See the cbz-cache creation block (grep
    # 'cached_cbz_path = os.path.join') for where this matters.
    "komikku",
})

# Dests that must NEVER be persisted to run_params.json. Every other
# dest is saved by default — adding a flag here is the explicit
# opt-out. Categorize by why it's transient.
#
# Adding a new CLI flag? You do NOT need to update this set for it
# to be saved/restored on --restore-parameters. Only add a dest
# here if the flag is one-shot (mode/search/orchestrator) or
# explicitly re-applied from the new CLI invocation on resume.
_RESUME_TRANSIENT_DESTS = frozenset({
    # Provided per-invocation; URL also lives in run_meta.json.
    "comic_url",
    # Format/epub_layout are intentionally re-overrideable on resume —
    # the restore block captures the new --format / --epub-layout from
    # the resume CLI and re-applies them after the setattr loop. Saving
    # them would be moot.
    "format", "epub_layout",
    # The resume flag itself.
    "restore_parameters",
    # Logging level — per-invocation choice.
    "verbose", "debug",
    # Pure --search mode flags. The original run resolved a query to a
    # URL via search; the resume CLI passes that URL directly, so
    # re-entering search mode would be both wrong and a validation
    # error (URL + --search are mutually exclusive). search_json is
    # output-mode plumbing for --search alone.
    #
    # NOTE: seeded_only / search_language / search_parallelism /
    # search_timeout / search_min_match are NOT here — they ALSO drive
    # find_alternatives_for_direct_url during a regular --multi-source
    # download (aio_search_cli.py ~line 654), so they must persist on
    # resume. Classifying them as transient hid the user's
    # --seeded-only preference on resume and triggered an unfiltered
    # 297-site search instead of the seeded ~26-site subset.
    "search", "auto_pick", "search_json",
    # One-shot mode/input flags. multi_source_prefetched is a
    # path to a per-spawn cache JSON (UI writes a fresh file before
    # each search-initiated download); on resume we want the alts
    # rediscovered against current site state, so this stays
    # transient and the multi-source path re-runs the lookup.
    "multi_source_prefetched", "list_chapters", "build_final_file",
    "prompt_urls",
    # Multi-URL orchestrator — children get these re-passed by the
    # parent via child_base; not meaningful for the single-URL resume
    # path. net_min_gap is also orchestrator-gated (only consumed when
    # AIO_COORD_ENABLED is set, which the Electron UI never sets).
    "jobs", "coord_dir", "net_min_gap",
    "job_stall_timeout", "job_hard_timeout", "job_retries",
    "job_spawn_gap",
})


_SAVED_PARAMS_FILE = "download_params.json"


def _save_download_params(out_dir: str, url: str, args, title: str) -> None:
    """Persist legacy update settings alongside the canonical .aio_series.json."""
    data = {
        "url": url,
        "title": title,
        "site": getattr(args, "site", None),
        "format": getattr(args, "format", "epub"),
        "language": getattr(args, "language", "en"),
        "width": getattr(args, "width", None),
        "aspect_ratio": getattr(args, "aspect_ratio", None),
        "quality": getattr(args, "quality", 85),
        "scaling": getattr(args, "scaling", 100),
        "cookies": getattr(args, "cookies", "") or "",
        "group": getattr(args, "group", []) or [],
        "split": getattr(args, "split", None),
        "mix_by_upvote": bool(getattr(args, "mix_by_upvote", False)),
        "no_group_fallback": bool(getattr(args, "no_group_fallback", False)),
        "no_partials": bool(getattr(args, "no_partials", False)),
        "download_volumes": bool(getattr(args, "download_volumes", False)),
        "keep_chapters": bool(getattr(args, "keep_chapters", False)),
        "keep_images": bool(getattr(args, "keep_images", False)),
        "no_final_file": bool(getattr(args, "no_final_file", False)),
        "no_processing": bool(getattr(args, "no_processing", False)),
        "no_cleanup": bool(getattr(args, "no_cleanup", False)),
        "verbose": bool(getattr(args, "verbose", False)),
        "debug": bool(getattr(args, "debug", False)),
    }
    if getattr(args, "format", None) == "epub":
        data["epub_layout"] = getattr(args, "epub_layout", "vertical")
    path = os.path.join(out_dir, _SAVED_PARAMS_FILE)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        log_verbose(f"  Saved download parameters to {path}")
    except Exception as exc:
        print(f"  Warning: could not save download parameters: {exc}")


def _append_saved_update_options(child_cmd: List[str], params: Dict[str, Any]) -> None:
    """Replay saved per-series options for --update-all child runs."""
    if params.get("site"):
        child_cmd.extend(["--site", str(params["site"])])
    if params.get("epub_layout"):
        child_cmd.extend(["--epub-layout", str(params["epub_layout"])])
    if params.get("width"):
        child_cmd.extend(["--width", str(params["width"])])
    if params.get("aspect_ratio"):
        child_cmd.extend(["--aspect-ratio", str(params["aspect_ratio"])])
    if not params.get("no_processing"):
        child_cmd.extend(["--quality", str(params.get("quality", 85))])
        child_cmd.extend(["--scaling", str(params.get("scaling", 100))])
    if params.get("cookies"):
        child_cmd.extend(["--cookies", str(params["cookies"])])
    groups = params.get("group") or []
    if isinstance(groups, str):
        groups = [groups]
    for group in groups:
        child_cmd.extend(["--group", str(group)])
    if params.get("split"):
        child_cmd.extend(["--split", str(params["split"])])
    for key, flag in (
        ("mix_by_upvote", "--mix-by-upvote"),
        ("no_group_fallback", "--no-group-fallback"),
        ("no_partials", "--no-partials"),
        ("download_volumes", "--download-volumes"),
        ("keep_images", "--keep-images"),
        ("no_final_file", "--no-final-file"),
        ("no_processing", "--no-processing"),
        ("no_cleanup", "--no-cleanup"),
        ("verbose", "--verbose"),
        ("debug", "--debug"),
    ):
        if params.get(key):
            child_cmd.append(flag)


def get_resumable_params(args, parser, calculated_width, calculated_aspect_ratio):
    """Auto-derives the dict of CLI flags to persist for resume.

    Walks the argparse parser and returns every dest NOT in
    _RESUME_TRANSIENT_DESTS. width/aspect_ratio are overridden with
    the resolved (post-format-default) values rather than raw args.*
    (which may be None when the user didn't pass --width).

    The returned dict's _RESUME_GATING_DESTS subset is what
    gating_hash() consumes for resume-compatibility checking; the
    rest of the dict is restored on --restore-parameters but does
    not affect resume invalidation.
    """
    skip = _RESUME_TRANSIENT_DESTS | {"help"}
    out = {
        action.dest: getattr(args, action.dest)
        for action in parser._actions
        if action.dest not in skip and hasattr(args, action.dest)
    }
    # Override raw width/aspect_ratio with resolved values so the
    # gating-hash compare is stable: a fresh-run "width=None →
    # format-default 2000" matches a resumed-run "width=2000 (from
    # JSON) → still 2000" cleanly.
    out["width"] = calculated_width
    out["aspect_ratio"] = calculated_aspect_ratio
    # Persist the user-intent flags so --restore-parameters preserves them.
    # Without these the resume invocation (which doesn't re-pass --width)
    # would set args.width via setattr from `out["width"]` above, then a
    # subsequent `args._user_set_width = args.width is not None` would
    # falsely flip to True, defeating the CBZ fast-path. Cross-file: the
    # original computation lives near parse_args() (grep '_user_set_width ='),
    # and the fast-path read site is aio-dl.py:cbz_fast_path (~line 6900).
    out["_user_set_width"] = bool(getattr(args, "_user_set_width", False))
    out["_user_set_aspect_ratio"] = bool(getattr(args, "_user_set_aspect_ratio", False))
    out["_user_set_quality"] = bool(getattr(args, "_user_set_quality", False))
    return out


def gating_hash(params):
    """Stable hash of the resume-gating subset of params.

    Two runs with the same gating_hash are guaranteed to produce
    byte-equivalent on-disk images (same resize, same quality, same
    chapter/group filter, same processing-on-or-off). Mismatch =
    saved tmp folder incompatible with the current invocation, must
    be wiped before fresh download.

    sha256 over a sorted-key JSON dump for stability across Python
    versions and dict-insertion-order changes. List fields (just
    `group` today) are order-sensitive — matches the previous
    field-by-field compare semantics (priority order matters).
    """
    gating = {k: params.get(k) for k in sorted(_RESUME_GATING_DESTS)}
    blob = json.dumps(gating, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _validate_resume_categories(parser):
    """Startup sanity check: dests in the category sets must exist
    in the parser, and the two sets must not overlap.

    Catches typos (renaming an add_argument's dest without updating
    the set) and accidental overlap (a dest classified as both
    gating and transient is incoherent — gating params must persist
    to compute the hash, transient ones must not).

    Does NOT enforce that every dest is categorized — by design,
    every uncategorized dest is auto-saved as a non-gating param.
    That is the robustness goal: new flags can't be silently
    dropped, only intentionally opted out.
    """
    all_dests = {a.dest for a in parser._actions if a.dest != "help"}
    typos_g = _RESUME_GATING_DESTS - all_dests
    typos_t = _RESUME_TRANSIENT_DESTS - all_dests
    overlap = _RESUME_GATING_DESTS & _RESUME_TRANSIENT_DESTS
    errors = []
    if typos_g:
        errors.append(
            f"_RESUME_GATING_DESTS contains dests not in parser: {sorted(typos_g)}"
        )
    if typos_t:
        errors.append(
            f"_RESUME_TRANSIENT_DESTS contains dests not in parser: {sorted(typos_t)}"
        )
    if overlap:
        errors.append(
            f"dests classified as both gating and transient: {sorted(overlap)}"
        )
    if errors:
        raise RuntimeError(
            "Resume category inconsistency (fix _RESUME_*_DESTS in aio-dl.py):\n  "
            + "\n  ".join(errors)
        )


def _apply_runtime_tunables(args):
    """Snapshot per-process tunables from args into module globals.

    Called twice in main():
      - Once after parse_args (initial seed from argparse defaults / CLI args)
      - Again from inside the --restore-parameters block (re-seed
        after JSON values have been setattr'd onto args)

    Globals here are read at runtime by make_request, dl_image,
    _process_chapter (watchdog), _process_chapter_strict (inline retry),
    and _vrf_prefetch_worker_loop (batch sizing). Without the second
    call after restore, these caches still hold the argparse defaults
    from the resume invocation's CLI (which only re-passes
    --restore-parameters --format … --verbose <url>) — so the user's
    original tunables are silently ignored at runtime even when they
    were correctly saved into run_params.json.

    Cross-file: when adding a new tunable that's snapshotted into a
    module global (rather than read directly from args at runtime),
    add it here. get_resumable_params auto-includes the dest in
    run_params.json by default; this function is what makes the
    restored value actually take effect at runtime.
    """
    globals()["_HTTP_TIMEOUT"] = float(getattr(args, "http_timeout", 30.0))
    globals()["_HTTP_MAX_RETRIES"] = int(getattr(args, "http_max_retries", 6))
    globals()["_HTTP_BACKOFF_BASE"] = float(getattr(args, "http_backoff_base", 1.0))
    globals()["_HTTP_BACKOFF_CAP"] = float(getattr(args, "http_backoff_cap", 45.0))
    globals()["_CHAPTER_DEADLINE"] = float(getattr(args, "chapter_deadline_seconds", 90.0))
    globals()["_CHAPTER_HOST_POISON"] = int(getattr(args, "chapter_host_poison_threshold", 5))
    globals()["_INLINE_CHAPTER_RETRIES"] = int(getattr(args, "inline_chapter_retries", 2))
    globals()["_INLINE_CHAPTER_BACKOFF"] = float(getattr(args, "inline_chapter_backoff", 30.0))
    _vrf_async_batch_state["parallel_count"] = max(
        1, int(getattr(args, "mangafire_vrf_parallel", 1) or 1)
    )
    # --no-fast-download: force-disable curl_cffi fast path globally. Read
    # by both the main-path SUPPORTS_FAST_DOWNLOAD gate AND the prefetch
    # worker's SUPPORTS_FAST_DOWNLOAD gate. Module-global so the prefetch
    # worker (which doesn't have args in scope as a closure capture) can
    # read it without parameter threading.
    globals()["_NO_FAST_DOWNLOAD"] = bool(getattr(args, "no_fast_download", False))
    # --image-prefetch-parallel: how many concurrent image-prefetch worker
    # threads. Same module-global pattern as _vrf_async_batch_state since
    # the workers are spawned by _ensure_image_prefetch_workers without
    # args in scope. Re-applied on --restore-parameters via the second
    # call to _apply_runtime_tunables.
    globals()["_image_prefetch_parallel"] = max(
        1, int(getattr(args, "image_prefetch_parallel", 2) or 2)
    )
    # Phase D (2026-05-13): clear per-run concurrency caps so each run
    # starts with fresh CDN trust. NOTE: This is called twice in main()
    # (once after parse_args, once on --restore-parameters); the second
    # call also resets caps, which is correct — resume = fresh run on
    # the CDN's side too.
    _reset_host_concurrency_caps()


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
# VRF prefetch – overlap next-N-chapter VRF capture with
#                current-chapter image downloads.
#
# How it works:
#   - After get_chapter_images(ch_N) returns the image URLs,
#     _start_vrf_prefetch_chain() pushes the next `depth` upcoming
#     chapters onto _vrf_prefetch_queue.
#   - A single long-lived daemon worker drains the queue, calling
#     vrf_gen.ensure_vrf() for each. Tokens land in the shared
#     _vrf_cache; later foreground calls hit cache instantly.
#   - With depth=4 (the new default), by the time the main loop
#     reaches chapter N+1, VRFs for N+2..N+4 are already captured
#     or in flight, fully hiding VRF cost behind image download.
#
# Pre-2026-05-09: depth was effectively 1 (single thread, single
# chapter ahead). Bumped to 4 + queue worker so capture starts
# earlier; this matters most for runs where image download time
# is short relative to VRF capture time (i.e. small chapters).
#
# Why this is safe with --jobs:
#   --jobs spawns separate subprocesses, each with their own
#   Playwright browser and VRF generator. This prefetch only
#   touches the current process's VRF generator.
# -----------------------------------------------------------
import queue as _stdlib_queue

_vrf_prefetch_queue: "_stdlib_queue.Queue[Optional[Tuple[str, str, str]]]" = _stdlib_queue.Queue()
_vrf_prefetch_worker: Optional[threading.Thread] = None
# Tracks paths already submitted (or completed) so we don't enqueue the
# same chapter VRF capture twice when overlapping prefetch windows include
# it. Cleared at process exit; growth is bounded by the chapter count.
_vrf_prefetch_seen: set = set()
_vrf_prefetch_lock = threading.Lock()
# Opt-in async batch capture instance, lazily created on first parallel
# prefetch with --mangafire-vrf-parallel > 1. None when sequential mode.
_vrf_async_batch_state: Dict[str, Any] = {"capturer": None, "parallel_count": 1}


def _vrf_prefetch_worker_loop() -> None:
    """Drain _vrf_prefetch_queue serially. Sequential mode uses sync
    ensure_vrf (proven, ~1.5-2s/chapter). Parallel mode (parallel_count>1)
    batches up to N items and submits to AsyncBatchVRFCapture for
    concurrent multi-page capture. Both flow tokens into the shared
    _vrf_cache, so subsequent ensure_vrf calls hit cache instantly."""
    try:
        from sites.mangafire_vrf_simple import get_vrf_generator
    except Exception:
        return  # No VRF backend; idle worker.

    parallel_count = max(1, int(_vrf_async_batch_state.get("parallel_count", 1) or 1))

    while True:
        item = _vrf_prefetch_queue.get()
        if item is None:  # Shutdown sentinel
            return

        # Drain up to parallel_count-1 additional items so we batch when
        # parallel mode is active. In sequential mode batch_items has 1.
        batch_items = [item]
        if parallel_count > 1:
            try:
                while len(batch_items) < parallel_count:
                    batch_items.append(_vrf_prefetch_queue.get_nowait())
            except _stdlib_queue.Empty:
                pass

        if parallel_count > 1 and len(batch_items) > 1:
            # Async batch path. Lazy-import to avoid loading async machinery
            # for users who never opt in.
            capturer = _vrf_async_batch_state.get("capturer")
            if capturer is None:
                try:
                    from sites.mangafire_vrf_async_batch import AsyncBatchVRFCapture
                    capturer = AsyncBatchVRFCapture()
                    _vrf_async_batch_state["capturer"] = capturer
                except Exception as exc:
                    log_verbose(
                        f"  [VRF Prefetch] AsyncBatch init failed; falling back "
                        f"to sequential for this batch: {exc}"
                    )
                    capturer = None
            if capturer is not None:
                try:
                    capturer.submit_batch(
                        [(cid, curl) for (cid, curl, _label) in batch_items],
                        parallel=parallel_count,
                    )
                    log_verbose(
                        f"  [VRF Prefetch] async-batch captured "
                        f"{len(batch_items)} VRFs (parallel={parallel_count})"
                    )
                    for _, _, label in batch_items:
                        log_verbose(f"  [VRF Prefetch] (async) chapter {label} ready")
                    [_vrf_prefetch_queue.task_done() for _ in batch_items]
                    continue
                except Exception as exc:
                    # Async batch failed — fall through to sequential.
                    log_verbose(
                        f"  [VRF Prefetch] async-batch failed ({exc}); "
                        "retrying sequentially"
                    )

        # Sequential path (default, also fallback when async fails).
        for chapter_id, chapter_url, chap_label in batch_items:
            ajax_path = f"/ajax/read/chapter/{chapter_id}"
            try:
                vrf_gen = get_vrf_generator()
                vrf_gen.ensure_vrf(ajax_path, page_url=chapter_url, init_url=chapter_url)
                log_verbose(f"  [VRF Prefetch] Cached VRF for chapter {chap_label}")
            except Exception as exc:
                # Not a problem — the foreground path will capture it.
                log_verbose(
                    f"  [VRF Prefetch] Failed for chapter {chap_label} "
                    f"(will retry normally): {exc}"
                )
            finally:
                _vrf_prefetch_queue.task_done()


def _ensure_vrf_prefetch_worker() -> None:
    """Lazy-start the worker on first chain push. Daemon thread, exits
    automatically on process shutdown."""
    global _vrf_prefetch_worker
    with _vrf_prefetch_lock:
        if _vrf_prefetch_worker is not None and _vrf_prefetch_worker.is_alive():
            return
        _vrf_prefetch_worker = threading.Thread(
            target=_vrf_prefetch_worker_loop,
            daemon=True,
            name="VRF-Prefetch-Queue",
        )
        _vrf_prefetch_worker.start()


def _start_vrf_prefetch_chain(
    upcoming: List[Dict[str, Any]], handler, depth: int
) -> None:
    """Push the next `depth` chapters' VRF capture jobs onto the prefetch
    queue. No-op for non-MangaFire handlers, missing chapter URLs, or
    already-queued chapters. depth=0 disables prefetch entirely.

    Replaces the pre-2026-05-09 _start_vrf_prefetch (single chapter
    ahead). The queue worker drains in order; parallel mode (controlled
    via _vrf_async_batch_state['parallel_count']) batches into multi-
    page async capture inside the worker.
    """
    if depth <= 0 or not upcoming:
        return
    if getattr(handler, "name", "") != "mangafire":
        return

    pushed = 0
    with _vrf_prefetch_lock:
        for ch in upcoming[:depth]:
            chapter_id = ch.get("hid")
            chapter_url = ch.get("url")
            if not chapter_id or not chapter_url:
                continue
            ajax_path = f"/ajax/read/chapter/{chapter_id}"
            if ajax_path in _vrf_prefetch_seen:
                continue
            _vrf_prefetch_seen.add(ajax_path)
            chap_label = str(ch.get("chap", "?"))
            _vrf_prefetch_queue.put((chapter_id, chapter_url, chap_label))
            pushed += 1

    if pushed:
        _ensure_vrf_prefetch_worker()


# Backward-compat shim: existing call sites elsewhere may still reference
# _start_vrf_prefetch (single chapter). Kept as a thin wrapper. Remove
# once all call sites migrate to _start_vrf_prefetch_chain.
def _start_vrf_prefetch(next_chapter: Optional[Dict], handler) -> None:
    if next_chapter is None:
        return
    _start_vrf_prefetch_chain([next_chapter], handler, depth=1)


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
# Phase B (2026-05-13): replaced single-in-flight thread with a
# queue+worker-pool pattern mirroring _vrf_prefetch_*. Multiple chapters
# can now download in parallel (controlled by --image-prefetch-parallel),
# and the queue depth (--image-prefetch-depth) lets us push ahead
# multiple chapters at the chain-fire site. Preserves the filesystem-
# mediated coordination contract: .download_prefetched marker on full
# success, rm_tree(target_tdir) on partial failure.


@dataclass
class _ImgPrefetchJob:
    """One queued image-prefetch task. Carries everything the worker needs
    to download a chapter's images independently — no shared in-memory
    state with main beyond the per-chapter Event used for consume-wait."""
    next_chapter: Dict[str, Any]
    target_tdir: str
    scraper: Any
    handler: Any
    image_workers: int
    fast_concurrency: int
    chap_label: str


# Bounded so a runaway depth value can't OOM the process. depth check at
# the fire site keeps this far below the cap in normal operation; the cap
# is purely defensive.
_image_prefetch_queue: "_stdlib_queue.Queue[Optional[_ImgPrefetchJob]]" = (
    _stdlib_queue.Queue(maxsize=16)
)
_image_prefetch_workers: List[threading.Thread] = []
_image_prefetch_seen: set = set()                       # dedupe: chap_label
_image_prefetch_done: Dict[str, threading.Event] = {}   # chap_label -> Event
_image_prefetch_lock = threading.Lock()                 # guards _seen/_done/_workers
# Set by _apply_runtime_tunables from --image-prefetch-parallel (default 2).
_image_prefetch_parallel: int = 2


def _image_prefetch_worker_loop() -> None:
    """Dequeue prefetch jobs forever. Daemon thread; exits with the
    process. Each iteration runs the same body the old _worker closure
    did, with one diff at the end: setting _image_prefetch_done[chap].set()
    so _consume_image_prefetch can unblock.

    Multiple workers may run this loop concurrently (one Python thread
    each). The queue handles synchronization; each chapter is processed
    by exactly one worker."""
    while True:
        job = _image_prefetch_queue.get()
        if job is None:  # Shutdown sentinel
            return
        try:
            _run_image_prefetch_job(job)
        finally:
            # Always signal completion (success or failure) so the main
            # thread's _consume_image_prefetch doesn't deadlock.
            evt = _image_prefetch_done.get(job.chap_label)
            if evt is not None:
                evt.set()
            _image_prefetch_queue.task_done()


def _run_image_prefetch_job(job: _ImgPrefetchJob) -> None:
    """The body of one prefetch job. Lifted verbatim from the old
    inline _worker closure in _start_image_prefetch (pre-2026-05-13)
    so the success/failure marker contract is unchanged.

    On full success: writes .download_prefetched marker into target_tdir.
    On partial failure: wipes target_tdir entirely so main's foreground
    download starts from a clean slate (no half-populated state)."""
    next_chapter = job.next_chapter
    target_tdir = job.target_tdir
    scraper = job.scraper
    handler = job.handler
    image_workers = job.image_workers
    fast_concurrency = job.fast_concurrency
    chap_label = job.chap_label
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
        # curl_cffi async path runs concurrently inside this daemon
        # thread (asyncio.run() spins up its own event loop here).
        # Handlers without SUPPORTS_FAST_DOWNLOAD (or globally disabled
        # via --no-fast-download) fall through to ThreadPoolExecutor.
        failed = 0
        if (
            getattr(handler, "SUPPORTS_FAST_DOWNLOAD", False)
            and not globals().get("_NO_FAST_DOWNLOAD", False)
        ):
            fast_conc = max(1, int(fast_concurrency))
            # Phase D: apply per-host concurrency cap on prefetch too —
            # if the foreground path dialed concurrency down for this
            # CDN, prefetch should respect the same limit.
            fast_conc = _effective_concurrency(
                urlparse(download_tasks[0][1]).netloc if download_tasks else "",
                fast_conc,
            )
            fast_timeout = float(globals().get("_HTTP_TIMEOUT", 30.0))
            # No host-poison feedback here: prefetch is best-effort. If
            # it fails, the partial-failure branch below wipes tdir and
            # main's foreground download retries with full instrumentation.
            fast_results = handler.fast_download_images(
                download_tasks,
                concurrency=fast_conc,
                timeout=fast_timeout,
                # Forward cookies (e.g. age-gate cookies for LineWebtoon)
                # so prefetch can fetch the same content the foreground
                # path would. Base impl filters to host-relevant cookies.
                scraper=scraper,
            )
            failed = sum(1 for _, p in fast_results if not p)
        else:
            workers = max(1, min(image_workers, len(download_tasks)))
            # Phase D: cap prefetch ThreadPool concurrency too.
            workers = max(1, _effective_concurrency(
                urlparse(download_tasks[0][1]).netloc if download_tasks else "",
                workers,
            ))
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


def _ensure_image_prefetch_workers() -> None:
    """Lazy-spawn up to _image_prefetch_parallel daemon worker threads.
    Mirrors _ensure_vrf_prefetch_worker but with N workers instead of 1.
    Called from _start_image_prefetch on first enqueue; idempotent on
    subsequent calls (re-checks alive count and tops up if any died)."""
    with _image_prefetch_lock:
        # Filter alive workers; replace died ones up to the target count.
        alive = [t for t in _image_prefetch_workers if t.is_alive()]
        _image_prefetch_workers[:] = alive
        target = max(1, int(globals().get("_image_prefetch_parallel", 2)))
        while len(_image_prefetch_workers) < target:
            t = threading.Thread(
                target=_image_prefetch_worker_loop,
                daemon=True,
                name=f"Img-Prefetch-Worker-{len(_image_prefetch_workers) + 1}",
            )
            t.start()
            _image_prefetch_workers.append(t)


def _start_image_prefetch(
    next_chapter: Optional[Dict[str, Any]],
    target_tdir: str,
    scraper,
    handler,
    image_workers: int,
    fast_concurrency: int = 8,
) -> None:
    """Enqueue an image-prefetch job for next_chapter. Signature preserved
    from pre-Phase-B for callsite back-compat; internals are now queue+pool.

    Honors split-cluster collapse: if next_chapter carries `_merged_parts`
    (set by group_chapters_for_download for rule-5 clusters), the worker
    fetches each part's media_entries and concatenates them in order —
    matching what _process_chapter_impl would have done synchronously.

    `fast_concurrency` bounds the curl_cffi async semaphore when the
    handler has SUPPORTS_FAST_DOWNLOAD=True. Other handlers (or runs with
    --no-fast-download) use image_workers via ThreadPoolExecutor.

    Dedupe: if a job for chap_label is already in flight or queued, skip
    re-enqueue. _consume_image_prefetch joins the existing job's done event.
    """
    if next_chapter is None:
        return
    chap_label = str(next_chapter.get("chap", "?"))
    if not chap_label or chap_label == "?":
        return

    with _image_prefetch_lock:
        if chap_label in _image_prefetch_seen:
            # Already queued or in-flight for this chapter — second enqueue
            # is a no-op. The existing job's Event will fire normally on
            # completion; _consume_image_prefetch joins that Event.
            return
        _image_prefetch_seen.add(chap_label)
        _image_prefetch_done[chap_label] = threading.Event()

    job = _ImgPrefetchJob(
        next_chapter=next_chapter,
        target_tdir=target_tdir,
        scraper=scraper,
        handler=handler,
        image_workers=image_workers,
        fast_concurrency=fast_concurrency,
        chap_label=chap_label,
    )
    try:
        _image_prefetch_queue.put(job, block=False)
    except _stdlib_queue.Full:
        # Queue is at maxsize (defensive cap, depth check usually keeps
        # us well below). Drop the job — main's foreground download
        # handles the chapter normally. Clear the seen/done state so a
        # future enqueue attempt isn't blocked.
        log_verbose(
            f"  [Img Prefetch] Queue full, dropping Ch {chap_label} "
            f"(main will download normally)"
        )
        with _image_prefetch_lock:
            _image_prefetch_seen.discard(chap_label)
            _image_prefetch_done.pop(chap_label, None)
        return
    _ensure_image_prefetch_workers()


def _start_image_prefetch_chain(
    upcoming: List[Dict[str, Any]],
    main_tmp_dir: str,
    scraper,
    handler,
    image_workers: int,
    fast_concurrency: int,
    depth: int,
    no_processing: bool,
) -> None:
    """Push the next `depth` chapters' image-prefetch jobs onto the queue.
    No-op when depth <= 0 (user opted out). Mirror of
    _start_vrf_prefetch_chain for the image-download side.

    Skips chapters whose tdir already has a success marker (processed-
    complete or download-complete depending on --no-processing) — same
    cache check the single-shot fire site used to do at line ~6253.

    The dedupe in _start_image_prefetch handles overlapping windows
    (e.g. chain fired at ch 10 queues 11+12; ch 11's chain fires 12+13
    and 12 is already in the queue from ch 10's chain — skipped)."""
    if depth <= 0 or not upcoming:
        return
    pushed = 0
    for ch in upcoming[:depth]:
        chap = ch.get("chap")
        if chap is None:
            continue
        target_tdir = os.path.join(main_tmp_dir, f"ch_{chap}")
        marker_name = ".download_complete" if no_processing else ".processed_complete"
        if os.path.exists(os.path.join(target_tdir, marker_name)):
            # Already fully processed (resume case); no point prefetching
            # bytes whose ch_dir is already marker-complete.
            continue
        _start_image_prefetch(
            ch, target_tdir, scraper, handler, image_workers, fast_concurrency
        )
        pushed += 1
    if pushed > 0:
        log_verbose(
            f"  [Img Prefetch] chain pushed {pushed}/{len(upcoming[:depth])} chapter(s)"
        )


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
    """Block until the prefetch for chap_label finishes (or no prefetch was
    queued for this chapter). Idempotent — call at the start of each
    chapter's processing. The prefetch's outputs are picked up via the
    .download_prefetched marker (filesystem-mediated, not in-memory).

    With the queue+pool refactor, the chapter may be IN-FLIGHT (a worker
    is processing it) or QUEUED (waiting for a worker). The per-chap Event
    handles both cases: it gets set when the worker finishes processing,
    regardless of which worker took the job."""
    chap_label = str(chap_label)
    with _image_prefetch_lock:
        evt = _image_prefetch_done.get(chap_label)
    if evt is None:
        # No prefetch was queued for this chapter — nothing to consume.
        return
    if not evt.is_set():
        log_verbose(f"  Waiting for image prefetch of Ch {chap_label}...")
        # 300s timeout matches pre-Phase-B behavior. If a queue backlog
        # pushes us beyond this, foreground download falls through and
        # main re-does the work — same recovery semantics as a single-
        # thread prefetch hanging.
        evt.wait(timeout=300.0)
    with _image_prefetch_lock:
        # Clean up per-chap state. Keep _image_prefetch_seen entry so a
        # second enqueue for the same chapter (e.g. inline retry) is
        # a no-op — main's foreground download path handles retries.
        _image_prefetch_done.pop(chap_label, None)


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
        default=os.getenv("AIO_COORD_DIR", os.path.join("manga", ".aio_coord")),
        help="Directory for cross-process coordination state/locks (default: manga/.aio_coord).",
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

    # ── Fast-download knobs (2026-05-13: generalized from MangaFire-only) ──
    # These apply to any handler with SUPPORTS_FAST_DOWNLOAD=True (currently
    # mangafire and linewebtoon; see sites/base.py:fast_download_images for
    # the implementation and sites/*.py for opt-ins). Resume-transient — see
    # _RESUME_TRANSIENT_DESTS for why these don't invalidate on-disk images.
    p.add_argument(
        "--image-concurrency",
        type=int,
        default=8,
        help="Concurrent in-flight image fetches for handlers with fast "
             "download support (curl_cffi async + HTTP/2; default: 8). "
             "Bench (MangaFire 83-page chapter): 8 hits ~5 MB/s near network "
             "ceiling; >12 is diminishing returns. Auto-dials down on CDN "
             "errors via per-host concurrency cap (independent of "
             "--chapter-host-poison-threshold which is the hard chapter "
             "abort). Drop to 3 or 4 if a CDN starts rate-limiting (rare on "
             "cookieless edge caches, but defensive).",
    )
    p.add_argument(
        "--image-prefetch-depth",
        type=int,
        default=2,
        help="How many chapters ahead to keep queued for image prefetch "
             "(default: 2). Set to 0 to disable image prefetch entirely. "
             "Higher depths help when main-loop processing is FAST relative "
             "to network download (e.g. CBZ fast-path on LINE Webtoon) — "
             "more chapters in the queue mean less waiting between chapters. "
             "Doesn't help when processing is the bottleneck (PDF assembly, "
             "WebP recompression with high effort settings).",
    )
    p.add_argument(
        "--image-prefetch-parallel",
        type=int,
        default=2,
        help="Concurrent prefetch worker threads (default: 2). Each worker "
             "processes one chapter at a time from the queue; parallel=2 "
             "means up to 2 chapters in flight simultaneously while the main "
             "thread processes a third. parallel=1 is the legacy single-in-"
             "flight behavior. Higher values = more concurrent host "
             "connections (parallel × image-concurrency). Webtoons.com and "
             "MangaFire's edge cache tolerate 2 well in practice.",
    )
    p.add_argument(
        "--no-fast-download",
        action="store_true",
        help="Force-disable the curl_cffi fast download path on all handlers; "
             "use the legacy ThreadPoolExecutor + dl_image cloudscraper path. "
             "Escape hatch for curl_cffi version regressions or weird CDN-vs-"
             "impersonation issues. Equivalent to setting "
             "SUPPORTS_FAST_DOWNLOAD=False per-handler, but global.",
    )

    # Deprecated 2026-05-13 — superseded by --image-concurrency (generalized
    # from MangaFire-only). Still accepted for back-compat; routed onto
    # args.image_concurrency in main() with a DeprecationWarning emitted
    # there. Hidden from --help via argparse.SUPPRESS so it doesn't pollute
    # the visible CLI surface for new scripts.
    p.add_argument(
        "--mangafire-image-concurrency",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )

    # MangaFire-specific VRF capture knobs (2026-05-09). VRF is MangaFire's
    # proprietary token-capture problem; no other handler has it. Kept under
    # the --mangafire- namespace because the flags don't apply to anyone else.
    p.add_argument(
        "--mangafire-vrf-prefetch-depth",
        type=int,
        default=4,
        help="How many chapters ahead to keep VRF prefetch queued for MangaFire "
             "(default 4). Sequential capture (~1.5-2s/chapter) but starts "
             "earlier so VRF capture overlaps fully with image download — by "
             "the time chapter N+1 begins, VRFs for N+2..N+4 are already "
             "cached or in flight. 0 disables prefetch entirely.",
    )
    p.add_argument(
        "--mangafire-vrf-parallel",
        type=int,
        default=1,
        help="Opt-in: capture N MangaFire chapter VRFs concurrently via "
             "Patchright async (default 1 = sequential, current behavior). "
             "4 is bench-confirmed working with single-IP storage_state and "
             "5.2x speedup over sequential, but can trigger CF rate-limiting "
             "on some sessions. Recommended only for large downloads (50+ "
             "chapters) on a stable IP; falls back to sequential transparently "
             "on detected throttle (homepage redirect).",
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
        "--enable-ml-rating",
        action="store_true",
        default=os.environ.get("AIO_ENABLE_ML_RATING", "").lower() in (
            "1", "true", "yes", "on",
        ),
        help="Enable ML-based image quality scoring (torch + pyiqa + "
             "torchmetrics). Off by default. When enabled, the search "
             "ranker uses T2 (CLIP-IQA + NIQE) and T3 (paired DISTS) on "
             "top of T1 (pixel-level numpy/PIL scoring), giving ~3-8%% "
             "more accurate rankings on borderline matches. Cost: torch "
             "import adds ~2-5 s of process startup, model weights are "
             "~150 MB on first-use download, and per-source probe gains "
             "~2-5 s. The default-off rationale (2026-05-20): torch's "
             "Windows import path calls platform.machine() which Python "
             "3.13 implements via WMI — that can stall indefinitely on "
             "hosts with a degraded WMI service, hanging --search forever. "
             "Honors AIO_ENABLE_ML_RATING=1 env var so power users can "
             "set the preference once.",
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
        "--collapse-splits",
        dest="collapse_splits",
        action="store_true",
        default=False,
        help="Enable split-fragment + cross-source-duplicate chapter collapse. "
             "Default OFF (2026-05-27 opt-in flip — see "
             "~/.claude/plans/ultrathink-mangafire-and-some-flickering-sparkle.md). "
             "When ON, the following are merged or dropped: "
             "(a) sequential X.1/X.2/X.3 splits with no integer X "
             "(MangaDex-style upload fragments) → merged into one chapter X; "
             "(b) integer X + scattered decimals like {X, X.1, X.5} → X kept, "
             ".5 kept as side story, .1 dropped as fragment; "
             "(c) integer X + single fragment-shaped decimal "
             "(.1/.2/.3/.4) with no peer source confirming it → "
             "the decimal is dropped as a duplicate upload of X. "
             "Decimals at .5 or higher, peer-confirmed decimals of any "
             "shape, chapter 0 / prologues, and source-only integer "
             "chapters are ALWAYS kept. The cross-source duplicate "
             "signal only fires under --multi-source / "
             "--multi-source-prefetched; direct-URL runs fall back to "
             "the in-source heuristics (current Rule 3a / 3b / 6 "
             "behavior with no consensus refinement).",
    )
    # Hidden deprecated alias — old --no-collapse-splits is now a no-op
    # (the new default IS "no collapse"), but keep parsing it so any script
    # pinned to the old flag continues to launch. Suppressed from --help to
    # avoid confusing new users with both flag forms.
    p.add_argument(
        "--no-collapse-splits",
        dest="collapse_splits",
        action="store_false",
        help=argparse.SUPPRESS,
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
        "--no-group-fallback",
        action="store_true",
        help="When --group is set, skip chapters missing all preferred groups instead of falling back to another group.",
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
        "--download-volumes",
        action="store_true",
        help="Download volumes instead of chapters, if the selected site exposes volume listing.",
    )
    p.add_argument(
        "--scan-library",
        action="store_true",
        help="Scan --output-dir and print library state as JSON, then exit.",
    )
    p.add_argument(
        "--update-all",
        action="store_true",
        help="Scan --output-dir for saved series metadata and download new chapters for each series.",
    )
    p.add_argument(
        "--serve",
        action="store_true",
        help="Start the FastAPI REST server instead of downloading.",
    )
    p.add_argument("--api-host", default="127.0.0.1")
    p.add_argument("--api-port", type=int, default=8000)


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
        "-o",
        "--output-dir",
        type=str,
        default=None,
        help="Directory to place library outputs. Priority: this flag, AIO_OUTPUT_DIR, aio_config.json, then 'manga'.",
    )
    p.add_argument(
        "--epub-dir",
        type=str,
        default=None,
        help="Optional override directory specifically for EPUB outputs.",
    )
    p.add_argument(
        "--temp-dir",
        type=str,
        default=None,
        help="Optional override base directory for temporary processing folders.",
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
        "--save-params",
        action="store_true",
        help="Save legacy download_params.json settings alongside .aio_series.json so future --update-all runs can replay detailed options.",
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
    # ── LINE Webtoon WebP recompression (Phase 1, 2026-05-11) ──
    # Targets webtoons.com's archival-quality PNG output (~2-3 MB/page on
    # newer Eleceed / TBATE chapters) which produced 40+ GB libraries. WebP
    # q85 lands at ~80 KB/page on color webtoon content (visually equivalent
    # on phone-screen viewing per user research) → ~95% size reduction. Only
    # applies when handler.name == 'linewebtoon' AND --format is cbz/epub.
    # See recompress_chapter_images_to_webp() and the call site near
    # _process_chapter_impl's --keep-images block (grep 'webtoon_recompress').
    p.add_argument(
        "--webtoon-recompress",
        action="store_true",
        help="LINE Webtoon ONLY (handler.name == 'linewebtoon'): re-encode "
             "lossless PNG pages to lossy WebP at --webtoon-recompress-quality "
             "before packaging. JPEG-source chapters are skipped (webtoons.com "
             "only serves JPEG for low-popularity series — those pages are "
             "already small and recompressing them is generation-loss). "
             "Targets the ~45GB per-series problem from the CDN's PNG output "
             "on popular series; q85 typically lands at ~5-7%% of the "
             "original library size with results indistinguishable from "
             "source on phone-screen viewing of color webtoons. Requires "
             "--format cbz or epub (PDF would re-encode the WebP as "
             "FlateDecode and INCREASE size). Files are converted in place "
             "in the tmp directory; original PNG bytes are not preserved on "
             "disk (use --keep-images to retain a copy in "
             "<out>/images/Chapter_<n>/). Changing the quality or method "
             "between runs invalidates the tmp folder via resume gating.",
    )
    p.add_argument(
        "--webtoon-recompress-quality",
        type=int,
        default=85,
        choices=range(1, 101),
        metavar="[1-100]",
        help="WebP quality factor for --webtoon-recompress (default: 85). "
             "85 = storage-optimized, indistinguishable from source on "
             "phone-screen viewing of color webtoons. 90 = archival-safe "
             "with insurance margin against zoom/high-DPI artifacts "
             "(~60%% larger files). Values above 95 produce diminishing "
             "returns for color content.",
    )
    p.add_argument(
        "--webtoon-recompress-method",
        type=int,
        default=4,
        choices=range(0, 7),
        metavar="[0-6]",
        help="libwebp encoder effort for --webtoon-recompress (default: 4). "
             "0 = fastest/largest, 6 = slowest/smallest. method=4 matches "
             "the existing WebP-lossless pool default (~line 2119); "
             "method=6 trades ~2-3x encode time for ~5%% smaller files — "
             "sensible for overnight bulk runs on a desktop, not phone CPUs.",
    )
    # ── Komikku-compatible per-chapter CBZ output (Komikku LocalSource format) ──
    # Writes per-chapter CBZs with per-chapter ComicInfo.xml, plus
    # cover.jpg and details.json at the series-folder root, matching the
    # Mihon/Tachiyomi/Komikku LocalSource on-disk format. Force-coerces
    # --format cbz --keep-chapters --no-final-file. Output path stays at
    # <workingDir>/manga/<Series>/ — sync to your phone's <Komikku-SAF>/
    # local/ via SyncThing/rclone/manual copy. Helpers + spec details:
    # grep '_komikku_status_to_digit\|build_per_chapter_comic_info_xml\|
    # _komikku_chapter_filename'.
    p.add_argument(
        "--komikku",
        action="store_true",
        help="Write Komikku/Mihon/Tachiyomi-compatible per-chapter CBZs. "
             "Each chapter gets its own ComicInfo.xml (with <Series>, "
             "<Number>, <Translator>, <Web>, <Year>/<Month>/<Day>), plus "
             "cover.jpg and details.json (status/genres/authors as a "
             "JSON object) at the series-folder root. Auto-coerces "
             "--format cbz --keep-chapters --no-final-file. Output "
             "stays at <workingDir>/manga/<Series>/ — sync into "
             "<Komikku-SAF-root>/local/ yourself.",
    )
    args = p.parse_args()
    _validate_resume_categories(p)  # fail-fast on dest typos / category overlap
    args.output_dir = resolve_output_dir(getattr(args, "output_dir", None))

    # --mangafire-image-concurrency deprecation routing. Back-compat shim
    # for scripts that still use the pre-2026-05-13 MangaFire-only flag —
    # the add_argument earlier in main() declares it with
    # help=argparse.SUPPRESS so it's hidden from --help. Routes the value
    # onto args.image_concurrency BEFORE _apply_runtime_tunables or any
    # fast-download consumer reads it, so the rename is transparent.
    if getattr(args, "mangafire_image_concurrency", None) is not None:
        args.image_concurrency = args.mangafire_image_concurrency
        import warnings
        warnings.warn(
            "--mangafire-image-concurrency is deprecated; use --image-concurrency",
            DeprecationWarning,
        )

    if args.serve:
        try:
            import uvicorn
        except Exception as exc:
            p.error(f"--serve requires uvicorn/fastapi dependencies: {exc}")
        uvicorn.run("api:app", host=args.api_host, port=args.api_port, reload=False)
        return

    if args.scan_library:
        from library_state import scan_library, to_jsonable

        print(json.dumps(to_jsonable(scan_library(args.output_dir)), indent=2))
        return

    if args.update_all:
        from library_state import scan_library

        scan_root = os.path.abspath(args.output_dir)
        entries = [entry for entry in scan_library(scan_root) if entry.get("url")]
        if not entries:
            sys.exit(f"No saved series metadata found in {scan_root}.")
        print(f"[*] Found {len(entries)} saved series in {scan_root}")
        child_procs = []
        for entry in entries:
            chapters_arg = entry.get("next_update") or "all"
            title = entry.get("name", "series")
            if chapters_arg == "all":
                print(f"  {title}: downloading all chapters")
            else:
                print(f"  {title}: resuming from chapter {chapters_arg[:-1]}")
            params = dict(entry.get("params") or {})
            child_cmd = [
                sys.executable,
                os.path.abspath(__file__),
                entry["url"],
                "--chapters",
                chapters_arg,
                "--format",
                params.get("format") or entry.get("format") or "epub",
                "--language",
                params.get("language") or entry.get("language") or "en",
                "--output-dir",
                scan_root,
                "--save-params",
                "--keep-chapters",
            ]
            _append_saved_update_options(child_cmd, params)
            child_procs.append((title, child_cmd))

        def _run_saved_update(title: str, cmd: List[str]) -> Tuple[str, int, str, str]:
            proc = subprocess.run(
                cmd,
                cwd=os.path.dirname(os.path.abspath(__file__)),
                capture_output=True,
                text=True,
            )
            return title, proc.returncode, proc.stdout, proc.stderr

        failed = []
        up_to_date = []
        jobs = max(1, int(getattr(args, "jobs", 1) or 1))
        if jobs > 1:
            print(f"[*] Running updates with up to {jobs} worker(s)...")
            with ThreadPoolExecutor(max_workers=jobs) as executor:
                futures = [
                    executor.submit(_run_saved_update, title, cmd)
                    for title, cmd in child_procs
                ]
                for future in as_completed(futures):
                    title, returncode, stdout, stderr = future.result()
                    print(f"\n{'=' * 60}")
                    print(f"Updating: {title}")
                    print(f"{'=' * 60}")
                    sys.stdout.write(stdout)
                    sys.stderr.write(stderr)
                    combined = stdout + stderr
                    if returncode != 0:
                        if "No chapters selected" in combined or "Filtered list down to 0 chapters" in combined:
                            up_to_date.append(title)
                            print("  Already up to date.")
                        else:
                            failed.append(title)
        else:
            for title, cmd in child_procs:
                print(f"\n{'=' * 60}")
                print(f"Updating: {title}")
                print(f"{'=' * 60}")
                _, returncode, stdout, stderr = _run_saved_update(title, cmd)
                sys.stdout.write(stdout)
                sys.stderr.write(stderr)
                combined = stdout + stderr
                if returncode != 0:
                    if "No chapters selected" in combined or "Filtered list down to 0 chapters" in combined:
                        up_to_date.append(title)
                        print("  Already up to date.")
                    else:
                        failed.append(title)
        print(f"\n{'=' * 60}")
        updated = len(child_procs) - len(failed) - len(up_to_date)
        print(f"Update complete: {updated} updated, {len(up_to_date)} up-to-date, {len(failed)} failed")
        if failed:
            print(f"Failed: {', '.join(failed)}")
            sys.exit(1)
        return

    # Phase B (2026-05-07) / Phase H follow-up (2026-05-16): snapshot which CLI
    # flags the user explicitly set on THIS invocation, BEFORE any later
    # mutations (--restore-parameters setattr loop, format-defaulting,
    # --komikku coercion) overwrite args.* with derived values. The CBZ
    # fast-path at ~line 6900 reads these booleans to detect "user wants the
    # wire bytes verbatim" vs "user asked for a transform." `--width` /
    # `--aspect-ratio` argparse-default to None so `is None` is the user-set
    # test; `--quality` defaults to 85 so we sniff sys.argv for it instead.
    #
    # Phase G4 (2026-05-08): --quality 100 means "highest quality, no
    # tradeoffs" — exactly what the fast-path provides. Treating it as a
    # transform-request would force CBZ into the legacy decode/recombine/
    # re-encode path, defeating the byte-preservation. The UI's Settings
    # quality slider defaults to 100, so without this guard EVERY
    # UI-spawned CBZ download fell into legacy. Only quality < 100 now
    # signals "user wants smaller/lossy."
    #
    # Position note (2026-05-16): this block USED to live after the
    # --restore-parameters setattr loop, which broke resume — restore
    # loaded calculated `width=1500` from JSON, then this assignment
    # flipped `_user_set_width` to True and disabled the fast-path for
    # every chapter on resume (Ch 25+ in Eleceed bulk download came out
    # as 130 MB lossless-PNG CBZs). Moved here so the user's CURRENT CLI
    # is captured first; the restore loop overrides from JSON when the
    # saved run actually had the flag set (get_resumable_params now
    # persists `_user_set_*` keys for this purpose).
    args._user_set_width = args.width is not None
    args._user_set_aspect_ratio = args.aspect_ratio is not None
    args._user_set_quality = (
        any(
            a == "--quality" or a.startswith("--quality=")
            for a in sys.argv[1:]
        )
        and args.quality < 100
    )

    # Generic CLI-user-set snapshot. Mirrors the _user_set_* booleans
    # above but covers EVERY argparse dest, not just the three with
    # fast-path heuristics. Consumed by the --restore-parameters loop
    # (~line 5523) so freshly-typed CLI overrides survive resume —
    # without this, `--restore-parameters --width 3000 URL` would have
    # the setattr loop silently restore the saved run's width=2000 and
    # the user got no log indication their override was discarded.
    # Built from p._actions's option_strings against sys.argv: each
    # `--flag` (or its `--flag=value` shorthand) maps back to its dest
    # via the same dest argparse uses for setattr. Positional args
    # (option_strings == []) are skipped because they are always
    # re-provided on the resume CLI and never appear in run_params.json
    # anyway.
    _user_set_dests: set = set()
    _opt_to_dest: Dict[str, str] = {}
    for _action in p._actions:
        for _opt in _action.option_strings:
            _opt_to_dest[_opt] = _action.dest
    for _tok in sys.argv[1:]:
        if not _tok.startswith("-"):
            continue
        _name = _tok.split("=", 1)[0]
        if _name in _opt_to_dest:
            _user_set_dests.add(_opt_to_dest[_name])
    args._user_set_dests = _user_set_dests

    # -----------------------------
    # Argument sanity checks / modes
    # -----------------------------

    # --komikku: silently coerce the three implementation flags that the
    # Komikku output layout requires. Runs BEFORE the "no_final_file requires
    # keep_chapters" check below so the implied keep_chapters=True satisfies
    # it. The explicit notice keeps the spawn-line behavior obvious in the
    # UI's LogPanel for users who toggle Komikku and then wonder why their
    # format selector was ignored. Cross-file: UI counterparts are
    # settings.defaults.komikku (SettingsTab.jsx) + form.komikku
    # (DownloadTab.jsx); both emit --komikku via downloader.js boolMap.
    if getattr(args, "komikku", False):
        coerced_bits: List[str] = []
        if args.format != "cbz":
            coerced_bits.append(f"--format cbz (was {args.format})")
            args.format = "cbz"
        if not args.keep_chapters:
            coerced_bits.append("--keep-chapters")
            args.keep_chapters = True
        if not args.no_final_file:
            coerced_bits.append("--no-final-file")
            args.no_final_file = True
        if coerced_bits:
            print(
                f"[Komikku] Forcing { ' '.join(coerced_bits) } for spec-"
                f"compliant per-chapter output."
            )
        else:
            print("[Komikku] Per-chapter CBZ output enabled (spec-compliant).")

    if args.no_final_file and (not args.keep_chapters):
        p.error("--no-final-file requires --keep-chapters.")

    # --webtoon-recompress compatibility checks. Run early so a multi-hour
    # download isn't started just to discover --format pdf would have made
    # the whole effort moot. The hard rejections cover combinations that
    # are strictly worse than not using the flag at all (PDF/FlateDecode
    # bloat, double-encode through Phase C save_final_images). Warnings
    # cover combinations that compose but lose extra quality (double-decode
    # paths) or defeat the disk-saving purpose (--keep-images).
    if getattr(args, "webtoon_recompress", False):
        if args.format == "pdf":
            p.error(
                "--webtoon-recompress is incompatible with --format pdf: "
                "PDF embeds JPEG via /DCTDecode but decodes WebP into "
                "uncompressed FlateDecode pixel data, which INCREASES file "
                "size. Use --format cbz (recommended) or --format epub."
            )
        if args.format == "none":
            p.error(
                "--webtoon-recompress requires --format cbz or epub. With "
                "--format none there is no archive file to write the "
                "converted pages into."
            )
        if getattr(args, "no_cbz_preserve_originals", False):
            p.error(
                "--webtoon-recompress is incompatible with "
                "--no-cbz-preserve-originals: the lossy WebP would be "
                "decoded and re-encoded again as WebP-lossless via Phase C "
                "auto-format, wrapping the lossy artifacts in a lossless "
                "container — strictly worse than either option alone."
            )
        # Warnings (not errors) for combinations that compose but produce
        # a double-encode loss. The user might know what they're doing.
        if args.width is not None:
            print(
                "  [!] --webtoon-recompress with --width forces the slow "
                "decode-resize-encode path; the output WebP will be "
                "re-encoded (twice-lossy). Consider dropping --width.",
                file=sys.stderr,
            )
        if args.aspect_ratio is not None:
            print(
                "  [!] --webtoon-recompress with --aspect-ratio forces the "
                "slow decode-resize-encode path (twice-lossy).",
                file=sys.stderr,
            )
        if args.scaling != 100:
            print(
                f"  [!] --webtoon-recompress with --scaling={args.scaling} "
                "forces the slow decode-resize-encode path (twice-lossy).",
                file=sys.stderr,
            )
        if args.keep_images:
            print(
                "  [i] --webtoon-recompress with --keep-images preserves "
                "the original PNG/JPEG downloads alongside the recompressed "
                "CBZ. Disable --keep-images to maximize disk savings.",
                file=sys.stderr,
            )
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

    # Seed module globals from args. Called again from inside the
    # --restore-parameters block (after setattr loop) so resumed runs
    # honor the user's saved tunables instead of the argparse defaults
    # the resume-CLI invocation would otherwise leave in place.
    _apply_runtime_tunables(args)

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
    # consensus_set for the refined collapse-splits Rule 2 / 3b / 6 drops at
    # group_chapters_for_download (2026-05-27). Populated alongside
    # _multi_source_alternatives from the same three carriers (auto-pick,
    # prefetched JSON, direct-URL discovery). None = no peer signal; the
    # group helper falls through to original in-source-only heuristics.
    _multi_source_consensus_set: Optional[Set[float]] = None

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
            _multi_source_consensus_set = _ms_state.get("consensus_set")
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

        coord_dir = os.getenv("AIO_COORD_DIR", "").strip() or getattr(args, "coord_dir", "") or os.path.join("manga", ".aio_coord")
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

    temp_dir_base = getattr(args, "temp_dir", None)
    if temp_dir_base:
        os.makedirs(temp_dir_base, exist_ok=True)
        main_tmp_dir = os.path.abspath(os.path.join(temp_dir_base, f"tmp_{hid}"))
    else:
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
                saved = json.load(f)
            if not isinstance(saved, dict):
                raise TypeError("run_params.json must be a JSON object")
            # New schema: {"gating_hash": ..., "params": {...}}.
            # Legacy schema (pre-rewrite): flat dict at top level.
            # See gating_hash() / get_resumable_params() in this file.
            if "gating_hash" in saved:
                restored_params = saved.get("params") or {}
            else:
                restored_params = saved

            # Update the args namespace with the restored parameters,
            # with two exclusions that preserve the user's CURRENT CLI:
            #
            #   1. Dests the user explicitly set on the resume CLI
            #      (tracked at parse_args time via args._user_set_dests).
            #      `--restore-parameters --width 3000 URL` should keep
            #      width=3000; the previous unconditional setattr loop
            #      silently restored the saved run's width and left
            #      no log indication the override was discarded.
            #
            #   2. `_user_set_*` sentinels themselves. get_resumable_params
            #      persists these alongside real values, but they describe
            #      THIS invocation's CLI intent (computed earlier from
            #      sys.argv) and must not be clobbered by the saved run's
            #      values. The fast-path heuristic at ~line 6892 reads
            #      _user_set_width/_user_set_aspect_ratio to decide whether
            #      to engage the CBZ wire-bytes path; flipping it from True
            #      to False mid-resume defeats the user's width override.
            _user_set_dests_resume: set = getattr(args, "_user_set_dests", set())
            _skipped_for_cli_override: List[str] = []
            for key, value in restored_params.items():
                if key.startswith("_user_set_"):
                    continue
                if key in _user_set_dests_resume:
                    _skipped_for_cli_override.append(key)
                    continue
                setattr(args, key, value)
            if _skipped_for_cli_override:
                _override_summary = ", ".join(
                    f"--{k.replace('_', '-')}={getattr(args, k, None)!r}"
                    for k in sorted(_skipped_for_cli_override)
                )
                print(
                    f"  [resume] Keeping fresh CLI override(s) over saved "
                    f"values: {_override_summary}"
                )

            # Crucially, apply the new format settings — these are
            # intentionally re-overrideable on resume. Redundant when the
            # user passed --format/--epub-layout on the resume CLI (the
            # _user_set_dests filter above would already have skipped them)
            # but harmless: the same value gets assigned to the same dest.
            args.format = new_format
            args.epub_layout = new_epub_layout

            # Re-seed module globals from the restored args. The initial
            # apply (right after parse_args) used argparse defaults for
            # any flag the user didn't pass on the resume CLI; now that
            # the JSON values have been setattr'd onto args, re-snapshot
            # so runtime-cached tunables (_HTTP_TIMEOUT, _CHAPTER_DEADLINE,
            # _vrf_async_batch_state, etc.) honor the user's original
            # choices instead of the argparse defaults.
            _apply_runtime_tunables(args)

            # Auto-derived: walk the restored params so newly-persisted
            # flags appear automatically. Underscore-prefixed entries
            # (`_user_set_*`) are internal fast-path sentinels — they
            # describe what the original CLI did, not user-meaningful
            # settings — so suppress them from the listing. Sorted for
            # determinism. See get_resumable_params() for what lands
            # in this dict; the print here is purely UX, not state.
            print("  Successfully restored parameters. The following settings will be used:")
            for _rk in sorted(restored_params.keys()):
                if _rk.startswith("_"):
                    continue
                _rv = restored_params[_rk]
                _rl = _rk.replace("_", " ").title()
                log_verbose(f"    - {_rl}: {_rv}")
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

    # Note: _user_set_width / _user_set_aspect_ratio / _user_set_quality
    # are computed earlier (right after parse_args) so they capture the
    # CURRENT invocation's CLI flags before --restore-parameters loads
    # calculated values from run_params.json. See the block tagged
    # "Position note (2026-05-16)" near parse_args for the full rationale.
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

    if getattr(args, "download_volumes", False):
        pool = handler.get_volumes(context, scraper, args.language, make_request)
        if not pool:
            sys.exit("This site handler does not expose volume listing.")
    else:
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
        and not getattr(args, "download_volumes", False)
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
                _ms_result = build_alternatives_from_prefetched(
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
                _ms_result = find_alternatives_for_direct_url(
                    primary_url=args.comic_url,
                    primary_handler=handler,
                    primary_context=context,
                    primary_chapters=pool,
                    args=args,
                    make_request=make_request,
                    record_rate_limit=_record_rate_limit,
                    on_status=lambda m: print(m, file=sys.stderr),
                )
            # New return shape (2026-05-27): both helpers now return a dict
            # with alts + consensus. Tolerate the legacy bare-dict shape too
            # in case some downstream call path bypassed the update.
            if isinstance(_ms_result, dict) and "alternatives_by_chap_num" in _ms_result:
                _ms_alts = _ms_result.get("alternatives_by_chap_num") or {}
                _ms_consensus = _ms_result.get("consensus_set")
            else:
                # Legacy / unexpected shape — treat the whole thing as the alts dict.
                _ms_alts = _ms_result if isinstance(_ms_result, dict) else {}
                _ms_consensus = None
            if _ms_alts:
                _multi_source_alternatives = _ms_alts
                _multi_source_consensus_set = _ms_consensus
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
    # create empty folders in manga/ just for checking.
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

    # Output goes into a per-title folder under ./manga (title, with hid only on collision)
    out_dir = allocate_series_output_dir(title, hid, root=args.output_dir)
    setattr(args, "output_dir", out_dir)
    epub_dir_base = getattr(args, "epub_dir", None)
    if epub_dir_base:
        epub_out_dir = allocate_series_output_dir(title, hid, root=epub_dir_base)
        setattr(args, "epub_dir", epub_out_dir)

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
            versions,
            args.group,
            args.mix_by_upvote,
            allow_group_fallback=not getattr(args, "no_group_fallback", False),
            log_debug_fn=log_debug,
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
    # Default flipped to False (opt-in) as of 2026-05-27. The new collapse
    # logic drops source-only .1/.2/.3/.4 fragments under --multi-source,
    # which is more aggressive than the old behavior — explicit user buy-in
    # is required to avoid surprise drops. Both --collapse-splits and the
    # deprecated --no-collapse-splits set this same dest.
    collapse_splits_enabled = bool(getattr(args, "collapse_splits", False))
    # consensus_set is sourced from whichever multi-source path populated it
    # (auto-pick search, prefetched JSON, or direct-URL discovery). None when
    # no peer data is available — group_chapters_for_download then falls
    # through to the original in-source-only Rule 2 / 3b / 6 behavior.
    # When non-None, Rule 2's lone source-only .1 fragment gets dropped
    # (the user's Shangri-La Frontier 52.1 / 75.1 / etc. case).
    groups = group_chapters_for_download(
        chapters,
        collapse_splits=collapse_splits_enabled,
        consensus_set=_multi_source_consensus_set,
    )
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
    out_dir = getattr(args, "output_dir", DEFAULT_OUTPUT_DIR)
    os.makedirs(out_dir, exist_ok=True)

    if getattr(args, "save_params", False):
        _save_download_params(out_dir, args.comic_url, args, title)

    resume_mode = False
    params_path = os.path.join(main_tmp_dir, "run_params.json")
    current_params = get_resumable_params(args, p, width, aspect_ratio_str)
    current_hash = gating_hash(current_params)

    if os.path.isdir(main_tmp_dir):
        print("Temporary directory found. Checking for resume compatibility...")
        if os.path.exists(params_path):
            try:
                with open(params_path, "r") as f:
                    old_data = json.load(f)
                if not isinstance(old_data, dict):
                    raise TypeError("not a JSON object")
                # New schema: {"gating_hash": ..., "params": {...}}.
                # Legacy schema (pre-rewrite): flat dict at top level —
                # recompute the hash from its gating-subset fields so a
                # tmp folder created by old code is still resumable as
                # long as the gating params haven't changed.
                if "gating_hash" in old_data:
                    old_hash = old_data["gating_hash"]
                else:
                    old_hash = gating_hash(old_data)
                if old_hash == current_hash:
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
        # Wrapped schema. Legacy flat-dict format is detected on read above;
        # any tmp folder created from this point forward uses the wrapped
        # {"gating_hash", "params"} structure.
        with open(params_path, "w") as f:
            json.dump(
                {"gating_hash": current_hash, "params": current_params},
                f, indent=4,
            )

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

    # ── Komikku series-level metadata (cover.jpg + details.json) ──
    # Spec §5 + §6.1: cover.jpg at series-folder root, details.json with
    # exact keys {title, author, artist, description, genre, status}.
    # Written once per run, fresh-or-overwriting on resume so the on-disk
    # metadata always reflects the latest comic_data (handler-extracted
    # genres/status may improve between runs as handlers evolve).
    # Cross-file: _komikku_status_to_digit (top of file, near
    # build_per_chapter_comic_info_xml). The cover-prepend to
    # current_book_content above is dead code in Komikku mode (we force
    # --no-final-file so the final CBZ build never fires) but kept for
    # parity with the non-Komikku CBZ path.
    if getattr(args, "komikku", False):
        try:
            if original_cover_path and os.path.exists(original_cover_path):
                cover_dst = os.path.join(out_dir, "cover.jpg")
                # Use copy2 so the file appears with timestamps from the
                # tmp copy (preserves mtime for Library-tab thumb-cache).
                shutil.copy2(original_cover_path, cover_dst)
                log_verbose(f"  Komikku: wrote cover.jpg → {cover_dst}")
            details_payload = {
                "title": title,
                "author": ", ".join(comic_data.get("authors", []) or []),
                "artist": ", ".join(comic_data.get("artists", []) or []),
                "description": comic_data.get("desc") or "",
                # Spec §6.1: `genre` is a JSON array of strings. Some
                # handlers merge `theme`/`format` into adjacent fields;
                # we keep `genre` as the canonical genres list only,
                # since that's what Komikku renders as tag chips.
                "genre": list(comic_data.get("genres", []) or []),
                "status": _komikku_status_to_digit(comic_data.get("status")),
            }
            details_path = os.path.join(out_dir, "details.json")
            with open(details_path, "w", encoding="utf-8") as f:
                json.dump(details_payload, f, ensure_ascii=False, indent=2)
            log_verbose(
                f"  Komikku: wrote details.json (status={details_payload['status']}, "
                f"{len(details_payload['genre'])} genre tags)"
            )
        except OSError as exc:
            # Don't fail the whole run for a metadata-write error. The
            # chapter CBZs still carry the same metadata via per-chapter
            # ComicInfo.xml, so Komikku will still display the manga.
            print(
                f"[!] Komikku metadata write failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
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

    def _process_chapter_impl(ch: Dict[str, Any], *, force_redownload: bool = False, next_chapter: Optional[Dict[str, Any]] = None, is_alt_source: bool = False, upcoming_chapters: Optional[List[Dict[str, Any]]] = None):
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
                except Exception as exc:
                    # Handler raised an arbitrary exception (e.g. requests.HTTPError
                    # from MangaDex's /at-home/server returning a transient 500
                    # after retries are exhausted, RuntimeError from a malformed
                    # API payload, the many `raise RuntimeError("...")` paths in
                    # the *scans Madara-style handlers, etc.). Convert to
                    # ChapterSkippedError so the strict wrapper's multi-source
                    # fallback + inline-retry path picks it up. Without this,
                    # the exception bypasses _process_chapter_strict (which only
                    # catches ChapterSkippedError) and the chapter loop's bare
                    # `except Exception` at the bottom of main() silently
                    # records the chapter as missed via _record_missed — user
                    # observes "Chapter N (group)" with no follow-up line,
                    # indistinguishable from a frozen download. The merged-parts
                    # branch above (~30 lines up) already has this conversion;
                    # this branch was the asymmetry letting the silent skip
                    # through. Symptom that drove this fix: Shuumatsu no
                    # Valkyrie Ch 5 on MangaDex 2026-05-16, /at-home/server 500.
                    if os.path.isdir(tdir):
                        rm_tree(tdir)
                    raise ChapterSkippedError(
                        reason=f"get_chapter_images_failed:{type(exc).__name__}",
                        host="",
                        pages_ok=0,
                        pages_total=0,
                    ) from exc
            _timing["vrf"] += time.monotonic() - _t0_vrf

            # --- VRF pipelining: enqueue the next `depth` chapters' VRF
            #     captures while this chapter's images download. The queue
            #     worker drains them serially (or batches into async multi-
            #     page if --mangafire-vrf-parallel > 1). With depth=4 and a
            #     ~6s image download, all 4 fit into the overlap window so
            #     subsequent chapters' VRFs are cached before they're needed.
            depth = max(0, int(getattr(args, "mangafire_vrf_prefetch_depth", 4) or 0))
            if upcoming_chapters:
                _start_vrf_prefetch_chain(upcoming_chapters, handler, depth=depth)
            elif next_chapter is not None:
                _start_vrf_prefetch_chain([next_chapter], handler, depth=depth)
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
                elif (
                    getattr(handler, "SUPPORTS_FAST_DOWNLOAD", False)
                    and not getattr(args, "no_fast_download", False)
                ):
                    # curl_cffi async path: HTTP/2 multiplex over one
                    # keep-alive AsyncSession. Bench (83-page chapter):
                    # ~1.7x faster than the ThreadPoolExecutor cloudscraper
                    # path. Cancellation + host-poison are bridged via
                    # callbacks so the handler stays decoupled from this
                    # module's globals. fast_download_images returns the
                    # same (page_idx, path_or_None) shape as dl_image.
                    fast_conc = max(
                        1, int(getattr(args, "image_concurrency", 8))
                    )
                    # Phase D: apply per-host concurrency cap. If a prior
                    # rate_limit / retryable failure dialed the cap down
                    # for this CDN, _effective_concurrency clamps to the
                    # cap. Healthy CDNs see the user-configured value.
                    fast_conc = _effective_concurrency(
                        urlparse(download_tasks[0][1]).netloc if download_tasks else "",
                        fast_conc,
                    )
                    fast_timeout = float(globals().get("_HTTP_TIMEOUT", 30.0))
                    log_verbose(
                        f"  Downloading {len(download_tasks)} image(s) via "
                        f"{handler.name} fast path (curl_cffi async, conc={fast_conc})..."
                    )
                    fast_results = handler.fast_download_images(
                        download_tasks,
                        concurrency=fast_conc,
                        timeout=fast_timeout,
                        is_cancelled=_chapter_cancelled,
                        # Bridge to _record_failure: classify all fast-path
                        # failures as 'retryable' (we don't have HTTP status
                        # context out here; the host-poison threshold treats
                        # any non-permanent failure the same).
                        # status/body_size are forwarded by the kwargs path
                        # in sites/base.py:_fetch_one when an HTTP response
                        # was received (vs. an exception with no body). They
                        # feed the ghost-chapter signature accumulator so
                        # uniform "every page returned identical error" is
                        # detected as ghost_chapter rather than host_poison.
                        # See aio-dl.py:_record_failure + _is_ghost_chapter_signature.
                        record_host_failure=lambda h, u, *, status=None, body_size=None: _record_failure(
                            h, u, "retryable", status=status, body_size=body_size,
                        ),
                        # Forward cookies from the cloudscraper session so
                        # handlers whose image CDN gates on session cookies
                        # (e.g. age-gated content) ride them. Base impl
                        # filters to host-relevant cookies; no-op for
                        # cookieless edge-cache CDNs (MangaFire, normal
                        # webtoons series).
                        scraper=scraper,
                    )
                    downloaded_images.extend(fast_results)
                elif image_workers > 1 and len(download_tasks) > 1:
                    # Phase D: apply per-host concurrency cap. ThreadPool
                    # max_workers can't be changed after creation, so we
                    # compute the effective worker count up front.
                    pool_workers = min(image_workers, len(download_tasks))
                    pool_workers = max(1, _effective_concurrency(
                        urlparse(download_tasks[0][1]).netloc if download_tasks else "",
                        pool_workers,
                    ))
                    log_verbose(
                        f"  Downloading {len(download_tasks)} image(s) with {pool_workers} parallel workers..."
                    )
                    with ThreadPoolExecutor(max_workers=pool_workers) as img_pool:
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
            # Reason precedence: 'incomplete' < 'time_budget' < 'host_poison'
            #   < 'ghost_chapter'.
            # The most informative reason takes priority for the diagnostic
            # log line and the timing summary block. ghost_chapter trumps
            # host_poison when both signals fire because ghost is the more
            # SPECIFIC classification — "every page returned identical
            # structural error" implies host_poison (5+ distinct URLs failed)
            # but the inverse isn't true. The ChapterSkippedError raised
            # with reason='ghost_chapter' causes _process_chapter_strict to
            # short-circuit the inline-retry path (no point retrying a
            # structural failure) and raise ChapterGhostError, which the
            # main loop catches as skip-and-continue instead of abort.
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
                # Ghost-chapter check FIRST. The detector uses pages_ok=0 +
                # uniform signatures across all recorded failures, which is
                # disjoint from "any pages succeeded" — so a chapter that
                # had even one successful download will never classify here.
                # primary_only feeds the threshold knob: when the alignment
                # data shows no non-primary source for this chapter, drop
                # the pages_total floor from 5 to 3 (independent corroboration
                # the chapter is fake). Cross-file:
                # _is_ghost_chapter_signature lives at ~line 990 here, the
                # alignment dict (_multi_source_alternatives) is populated
                # in main() at ~line 5821 / 6388.
                primary_only_for_ghost: Optional[bool] = None
                if _multi_source_alternatives:
                    try:
                        chap_str = str(ch.get("chap") or "").strip()
                        m = re.search(r"(\d+(?:\.\d+)?)", chap_str)
                        if m:
                            chap_float = float(m.group(1))
                            primary_only_for_ghost = (
                                not _multi_source_alternatives.get(chap_float)
                            )
                    except (TypeError, ValueError):
                        primary_only_for_ghost = None
                if _is_ghost_chapter_signature(
                    pages_ok=pages_ok,
                    pages_total=pages_total,
                    primary_only=primary_only_for_ghost,
                ):
                    reason = "ghost_chapter"
                    host_blame = _resolve_host_blame()
                elif poisoned_hosts:
                    reason = "host_poison"
                    host_blame = poisoned_hosts[0]
                elif deadline_hit:
                    reason = "time_budget"
                    host_blame = _resolve_host_blame()
                else:
                    reason = "incomplete"
                    host_blame = _resolve_host_blame()
                # Reason-aware log line. The "Will inline-retry" suffix is
                # false for ghost_chapter — _process_chapter_strict
                # short-circuits the inline-retry sleep + redo when the
                # primary reason is ghost (a structural failure won't
                # change after a 30s sleep). For ghost, we hand the
                # decision off to multi-source alts and, if those also
                # fail, raise ChapterGhostError (skip+continue, not abort).
                #
                # primary_only-aware descriptor: "ghost" alone is misleading
                # for chapters that exist on alt sources but happen to be
                # broken on the primary (the canonical 2026-05-27 case:
                # mangafire chapter 1 has uniform 5051-byte 403s but is on
                # atsumaru, mangakatana, etc.). User feedback was clear that
                # such chapters aren't "ghosts" — they're broken-on-primary
                # and exactly the scenario multi-source exists to fix. The
                # three states:
                #   primary_only=True  → genuine placeholder (no other source
                #                        lists this chapter); skip is the
                #                        right disposition
                #   primary_only=False → real chapter, primary CDN broken;
                #                        multi-source alt-fetch should rescue
                #   primary_only=None  → multi-source disabled / alignment
                #                        not built; can't tell
                if reason == "ghost_chapter":
                    if primary_only_for_ghost is True:
                        descriptor = "ghost (primary-only — no other source has this chapter)"
                    elif primary_only_for_ghost is False:
                        descriptor = "primary unavailable (alt sources have this chapter — will rescue if possible)"
                    else:
                        descriptor = "uniform error on primary"
                    print(
                        f"  [!] Chapter {n} {descriptor}: "
                        f"{pages_ok}/{pages_total} pages, every failure had identical "
                        f"signature (host={host_blame or '-'}). "
                        f"Trying alternative sources next."
                    )
                else:
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

            # Phase 1 (2026-05-11): LINE Webtoon WebP recompression.
            # Mutates raw_image_paths in place — converted .webp files
            # replace the original PNG/JPEG paths so both the CBZ fast
            # path (~line 5910) and the slow path (~line 5965) see the
            # already-compressed bytes. Runs AFTER --keep-images copytree
            # so users opting into both get unconverted originals in
            # <out>/images/ AND the recompressed CBZ.
            #
            # Gating:
            #   * --webtoon-recompress was passed
            #   * handler.name == "linewebtoon" (the LINE Webtoon handler;
            #     multi-source fallback may swap `handler` via `nonlocal`,
            #     in which case the check correctly evaluates against the
            #     active source and skips recompression for non-webtoon
            #     alt sources like mangadex)
            #   * args.format in ("cbz", "epub") (PDF would re-encode as
            #     FlateDecode and bloat; argparse validation already
            #     rejects --format pdf at startup, but we re-check here
            #     to be defensive against `--format none` and any other
            #     odd-mode arrival)
            #   * raw_image_paths is non-empty (defensive)
            #
            # Cross-file: argparse flags ~line 4070, _RESUME_GATING_DESTS
            # ~line 2900, recompress_chapter_images_to_webp() ~line 2190.
            if (
                getattr(args, "webtoon_recompress", False)
                and handler.name == "linewebtoon"
                and args.format in ("cbz", "epub")
                and raw_image_paths
            ):
                log_verbose(
                    f"  [recompress] Converting {len(raw_image_paths)} pages "
                    f"to WebP q{args.webtoon_recompress_quality} "
                    f"method={args.webtoon_recompress_method}..."
                )
                _t0_recompress = time.monotonic()
                raw_image_paths = recompress_chapter_images_to_webp(
                    raw_image_paths,
                    quality=args.webtoon_recompress_quality,
                    method=args.webtoon_recompress_method,
                )
                log_verbose(
                    f"  [recompress] Done in "
                    f"{time.monotonic() - _t0_recompress:.1f}s."
                )

            _t0_proc = time.monotonic()
            os.makedirs(processed_tdir, exist_ok=True)

            # Phase G7 (2026-05-08; Phase B chain 2026-05-13): kick off
            # image prefetch chain for upcoming chapters NOW — after this
            # chapter's downloads + validation succeeded, before the CPU-
            # bound processing/encoding begins. While the main thread is
            # decoding/scaling/saving this chapter's images, prefetch
            # workers download the next `image_prefetch_depth` chapters'
            # images in parallel (up to `image_prefetch_parallel` workers).
            # _process_chapter_impl's next iteration consumes the prefetch
            # via the .download_prefetched marker.
            #
            # Worker count knobs:
            #   --prefetch-image-workers: parallelism WITHIN one chapter
            #     prefetch (default -1 = match --image-workers). 0
            #     disables prefetch entirely.
            #   --image-prefetch-depth: how many chapters ahead to queue
            #     (default 2).
            #   --image-prefetch-parallel: concurrent prefetch worker
            #     threads (default 2). _ensure_image_prefetch_workers
            #     spawns up to this many daemons.
            #
            # Chain dedupe: when ch N fires the chain it queues N+1, N+2;
            # when ch N+1 fires it tries to queue N+2 (already in queue,
            # dedup'd) + N+3 (new). _start_image_prefetch's _seen set
            # handles this.
            #
            # Skipped on:
            #   - prefetch_image_workers <= 0 (user opt-out)
            #   - image_prefetch_depth <= 0 (chain disabled)
            #   - force_redownload=True (inline retry — don't fire side
            #     work that the retry path will also fire)
            #   - is_alt_source=True (multi-source fallback active)
            #   - all upcoming chapters already cached
            prefetch_workers_raw = getattr(args, "prefetch_image_workers", -1)
            if prefetch_workers_raw is None:
                prefetch_workers_raw = -1
            if prefetch_workers_raw < 0:
                effective_prefetch_workers = image_workers
            else:
                effective_prefetch_workers = int(prefetch_workers_raw)
            depth = max(0, int(getattr(args, "image_prefetch_depth", 2) or 0))
            if (
                effective_prefetch_workers > 0
                and depth > 0
                and not force_redownload
                and not is_alt_source
            ):
                # Prefer the windowed upcoming_chapters list (same shape
                # passed to the VRF chain), falling back to [next_chapter]
                # when the chapter loop didn't propagate a window.
                chain_upcoming: List[Dict[str, Any]] = (
                    list(upcoming_chapters)
                    if upcoming_chapters
                    else ([next_chapter] if next_chapter is not None else [])
                )
                if chain_upcoming:
                    _start_image_prefetch_chain(
                        chain_upcoming,
                        main_tmp_dir,
                        scraper,
                        handler,
                        effective_prefetch_workers,
                        fast_concurrency=int(getattr(args, "image_concurrency", 8) or 8),
                        depth=depth,
                        no_processing=bool(args.no_processing),
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
                    # source format (WebP→WebP-lossless or webp_q85 per
                    # Phase H scoping below, JPEG→JPEG q=95, else PNG).
                    # When recombination drew from multiple inputs (1:N
                    # mapping), source_paths is None and "auto" falls to
                    # PNG — but if every source was WebP we route to
                    # webp_lossless/webp_q85 explicitly based on the same
                    # Phase H scoping signal.
                    #
                    # Phase H (2026-05-16): _webp_source_is_lossy is True
                    # iff we know the WebP sources are already lossy q85
                    # from our own recompress step on LineWebtoon. This
                    # avoids wrapping recompressed q85 in a ~10x bigger
                    # lossless WebP. It's gated on handler.name +
                    # args.webtoon_recompress so natively-WebP sites
                    # (Atsumaru, MangaDex, etc.) keep the lossless preserve
                    # behavior — re-encoding their publisher-chosen quality
                    # at q85 would be generation-loss for those archives.
                    _webp_source_is_lossy = (
                        getattr(args, "webtoon_recompress", False)
                        and handler.name == "linewebtoon"
                    )
                    if args.format == "pdf":
                        _output_format = "jpeg"
                        _src_paths_for_save = None
                    elif args.format == "cbz":
                        if len(images_to_save) == len(raw_image_paths):
                            # 1:1 mapping: source paths line up per output
                            # page, so save_final_images' auto-mode can
                            # probe each one individually.
                            _output_format = "auto"
                            _src_paths_for_save = list(raw_image_paths)
                        elif _webp_source_is_lossy:
                            # 1:N mapping AND --webtoon-recompress is on
                            # for the active LineWebtoon handler. Match
                            # the user's intent (lossy q85 WebP) regardless
                            # of the source extension mix. This catches the
                            # case where some pages stayed .jpg as small
                            # passthrough (pre-2026-05-16 the JPEG
                            # eligibility predicate would skip near-empty
                            # panels; even after dropping it, a corrupt
                            # page can still fall back to .jpg). Without
                            # this branch the next `all(.webp)` check
                            # would fail and the chapter would silently
                            # fall through to lossless PNG — producing
                            # 130 MB CBZs on Eleceed Ch 25+.
                            _output_format = "webp_q85"
                            _src_paths_for_save = None
                        elif raw_image_paths and all(
                            os.path.splitext(p)[1].lower() == ".webp"
                            for p in raw_image_paths
                        ):
                            # 1:N mapping with publisher-supplied lossless
                            # WebP (Atsumaru, MangaDex, etc.). Source
                            # paths can't be matched per-page so auto-mode
                            # probing would fall to PNG; pick the lossless
                            # WebP variant explicitly so the publisher's
                            # chosen quality is preserved.
                            _output_format = "webp_lossless"
                            _src_paths_for_save = None
                        else:
                            # 1:N mapping with mixed / non-WebP sources
                            # AND no --webtoon-recompress intent. Falls
                            # through to save_final_images' auto-without-
                            # source-paths default (lossless PNG). This is
                            # the legacy behavior for sites that don't ship
                            # uniform-format images.
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
                            webp_source_is_lossy=_webp_source_is_lossy,
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
                #
                # Komikku mode (2026-05-12, Komikku LocalSource format): when --komikku is
                # set, embed a per-chapter ComicInfo.xml in the cache zip at
                # creation time. The XML carries <Series>/<Number>/<Title>/
                # <Translator>/<Web>/<Year>-<Month>-<Day>, which Komikku
                # v1.13.5+ uses to override filename-derived metadata. The
                # cache then becomes byte-identical to what the final
                # destination CBZ needs, so the --keep-chapters block below
                # carries the ComicInfo.xml across for free via shutil.copy2.
                # build_cbz_from_content (series-level wrapper) explicitly
                # filters ComicInfo.xml during member-copy, so writing it
                # here doesn't pollute the eventual series archive — though
                # in Komikku mode we force --no-final-file, so the series
                # archive never gets built anyway.
                if processed_page_images:
                    cached_cbz_path = os.path.join(processed_tdir, f"{n}.cbz")
                    with zipfile.ZipFile(
                        cached_cbz_path, "w", zipfile.ZIP_STORED
                    ) as zf:
                        for i, p in enumerate(processed_page_images):
                            zf.write(p, f"{i:04d}{os.path.splitext(p)[1]}")
                        if getattr(args, "komikku", False):
                            per_chap_xml = build_per_chapter_comic_info_xml(
                                series_title=title,
                                chapter_title=ch.get("title") or "",
                                chapter_num=n,
                                volume=ch.get("vol"),
                                scanlator=grp_name,
                                web_url=ch.get("url") or args.comic_url,
                                uploaded_epoch=ch.get("uploaded"),
                                comic_info=comic_data,
                                publishers=[grp_name] if grp_name else [],
                                lang=args.language,
                                page_count=len(processed_page_images),
                            )
                            zf.writestr(
                                "ComicInfo.xml",
                                per_chap_xml,
                                compress_type=zipfile.ZIP_DEFLATED,
                            )
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
            # Komikku mode: filename adopts Vol.{vv} Ch.{ccc} - {title}.cbz
            # (spec recommendation 7) which Mihon's ChapterRecognition parses
            # correctly AND is human-readable. We drop the series-title
            # prefix because the parent folder already IS the title under
            # Komikku's <SAF-root>/local/<Title>/ convention. Komikku also
            # ignores --epub-dir (Komikku is CBZ-only and lives under
            # out_dir/<Title>/), so active_out_dir is None in that branch.
            # See _komikku_chapter_filename for padding/decimal rules.
            if getattr(args, "komikku", False):
                ch_filename = _komikku_chapter_filename(
                    n, ch.get("vol"), ch.get("title")
                )
                ch_suffix = f"Ch {format_chap_for_filename(n)}"  # logging only
                active_out_dir = None  # Komikku always uses out_dir
            else:
                ch_suffix = f"Ch {format_chap_for_filename(n)}"
                ch_filename = f"{join_name(base_filename, ch_suffix)}.{args.format}"
                active_out_dir = getattr(args, "epub_dir", None) if args.format == "epub" else None
            if active_out_dir:
                os.makedirs(active_out_dir, exist_ok=True)
            ch_out_path = os.path.join(active_out_dir or out_dir, ch_filename)
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
                # In --komikku, the cache already carries the per-chapter
                # ComicInfo.xml from the cache-create block above, so the
                # copy ports it across unchanged.
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
                    # entries (pre-Phase-D code path). Build directly. In
                    # --komikku, pass the per-chapter ComicInfo.xml so this
                    # slow path matches the fast-path output byte-for-byte.
                    cbz_images = [
                        item["path"]
                        for item in chapter_content
                        if item.get("type") == "image"
                    ]
                    chapter_xml = None
                    if getattr(args, "komikku", False):
                        chapter_xml = build_per_chapter_comic_info_xml(
                            series_title=title,
                            chapter_title=ch.get("title") or "",
                            chapter_num=n,
                            volume=ch.get("vol"),
                            scanlator=grp_name,
                            web_url=ch.get("url") or args.comic_url,
                            uploaded_epoch=ch.get("uploaded"),
                            comic_info=comic_data,
                            publishers=[grp_name] if grp_name else [],
                            lang=args.language,
                            page_count=len(cbz_images),
                        )
                    with _cpu_guard('build_cbz'):
                        build_cbz(
                            cbz_images,
                            ch_out_path,
                            ch_title,
                            comic_data,
                            [grp_name] if grp_name else [],
                            args.language,
                            chapter_comic_info_xml=chapter_xml,
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


    def _process_chapter(ch: Dict[str, Any], *, force_redownload: bool = False, next_chapter: Optional[Dict[str, Any]] = None, is_alt_source: bool = False, upcoming_chapters: Optional[List[Dict[str, Any]]] = None):
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
            return _process_chapter_impl(ch, force_redownload=force_redownload, next_chapter=next_chapter, is_alt_source=is_alt_source, upcoming_chapters=upcoming_chapters)
        finally:
            if timer is not None:
                timer.cancel()
            # Clear the global so dl_image calls outside a chapter (e.g. the
            # cover image on the next run, or a follow-up batch URL) don't
            # see a stale set Event that would make them fast-fail.
            _CHAPTER_CANCEL = None


    def _process_chapter_strict(ch: Dict[str, Any], *, force_redownload: bool = False, next_chapter: Optional[Dict[str, Any]] = None, upcoming_chapters: Optional[List[Dict[str, Any]]] = None):
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
            return _process_chapter(ch, force_redownload=redo_primary, next_chapter=next_chapter, upcoming_chapters=upcoming_chapters)
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
                    alt_result = _process_chapter(
                        alt_chapter, force_redownload=True, next_chapter=next_chapter, is_alt_source=True
                    )
                    # Alt rescue succeeded. Record the rescue in the
                    # cross-chapter tally so the timing summary can
                    # surface multi-source value. Also print an explicit
                    # "rescued" line when the primary failure was the
                    # ghost-signature shape (the canonical case the user
                    # said "multi-source exists exactly for this" about).
                    # For other reasons (host_poison / time_budget /
                    # incomplete) the existing "[Multi-source] -> X"
                    # line + the per-chapter "CBZ saved" already make
                    # the rescue obvious; we only need the extra line for
                    # ghost because the in-chapter log claimed "every
                    # failure had identical signature" and we want to
                    # close the loop visually.
                    multi_source_rescues.append({
                        "chap": n_for_log,
                        "alt_site": alt_handler.name,
                        "primary_site": primary_state[0].name,
                        "primary_reason": primary_err.reason,
                    })
                    if primary_err.reason == "ghost_chapter":
                        print(
                            f"    [Multi-source] ✓ Chapter {n_for_log} rescued "
                            f"from {primary_state[0].name} ({primary_err.reason}) "
                            f"via {alt_handler.name}"
                        )
                    return alt_result
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

        # All alternatives failed (or none available).
        #
        # Ghost-chapter short-circuit: when the PRIMARY failed with the ghost
        # signature (every page returned an identical structural error), the
        # inline-retry sleep + redo is pointless — the response template that
        # produced the signature is generated by a server-side rule that won't
        # change in 30s or 60s. mangafire's 5051-byte CF 403 for chapter-0
        # placeholders is the canonical case. Hand off to ChapterGhostError
        # (caught by the main loop as skip-and-continue) so the run isn't
        # aborted on a structurally-fake chapter. Cross-source diagnostic:
        # carry the primary_only flag through from the alignment lookup so
        # the log line tells the user whether the chapter was primary-only
        # (strongest fake signal) or just unavailable everywhere (alts
        # listed it but couldn't deliver — could be coincident outage).
        if primary_err.reason == "ghost_chapter":
            primary_only_for_ghost: Optional[bool] = None
            if _multi_source_alternatives:
                try:
                    chap_str = str(ch.get("chap") or "").strip()
                    m = re.search(r"(\d+(?:\.\d+)?)", chap_str)
                    if m:
                        chap_float = float(m.group(1))
                        primary_only_for_ghost = (
                            not _multi_source_alternatives.get(chap_float)
                        )
                except (TypeError, ValueError):
                    primary_only_for_ghost = None
            raise ChapterGhostError(
                chap=n_for_log,
                host=primary_err.host,
                pages_total=primary_err.pages_total,
                primary_only=primary_only_for_ghost,
            ) from primary_err

        # Other reasons (incomplete / time_budget / host_poison): fall back
        # to inline-retry on the primary source — these CAN be transient.
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

    # Consecutive-ghost escalation. Ghost detection alone is right for the
    # canonical "chapter 0 is a fake placeholder" case (skip + continue), but
    # WRONG when the host is globally CF-blocked / auth-expired and EVERY
    # chapter ghosts identically — silently slogging through 290 chapters of
    # uniform 403s is the "reliability compromise" we explicitly rejected.
    # Real-world test 2026-05-27: user's mangafire was returning uniform
    # 5051-byte 403s for chapter 0 (host l1n.mfcdn2.xyz) AND chapter 1 (host
    # k99.mfcdn2.xyz) — different hosts, same ghost shape — meaning the
    # block was at the auth/account level, not the chapter level.
    #
    # Escalation rule: when GHOST_ABORT_THRESHOLD consecutive chapters
    # classify as ghost without a successful chapter or non-ghost failure
    # between them, escalate to abort. The user sees the same FATAL message
    # they would have seen pre-fix, just slightly later (after N ghosts
    # instead of after the very first chapter's inline-retries exhausted).
    #
    # Threshold = 3:
    #   - 2 would catch the host-outage case but false-trigger on real
    #     series with two placeholder chapters at the start (e.g. chapter 0
    #     + chapter 0.5 both fake — rare but documented).
    #   - 3 is a safe margin: a user who hits 3 placeholder chapters at the
    #     start of a manga has bigger problems than this escalator. Real
    #     host outages produce many more than 3 consecutive ghosts, so 3 is
    #     fine for the escalation trigger.
    #
    # Reset conditions (counter goes back to 0):
    #   - any successful chapter (chapter_content non-empty after the
    #     strict wrapper returns)
    #   - any non-ghost ChapterAbortedError (we're aborting anyway)
    #   - any generic Exception (recorded as 'exception' missed; not ghost)
    #   - any empty_content miss (no content but not ghost either)
    consecutive_ghosts = 0
    GHOST_ABORT_THRESHOLD = 3

    # Multi-source rescue tally: chapters whose primary source failed AND
    # an alt source successfully delivered them. Each entry is a dict with
    # chap, alt_site, primary_site, primary_reason. Surfaced in the
    # end-of-run timing summary so the user can see multi-source's value
    # at a glance. User feedback 2026-05-27: "chapter 1 isn't a ghost
    # chapter, it's an exact target for multi-source since it's broken on
    # MF. That's exactly why multi-source exists." The summary line makes
    # that value tangible — without it, users may think the run "had
    # failures" when in fact half the failures were silently rescued.
    multi_source_rescues: List[Dict[str, Any]] = []

    for ch_idx, ch in enumerate(chapters):
        grp_name = handler.get_group_name(ch)
        insert_list_index = len(current_book_content)
        insert_chapter_index = len(current_book_chapters)
        insert_marker_index = len(current_epub_markers)
        insert_page_index = _running_page_count if args.format == 'epub' else 0
        # Look ahead so _process_chapter_impl can prefetch upcoming chapters'
        # VRFs (chain depth from --mangafire-vrf-prefetch-depth, default 4)
        # while downloading images for this one. next_ch retained for the
        # inter-chapter image prefetch (Phase G7) which is single-chapter.
        next_ch = chapters[ch_idx + 1] if ch_idx + 1 < len(chapters) else None
        # Slice depth+a-bit so the chain push has enough lookahead even if
        # depth is bumped at runtime via env (we don't have a depth-aware
        # truncation here; passing 8 is fine since dedupe drops duplicates).
        upcoming_slice = chapters[ch_idx + 1 : ch_idx + 1 + 8]
        try:
            chapter_content, grp_name, n, chapter_content_size = _process_chapter_strict(
                ch, next_chapter=next_ch, upcoming_chapters=upcoming_slice
            )
        except ChapterGhostError as cge:
            # Soft skip: chapter looks structurally absent on the primary
            # (uniform error signature across every page) and no alternative
            # source could deliver it either. NOT abort by default — recording
            # missed and continuing is the right call, because the failure
            # shape is "this chapter doesn't exist here" not "the CDN is
            # broken." See ChapterGhostError docstring near top of file for
            # rationale, and _process_chapter_strict's
            # primary_err.reason == "ghost_chapter" branch for the raise site.
            # Cross-file: _is_ghost_chapter_signature is the detector;
            # _record_failure_signature is what feeds it from the download
            # paths.
            #
            # EXCEPT when GHOST_ABORT_THRESHOLD consecutive ghosts have fired
            # without a successful chapter between — see the
            # consecutive_ghosts comment block above main()'s for-loop for
            # why. At that point we escalate to abort because the failure
            # is host-level, not chapter-level, and slogging through the
            # remaining queue is the "speed and reliability compromise" the
            # user explicitly rejected.
            consecutive_ghosts += 1
            # primary_only-aware descriptor (same three-state taxonomy as
            # the in-chapter log line ~line 7385). When this branch runs,
            # the alt loop in _process_chapter_strict has already been
            # tried and exhausted — so primary_only=False here means "alts
            # exist but ALSO couldn't deliver this chapter," which is
            # a different shape than "primary-only ghost" (genuinely fake)
            # OR "primary unavailable, untried" (the in-chapter pre-alt
            # message). Keep these distinct so the user can tell at a
            # glance whether the missed chapter is a placeholder or a
            # multi-source rescue that just didn't pan out.
            if cge.primary_only is True:
                descriptor = "primary-only ghost (no other source has this chapter)"
            elif cge.primary_only is False:
                descriptor = "primary unavailable AND all alt sources failed"
            else:
                descriptor = "uniform-error ghost on primary (multi-source disabled)"
            if consecutive_ghosts >= GHOST_ABORT_THRESHOLD:
                # Escalate. Print FATAL message + record this chapter as
                # aborted:ghost_chapter (NOT plain ghost_chapter) so the
                # missed-chapters log distinguishes the escalation chapter
                # from the leading ghost-skipped ones. Record remaining
                # chapters as not_attempted_after_abort to mirror the
                # ChapterAbortedError branch's bookkeeping.
                print(
                    f"\n[!] FATAL: Chapter {cge.chap} is the "
                    f"{GHOST_ABORT_THRESHOLD}rd consecutive uniform-error "
                    f"chapter on primary (host={cge.host or '?'}; {descriptor}). "
                    f"The uniform-error pattern across multiple chapters "
                    f"indicates a host-level block (auth expired, CF rule "
                    f"tightened, CDN broken globally) rather than per-chapter "
                    f"placeholder absence."
                )
                print(
                    f"    Aborting run. Chapters successfully saved before "
                    f"this point are kept (per-chapter files via "
                    f"--keep-chapters)."
                )
                _record_missed(
                    ch, grp_name, "aborted:ghost_chapter",
                    f"escalated after {consecutive_ghosts} consecutive ghosts on primary",
                    insert_list_index=insert_list_index,
                    insert_chapter_index=insert_chapter_index,
                    insert_marker_index=insert_marker_index,
                    insert_page_index=insert_page_index,
                    host=cge.host,
                    pages_ok=0,
                    pages_total=cge.pages_total,
                )
                aborted_remaining = True
                aborted_chapter = ch
                for j in range(ch_idx + 1, len(chapters)):
                    skipped_ch = chapters[j]
                    _record_missed(
                        skipped_ch,
                        handler.get_group_name(skipped_ch),
                        "not_attempted_after_abort",
                        f"main pass aborted after {GHOST_ABORT_THRESHOLD} consecutive ghosts",
                        insert_list_index=len(current_book_content),
                        insert_chapter_index=len(current_book_chapters),
                        insert_marker_index=len(current_epub_markers),
                        insert_page_index=_running_page_count if args.format == 'epub' else 0,
                        host=cge.host,
                        pages_ok=0,
                        pages_total=0,
                    )
                break

            # Normal ghost handling — skip + continue, with a counter hint
            # so the user sees the escalation looming.
            counter_hint = (
                f" [{consecutive_ghosts}/{GHOST_ABORT_THRESHOLD} "
                f"consecutive — will abort at {GHOST_ABORT_THRESHOLD}]"
                if consecutive_ghosts > 1 else ""
            )
            print(
                f"\n[!] Chapter {cge.chap}: {descriptor} on "
                f"{cge.host or '?'} ({cge.pages_total} pages, 0 succeeded). "
                f"Recorded as missed; the run continues.{counter_hint}"
            )
            _record_missed(
                ch, grp_name, "ghost_chapter",
                f"uniform error signature on primary; primary_only={cge.primary_only}",
                insert_list_index=insert_list_index,
                insert_chapter_index=insert_chapter_index,
                insert_marker_index=insert_marker_index,
                insert_page_index=insert_page_index,
                host=cge.host,
                pages_ok=0,
                pages_total=cge.pages_total,
            )
            continue
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
            # Defense-in-depth: print so the user actually sees the failure
            # in the live log. Phase 1 (get_chapter_images) exceptions are
            # converted to ChapterSkippedError at the call site so they engage
            # multi-source fallback + inline-retry inside _process_chapter_strict
            # — they won't reach here. This branch catches the residual cases:
            # Phase 3 build errors (CBZ assembly, PDF encode), unexpected
            # exceptions inside the handler that slipped past the per-phase
            # try/except blocks, or a future code path that raises before
            # being wrapped. Recording the entry without surfacing it was the
            # bug that masked Shuumatsu no Valkyrie Ch 5 in the user's
            # 2026-05-16 run; printing here means future regressions of the
            # same shape are visible immediately rather than only via
            # missed_chapters.json after the run ends.
            consecutive_ghosts = 0  # reset: this is not a ghost
            print(
                f"  [!] Chapter {ch.get('chap', '?')} hit an unexpected error: "
                f"{type(e).__name__}: {str(e)[:200]}. "
                f"Recorded as missed; the run will continue."
            )
            _record_missed(ch, grp_name, 'exception', repr(e), insert_list_index=insert_list_index, insert_chapter_index=insert_chapter_index, insert_marker_index=insert_marker_index, insert_page_index=insert_page_index)
            continue

        if not chapter_content:
            consecutive_ghosts = 0  # reset: empty content is not a ghost
            _record_missed(ch, grp_name, 'empty_content', 'No downloadable content', insert_list_index=insert_list_index, insert_chapter_index=insert_chapter_index, insert_marker_index=insert_marker_index, insert_page_index=insert_page_index)
            continue

        # Successful chapter — reset the consecutive-ghost counter. The
        # canonical "chapter 0 fake" pattern produces 1 ghost then real
        # downloads; this reset is what keeps that working.
        consecutive_ghosts = 0

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
            active_out_dir = getattr(args, "epub_dir", None) if args.format == "epub" else None
            if active_out_dir:
                os.makedirs(active_out_dir, exist_ok=True)
            final_path = os.path.join(active_out_dir or out_dir, f"{base_filename}.{args.format}")
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
            "download_volumes": bool(getattr(args, "download_volumes", False)),
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

    if getattr(args, "save_params", False):
        if original_cover_path and os.path.exists(original_cover_path):
            dest_cover = os.path.join(out_dir, ".cover.jpg")
            if not os.path.exists(dest_cover):
                try:
                    shutil.copy2(original_cover_path, dest_cover)
                except Exception:
                    pass

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

    # --- Multi-source rescue tally ---
    # Surfaces the value of multi-source: chapters whose primary source
    # failed (any reason — ghost / host_poison / time_budget / incomplete)
    # and were successfully delivered from an alternative source. The
    # tally is built up during the main loop in _process_chapter_strict's
    # alt-success branch. Skipped entirely when empty so single-source
    # runs aren't polluted with a hollow header. Cross-file:
    # multi_source_rescues declared near the top of main()'s chapter loop
    # (~line 8355 with the consecutive_ghosts state). For why this block
    # exists at all: user feedback 2026-05-27 emphasized that broken-on-
    # primary chapters (chapter 1 in the Shangri-La Frontier failure case)
    # are exactly what multi-source exists to handle — making the rescue
    # visible in the summary closes the loop visually for the user.
    if multi_source_rescues:
        print(f"\n--- Multi-source Rescues ---")
        print(
            f"  {len(multi_source_rescues)} chapter(s) rescued from primary "
            f"failures via alternative sources:"
        )
        # Stable ordering: insertion order matches chapter-loop order which
        # is already chap-ascending after collapse-splits + filter.
        for r in multi_source_rescues:
            print(
                f"    Ch {r['chap']:<8} <- {r['alt_site']:<14} "
                f"(primary {r['primary_site']} failed: {r['primary_reason']})"
            )

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
