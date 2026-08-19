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
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
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
from sites.chapter_merger import group_chapters_for_download, _extract_chapter_num
from sites.base import (
    SiteComicContext,
    IncompleteChapterError,
    GroupSelectionPolicy,
    build_group_census,
)
from sites.group_quality import MTL_CONFIRMED, classify_mtl
from sites._image_io import (
    sniff_image_extension as _sniff_image_extension,
    image_magic_extension as _image_magic_extension,
    finalize_pending_image as _finalize_pending_image,
    JPEG_MAGIC as _JPEG_MAGIC,
    PNG_MAGIC as _PNG_MAGIC,
    GIF_MAGIC as _GIF_MAGIC,
    content_type_to_ext as _content_type_to_ext,
    IMAGE_ACCEPT_HEADERS as _IMAGE_ACCEPT_HEADERS,
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
    there — a soft-launched placeholder, a CDN URL signed for a chapter
    that was unpublished, etc. No
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


class ChapterPermanentSkipError(Exception):
    """Raised by _process_chapter_strict for a chapter that failed with a
    DETERMINISTIC per-chapter gate no retry or alternate source can clear —
    currently a mature/age/login interstitial (reason 'mature_login_required',
    grep _PERMANENT_SKIP_REASONS). The main loop treats this as skip-and-
    continue: record missed + move on, WITHOUT the ghost path's consecutive-
    host-block escalation (a mature series legitimately has many consecutive
    gated episodes, so counting them toward a host-level abort would be wrong).

    Why distinct from ChapterGhostError: ghost means "this chapter looks
    structurally absent / the host is misbehaving" and escalates to a run abort
    after GHOST_ABORT_THRESHOLD in a row; a login gate is a per-episode content
    restriction, not a host fault. Why distinct from ChapterAbortedError: the
    OLD flow inline-retried 'mature_login_required' to exhaustion and then
    ABORTED the whole run on a single login-gated tapas BGM episode (the aux
    veto — grep _chapter_carries_aux — also blocked alt-rescue), losing every
    later good chapter over one age-gated one. Cross-file: sites/tapas.py raises
    IncompleteChapterError(reason='mature_login_required') from
    get_chapter_images when a logged-out mature interstitial replaces the
    panels; the conversion to this class happens in _process_chapter_strict.
    """
    def __init__(self, chap, reason: str = "", host: str = "", pages_total: int = 0):
        self.chap = chap
        self.reason = reason
        self.host = host
        self.pages_total = pages_total
        super().__init__(
            f"chapter {chap} permanent-skip: reason={reason} "
            f"host={host or '-'} pages={pages_total}"
        )


# Deterministic per-chapter failure reasons that no inline retry can clear —
# routed to ChapterPermanentSkipError (skip + continue) by
# _process_chapter_strict instead of the inline-retry→abort path, AFTER the
# multi-source alt-rescue loop has been tried (a permanent reason on the
# PRIMARY can still be delivered by an alternative site — so these are "primary
# can't, and no alt did either" by the time they convert). A set so handlers
# can add their own login/paywall gate reasons; producers today (sites/tapas.py):
#   - "mature_login_required": logged-out mature interstitial replaced the panels.
#   - "locked": premium/wait-to-unlock episode emitted as a placeholder
#     (get_chapter_images short-circuits it) so --multi-source can fill it; when
#     no alt has it, this clean-skips instead of aborting the whole run.
#   - "comix_pages_stalled" (sites/comix.py): comix's reader defers every 10th
#     page — a chunk boundary — until the viewport reaches it, and when the
#     handler's full recovery ladder (re-nudge → reload → lazy-mode) still can't
#     reach one, re-rendering the identical chapter cannot either. Live
#     2026-08-02: 99/107 captured, inline retry, 99/107, inline retry, 99/107,
#     then the whole 256-chapter run aborted on chapter 1. The two 90s retries
#     were pure cost. Note comix raises this ONLY for the deferral signature
#     (grep _looks_like_stalled_capture); an ordinary render miss stays
#     retryable as "comix_dom_render_incomplete".
#   * decode_dropped_pages — pages downloaded fine and then failed to DECODE
#     (live cause: Chaquopy's Pillow ships no WebP codec, android/PARITY.md D4).
#     Deterministic for this build: the same bytes through the same missing
#     codec fail identically, so the two long inline retries were pure cost. It
#     still reaches the alt-source loop first, and that is the point — the
#     rescue that works is an ALTERNATE SOURCE serving a format this build can
#     read, not a retry.
_PERMANENT_SKIP_REASONS = frozenset({
    "mature_login_required", "locked", "comix_pages_stalled",
    "decode_dropped_pages",
})


# -----------------------------------------------------------
# Cross-process folder allocation (avoid mixing same-title series)
# -----------------------------------------------------------
class _AIOFileLock:
    """A tiny cross-platform exclusive file lock.

    RACE-1: a single instance (the _COORD._net_lock / _cpu_lock / _state_lock
    singletons) is entered by MULTIPLE THREADS. With only one self._fd, two
    concurrent __enter__s clobbered it — the first thread's __exit__ then closed
    the SECOND thread's fd (EBADF, swallowed → it ran UNLOCKED) while the first
    thread's real OS lock leaked for the process lifetime (+10s msvcrt spins per
    later acquire on Windows; POSIX siblings block forever). Serialize entry/exit
    with a threading.Lock so exactly one thread owns self._fd at a time. This is
    correct: it's an EXCLUSIVE lock, so same-process threads must be mutually
    exclusive too, not just cross-process — the file lock still provides the
    cross-process half. Non-reentrant: no coordinator method nests the same lock
    (verified — net_phase/cpu_phase/state ops each take one lock, sequentially).
    Only exercised under --jobs>1 (grep AIO_COORD_ENABLED / _COORD).
    """
    def __init__(self, path: str):
        self.path = path
        self._fd = -1
        self._tlock = threading.Lock()

    def __enter__(self):
        self._tlock.acquire()
        try:
            # makedirs is INSIDE the try (RACE-1 follow-up): if it raised after
            # the _tlock.acquire() above, __enter__ would propagate WITHOUT
            # Python ever calling __exit__ (it doesn't, when __enter__ raises) →
            # the mutex leaks and every later acquire on this singleton
            # deadlocks. Folding it into the best-effort block degrades a
            # transient FS error (unmount / ACL flip / path-is-now-a-file under
            # --jobs>1) to "proceed without the OS lock" — the threading.Lock is
            # still held for intra-process exclusion — exactly like an os.open
            # failure below.
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
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
            # Best effort — close fd and proceed without the OS lock (the
            # threading.Lock is still held, so intra-process exclusion holds).
            if self._fd >= 0:
                try:
                    os.close(self._fd)
                except Exception:
                    pass
                self._fd = -1
        return self

    def __exit__(self, exc_type, exc, tb):
        # ALWAYS release the threading.Lock, even on the best-effort no-fd path,
        # or a failed os.open would leak the mutex and hang every later acquire.
        try:
            if self._fd >= 0:
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
        finally:
            self._tlock.release()



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

    Normally uses: root/<title>. Falls back to root/<title> (hid=<hid>) only on
    a genuine collision: a DIFFERENT series with the same title that already has
    downloaded content.

    A hidden marker file (.series_hid) stores the hid so multiple runs and
    processes stay consistent. Matching is hash-tolerant: a marker is reused
    when it equals the hid, or equals it after stripping a rotating hash suffix
    (so the stabilized asura hid maps onto folders downloaded with the old
    full-slug hid). An EMPTY existing folder is always reclaimed, even when a
    stale marker points at a different hid — this prevents the empty orphan
    folders left when a run crashes after allocation but before any chapter is
    written and the next attempt carries a different hid. See _marker_matches
    and the reclaim branch below.
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

    def _strip_rotating_suffix(value: str) -> str:
        # Asura (and similar) bake a rotating hex hash into the slug they use
        # as the hid: "sss-class-suicide-hunter-46f09241". The asura handler
        # now stabilizes the hid by stripping that hash, but folders downloaded
        # before the fix are still marked with the OLD full-slug hid. Reduce
        # both to a hash-free base so a stabilized hid maps onto the existing
        # folder. Non-rotating hids (comick short ids, mangadex UUIDs whose
        # groups never change) only reach here when existing != want, which
        # for stable hids never happens — so this is effectively asura-only.
        return re.sub(r"-[0-9a-f]{6,}$", "", str(value or ""))

    def _marker_matches(existing: "str | None", want: str) -> bool:
        # True when the folder belongs to this series. Exact match, or equal
        # once a rotating hash suffix is stripped from both sides (migration
        # for the stabilized asura hid). Require a non-empty base so two
        # unrelated markers can't both collapse to "" and false-match.
        if existing is None:
            return False
        if existing == str(want):
            return True
        base_existing = _strip_rotating_suffix(existing)
        return bool(base_existing) and base_existing == _strip_rotating_suffix(str(want))

    with _AIOFileLock(lock_path):
        preferred = os.path.join(root, base)
        if os.path.exists(preferred):
            existing = _read_marker(preferred)
            if _marker_matches(existing, hid):
                # Converge the marker onto the current (canonical) hid so a
                # stabilized hid is treated as an exact match next run.
                if existing != str(hid):
                    _write_marker(preferred)
                return preferred
            # Empty folder: reclaim regardless of any stale marker — there is
            # no downloaded content to protect. This is what kills the empty
            # orphan folders left when a run crashes AFTER folder allocation
            # (which eagerly writes .series_hid) but BEFORE any chapter is
            # written, and the next attempt carries a different hid (asura
            # slug rotation, or re-picking the series from another source).
            # Pre-fix this branch required `existing is None`, so a stale
            # marker forced a "(hid=...)" sibling and orphaned the empty bare
            # folder. (grep: orphan bare folders)
            if not _folder_nonempty(preferred):
                _write_marker(preferred)
                return preferred
            # Otherwise a genuine collision: a DIFFERENT series with the same
            # title AND real downloaded content. Disambiguate with a suffix.
            candidate_base = _sanitize_folder_component(f"{clean_title} (hid={hid})")
            candidate = os.path.join(root, candidate_base)
            k = 2
            while os.path.exists(candidate):
                ex = _read_marker(candidate)
                if _marker_matches(ex, hid):
                    if ex != str(hid):
                        _write_marker(candidate)
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
                # A Cloudflare interstitial lands here too (403 + ~5 KB of
                # challenge HTML), and handing it back as "a response with
                # content" is how a challenged site reads downstream as an empty
                # page: no title, no chapters, and the run dies on "No chapters
                # selected" with Cloudflare never mentioned once.
                #
                # WARN AND STILL RETURN, deliberately. sites/madara.py:_fetch_html
                # (the CF chokepoint for 244 handlers), mangathemesia's
                # _fetch_html_guarded and kappabeast's _read_frontend_text all
                # call make_request and THEN inspect the body to decide whether to
                # rescue — raising here would silently disable every one of those
                # rescues. This only makes the failure legible for handlers that
                # have no rescue of their own (kagane, manhuaus).
                # Cross-file: sites/crawlee_utils.py:warn_cf_rescue dedups to one
                # line per host per process.
                try:
                    from sites.crawlee_utils import is_cf_challenge, warn_cf_rescue

                    if is_cf_challenge(r.status_code, r.text or ""):
                        warn_cf_rescue(
                            url,
                            "the site served a Cloudflare verification page "
                            f"instead of content (HTTP {r.status_code}). Anything "
                            "parsed from this response will be empty.",
                            kind="detected",
                        )
                except Exception:
                    pass
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


def _chap_as_float(chap) -> Optional[float]:
    """Tolerant chapter-number parse for the post-bucketing filter/sort sites.

    Returns the label as a float, or None for a non-numeric label. The chapter-
    bucketing step (grep chapters_by_num) already normalizes float/"Oneshot"
    labels to a numeric STRING and drops raw non-numeric ones, so in the normal
    flow every downstream ch["chap"] parses — but the collapse-splits relabel
    (sites/chapter_merger.py group.label, which can be "?" or "" for an
    empty/None chap) can reintroduce a non-numeric label. The --no-partials /
    --chapters filters and the .aio_series.json sort used a bare
    float(c["chap"]) that crashed the whole run on that edge; this keeps them
    defensive. C1 review finding.
    """
    try:
        f = float(chap)
    except (TypeError, ValueError):
        return None
    # Reject non-finite: float("inf") / "nan" / "1e400" all parse but aren't
    # valid chapter numbers — inf/nan would crash int(cf) in _is_numeric_partial
    # and poison the (is-None, value) sort keys. Residual follow-up.
    return f if math.isfinite(f) else None


def _chap_label_str(chap) -> str:
    """Canonical chapter-label string matching --list-chapters' normalization.

    --list-chapters emits f"{num:g}" for numeric labels (grep deduped_pool), so
    the .aio_series.json chapters_downloaded set MUST use the same form or the
    UI's update-check diff (main.js Set difference of raw strings) never matches
    a float-emitting handler's chapters (mangathemesia family: 4.0 vs 4) and the
    series sticks at "+N new" forever, re-downloading everything on each update.
    Non-numeric labels fall back to str(). XF-1 review finding.
    """
    f = _chap_as_float(chap)
    return str(chap) if f is None else f"{f:g}"


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


def _chapter_range_floor(range_spec: str) -> Optional[float]:
    """Lowest chapter number a `--chapters` spec can possibly select, or None.

    Feeds BaseSiteHandler.chapter_floor_hint (see the contract there): purely
    an optimization hint letting a handler with expensive paginated listing
    stop early. Returns None — meaning "no floor, list everything" — whenever
    the answer isn't provably safe, so a wrong guess can never drop a chapter
    the user asked for. That includes:

      * "all" (the default),
      * the negative "last N chapters" form (`--chapters -20`), whose floor
        depends on the very list we haven't fetched yet,
      * any spec with a part we can't parse a lower bound from (open-ended
        `100-`, junk, an unrecognized alias).

    The result is a FLOOR over the whole comma-separated spec, i.e. the minimum
    across parts, so `--chapters 5,300-400` yields 5.0 and still lists
    everything down to chapter 5.
    """
    text = (range_spec or "").strip()
    if not text or text.lower() == "all":
        return None
    # "-20" (and only that form) means "last 20 chapters" to the caller.
    if text.startswith("-") and "," not in text:
        return None
    lows: List[float] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            return None
        low: Optional[float] = None
        if "-" in part:
            raw_start, _, raw_end = part.partition("-")
            start = _parse_chapter_spec_number(raw_start)
            end = _parse_chapter_spec_number(raw_end)
            # Both ends must parse; "chapter-1" style values aren't ranges and
            # fall through to the single-number read below.
            if start is not None and end is not None:
                low = start
        if low is None:
            low = _parse_chapter_spec_number(part)
        if low is None:
            return None
        lows.append(low)
    return min(lows) if lows else None


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
# the whole run. Reset by _reset_host_concurrency_caps at run start.
# Floor is 1; we never reduce below "one request at a time" because
# concurrency reduction is a coarse control and the polite-delay
# machinery handles fine-grained request pacing separately.
#
# AIMD recovery (2026-07-03; reverses the original "capped for the rest of
# the run" decision): once the prefetch fast path started feeding this cap
# (grep record_host_failure in _run_image_prefetch_job), descent got fast
# enough that one bad patch could pin a 400-chapter run at cap 1-3 for hours
# after the CDN recovered. So: _HOST_CLEAN_STREAK counts gate-accepted
# chapters since the host's last recorded failure (any failure resets it);
# every _HOST_CAP_RECOVERY_STREAK consecutive clean chapters buy +1 cap
# (grep _record_host_clean_chapter). _HOST_CAP_BASELINE remembers the
# pre-reduction cap from the host's FIRST reduction; on climbing back to it
# the cap entry is DELETED rather than set to a number — deletion is the
# true fully-recovered state because _effective_concurrency clamps to
# min(base, cap) and a pool whose base exceeds the default 8 (e.g.
# --image-workers 10) must return to its own base, not to 8.
# All three structures share _HOST_CAP_LOCK.
_HOST_CONCURRENCY_CAP: Dict[str, int] = {}
_HOST_CAP_BASELINE: Dict[str, int] = {}
_HOST_CLEAN_STREAK: Dict[str, int] = {}
_HOST_CAP_RECOVERY_STREAK = 3
_HOST_CAP_LOCK = threading.Lock()

# Set by the per-chapter watchdog Timer in _process_chapter when the chapter's
# wall-clock deadline expires. dl_image / _try_download_url check this and
# return early so the chapter aborts within seconds of the deadline.
# None outside the chapter loop (e.g. during cover download).
_CHAPTER_CANCEL: Optional[threading.Event] = None

# RUN-level cancel — "the user pressed stop", not "this chapter ran long".
#
# WHY A SECOND EVENT instead of reusing _CHAPTER_CANCEL: that one is owned by
# the per-chapter watchdog and is deliberately torn down twice per chapter —
# the completeness gate CLEARS it when a chapter finished N/N despite the
# deadline (grep "accepting despite"), and _process_chapter's finally sets the
# global back to None. Either would silently un-cancel a user's stop request.
# Keeping them separate means the watchdog can keep standing itself down
# without ever countermanding the user.
#
# Always a real Event, never None — unlike _CHAPTER_CANCEL, which needs the
# capture-once dance at _chapter_cancelled to dodge a None race (see S5-6 in
# that function). Nothing sets this back to None, so readers can just poll it.
#
# Who sets it: embedders that can't kill the process. On desktop a cancel is
# `taskkill /f /t` / SIGTERM and this stays clear for the whole run; on Android
# there is no separate process to kill, so aio_android.py flips this instead.
# Cross-file: grep request_run_cancel.
_RUN_CANCEL = threading.Event()


def request_run_cancel() -> None:
    """Ask the current run to wind down at the next cancellation checkpoint.

    Cooperative, not immediate: in-flight page downloads finish or time out on
    their own, then the chapter loop, the image pools, and the prefetch workers
    all notice via _chapter_cancelled / run_cancelled. Callers that need a hard
    stop should still kill the process."""
    _RUN_CANCEL.set()


def clear_run_cancel() -> None:
    """Reset before starting a new run in the same process. Only embedders that
    reuse an interpreter need this — a one-shot CLI process never does."""
    _RUN_CANCEL.clear()


def run_cancelled() -> bool:
    return _RUN_CANCEL.is_set()


def _self_spawn_unavailable() -> Optional[str]:
    """Why this process can't re-invoke itself, or None when it can.

    Two modes fan out by spawning `[sys.executable, <this file>, ...]`:
    --update-all and the multi-URL supervisor. That assumes CPython is running
    as an interpreter BINARY. Wherever it's EMBEDDED instead — Chaquopy on
    Android is the live case — sys.executable is not something you can exec,
    and Popen either fails cryptically or (worse) launches the host app again.

    Checked so those modes can refuse with a real explanation. Desktop always
    returns None here, so nothing changes.
    """
    exe = sys.executable or ""
    if not exe or not os.path.isfile(exe):
        return (
            f"this build has no runnable Python interpreter to spawn "
            f"(sys.executable={exe!r}); run one URL per invocation instead"
        )
    return None


# ---------------------------------------------------------------------------
# Structured progress events (opt-in; None on desktop).
#
# The Electron UI learns a run's progress by REGEX-SCRAPING this file's
# human-readable stdout — 14 patterns, see UI-source/electron/downloader.js
# :parseProgressLine. That works, but it couples the UI to print() wording:
# rephrase a log line and a progress bar quietly stops moving.
#
# An embedder that runs main() IN-PROCESS (no pipe to scrape) needs the data
# anyway, so it gets a typed channel instead. Android is the live case —
# grep set_event_sink in aio_android.py.
#
# DELIBERATELY ADDITIVE: every _emit() sits BESIDE its print(), never replaces
# it. Desktop stdout is byte-identical and downloader.js's parser keeps
# working untouched. The sink defaults to None, so on desktop _emit is one
# global read and a return.
#
# Event kinds emitted today: series, chapters_selected, chapter_start,
# chapter_saved, phase, file_saved, missed, recovered, still_missed, done.
# Treat the set as open — consumers must ignore unknown kinds.
# ---------------------------------------------------------------------------
_EVENT_SINK: Optional[Callable[[Dict[str, Any]], None]] = None


def set_event_sink(sink: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    """Install (or clear, with None) the structured-event callback.

    Called once per run by an embedder before main(). The sink runs ON THE
    THREAD THAT EMITS — usually the main chapter loop, but chapter_saved can
    fire from a worker — so it must be cheap and thread-safe. Queue and return;
    don't do UI work inline.
    """
    global _EVENT_SINK
    _EVENT_SINK = sink


def _emit(kind: str, **fields: Any) -> None:
    """Best-effort structured event. Never raises.

    A misbehaving embedder sink must not be able to kill a download that is
    otherwise going fine — losing a progress tick is always preferable to
    losing the run, so every failure here is swallowed on purpose.
    """
    sink = _EVENT_SINK
    if sink is None:
        return
    try:
        payload: Dict[str, Any] = {"kind": kind}
        payload.update(fields)
        sink(payload)
    except Exception:
        pass


# RACE-2: the LEGACY (non-fast-download) image-prefetch path downloads the NEXT
# chapters' pages via dl_image on a background ThreadPool. dl_image /
# _try_download_url poll the FOREGROUND chapter's watchdog (_chapter_cancelled)
# and per-host poison tally (_host_fail_count) — so a foreground chapter's
# deadline/poison would abort the unrelated background prefetch of chapter N+k,
# which then wipes its tdir and the foreground re-downloads it (the exact waste
# the prefetch exists to avoid). And the prefetch's own failures fed the
# foreground poison/ghost accumulators via _record_failure. This thread-local
# marks a thread as a background-prefetch worker; _chapter_cancelled returns
# False, _host_fail_count returns 0, and _record_failure routes to backoff-ONLY
# for such threads — matching the fast path's already-decoupled behavior (grep
# _prefetch_backoff_feedback). dl_image propagates the flag into its parallel
# variant sub-threads (grep _bg_prefetch). The fast path never set this (it
# doesn't call these foreground helpers at all).
_PREFETCH_TLS = threading.local()


def _in_background_prefetch() -> bool:
    return getattr(_PREFETCH_TLS, "active", False)


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
    # RACE-2: a background image-prefetch worker (grep _in_background_prefetch)
    # runs concurrently with some OTHER foreground chapter's window. Its failures
    # must NOT pollute that chapter's poison tally or ghost-signature accumulator
    # — feed ONLY the per-host backoff cap, exactly like the fast path's
    # _prefetch_backoff_feedback (4xx skipped: prefetch has no ghost detector
    # downstream, so a placeholder chapter of uniform 403s must not throttle the
    # run's CDN trust).
    if _in_background_prefetch():
        if status is not None and 400 <= int(status) < 500:
            return
        if host and cls != "permanent":
            _record_host_failure_for_backoff(host, cls)
        return

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
    # RACE-2: a background prefetch worker must not fast-fail on the FOREGROUND
    # chapter's poison tally — 0 keeps it downloading the next chapters.
    if _in_background_prefetch():
        return 0
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

    Called from _record_failure right after the URL bookkeeping, and directly
    from the prefetch fast path's _prefetch_backoff_feedback callback
    (backoff-ONLY feedback — grep _run_image_prefetch_job; it deliberately
    bypasses _record_failure so background failures can't leak into the
    FOREGROUND chapter's poison tally or ghost signatures). Floor is 1.
    Class behavior:
      - rate_limit:   cap //= 2  (aggressive — server is mad at request rate)
      - retryable:    cap -= 1   (we got unlucky; light decrement)
      - origin_error: no-op      (CF 520-527 — upstream sickness; concurrency
                                  reduction doesn't help and may slow recovery
                                  when origin comes back)
      - permanent:    no-op      (4xx — already filtered by _record_failure)

    Every non-permanent call also resets the host's AIMD clean-chapter streak
    (even when the cap doesn't move, e.g. already at floor or origin_error) —
    a fresh failure means recovery must re-earn its next +1 step. Grep
    _record_host_clean_chapter for the increase side.

    Cross-process backoff (sibling worker processes via _COORD) is NOT
    triggered from here — that path goes through _record_rate_limit which
    is hit by the in-band retry logic. The concurrency cap is purely
    local to this process.

    No-op for empty host (defensive — _record_failure already guards but
    we re-check for cheap insurance)."""
    if cls == "permanent":
        return
    if not host:
        return
    with _HOST_CAP_LOCK:
        # Any qualifying failure restarts the recovery clock first, so the
        # streak resets even on classes that don't move the cap.
        _HOST_CLEAN_STREAK.pop(host, None)
        if cls == "origin_error":
            return
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
            # First reduction remembers where the host started so recovery
            # knows when it is fully healed (grep _HOST_CAP_BASELINE).
            _HOST_CAP_BASELINE.setdefault(host, current)
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


def _host_concurrency_capped(host: str) -> bool:
    """True while `host` is under a reduced concurrency cap (it has recorded
    failures this run and hasn't fully recovered — full recovery DELETES the
    cap entry). Used by the prefetch chain-push to shrink queue pressure
    against a struggling CDN (grep 'chain depth' in _process_chapter_impl)."""
    if not host:
        return False
    with _HOST_CAP_LOCK:
        return host in _HOST_CONCURRENCY_CAP


def _record_host_clean_chapter(host: str) -> None:
    """AIMD additive-increase: credit one gate-accepted chapter to `host`;
    after _HOST_CAP_RECOVERY_STREAK consecutive clean chapters, step the
    concurrency cap back up by 1. Climbing back to the host's pre-reduction
    baseline DELETES the cap entry (every pool returns to its own base) —
    see the _HOST_CAP_BASELINE comment at the declarations.

    Called once per chapter from _process_chapter_impl right after the
    completeness gate accepts. Prefetch-adopted chapters count: the host
    served those bytes cleanly too, just on a background worker, and the
    gate fires exactly once per chapter regardless of who downloaded.
    Chapters served by an alt source credit the ALT host (host is the
    chapter's blame host, not the primary's) — correct, since it's the host
    that demonstrated health. The decrease side (_record_host_failure_for_
    backoff) resets the streak on any non-permanent failure, including
    failures recorded concurrently by prefetch workers — so a see-saw under
    sustained sickness settles near the capacity point instead of ratcheting
    to zero or climbing on luck."""
    if not host:
        return
    with _HOST_CAP_LOCK:
        cap = _HOST_CONCURRENCY_CAP.get(host)
        if cap is None:
            # Healthy host: drop any stale streak so the dict can't grow
            # unbounded over a long multi-host run.
            _HOST_CLEAN_STREAK.pop(host, None)
            return
        streak = _HOST_CLEAN_STREAK.get(host, 0) + 1
        if streak < _HOST_CAP_RECOVERY_STREAK:
            _HOST_CLEAN_STREAK[host] = streak
            return
        # Streak complete — spend it on one +1 step.
        _HOST_CLEAN_STREAK.pop(host, None)
        baseline = _HOST_CAP_BASELINE.get(host, 8)
        new_cap = cap + 1
        if new_cap >= baseline:
            _HOST_CONCURRENCY_CAP.pop(host, None)
            _HOST_CAP_BASELINE.pop(host, None)
            log_verbose(
                f"  [Backoff] {host} concurrency fully recovered "
                f"({cap} -> uncapped)"
            )
        else:
            _HOST_CONCURRENCY_CAP[host] = new_cap
            log_verbose(
                f"  [Backoff] Recovering {host} concurrency: {cap} -> "
                f"{new_cap} (after {_HOST_CAP_RECOVERY_STREAK} clean chapters)"
            )


def _reset_host_concurrency_caps() -> None:
    """Clear per-run concurrency-cap state (cap + AIMD baseline/streak).
    Called by _apply_runtime_tunables at run start so each run begins with
    fresh CDN trust. Within a run, recovery is handled by the AIMD increase
    (_record_host_clean_chapter) — the old "capped for the rest of the run"
    rule was retired 2026-07-03 when the prefetch path started feeding the
    cap and descent became fast enough to need an exit ramp."""
    with _HOST_CAP_LOCK:
        _HOST_CONCURRENCY_CAP.clear()
        _HOST_CAP_BASELINE.clear()
        _HOST_CLEAN_STREAK.clear()


def _chapter_cancelled() -> bool:
    """True if the current chapter's watchdog has fired or _CHAPTER_CANCEL was
    set explicitly. Returns False outside a chapter (cover download etc.)."""
    # A RUN-level cancel outranks everything below, INCLUDING the background-
    # prefetch exemption. That ordering is the whole point: prefetch workers are
    # exempt from the foreground chapter's watchdog (RACE-2 below), so checking
    # this after the exemption would leave them downloading the next chapters
    # for minutes after the user pressed stop. Grep request_run_cancel.
    if _RUN_CANCEL.is_set():
        return True
    # RACE-2: background prefetch workers download the NEXT chapters and must not
    # honor the FOREGROUND chapter's watchdog/cancel.
    if _in_background_prefetch():
        return False
    # Capture the global once: the main loop flips _CHAPTER_CANCEL to None between
    # chapters, so reading it twice ("is not None" then ".is_set()") could hit a
    # None on the second read and AttributeError in a worker thread. S5-6.
    evt = _CHAPTER_CANCEL
    return evt is not None and evt.is_set()


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
                # Per-request image Accept: the session default (cloudscraper's)
                # asks for text/html first, and hosts that content-negotiate
                # answer an image URL with an HTML wrapper page — see
                # sites/_image_io.py:IMAGE_ACCEPT. Never hoist onto
                # scraper.headers; this session also fetches HTML pages.
                r = scraper.get(
                    url, stream=True, timeout=timeout,
                    headers=_IMAGE_ACCEPT_HEADERS,
                )
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


def _log_url_for_page(url: str, limit: int = 160) -> str:
    """Shorten a URL for a one-line log. A data: URI is megabytes of base64 —
    print only its MIME segment."""
    if not isinstance(url, str):
        return ""
    if url.startswith("data:"):
        return "data:" + url[5:].split(",", 1)[0][:60]
    return url if len(url) <= limit else url[:limit] + "…"


def _finalize_downloaded_image(pending_pth, folder, base, content_type, name, url):
    """`_finalize_pending_image` + the single verbose log line when the body is
    a 200 that isn't an image (dead host serving an HTML error page under an
    image URL — see sites/_image_io.py:looks_like_real_image).

    Returns the final path, or None. A None is the normal "page failed"
    signal; callers with variants left MUST fall through to them rather than
    give up, because a mirror URL may serve the real bytes.

    Deliberately does NOT call `_record_failure` / `_record_host_failure_for_backoff`:
    the host answered 200 and is healthy, so this is a content problem, and
    feeding it to the Phase-D AIMD concurrency cap would throttle a CDN that
    is doing nothing wrong (grep _HOST_CONCURRENCY_CAP)."""
    reasons = []
    final = _finalize_pending_image(
        pending_pth, folder, base, content_type, on_reject=reasons.append
    )
    if final is None and reasons:
        log_verbose(
            f"  Rejected page {os.path.basename(base)}: {reasons[0]} "
            f"— {_log_url_for_page(url)}"
        )
    return final


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

    # RACE-2: capture prefetch mode once so the parallel variant sub-threads
    # spawned below inherit it (thread-locals don't cross threads); grep
    # _in_background_prefetch / _bg_prefetch.
    _bg = _in_background_prefetch()

    # Phase A (2026-05-07): callers pass `name` like "5_0001.jpg" by historic
    # convention, but the actual bytes may be webp/png/avif. Strip the
    # extension to get the base, write to a `.pending_<base>` tempfile in the
    # same folder, sniff format from magic + Content-Type once bytes land,
    # then atomic-rename to `<base><real_ext>`. Crash window only leaves
    # `.pending_*` files which the resume globs don't match (safe).
    base, _orig_ext = os.path.splitext(name)
    if not base:
        base = name
    # S5-2 (write race): a background-prefetch download uses a DISTINCT pending
    # name so it can't collide with the foreground writing the SAME page into
    # the same tdir after it adopts this chapter (grep _image_prefetch_is_abandoned
    # / _bg above). finalize_pending_image renames by the explicit `base`, NOT
    # the pending basename, so the final page name is identical either way; the
    # two writers' atomic os.replace to that shared final name is last-writer-
    # wins (both fetched the same URL -> same bytes). Leftover .pending_* dotfiles
    # are excluded from the CBZ build (grep "not fn.startswith").
    pending_pth = os.path.join(
        folder, f".pending_{base}" + (".bgprefetch" if _bg else "")
    )

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
        # No fallback exists for an inline body — a rejection is terminal.
        return _finalize_downloaded_image(pending_pth, folder, base, ct, name, url)

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
            # The browser captured these bytes off the wire; there is no URL
            # variant to retry, so a rejection is terminal.
            return _finalize_downloaded_image(
                pending_pth, folder, base, _ct, name, url
            )
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
        _final = _finalize_downloaded_image(
            pending_pth, folder, base, content_type, name, url
        )
        if _final is not None:
            # Logged AFTER validation, not on the 200 — a dead host answering
            # with an HTML error page is an HTTP success and would otherwise
            # print "Successfully downloaded" immediately above "Rejected page".
            log_debug(f"  Successfully downloaded {os.path.basename(name)} using first variant.")
            return _final
        # HTTP said 200 but the body was not an image (dead host answering with
        # an HTML error page). Do NOT return — fall through to the variant
        # cascade below; a `-m`/other-extension mirror may serve real bytes.
        # `first_error` is already None here (_try_download_url returns
        # (True, None, ct) on success), so the sequential-vs-parallel choice
        # below correctly treats this as "no throttling seen".

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
                _final = _finalize_downloaded_image(
                    pending_pth, folder, base, alt_content_type, name, alt_url
                )
                if _final is None:
                    # 200 with a non-image body — keep walking the variants
                    # instead of failing the page on one bad mirror.
                    continue
                log_verbose(
                    f"  Successfully downloaded {os.path.basename(name)} via sequential fallback variant: {os.path.basename(alt_url)}"
                )
                print(f"  [Fallback] {os.path.basename(name)}: succeeded with variant {os.path.basename(alt_url)}")
                return _final

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
        # RACE-2: this runs in a fresh variant sub-thread; carry the prefetch
        # flag over so _chapter_cancelled()/_host_fail_count() below (and inside
        # _try_download_url) stay decoupled from the foreground chapter.
        _PREFETCH_TLS.active = _bg
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
            # Validate + sniff the winning temp file, then atomic-rename into
            # <folder>/<base><ext>. mkstemp put temp_path in `folder`, so this
            # is a same-folder rename — atomic on POSIX and NT, and os.replace
            # (inside the helper) is overwrite-safe where shutil.move was not.
            final_pth = _finalize_downloaded_image(
                temp_path, folder, base, parallel_ct, name, successful_url
            )
            if final_pth is not None:
                log_verbose(f"  Successfully downloaded {os.path.basename(name)} using variant: {os.path.basename(successful_url)}")
                print(f"  [Fallback] {os.path.basename(name)}: succeeded with variant {os.path.basename(successful_url)}")
                return final_pth
            # Winner's body was not an image (the helper already deleted the
            # tempfile). Every variant is exhausted at this point, so fall
            # through to the all-attempts-failed return below.
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

        # Emit every full segment as its own line; carry only the LAST segment
        # as `current` so the next word can join the trailing partial. The old
        # `and not current` gate held the first segment then flushed ALTERNATING
        # segments, dropping every other one — halving CJK / long-word text that
        # has no spaces to break on (a 6-segment word became 3 lines). Review
        # finding S1-1.
        segments = _split_long_word(word, font, max_width)
        for segment in segments[:-1]:
            lines.append(segment)
        if segments:
            current = segments[-1]

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


# ── CPU-pool budget (Settings → Resource Limits → Max CPU usage) ──────────
# _CPU_POOL_PERCENT scales the logical-core budget the CPU-BOUND image pools
# may use. Set from --max-cpu-percent (env AIO_MAX_CPU_PERCENT) in
# _apply_runtime_tunables; re-seeded on --restore-parameters. 100 = the prior
# default (os.cpu_count()), so a default run is byte-for-byte unchanged.
#
# Consumed by the THREE CPU-bound pool sites — grep _cpu_pool_budget:
#   process_chapter_images (PDF/img decode), save_final_images (final encode),
#   _run_recompress_pool (modernize + webp recompress). Each does
#   ``workers = cpu//2`` and (recompress) ``enc_threads = cpu//workers``, so
#   scaling the budget scales workers AND per-encode threads together — total
#   threads track the budget (reducing only workers would keep total flat).
# NOT applied to the network pools (prefetch / foreground download) — those are
# throttled via --image-concurrency / --image-workers / --image-prefetch-*.
# UI: electron/main.js resolves Settings.cpuLimit → --max-cpu-percent (grep
# cpuPercentForLevel in UI-source/electron/resource-limits.js).
_CPU_POOL_PERCENT = 100


def _cpu_pool_budget() -> int:
    """Effective logical-core budget for the CPU-bound image pools, scaled by
    --max-cpu-percent. Returns os.cpu_count() at 100% (the unchanged default)."""
    cpu = os.cpu_count() or 4
    return max(1, round(cpu * globals().get("_CPU_POOL_PERCENT", 100) / 100.0))


def process_chapter_images(
    input_paths: List[str],
    target_w: int,
    target_h: int,
    *,
    dropped: Optional[List[str]] = None,
) -> List[Image.Image]:
    """
    Uses a "fill the gap" algorithm to combine and slice images in memory.
    Returns a list of final page images as PIL objects.

    ``dropped`` (out-parameter): every input path whose decode RAISED is appended
    to it. Callers need this because the returned length is 1:N by design here —
    the fill-the-gap assembly merges many source strips into fewer pages — so
    comparing counts cannot distinguish "recombined" from "lost N pages to decode
    failures". The reconciliation that consumes it is at the _process_chapter_impl
    call site; grep decode_dropped_pages. ``list.append`` is atomic, so the decode
    ThreadPool below appends without a lock.

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
            if dropped is not None:
                dropped.append(path)
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
        # CPU budget, scaled by --max-cpu-percent (grep _cpu_pool_budget)
        cpu = _cpu_pool_budget()
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
    input_paths: List[str],
    target_w: int,
    *,
    dropped: Optional[List[str]] = None,
) -> List[Image.Image]:
    """Resizes images to a target width and returns PIL objects.

    ``dropped`` (out-parameter): paths whose decode raised. Same contract as
    process_chapter_images' — grep decode_dropped_pages for the consumer.
    """
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
            if dropped is not None:
                dropped.append(path)
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
                # MAGIC BYTES, not Image.open. `images[i]` is already a decoded
                # PIL image — the source file is consulted ONLY to learn what
                # format it arrived in, and the three-way decision below
                # (WEBP / JPEG / everything-else-goes-PNG) is exactly what a
                # 64-byte header answers. Opening it with PIL made a
                # metadata-only question pay a decode:
                #   * desktop — one PIL open + parse per page, per chapter;
                #   * Android — the image-codec bridge shim decodes EAGERLY in
                #     _open (a JNI BitmapFactory decode, a PNG re-encode and a
                #     PIL PNG decode), so reading a format string cost a full
                #     round trip through the bridge for every page.
                # An unrecognized signature yields None and falls to the same
                # PNG branch that a failed Image.open already fell to, so the
                # decision is unchanged for every format either path knows.
                # Cross-file: sites/_image_io.py:image_magic_extension.
                try:
                    with open(source_paths[i], "rb") as _probe_fh:
                        _head = _probe_fh.read(64)
                    src_fmt = {
                        ".webp": "WEBP",
                        ".jpg": "JPEG",
                    }.get(_image_magic_extension(_head) or "")
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
        # CPU budget, scaled by --max-cpu-percent (grep _cpu_pool_budget)
        cpu = _cpu_pool_budget()
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
# Shared page-recompress concurrency driver
# -----------------------------------------------------------
def _run_recompress_pool(
    raw_paths: List[str],
    convert_one: Callable[
        [Tuple[int, str], int], Tuple[int, str, Optional[Exception]]
    ],
    *,
    guard: str,
    thread_prefix: str,
    label: str,
    mop_up: Optional[
        Callable[[List[Tuple[int, str]], List[Optional[str]], int, int], None]
    ] = None,
) -> List[str]:
    """Shared dispatch skeleton for the two in-place page-recompress passes
    (recompress_chapter_images_to_webp + recompress_chapter_images_modern),
    which carried a byte-identical worker-pool loop.

    Owns everything both twins shared: the cpu//2 worker count, the per-encode
    thread budget (``enc_threads = cpu//workers`` — the anti-oversubscription
    cap; webp ignores it, modern hands it to libjxl/libavif), the
    serial-vs-ThreadPoolExecutor branch, the every-8-pages ``_hb`` heartbeat, and
    the ``[p for p in out if p is not None]`` return. ``convert_one`` takes
    ``(enumerate-entry, enc_threads)`` and returns ``(idx, dst, err)``: ``err`` is
    ``None`` for webp (no encode-failure path) and carries the exception for
    modern's mop-up. When ``mop_up`` is provided it runs INSIDE ``_cpu_guard``
    (modern's serial retry must stay CPU-guarded — a real cross-process guard
    when _COORD is set) with ``(failed, out, enc_threads, cpu)``; it mutates
    ``out`` in place. grep _run_recompress_pool for the two callers.

    workers uses ``max(1, min(cpu//2, len))`` — proven identical to webp's older
    ``max(1, min(max(1, cpu//2), len))`` for every cpu>=1, and raw_paths is
    always non-empty here (both callers early-return on the empty list).
    """
    # CPU budget, scaled by --max-cpu-percent (grep _cpu_pool_budget)
    cpu = _cpu_pool_budget()
    workers = max(1, min(cpu // 2, len(raw_paths)))
    enc_threads = max(1, cpu // workers)

    out: List[Optional[str]] = [None] * len(raw_paths)
    failed: List[Tuple[int, str]] = []  # (idx, src) whose encode raised
    with _cpu_guard(guard):
        if workers == 1 or len(raw_paths) == 1:
            for entry in enumerate(raw_paths):
                idx, dst, err = convert_one(entry, enc_threads)
                out[idx] = dst
                if err is not None:
                    failed.append((idx, raw_paths[idx]))
                if idx % 8 == 0:
                    _hb("cpu", f"{label} {idx+1}/{len(raw_paths)}")
        else:
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix=thread_prefix
            ) as pool:
                # pool.map preserves submission order; consumed iteratively so
                # memory stays bounded. Native encoders (libwebp/libjxl/libavif)
                # release the GIL during the heavy encode, so workers translate
                # to speedup; a GIL-holding build just serializes (correct, only
                # slower). _hb every 8 keeps the per-chapter watchdog satisfied.
                for idx, dst, err in pool.map(
                    lambda e: convert_one(e, enc_threads),
                    list(enumerate(raw_paths)),
                ):
                    out[idx] = dst
                    if err is not None:
                        failed.append((idx, raw_paths[idx]))
                    if idx % 8 == 0:
                        _hb("cpu", f"{label} {idx+1}/{len(raw_paths)}")

        if mop_up is not None and failed:
            mop_up(failed, out, enc_threads, cpu)

    # No None entries possible on the webp path (every convert returns a str);
    # modern's mop-up also fills every slot. The filter/cast keeps mypy quiet.
    return [p for p in out if p is not None]


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
                # Animated source (animated GIF / APNG): a static WebP re-encode
                # would collapse it to frame 0. Preserve the original bytes.
                # Flatten guard — grep _is_animated_image. (.webp sources
                # already passed through above; animated WebP is handled there.)
                if getattr(im, "is_animated", False):
                    log_debug(
                        f"    Recompress skip (animated, preserved): "
                        f"{os.path.basename(src)}"
                    )
                    return idx, src
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

    # Shared concurrency driver (grep _run_recompress_pool). webp has no
    # encode-failure retry path, so the adapter tags every result err=None (the
    # mop-up never fires) and ignores the enc_threads budget.
    return _run_recompress_pool(
        raw_paths,
        lambda entry, _enc_threads: (*_convert_one(entry), None),
        guard="recompress_webp",
        thread_prefix="webp-recompress",
        label="recompress",
    )


# -----------------------------------------------------------
# Content-aware JXL/AVIF recompression (opt-in via --modernize)
# -----------------------------------------------------------
# Structural twin of recompress_chapter_images_to_webp (above): same cpu//2
# ThreadPool, same write-temp-then-os.remove-original atomicity, same broad-
# catch keep-original-on-failure. Differences:
#   * Content-aware per-page routing: B&W line art -> JXL, color -> AVIF.
#     Routing is a SIZE decision ONLY — we never convert("L") (empirically 0%
#     gain at distance 1.0, since libjxl already zeroes imperceptible chroma,
#     and it would be the one irreversible op), so a misroute costs bytes,
#     never pixels.
#   * A per-page min_saving guard: adopt the new file only if it's smaller than
#     orig*min_saving, else keep the original byte-for-byte. Never bloats, and
#     self-corrects a gray->AVIF misroute (AVIF on line art is ~95% of source,
#     above the 0.92 guard, so the original is kept).
#   * Skips WEBP/AVIF/GIF/JXL sources (already efficient / animated / already
#     modern) so re-runs are idempotent.
# Cross-file: gated call site at the top of the `if raw_image_paths:` block in
# the chapter pipeline (grep 'recompress_chapter_images_modern('), which runs
# BEFORE cbz_fast_path so the new bytes flow straight into build_cbz with
# correct per-file extensions (build_cbz arcname preserves splitext, ~line
# 3389). --modernize is hard-gated at parse time to the CBZ byte-passthrough
# fast-path (grep '--modernize compatibility checks'): every fast-path-
# disabling flag is a p.error(), because the slow save_final_images path only
# understands WEBP/JPEG and would re-encode .jxl/.avif to PNG. Resume: the
# --modernize* dests are in _RESUME_GATING_DESTS so changing them re-transcodes.

# Pages larger than this in either dimension route to JXL regardless of color:
# AVIF encodes them fine but its on-device (Android) decode at extreme heights
# (long-strip webtoons up to ~15000px) is unverified, and JXL measured strictly
# better on tall strips anyway — 12 MP strip: JXL d1 968,501 B / butteraugli
# 1.23 / 27.6 MP/s decode vs AVIF-444 1,072,135 B / bt 2.28 / 13.0 MP/s
# (libavif 1.4.2, 2026-07-02 bench). Under an
# avif-only policy the user opted out of JXL, so oversized pages are skipped
# (original kept) instead of emitting an unasked-for format.
_MODERNIZE_MAX_DIM = 8192

# Source formats with no re-encode headroom (already efficient, animated, or
# already a modern codec). Matched against PIL's reported Image.format.
_MODERN_SKIP_FORMATS = frozenset({"WEBP", "AVIF", "GIF", "JXL"})

# In strict-lossless mode (--modernize-distance 0.0) pillow_jxl does our
# intended bit-exact JPEG->JXL reconstruction and warns once per page
# suggesting lossless_jpeg — pure noise, since reconstruction is exactly what
# we want there. Silence that one specific message process-wide. Set at import
# (not in recompress_chapter_images_modern, which would re-append to the global
# filter list every chapter) and only matches our own JXL save, so nothing else
# is affected. The lossy default path passes lossless_jpeg=False and never
# triggers it. Grep 'lossless_jpeg' for the matching save site.
import warnings as _warnings  # noqa: E402

_warnings.filterwarnings(
    "ignore", message="Using JPEG reconstruction", category=UserWarning
)


def _page_is_grayscale(im, chroma_thresh: int = 16, area_frac: float = 0.005) -> bool:
    """True if a page should route to JXL (grayscale) vs AVIF (color).

    ROUTING ONLY — never a pixel decision. Because recompress_chapter_images_
    modern never reduces a page to mode L, a wrong verdict here only changes
    which codec is tried (a size trade-off), never destroys color.

    Full-resolution colored-area fraction, not a downscaled probe: a small but
    real color element (e.g. a 5%-area panel) survives at full res but gets
    averaged away by a 20x20 thumbnail — which is why we do NOT reuse
    sites.search_orchestrator._is_grayscale_pil (its p90/thumbnail probe was
    built for whole-series classification, not per-page color preservation, and
    importing it would couple this hot path to the search/ML module). Counting
    the fraction of pixels whose channel spread exceeds chroma_thresh is robust
    to JPEG chroma ringing on B&W scans (low-amplitude, sub-threshold) yet
    catches genuine local color. Thresholds are starting points — tune against
    the real library if routing drifts (grep '_page_is_grayscale').
    """
    if getattr(im, "mode", None) in ("L", "LA", "1"):
        return True
    import numpy as np  # hard dep (requirements.txt); lazy like _is_grayscale_pil
    arr = np.asarray(im.convert("RGB"), dtype=np.int16)
    chroma = arr.max(axis=2) - arr.min(axis=2)  # per-pixel max channel spread
    return float((chroma > chroma_thresh).mean()) < area_frac


def _is_animated_image(path: str) -> bool:
    """True if the file at ``path`` holds more than one frame — animated GIF,
    APNG (animated PNG), or animated WebP.

    WHY generic multi-frame detection instead of format-name matching: APNG
    reports Image.format == "PNG" (so it slips straight through
    _MODERN_SKIP_FORMATS, which only catches literal GIF/WEBP/AVIF/JXL), and a
    static single-frame GIF should NOT be treated as animated. PIL's
    is_animated / n_frames is the reliable signal. Used by the transform-path
    flatten guard (grep '_is_animated_image'): the WebP/modern recompress fast
    paths pass the animated original through byte-for-byte instead of decoding
    it to frame 0, and the lossy slow path (EPUB/PDF/scaling) emits a one-time
    warning. Any open failure returns False — treat as static and let the
    caller's existing decode path deal with a genuinely broken file.

    Callers that already have the PIL image open (recompress _convert_one)
    check ``getattr(im, "is_animated", False)`` inline to avoid a second open;
    this helper is for the path-only sites (the slow-path warning scan).
    """
    try:
        with Image.open(path) as im:
            if getattr(im, "is_animated", False):
                return True
            return int(getattr(im, "n_frames", 1) or 1) > 1
    except Exception:
        return False


def _warn_animated_flatten_once(raw_paths: List[str], fmt: str) -> None:
    """Emit ONE run-wide warning when a page on the lossy transform path
    (EPUB / PDF / --width / --scaling≠100 / --quality<100) is animated — those
    paths decode to a single PIL frame and drop the animation.

    The default CBZ fast-path and the --webtoon-recompress / --modernize paths
    preserve animation byte-for-byte (grep _is_animated_image); this fires only
    where flattening is structural, so the user is told instead of silently
    losing frames. Once-per-run state lives on a function attribute (no global
    needed from the nested caller). Short-circuits on the first animated page,
    so it's cheap even on the slow path we're already decoding.
    """
    if getattr(_warn_animated_flatten_once, "_warned", False):
        return
    if any(_is_animated_image(p) for p in raw_paths):
        _warn_animated_flatten_once._warned = True  # type: ignore[attr-defined]
        print(
            f"[!] Animated page(s) detected but --format {fmt} flattens them "
            f"to the first frame. Use the default --format cbz with no "
            f"--width/--scaling/--quality override to preserve animated "
            f"GIF/APNG byte-for-byte.",
            file=sys.stderr,
        )


def recompress_chapter_images_modern(
    raw_paths: List[str],
    *,
    policy: str,
    gray_quality: float,
    color_quality: int,
    min_saving: float,
    effort: int = 7,
    speed: int = 6,
) -> List[str]:
    """Content-aware transcode of JPEG/PNG pages to JXL (B&W) / AVIF (color).

    Opt-in via --modernize. ``policy``: "auto" (JXL for gray, AVIF for color) |
    "jxl" | "avif" | "jxl+avif" (encode both, keep the smaller). ``gray_quality``
    is the JXL distance (1.0 ~ visually lossless; 0.0 selects JXL lossless
    mode); ``color_quality`` is the AVIF quality (90 default; 85 aggressive).
    ``min_saving`` is the keep-threshold: the new file replaces the original
    only if its size < orig_size * min_saving, else the original is kept
    byte-for-byte (so already-dense pages are auto-skipped and nothing bloats).

    ``effort`` (JXL, 1-9) and ``speed`` (AVIF, 0-10) are the pure CPU<->size
    knobs, surfaced as --modernize-effort / --modernize-avif-speed. They change
    encode time and output size ONLY — never the decoded pixels — so they are
    deliberately NOT in _RESUME_GATING_DESTS (a mid-run change keeps completed
    chapters and applies the new value going forward, rather than nuking the
    partial). Axes are INVERSE: higher JXL effort = slower + smaller; higher
    AVIF speed = faster + larger. Defaults 7 / 6 are the measured sweet spot —
    e9 is a CPU trap (~7.5x slower than e7 for ~5% smaller; e8 matches e9 size
    at ~1.5x speed), and AVIF s4 is ~5x slower than s6 for ~2% smaller while
    s10 bloats ~13%. Bench: tools/modernize_library.py header + the memory
    note modernize-effort9-cpu-trap.

    Returns the per-slot path list (new .jxl/.avif where it helped, otherwise
    the unchanged original path), matching recompress_chapter_images_to_webp's
    contract — the caller reassigns raw_image_paths to it. See the section
    header above for the routing/guard rationale and the fast-path coupling.
    """
    if not raw_paths:
        return list(raw_paths)

    # JXL is an optional plugin (pillow-jxl-plugin); importing it registers the
    # encoder in PIL.Image.SAVE. AVIF is native in Pillow >= 12. --modernize is
    # validated at parse time (grep '--modernize compatibility checks') so both
    # are present in the normal flow; the try/except keeps the function callable
    # from tests when JXL isn't installed (a missing-encoder save then fails
    # per-page and the original is kept).
    if policy != "avif":
        try:
            import pillow_jxl  # noqa: F401
        except ImportError:
            pass
    if policy != "jxl":
        # AVIF is native in Pillow >= 12; the pillow-avif-plugin fallback for
        # older Pillow only registers on import (Image.init() won't trigger it).
        # Best-effort so an avif/auto/jxl+avif policy works on pre-12 Pillow too,
        # mirroring the pillow_jxl import and the parse-time gate (grep
        # 'import pillow_avif'). Missing -> the AVIF save fails per-page and the
        # original is kept (broad catch in _convert_one).
        try:
            import pillow_avif  # noqa: F401
        except ImportError:
            pass
    # Register plugins once, single-threaded, before the worker pool starts.
    # The native AVIF plugin registers lazily on first save; letting parallel
    # workers trigger that registration concurrently is a data race. Idempotent.
    Image.init()

    def _pick_target(im, w: int, h: int) -> str:
        if max(w, h) > _MODERNIZE_MAX_DIM:
            return "jxl" if policy != "avif" else "skip"
        if policy == "jxl":
            return "jxl"
        if policy == "avif":
            return "avif"
        if policy == "jxl+avif":
            return "both"
        return "jxl" if _page_is_grayscale(im) else "avif"  # auto

    def _convert_one(
        entry: Tuple[int, str], nthreads: int
    ) -> Tuple[int, str, Optional[Exception]]:
        """Transcode one page; return (idx, result_path, error).

        error is None on success OR a legitimate keep (skip format, animated
        source, no min_saving headroom). It carries the exception ONLY when the
        encode itself raised — those slots keep the original for now and are
        handed to the serial mop-up pass in the driver below. ``nthreads`` bounds
        each encoder's internal thread pool (grep enc_threads — the fix for the
        intermittent libjxl JXL_ENC_ERROR under pool oversubscription).
        """
        idx, src = entry
        base, _ = os.path.splitext(src)
        try:
            orig_size = os.path.getsize(src)
            with Image.open(src) as im:
                src_fmt = (im.format or "").upper()
                # Skip already-efficient/modern codecs AND any animated source.
                # APNG reports src_fmt == "PNG" (so it slips _MODERN_SKIP_FORMATS)
                # yet is_animated catches it — flattening it to a single JXL/AVIF
                # frame would drop the animation. Flatten guard: grep
                # _is_animated_image.
                if src_fmt in _MODERN_SKIP_FORMATS or getattr(im, "is_animated", False):
                    return idx, src, None
                w, h = im.size
                target = _pick_target(im, w, h)
                if target == "skip":
                    return idx, src, None
                # (size, path, is_recon). is_recon marks the JXL save that ran
                # pillow_jxl's bit-exact JPEG->JXL *reconstruction* (JPEG file
                # source + strict-lossless tier + no mode conversion): zero
                # quality cost AND byte-recoverable (djxl reconstructs the
                # original .jpg), so the adopt guard below exempts it from
                # min_saving — any saving is pure win. Mirrored in
                # tools/modernize_library.py (grep is_recon).
                candidates: List[Tuple[int, str, bool]] = []
                if target in ("jxl", "both"):
                    jxl_path = base + ".jxl"
                    # Encode as-is (no convert("L") — see header). Keep alpha-
                    # capable modes (JXL carries alpha); map paletted
                    # transparency to RGBA so palette alpha isn't flattened; only
                    # widen truly exotic modes pillow_jxl can't take (opaque P /
                    # CMYK / I / ...) to RGB. Mirrors the AVIF branch's no-flatten
                    # rule (grep 'AVIF carries alpha').
                    if im.mode in ("L", "LA", "RGB", "RGBA"):
                        jxl_src = im
                    elif im.mode == "PA" or (
                        im.mode == "P" and "transparency" in im.info
                    ):
                        jxl_src = im.convert("RGBA")
                    else:
                        jxl_src = im.convert("RGB")
                    # JPEG quirk (measured on file-based sources, which is all
                    # we get): pillow_jxl's default lossless_jpeg=True does
                    # bit-exact JPEG *reconstruction* and SILENTLY IGNORES
                    # distance (~78%, diff=0, and warns). So:
                    #   * lossy/visually-lossless default -> force
                    #     lossless_jpeg=False so --modernize-distance actually
                    #     applies (58%, diff>0, no warning).
                    #   * gray_quality == 0.0 (strict lossless) -> KEEP the
                    #     default: lossless=True then gives bit-exact JPEG->JXL
                    #     reconstruction (~78%) for JPEG and pixel-lossless for
                    #     PNG automatically — exactly the two lossless tiers, no
                    #     external cjxl needed. (PNG/other sources ignore
                    #     lossless_jpeg either way.) Bench 2026-07-02:
                    #     reconstruction = 12.7-92.6% of the source JPEG,
                    #     sha256-verified reversible; see
                    #     ~/.claude/plans/compression-modernize-handoff.md.
                    # num_threads caps pillow_jxl's internal Encoder pool
                    # (default -1 = ALL cores) so `workers` concurrent encodes
                    # don't oversubscribe — the fix for the intermittent
                    # JXL_ENC_ERROR on large pages (grep enc_threads in the
                    # driver). Bit-identical output, verified across 1/2/4/-1.
                    jxl_src.save(
                        jxl_path,
                        format="JXL",
                        effort=effort,
                        num_threads=nthreads,
                        **({"lossless": True} if gray_quality == 0.0
                           else {"distance": gray_quality, "lossless_jpeg": False}),
                    )
                    # A CMYK JPEG went through convert("RGB") above, severing
                    # pillow_jxl's access to the original bitstream — that save
                    # is a pixel-lossless encode of converted pixels, NOT a
                    # byte-recoverable reconstruction, hence the `is im` term.
                    is_recon = (
                        src_fmt == "JPEG"
                        and gray_quality == 0.0
                        and jxl_src is im
                    )
                    candidates.append(
                        (os.path.getsize(jxl_path), jxl_path, is_recon)
                    )
                if target in ("avif", "both"):
                    avif_path = base + ".avif"
                    # AVIF carries alpha — never flatten it. The CBZ byte-
                    # passthrough fast path this rides preserved the original PNG,
                    # so a transparent page (RGBA / LA / paletted-transparency)
                    # must stay transparent; converting it to RGB here would make
                    # the background opaque. Map every alpha-bearing source to
                    # RGBA and widen the rest (L / opaque-P / CMYK / ...) to RGB.
                    # Grayscale color-routed pages are rare (auto sends B&W to
                    # JXL), so the L->RGB widening only bites forced
                    # --modernize-format avif / jxl+avif.
                    if im.mode == "RGB":
                        avif_src = im
                    elif im.mode in ("RGBA", "LA", "PA") or (
                        im.mode == "P" and "transparency" in im.info
                    ):
                        avif_src = im.convert("RGBA")
                    else:
                        avif_src = im.convert("RGB")
                    # 4:4:4 chroma, NOT Pillow's 4:2:0 default: q90 at 4:2:0
                    # measured butteraugli ~7.1 / SSIMULACRA2 ~78 on saturated
                    # color (chroma bleed on fine colored detail), reproduced on
                    # libavif 1.4.2 — subsampling-inherent, not encoder vintage.
                    # 4:4:4 measured bt ~1.3 at ~+12% bytes (2026-07-02 bench).
                    # Hardcoded, not a flag: no new argparse dest, so no
                    # resume-gating impact (grep _RESUME_GATING_DESTS).
                    # max_threads caps libavif's pool (default = all cores) for
                    # the same anti-oversubscription reason as JXL's num_threads.
                    avif_src.save(
                        avif_path,
                        format="AVIF",
                        quality=color_quality,
                        speed=speed,
                        subsampling="4:4:4",
                        max_threads=nthreads,
                    )
                    candidates.append(
                        (os.path.getsize(avif_path), avif_path, False)
                    )
        except Exception as e:
            # Corrupt page, missing encoder, DecompressionBomb, OR a transient
            # encoder failure under load (libjxl JXL_ENC_ERROR — grep
            # enc_threads). Clean the partial temp and report the error UP: the
            # driver's serial mop-up retries this page single-file (no pool
            # contention) before conceding, so we do NOT warn here — the mop-up
            # warns only if the retry also fails. One bad page never aborts the
            # chapter (mirrors the webp fn's broad catch at ~line 2894).
            for _ext in (".jxl", ".avif"):
                try:
                    os.remove(base + _ext)
                except OSError:
                    pass
            return idx, src, e

        # Pick the smallest candidate; discard the rest (jxl+avif loser).
        candidates.sort(key=lambda c: c[0])
        best_size, best_path, best_is_recon = candidates[0]
        for _sz, loser, _recon in candidates[1:]:
            try:
                os.remove(loser)
            except OSError:
                pass
        # Guard: adopt the new file only if it clears the savings threshold.
        # JPEG reconstructions are exempt (adopt iff strictly smaller): the
        # candidate is byte-recoverable, so unlike a lossy candidate there is
        # no quality cost to weigh a marginal saving against — 92-93%-ratio
        # line-art JPEGs would otherwise keep their originals for no benefit.
        if best_size < orig_size * (1.0 if best_is_recon else min_saving):
            try:
                os.remove(src)
            except OSError as e:
                # New file is fine; couldn't delete the original (AV / OneDrive
                # lock). Leftover gets wiped on the next chapter-dir reset.
                log_debug(
                    f"    Modernize: kept original alongside "
                    f"{os.path.splitext(best_path)[1]} ({e}): "
                    f"{os.path.basename(src)}"
                )
            return idx, best_path, None
        # Not enough headroom — drop the new file, keep the original bytes.
        try:
            os.remove(best_path)
        except OSError:
            pass
        return idx, src, None

    # Serial mop-up for pages whose parallel encode raised (almost always a
    # transient libjxl JXL_ENC_ERROR under pool oversubscription — grep
    # enc_threads): retry each single-file with the FULL core count (no sibling
    # encoders competing — the low-contention condition that never failed in
    # testing), twice with a short backoff before conceding, so a transient
    # failure becomes "page transcoded a beat later" instead of "silently left
    # un-modernized". One bad page never aborts the chapter. _run_recompress_pool
    # invokes this INSIDE _cpu_guard (before releasing it); it mutates `out`.
    def _mop_up(
        failed: List[Tuple[int, str]],
        out: List[Optional[str]],
        _enc_threads: int,
        cpu: int,
    ) -> None:
        log_verbose(
            f"  [modernize] Retrying {len(failed)} page(s) that failed "
            f"under load (serial)..."
        )
        for idx, src in failed:
            dst = src
            err: Optional[Exception] = None
            for _attempt in range(2):
                _hb("cpu", f"modernize retry {os.path.basename(src)}")
                _, dst, err = _convert_one((idx, src), cpu)
                if err is None:
                    break
                time.sleep(0.3 * (_attempt + 1))
            out[idx] = dst
            if err is not None:
                log_verbose(
                    f"  Warning: modernize transcode failed for "
                    f"{os.path.basename(src)}: {err}. Keeping original."
                )

    # Shared concurrency driver (grep _run_recompress_pool) owns the cpu//2
    # worker count + the enc_threads=cpu//workers anti-oversubscription cap — the
    # fix for the intermittent libjxl "Generic Error" / JXL_ENC_ERROR: pillow_jxl
    # AND native AVIF each default to an all-cores internal pool; nested under a
    # `workers`-thread page pool they compound to workers*cpu threads (e.g.
    # 6*12=72 on a 12-core box) and trip libjxl's runner on ~62 MP pages under
    # load. Thread count NEVER changes output bytes (sha-identical + recon
    # bit-exact across 1/2/4/-1), so it's invisible to the CBZ and resume gating.
    # _convert_one already matches the driver's (entry, enc_threads) ->
    # (idx, dst, err) contract.
    return _run_recompress_pool(
        raw_paths,
        _convert_one,
        guard="recompress_modern",
        thread_prefix="modern-recompress",
        label="modernize",
        mop_up=_mop_up,
    )


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


def _emit_tags_extended(tags: List[Any], indent: str = "  ") -> str:
    """Render an AniList tag list as a <TagsExtended> XML block.

    Empty list returns empty string so the caller can do
    `if block: out.append(block)`. Categories and names are XML-escaped
    (quoteattr for attribute values, escape for text content); numeric
    and boolean attributes are rendered without quoting concerns.

    `tags` is duck-typed as iterable of sites.external_metadata.AnilistTag
    instances (name/category/rank/is_media_spoiler/is_general_spoiler).

    Custom non-standard ComicInfo element — used by the user's own
    reader for category/rank/spoiler-aware tag display. Standard
    readers (Komga, Kavita) silently drop unknown elements per the
    ComicInfo lenient-parsing convention. Cross-file:
    sites/external_metadata.py:AnilistTag is the source dataclass; the
    parallel comma-separated <Tags>/<SpoilerTags> emit alongside this
    for standard-reader compat.
    """
    if not tags:
        return ""
    lines = [f"{indent}<TagsExtended>"]
    for t in tags:
        cat_attr = xml.sax.saxutils.quoteattr(getattr(t, "category", "") or "")
        rank_attr = int(getattr(t, "rank", 0) or 0)
        gen_spoiler = "true" if getattr(t, "is_general_spoiler", False) else "false"
        med_spoiler = "true" if getattr(t, "is_media_spoiler", False) else "false"
        name = xml.sax.saxutils.escape(str(getattr(t, "name", "") or ""))
        lines.append(
            f'{indent}  <Tag category={cat_attr} rank="{rank_attr}" '
            f'generalSpoiler="{gen_spoiler}" mediaSpoiler="{med_spoiler}">'
            f'{name}</Tag>'
        )
    lines.append(f"{indent}</TagsExtended>")
    return "\n".join(lines)


def build_comic_info_xml(
    title: str,
    comic_info: Dict,
    publishers: List[str],
    lang: str,
    page_count: int,
) -> str:
    """Generates the ComicInfo.xml string for CBZ files.

    Standard ComicInfo.xml elements are always emitted. Custom non-
    standard elements (<Tags>/<SpoilerTags>/<TagsExtended>/
    <CountryOfOrigin>/<MediaFormat>/<AnilistId>/<MalId>) are emitted
    only when populated by --metadata-source=anilist enrichment (see
    sites/external_metadata.py). Standard readers like Komga and
    Kavita silently drop the custom ones.
    """

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

    # AniList enrichment fields — populated only when
    # --metadata-source=anilist found a confident match (else absent).
    # See sites/external_metadata.py for the field provenance.
    anilist_tags = comic_info.get("anilist_tags") or []
    anilist_spoiler_tags = comic_info.get("anilist_spoiler_tags") or []
    country = comic_info.get("country_of_origin")
    media_format = comic_info.get("media_format")
    anilist_id = comic_info.get("anilist_id")
    mal_id = comic_info.get("mal_id")

    lines: List[str] = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
        f'    <Title>{escape(title)}</Title>',
        f'    <Series>{escape(title)}</Series>',
        f'    <Summary>{escape(description)}</Summary>',
        f'    <Writer>{escape(authors)}</Writer>',
        f'    <Penciller>{escape(artists)}</Penciller>',
        f'    <Publisher>{escape(publisher)}</Publisher>',
        f'    <Genre>{escape(genre)}</Genre>',
    ]

    if anilist_tags:
        comma_tags = ", ".join(t.name for t in anilist_tags)
        lines.append(f'    <Tags>{escape(comma_tags)}</Tags>')
    if anilist_spoiler_tags:
        comma_spoilers = ", ".join(t.name for t in anilist_spoiler_tags)
        lines.append(f'    <SpoilerTags>{escape(comma_spoilers)}</SpoilerTags>')
    if anilist_tags or anilist_spoiler_tags:
        # Combine into one <TagsExtended> block — the per-tag
        # mediaSpoiler/generalSpoiler attributes preserve the split.
        block = _emit_tags_extended(
            list(anilist_tags) + list(anilist_spoiler_tags),
            indent="    ",
        )
        if block:
            lines.append(block)

    lines.append(f'    <LanguageISO>{escape(lang)}</LanguageISO>')
    lines.append(f'    <PageCount>{page_count}</PageCount>')
    lines.append(f'    <ScanInformation>{escape(publisher)}</ScanInformation>')

    if country:
        lines.append(f'    <CountryOfOrigin>{escape(country)}</CountryOfOrigin>')
    if media_format:
        lines.append(f'    <MediaFormat>{escape(media_format)}</MediaFormat>')
    if anilist_id is not None:
        lines.append(f'    <AnilistId>{int(anilist_id)}</AnilistId>')
    if mal_id is not None:
        lines.append(f'    <MalId>{int(mal_id)}</MalId>')

    lines.append('</ComicInfo>')
    return "\n".join(lines) + "\n"


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
    aux_records: Optional[Dict[str, Any]] = None,
    missing_pages: Optional[List[int]] = None,
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

    # AniList enrichment fields — emitted only when --metadata-source=
    # anilist produced a confident match for this series. See
    # sites/external_metadata.py + the enrichment block in main() right
    # after allocate_series_output_dir. Standard ComicInfo readers
    # (Komga, Kavita) drop unknown elements silently.
    anilist_tags = comic_info.get("anilist_tags") or []
    anilist_spoiler_tags = comic_info.get("anilist_spoiler_tags") or []
    if anilist_tags:
        comma_tags = ", ".join(t.name for t in anilist_tags)
        lines.append(f'  <Tags>{escape(comma_tags)}</Tags>')
    if anilist_spoiler_tags:
        comma_spoilers = ", ".join(t.name for t in anilist_spoiler_tags)
        lines.append(f'  <SpoilerTags>{escape(comma_spoilers)}</SpoilerTags>')
    if anilist_tags or anilist_spoiler_tags:
        block = _emit_tags_extended(
            list(anilist_tags) + list(anilist_spoiler_tags),
            indent="  ",
        )
        if block:
            lines.append(block)

    if web_url:
        lines.append(f'  <Web>{escape(web_url)}</Web>')
    if lang:
        lines.append(f'  <LanguageISO>{escape(lang)}</LanguageISO>')
    if year is not None:
        lines.append(f'  <Year>{year}</Year>')
        lines.append(f'  <Month>{month}</Month>')
        lines.append(f'  <Day>{day}</Day>')
    lines.append(f'  <PageCount>{int(page_count) if page_count else 0}</PageCount>')

    # AniList enrichment singletons. Placed at the end (after <PageCount>)
    # so the standard ComicInfo block stays first and structurally intact
    # for any reader that scans top-to-bottom.
    country = comic_info.get("country_of_origin")
    media_format = comic_info.get("media_format")
    anilist_id = comic_info.get("anilist_id")
    mal_id = comic_info.get("mal_id")
    if country:
        lines.append(f'  <CountryOfOrigin>{escape(country)}</CountryOfOrigin>')
    if media_format:
        lines.append(f'  <MediaFormat>{escape(media_format)}</MediaFormat>')
    if anilist_id is not None:
        lines.append(f'  <AnilistId>{int(anilist_id)}</AnilistId>')
    if mal_id is not None:
        lines.append(f'  <MalId>{int(mal_id)}</MalId>')

    # Pages the source could not deliver, recorded so a gap is never SILENT.
    #
    # Only ever populated under an explicit opt-in
    # (--comix-allow-gapped-chapters). It exists because pages are renumbered
    # 0001..000N on the way into the archive, so a chapter missing its 10th page
    # is byte-for-byte indistinguishable from a complete one that simply had
    # fewer pages — which is how a 67-of-68 chapter shipped unnoticed in
    # 2026-08. These are the SOURCE's page numbers, not archive indices, so they
    # stay meaningful after renumbering. Aio-prefixed and therefore dropped
    # silently by Komga/Kavita, exactly like <AnilistId>.
    if missing_pages:
        joined = ",".join(str(int(p)) for p in sorted(missing_pages))
        lines.append(f'  <AioMissingPages>{escape(joined)}</AioMissingPages>')

    # Auxiliary assets (audio / motion-toon) that ride INSIDE this CBZ under the
    # _aio/ prefix. Aio-prefixed custom elements — dropped silently by Komga/
    # Kavita exactly like <AnilistId>, parsed by the user's own reader.
    # <AioChapterResources> is the compact machine-readable JSON blob (paths
    # CBZ-relative, e.g. _aio/bgm_<ep>_<n>.m4a); the <AioMotionManifest>/
    # <AioAudioFile>/<AioAudioReference> children are human-scannable duplicates.
    # Written only when a handler stashed aux (webtoons motion+BGM, tapas audio)
    # — see _materialize_chapter_aux. --refresh-rewrite-cbz preserves both the
    # _aio/ members (blob-copied) AND this blob (re-parsed from the old ComicInfo
    # and passed back as aux_records — grep 'acr = _gx').
    if aux_records and isinstance(aux_records, dict):
        has_payload = (
            aux_records.get("motion_manifest")
            or aux_records.get("audio")
            or aux_records.get("audio_refs")
            or aux_records.get("has_bgm")
            or aux_records.get("layers")
        )
        if has_payload:
            try:
                blob = json.dumps(aux_records, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                blob = ""
            if blob:
                lines.append(
                    f'  <AioChapterResources>{escape(blob)}</AioChapterResources>'
                )
            manifest_rel = aux_records.get("motion_manifest")
            if manifest_rel:
                lines.append(
                    f'  <AioMotionManifest>{escape(manifest_rel)}</AioMotionManifest>'
                )
            for audio_rel in aux_records.get("audio") or []:
                lines.append(f'  <AioAudioFile>{escape(audio_rel)}</AioAudioFile>')
            for ref in aux_records.get("audio_refs") or []:
                ref_url = ref.get("url") if isinstance(ref, dict) else None
                if ref_url:
                    provider = (
                        ref.get("provider") if isinstance(ref, dict) else None
                    ) or ""
                    attr = f' provider="{escape(provider)}"' if provider else ""
                    lines.append(
                        f'  <AioAudioReference{attr}>{escape(ref_url)}</AioAudioReference>'
                    )
            if aux_records.get("has_bgm"):
                lines.append('  <AioHasBgm>true</AioHasBgm>')

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


# -----------------------------------------------------------
# Sidecar auxiliary assets (audio / motion-toon manifest / layer map)
# -----------------------------------------------------------
# Faithful-archival feature (local branch — see
# ~/.claude/plans/i-want-to-add-rustling-penguin.md). Handlers (tapas,
# linewebtoon motion) stash sites.base.AssetSpec objects on the mutable
# chapter dict as chapter["_aux_assets"]; the CBZ-build path calls
# _materialize_chapter_aux to fetch them into in-memory ZIP members written
# INTO the chapter CBZ under the reserved _aio/ prefix (audio bytes + motion
# manifest), plus a metadata record embedded in the per-chapter ComicInfo.xml
# (<AioChapterResources>, grep 'aux_records'). _aio/ members are renumber-
# EXEMPT: build_cbz_from_content skips + preserves them (else the combined-
# archive renumber would turn an in-CBZ .m4a into a bogus page). No loose
# _assets/ sidecar anymore; details.json gets a chapter_assets rollup rebuilt
# from the CBZs' ComicInfo at end-of-run (grep '_scan_chapter_cbz_aux').

def _fetch_binary_asset_bytes(
    url: str,
    scraper,
    make_request_fn,
    *,
    retries: int = 3,
) -> Optional[bytes]:
    """Fetch a non-image binary asset (audio, manifest) and RETURN ITS BYTES,
    or None on failure. Bytes not a file: aux assets are written straight into
    the chapter CBZ via zipfile.writestr (grep _materialize_chapter_aux), never
    a loose sidecar.

    Deliberately NOT dl_image: dl_image sniffs image magic bytes and would
    mislabel an .mp3/.m4a. Routes through make_request (the project's canonical
    GET with backoff + rate-limit coordination).

    Deliberately IGNORES the per-chapter watchdog (2026-07-03; it used to
    fast-fail on _chapter_cancelled): aux materialization only runs AFTER the
    completeness gate accepted the chapter (grep _materialize_chapter_aux's
    call site — inside `if process_this_chapter:` post-gate), so the pages are
    already secured and the deadline has nothing left to protect. Honoring it
    meant a chapter whose page phase ran long SILENTLY dropped its BGM (the
    record kept has_bgm=True but no _aio/ audio member). Wall-clock stays
    bounded by retries x make_request's own timeout/backoff budget, and the
    host-poison guard still stops a dead audio CDN from grinding.
    """
    host = urlparse(url).netloc
    poison = int(globals().get("_CHAPTER_HOST_POISON", 5))
    for attempt in range(1, max(1, retries) + 1):
        if poison > 0 and _host_fail_count(host) >= poison:
            return None
        try:
            resp = make_request_fn(url, scraper)
            body = getattr(resp, "content", None)
            status = getattr(resp, "status_code", 200)
            if body and status < 400:
                return bytes(body)
            log_verbose(f"    [assets] non-OK response ({status}) for {url}")
        except Exception as exc:
            log_verbose(
                f"    [assets] fetch attempt {attempt}/{retries} failed "
                f"for {url}: {type(exc).__name__}: {exc}"
            )
        if attempt < retries:
            time.sleep(0.5 * attempt)
    return None


def _materialize_chapter_aux(
    specs: List[Any],
    scraper,
    make_request_fn,
) -> Tuple[Optional[Dict[str, Any]], List[Tuple[str, bytes]]]:
    """Fetch a chapter's AssetSpec list into (record, members):
      - members: [(arcname, bytes)] written verbatim INTO the chapter CBZ under
        the reserved `_aio/` prefix (renumber-exempt). Audio is fetched with
        _fetch_binary_asset_bytes (NOT dl_image); the motion manifest rides
        inline on the spec.
      - record: the reader-facing metadata dict embedded in the per-chapter
        ComicInfo.xml <AioChapterResources> blob. Paths are CBZ-relative
        (`_aio/<name>`). Only successfully-fetched audio is listed, but has_bgm
        is set from spec meta even on a fetch failure so the reader still learns
        BGM existed. audio_reference (SoundCloud, BGM-presence marker) is
        record-only — never downloaded (locked design decision).

    Returns (None, []) for empty specs — the common case for every normal site,
    so the CBZ-build path adds nothing and the archive is byte-identical to
    before this feature.
    """
    if not specs:
        return None, []

    record: Dict[str, Any] = {
        "motion_manifest": None,
        "audio": [],
        "audio_refs": [],
        "layers": None,
        "has_bgm": False,
    }
    members: List[Tuple[str, bytes]] = []
    used_names: set = set()

    def _uniq(preferred: Optional[str], fallback: str) -> str:
        base = _sanitize_folder_component(preferred or "") or fallback
        stem, ext = os.path.splitext(base)
        candidate = base
        i = 1
        while candidate.lower() in used_names:
            candidate = f"{stem}_{i}{ext}"
            i += 1
        used_names.add(candidate.lower())
        return candidate

    for spec in specs:
        stype = getattr(spec, "type", None)
        meta = getattr(spec, "meta", None) or {}

        if stype == "audio_reference":
            ref: Dict[str, Any] = {}
            src = getattr(spec, "source_url", None)
            if src:
                ref["url"] = src
            for k, v in meta.items():
                ref[k] = v
            if ref:
                record["audio_refs"].append(ref)
            if meta.get("has_bgm"):
                record["has_bgm"] = True
            continue

        if stype == "motion_manifest":
            data = getattr(spec, "data", None)
            if data:
                fname = _uniq(
                    getattr(spec, "filename", None) or "motion.json", "motion.json"
                )
                payload = (
                    bytes(data) if isinstance(data, (bytes, bytearray))
                    else str(data).encode("utf-8")
                )
                members.append((f"_aio/{fname}", payload))
                record["motion_manifest"] = f"_aio/{fname}"
            if meta.get("layers"):
                record["layers"] = meta["layers"]
            continue

        if stype == "motion_layer":
            if meta.get("layers"):
                record["layers"] = meta["layers"]
            continue

        if stype == "audio_download":
            src = getattr(spec, "source_url", None)
            if not src:
                continue
            # A handler can flag an audio_download as background music (webtoons
            # BGM) so the chapter is marked has_bgm even if the fetch below
            # fails — same has_bgm signal the audio_reference branch sets.
            if meta.get("has_bgm"):
                record["has_bgm"] = True
            url_name = os.path.basename(urlparse(src).path) or "audio.bin"
            fname = _uniq(getattr(spec, "filename", None) or url_name, "audio.bin")
            data = _fetch_binary_asset_bytes(src, scraper, make_request_fn)
            if data:
                members.append((f"_aio/{fname}", data))
                record["audio"].append(f"_aio/{fname}")
            continue
        # Unknown spec.type — ignored (forward-compatible).

    return record, members


def _chapter_carries_aux(ch: Dict[str, Any]) -> bool:
    """True when this chapter is known to carry faithful-archival aux content
    on the PRIMARY source (BGM audio, motion-toon manifest/sounds, or an
    audio-reference marker). Consulted by _process_chapter_strict to VETO the
    multi-source alt rescue: alternative sites only mirror flattened pages, so
    a rescue would silently trade real content (audio/motion) for availability
    (user directive 2026-07-03 — "BGM or animation chapters shouldn't be
    rescuable by --multi-source").

    Signals, most specific first:
      - ch["_aux_assets"]: AssetSpec list stashed by get_chapter_images
        (linewebtoon._stash_normal_audio / _stash_motion_aux,
        tapas._stash_aux_assets). Present whenever the failed attempt got as
        far as the episode page — the overwhelmingly common failure point is
        Phase 2 page downloads, which is after the stash.
      - list-time BGM flags, covering attempts that died before/inside
        get_chapter_images: linewebtoon.get_chapters stamps has_bgm from the
        episode API's hasBgm; tapas.get_chapters stamps _has_bgm/_bgm_url.
        Motion-toons have no list-time flag — an attempt that fails that
        early stays rescue-eligible (nothing was fetched to lose, and we
        can't know it's motion without the page).
      - merged collapse-split parts (_merged_parts): any part carrying
        either signal vetoes the whole synthesized chapter.
    """
    def _one(d: Dict[str, Any]) -> bool:
        return bool(
            d.get("_aux_assets")
            or d.get("has_bgm")
            or d.get("_has_bgm")
            or d.get("_bgm_url")
        )

    if _one(ch):
        return True
    return any(
        _one(p) for p in ch.get("_merged_parts") or [] if isinstance(p, dict)
    )


def _scan_chapter_cbz_aux(out_dir: str) -> Dict[str, Dict[str, Any]]:
    """Rebuild the per-chapter aux rollup by reading each chapter CBZ's embedded
    ComicInfo.xml <AioChapterResources> JSON blob. This is the source of truth
    now that aux lives INSIDE the CBZs (no _assets/ sidecar to scan). Keyed by
    the ComicInfo <Number> (falls back to the filename stem). Self-healing for
    incremental/resume: prior-run CBZs on disk are re-read, so the rollup stays
    complete. Only CBZs that carry aux contribute (others have no blob). Reads
    just the ComicInfo member of each zip (central-directory + one entry), so
    the end-of-run cost is a few ms per chapter."""
    records: Dict[str, Dict[str, Any]] = {}
    try:
        names = os.listdir(out_dir)
    except OSError:
        return records
    for name in sorted(names):
        if not name.lower().endswith(".cbz"):
            continue
        path = os.path.join(out_dir, name)
        try:
            with zipfile.ZipFile(path, "r") as zf:
                if "ComicInfo.xml" not in zf.namelist():
                    continue
                xml_text = zf.read("ComicInfo.xml").decode("utf-8", "replace")
        except Exception:
            continue
        m = re.search(
            r"<AioChapterResources>(.*?)</AioChapterResources>", xml_text, re.DOTALL
        )
        if not m:
            continue
        try:
            rec = json.loads(xml.sax.saxutils.unescape(m.group(1)))
        except (ValueError, TypeError):
            continue
        if not isinstance(rec, dict):
            continue
        num_m = re.search(r"<Number>([^<]*)</Number>", xml_text)
        chap = (num_m.group(1).strip() if num_m else "") or os.path.splitext(name)[0]
        records[chap] = rec
    return records


def _warn_aux_needs_cbz_once(fmt: str) -> None:
    """One-time notice that a handler produced audio/motion extras but the
    current output format can't embed them (aux rides INSIDE the CBZ under
    _aio/; EPUB/PDF have no per-chapter equivalent). Function-attribute flag →
    fires once per run."""
    if getattr(_warn_aux_needs_cbz_once, "_warned", False):
        return
    _warn_aux_needs_cbz_once._warned = True
    print(
        f"  [assets] Note: this series has audio/motion extras, but --format "
        f"{fmt} can't archive them — use --format cbz (or --komikku)."
    )


def _patch_details_json_with_assets(
    out_dir: str,
    aux_seen: Dict[str, bool],
) -> None:
    """Merge the sidecar-asset rollup into an EXISTING details.json (Komikku
    mode): has_motion / has_audio series flags + a chapter_assets map keyed by
    chapter label, rebuilt from each chapter CBZ's ComicInfo <AioChapterResources>
    (grep _scan_chapter_cbz_aux) — the source of truth now that aux lives INSIDE
    the CBZs.

    WHY end-of-run: details.json is written ONCE up front (grep
    'details_payload.update'), BEFORE any chapter, so the rollup can't be made
    in that pass. Rebuilding from the on-disk CBZ scan keeps incremental/resume
    runs complete (prior chapters' CBZs are re-read). Non-destructive: adds the
    three keys, preserves the six canonical + AniList extras. The refresh flow
    (grep 'new_details.update') copies them forward via dict(existing_details).

    Cheap on aux-free series: skips the CBZ scan entirely unless this run
    produced aux (aux_seen) OR details.json already carries the flags. `aux_seen`
    is {"audio": bool, "motion": bool} accumulated during the run.
    """
    details_path = os.path.join(out_dir, "details.json")
    if not os.path.isfile(details_path):
        return
    try:
        with open(details_path, "r", encoding="utf-8") as fh:
            details = json.load(fh)
        if not isinstance(details, dict):
            return
    except (OSError, ValueError):
        return

    had_flags = any(
        k in details for k in ("has_audio", "has_motion", "chapter_assets")
    )
    ran_aux = bool(aux_seen.get("audio") or aux_seen.get("motion"))
    if not ran_aux and not had_flags:
        # Aux-free series (this run + never before) — keep details.json clean
        # and skip the CBZ scan entirely (zero cost on every normal download).
        return

    records = _scan_chapter_cbz_aux(out_dir)

    chapter_assets: Dict[str, Any] = {}
    has_motion = False
    has_audio = False
    for chap, rec in records.items():
        entry: Dict[str, Any] = {
            "motion_manifest": rec.get("motion_manifest"),
            "audio": list(rec.get("audio") or []),
            "audio_refs": list(rec.get("audio_refs") or []),
            "has_bgm": bool(rec.get("has_bgm")),
        }
        if rec.get("layers"):
            entry["layers"] = rec["layers"]
        chapter_assets[str(chap)] = entry
        if entry["motion_manifest"] or entry.get("layers"):
            has_motion = True
        if entry["audio"] or entry["audio_refs"] or entry["has_bgm"]:
            has_audio = True

    details["has_motion"] = has_motion
    details["has_audio"] = has_audio
    details["chapter_assets"] = chapter_assets
    try:
        with open(details_path, "w", encoding="utf-8") as fh:
            json.dump(details, fh, ensure_ascii=False, indent=2)
        log_verbose(
            f"  [assets] patched details.json: has_motion={has_motion}, "
            f"has_audio={has_audio}, {len(chapter_assets)} chapter(s)"
        )
    except OSError as exc:
        log_verbose(f"  [assets] details.json patch failed: {exc}")


def build_cbz(
    slices: List[str],
    out_path: str,
    title: str,
    comic_info: Dict,
    publishers: List[str],
    lang: str,
    chapter_comic_info_xml: Optional[str] = None,
    extra_members: Optional[List[Tuple[str, bytes]]] = None,
):
    """Builds a CBZ file from a list of image slices with metadata.

    chapter_comic_info_xml: when provided, used in place of the series-level
    ComicInfo.xml that build_comic_info_xml would generate. Used by the
    legacy --keep-chapters fallback path in --komikku mode to inject the
    per-chapter ComicInfo.xml (the cbz_cache fast-path embeds the same XML
    at cache-creation time; this is the slow-path equivalent for pre-Phase-D
    resumes where chapter_content carries 'image' entries instead of
    'cbz_cache' entries).

    extra_members: optional [(arcname, bytes)] written verbatim — used for the
    _aio/ aux sidecars (audio/motion) in this legacy image-entry path. Names are
    NOT renumbered, so keep them under the reserved _aio/ prefix (grep
    _materialize_chapter_aux); the fast cbz_cache path writes them inline.
    """
    xml_content = chapter_comic_info_xml or build_comic_info_xml(
        title, comic_info, publishers, lang, len(slices)
    )
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as zf:
        for i, image_path in enumerate(slices):
            arcname = f"{i:04d}{os.path.splitext(image_path)[1]}"
            zf.write(image_path, arcname)
        for arcname, data in (extra_members or []):
            zf.writestr(arcname, data)
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

    Aux sidecars (audio/motion) inside a cbz_cache live under the reserved
    `_aio/` prefix. They are NOT pages, so they're excluded from page_count and
    copied VERBATIM (never renumbered — a renumbered `_aio/…m4a` would become a
    bogus 0001.m4a "page"). Combined archives namespace them per source chapter
    (`_aio/ch_<n>/…`, from item["chap"]) so parts don't collide; per-chapter
    Komikku output skips this function entirely (--no-final-file forced).
    """
    def _is_aux_member(fn: str) -> bool:
        return fn.startswith("_aio/")

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
                        and not _is_aux_member(info.filename)
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
                chap = item.get("chap")
                try:
                    with zipfile.ZipFile(item["path"], "r") as zin:
                        for info in zin.infolist():
                            if info.filename == "ComicInfo.xml":
                                continue
                            if _is_aux_member(info.filename):
                                # Preserve verbatim, namespaced per chapter so
                                # combined archives don't collide. NOT a page.
                                rel = info.filename[len("_aio/"):]
                                arc = (
                                    f"_aio/ch_{_sanitize_folder_component(str(chap))}/{rel}"
                                    if chap is not None else info.filename
                                )
                                zout.writestr(arc, zin.read(info))
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
    # encoding="utf-8" on every EPUB text write below: the default open() uses
    # the platform locale codepage (cp1254 on this Turkish-locale machine), so a
    # non-ASCII chapter title / description / body raised UnicodeEncodeError (or
    # silently mojibake'd) against the UTF-8 the XHTML/OPF declare. S2-5 finding.
    with open(os.path.join(temp_dir, "mimetype"), "w", encoding="utf-8") as f:
        f.write("application/epub+zip")

    # --- 2. container.xml ---
    container_xml = '''<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
    <rootfiles>
        <rootfile full-path="EPUB/content.opf" media-type="application/oebps-package+xml"/>
    </rootfiles>
</container>'''
    with open(os.path.join(temp_dir, "META-INF", "container.xml"), "w", encoding="utf-8") as f:
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
    with open(style_path, "w", encoding="utf-8") as f:
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
    with open(text_style_path, "w", encoding="utf-8") as f:
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
    with open(nav_style_path, "w", encoding="utf-8") as f:
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
            with open(os.path.join(epub_dir, "cover.xhtml"), "w", encoding="utf-8") as f:
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
            with open(os.path.join(epub_dir, page_filename), "w", encoding="utf-8") as f:
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
        with open(os.path.join(epub_dir, "nav.xhtml"), "w", encoding="utf-8") as f:
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
    with open(os.path.join(epub_dir, "content.opf"), "w", encoding="utf-8") as f:
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

# ---------------------------------------------------------------------------
# Combined-archive overwrite guard
# ---------------------------------------------------------------------------
# The end-of-run final-file build writes IN PLACE over the same
# `<base_filename>.<format>` a complete run produced, and both writers TRUNCATE
# (grep `ZipFile(out_path, "w"` in build_cbz_from_content; merge_pdf_files opens
# "wb"). Two routine paths reach that gate holding only PART of the series:
#
#   * DELTA RUN — the UI's update-check passes only the missing range as
#     `--chapters` (UI-source/src/lib/downloadArgs.js:buildLibraryDownloadArgs,
#     both platforms), so a 3-chapter run replaced a 50-chapter Series.cbz. Note
#     that run set is typically DISJOINT from what is on disk, not a subset of
#     it — which is why the predicate below is "does the existing archive cover
#     chapters this run does not" and not "is the run a subset".
#   * COOPERATIVE CANCEL — the chapter loop breaks (grep run_cancelled) and falls
#     through to the same gate. Desktop escapes only because its cancel kills the
#     process (UI-source/electron/downloader.js); Android returns normally.
#
# The damage used to be invisible afterwards: `.aio_series.json`'s
# `chapters_downloaded` is UNIONed with the prior file's on every write, so the
# library update-check saw nothing missing and nothing ever offered a repair.
# `final_file_chapters` (written only by a build that actually happened) is the
# un-unioned counterpart these helpers read.
#
# Deliberately NO in-place merge: these archives are multi-GB and a half-merged
# output is a worse failure than declining to write. The caller prints an
# actionable line and emits `final_file_skipped` instead.


def _final_file_recorded_coverage(
    meta: Optional[Dict[str, Any]], fmt: Optional[str] = None
) -> Set[str]:
    """Chapter labels the combined `<base>.<fmt>` on disk is believed to cover.

    PER FORMAT, and that qualifier is the whole point. One `.aio_series.json`
    serves the series folder, but there is one archive PER FORMAT in it
    (`<base>.cbz`, `<base>.epub`, `<base>.pdf`) — `out_dir` does not vary with
    --format, and --epub-dir moves only the artifact, never the metadata. A
    single unqualified list meant the last build of ANY format described every
    archive: download 50 chapters as CBZ, export 3 of them as EPUB, and the next
    CBZ run compared itself against the EPUB's 3, found nothing dropped, and
    truncated the 50-chapter CBZ. That is the archive-overwrite hazard reopened
    through a side door.

    `final_file_chapters` is therefore a {format: [labels]} map. Reading rules,
    in order:
      1. map with an entry for `fmt` — exact, use it.
      2. LEGACY list (written before the map) — it describes exactly one build,
         and `meta["format"]` records which. Use it only when that format
         matches; otherwise it says nothing about the archive being asked about.
      3. anything else, including a map with no entry for `fmt` — fall back to
         `chapters_downloaded`, a running UNION across runs that can OVERSTATE
         coverage. Overstating is the SAFE direction: it skips a build the user
         can redo, where understating destroys an archive irreversibly.

    Labels are renormalized through _chap_label_str so a legacy "4.0" entry
    compares equal to this run's "4".
    """
    if not isinstance(meta, dict):
        return set()
    raw = meta.get("final_file_chapters")
    if isinstance(raw, dict):
        entry = raw.get(fmt) if fmt else None
        raw = entry if isinstance(entry, (list, tuple, set, frozenset)) else None
    elif isinstance(raw, (list, tuple, set, frozenset)):
        # Legacy single-format shape. meta["format"] is the format that wrote it.
        if fmt is not None and meta.get("format") != fmt:
            raw = None
    else:
        raw = None
    if raw is None:
        raw = meta.get("chapters_downloaded")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return set()
    return {_chap_label_str(x) for x in raw}


def _format_chapter_label_sample(labels: Iterable[Any], limit: int = 6) -> str:
    """Chapter labels as a short, numerically-sorted human sample."""
    ordered = sorted(
        {_chap_label_str(x) for x in labels},
        key=lambda x: (_chap_as_float(x) is None, _chap_as_float(x) or 0.0, x),
    )
    head = ", ".join(ordered[:limit])
    if len(ordered) > limit:
        head += f", … (+{len(ordered) - limit} more)"
    return head


def _final_file_would_shrink(
    run_labels: Iterable[Any],
    existing_meta: Optional[Dict[str, Any]],
    *,
    final_file_exists: bool,
    fmt: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Would writing the combined file now LOSE chapters that are already in it?

    INVARIANT: returns None only when the write is safe — either there is no
    archive to destroy, or this run carries every chapter the existing archive is
    recorded as covering. Otherwise returns the skip payload
    {reason, run_chapters, existing_chapters, dropped_chapters, sample}, which the
    caller renders as one printed line plus a `final_file_skipped` event.

    `final_file_exists` is checked FIRST and is what keeps this from misfiring:
    with nothing on disk no loss is possible, and it also kills the false positive
    where `chapters_downloaded` describes split parts / --no-final-file / komikku
    output that never produced a combined archive in the first place.

    KNOWN RESIDUAL HOLE, accepted deliberately: an archive whose `.aio_series.json`
    was deleted has NO recorded coverage, so a delta run against it still rebuilds
    (only the zero-chapter floor below still catches). The archive itself cannot
    close it — a CBZ records page counts, never chapter labels, so any check
    derived from it would be guesswork, and a false positive here permanently
    refuses legitimate rebuilds. The metadata is rewritten on every run, so this
    only reaches a user who deleted it by hand.
    """
    if not final_file_exists:
        return None
    run = {_chap_label_str(x) for x in (run_labels or [])}
    existing = _final_file_recorded_coverage(existing_meta, fmt)
    dropped = existing - run
    if not dropped:
        if not run:
            # The floor is ONE PAGE, not one chapter: for CBZ the cover is
            # appended to current_book_content before the chapter loop, so the
            # build gate's `current_book_content` term is already true with zero
            # chapters downloaded. Overwriting a real archive with a cover-only
            # one is never right, even when metadata is absent and `existing` is
            # therefore empty.
            return {
                "reason": "no_chapters",
                "run_chapters": 0,
                "existing_chapters": len(existing),
                "dropped_chapters": 0,
                "sample": "",
            }
        return None
    return {
        "reason": "partial_coverage",
        "run_chapters": len(run),
        "existing_chapters": len(existing),
        "dropped_chapters": len(dropped),
        "sample": _format_chapter_label_sample(dropped),
    }


def _run_claimed_chapter_labels(
    attempted_labels: Iterable[Any],
    missed_entries: Optional[List[Dict[str, Any]]],
) -> List[str]:
    """Chapter labels this run may claim it put on disk, normalized and sorted.

    INVARIANT: never contains a chapter the run did not REACH. That is the whole
    point of taking the attempted set (grep attempted_chapter_labels at the
    chapter loop) rather than the selected chapter list.

    The selected list is wrong for any run that stops early. The cancel
    checkpoint breaks out of the loop and the abort path leaves it too, and
    NEITHER records a missed entry for the chapters past that point — so
    "selection minus misses" counted every un-reached chapter as downloaded.
    Cancel a `--chapters 51-53` library update after chapter 51 and
    .aio_series.json claimed 51, 52 and 53 with only 51 anywhere on disk; the
    update-check then saw nothing missing and never offered a repair. Same
    arithmetic hid the tail of an aborted run.

    Extracted to module level for the same reason _final_file_would_shrink was:
    inline in main() this is a set expression nobody can test, and it is the
    exact expression that was wrong.
    """
    attempted = {_chap_label_str(x) for x in (attempted_labels or ())}
    missed = {
        _chap_label_str(e["chap"]) for e in (missed_entries or ()) if "chap" in e
    }
    return sorted(
        attempted - missed,
        key=lambda x: (_chap_as_float(x) is None, _chap_as_float(x) or 0.0),
    )


def _load_series_meta(out_dir: str) -> Dict[str, Any]:
    """Parsed `.aio_series.json` from `out_dir`, or {} when absent/unreadable.

    One reader for two consumers in main(): the overwrite guard above (which
    needs it BEFORE the final-file build) and the metadata writer at the end
    (which unions `chapters_downloaded` into it). Tolerant of every failure mode
    for the same reason _load_cached_anilist_id is — a corrupt metadata file must
    degrade to "no prior knowledge", never abort a finished download.
    """
    if not out_dir:
        return {}
    path = os.path.join(out_dir, ".aio_series.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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
# Scanlation-group / chapter-version selection
#
# The per-chapter ranking itself lives in sites/base.py
# (select_best_chapter_version + _rank_version). These two helpers turn CLI
# args into the run-level policy that ranking needs, and are the only place
# aio-dl.py reasons about groups directly.
# ──────────────────────────────────────────────────────────────────
def _build_group_selection_policy(handler, chapters_by_num, args):
    """Assemble the per-series GroupSelectionPolicy.

    Called ONCE per series, right after the chapter pool has been bucketed by
    chapter number and before any version is selected — that is the only point
    where the whole series is visible, and the census (how many chapters each
    group actually supplies) cannot be computed from a single bucket.

    Runtime-only: nothing here is persisted or hashed. The --mtl /
    --exclude-group ARGS are resume-gating (grep _RESUME_GATING_DESTS), but the
    derived census is rebuilt from the live chapter list every run.
    """
    census, census_total = build_group_census(handler, chapters_by_num)
    excluded_keys = frozenset(
        key for key in (
            handler.get_group_match_key(name)
            for name in (getattr(args, "exclude_group", None) or [])
        ) if key
    )
    policy = GroupSelectionPolicy(
        census=census or None,
        census_total=census_total,
        mtl=getattr(args, "mtl", "avoid") or "avoid",
        excluded_keys=excluded_keys,
    )
    if census:
        top = sorted(census.items(), key=lambda kv: -kv[1])[:5]
        log_verbose(
            "  Group census ({} distinct chapters): {}".format(
                census_total,
                ", ".join(f"{k}={v}" for k, v in top) or "none",
            )
        )
    return policy


def _version_is_confirmed_mtl(handler, version):
    """True when EVERY credited group on a version is confirmed machine TL.

    Deliberately stricter than the ranker's own folding rule (which takes the
    best verdict across co-credited groups): this only drives the user-facing
    "skipped under --mtl exclude" tally, and over-reporting a skip is worse
    than under-reporting it.
    """
    infos = handler.get_group_infos(version)
    if not infos:
        return False
    return all(
        classify_mtl(info.name, description=info.description)[0] == MTL_CONFIRMED
        for info in infos
    )


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
    # Group/version selection: these decide WHICH version's image bytes land
    # on disk, so a mid-run change must invalidate them. Without gating, a run
    # started under --mtl allow and resumed under --mtl exclude would stitch a
    # half-machine-translated volume.
    "mtl", "exclude_group",
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
    # Content-aware JXL/AVIF transcode (--modernize, CBZ-only). Changing any of
    # these re-routes or re-encodes pages, and the transcode DELETES the
    # original JPEG/PNG bytes in place — so a resume with different settings
    # must re-download + re-transcode (the on-disk .jxl/.avif no longer match
    # the requested encoding). See recompress_chapter_images_modern().
    "modernize",
    "modernize_format",
    "modernize_quality",
    "modernize_distance",
    "modernize_min_saving",
    # NOTE: modernize_effort / modernize_avif_speed are DELIBERATELY not here.
    # This set is "image-affecting" params (a mismatch WIPES the partial via
    # rm_tree). effort/speed change encode time + file size ONLY, never the
    # decoded pixels — so a mid-run change should keep completed chapters and
    # apply the new value going forward, not nuke a 200-chapter partial to shave
    # ~5% off. They ARE still persisted (get_resumable_params auto-collects all
    # non-transient dests) so --restore-parameters restores them; they are just
    # not gating. See recompress_chapter_images_modern()'s docstring.
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
    """Persist legacy update settings alongside the canonical .aio_series.json.

    Cross-file: _append_saved_update_options reads the same dict shape to
    rebuild child commands during --update-all. Any key written here that
    isn't also read there silently becomes a no-op on replay — pair every
    new field with a matching replay branch.
    """
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
        "exclude_group": getattr(args, "exclude_group", []) or [],
        "mtl": getattr(args, "mtl", "avoid"),
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
        # External metadata enrichment (--metadata-source family). Must be
        # persisted so --update-all child commands continue to apply the
        # AniList enrichment instead of silently downgrading to the default
        # metadata_source=none. Without this, every newly downloaded update
        # chapter would carry only site-derived ComicInfo tags +
        # description, diverging from the original download's enriched
        # ComicInfo (Tags / SpoilerTags / TagsExtended / CountryOfOrigin /
        # MediaFormat / AnilistId / MalId). Cross-file: the argparse
        # registration is near --enable-ml-rating (grep --metadata-source);
        # enrichment runs in main() at the
        # `if getattr(args, "metadata_source", "none") == "anilist":`
        # check; the replay branch lives in _append_saved_update_options.
        # The `or "none"` / `or 50` defenses handle the rare case where
        # args carries the dest but with an explicit None (e.g. test
        # harness building a Namespace by hand). Argparse default-paths
        # always populate the string/int form.
        "metadata_source": str(getattr(args, "metadata_source", "none") or "none"),
        # `is not None` (not `or 50`) — 0 is a documented-legal min-rank (keep
        # all tags) that `or` would silently clobber to 50, so --update-all
        # children would then filter every rank-0 tag. Mirrors the
        # modernize_distance guard below. S2-3 review finding.
        "metadata_tag_min_rank": int(
            getattr(args, "metadata_tag_min_rank", 50)
            if getattr(args, "metadata_tag_min_rank", 50) is not None
            else 50
        ),
        "metadata_refresh": bool(getattr(args, "metadata_refresh", False)),
        # Content-aware JXL/AVIF transcode (--modernize family). Persisted so
        # --update-all child runs keep transcoding newly downloaded chapters;
        # without this a series first grabbed with --save-params --modernize
        # would get plain JPEG/PNG pages on every update, leaving the library
        # half-modernized. Replay branch: _append_saved_update_options. NOTE: no
        # `or` defaults on distance/min_saving — 0.0 distance (JXL lossless) is a
        # valid value that `or` would silently clobber to the default.
        "modernize": bool(getattr(args, "modernize", False)),
        "modernize_format": str(getattr(args, "modernize_format", "auto") or "auto"),
        "modernize_quality": int(getattr(args, "modernize_quality", 90) or 90),
        "modernize_distance": float(
            getattr(args, "modernize_distance", 1.0)
            if getattr(args, "modernize_distance", 1.0) is not None
            else 1.0
        ),
        "modernize_min_saving": float(
            getattr(args, "modernize_min_saving", 0.92)
            if getattr(args, "modernize_min_saving", 0.92) is not None
            else 0.92
        ),
        # CPU<->size knobs (no `or` default — AVIF speed 0 is valid-but-falsy,
        # like distance 0.0 above). Persisted so --update-all keeps encoding new
        # chapters at the user's chosen effort/speed; replay: _append_saved_
        # update_options. Non-gating (see _RESUME_GATING_DESTS note).
        "modernize_effort": int(
            getattr(args, "modernize_effort", 7)
            if getattr(args, "modernize_effort", 7) is not None
            else 7
        ),
        "modernize_avif_speed": int(
            getattr(args, "modernize_avif_speed", 6)
            if getattr(args, "modernize_avif_speed", 6) is not None
            else 6
        ),
        # Chapter-SET / output-LAYOUT flags that were silently dropped from the
        # replay before (grep _append_saved_update_options for the matching
        # branches). Without these, --update-all children diverged from the
        # original download:
        #   collapse_splits — re-downloads previously-collapsed .1/.2 fragments
        #     as perpetual "new" chapters (changes the chapter set). S4-2.
        #   komikku — loses per-chapter ComicInfo + cover.jpg/details.json layout
        #     (the child re-coerces format/keep/no-final itself). S2-2/S4-3.
        #   webtoon_recompress[_quality/_method] — new LINE Webtoon chapters ship
        #     as full-size PNG instead of the chosen WebP. S2-2.
        #   no_sidecar_assets — the user's opt-out wasn't honored on updates.
        # method default 4 uses `is not None` (0 is a valid libwebp method);
        # quality min is 1 so plain `or` would be safe but is kept symmetric.
        "collapse_splits": bool(getattr(args, "collapse_splits", False)),
        "komikku": bool(getattr(args, "komikku", False)),
        "webtoon_recompress": bool(getattr(args, "webtoon_recompress", False)),
        "webtoon_recompress_quality": int(
            getattr(args, "webtoon_recompress_quality", 85)
            if getattr(args, "webtoon_recompress_quality", 85) is not None
            else 85
        ),
        "webtoon_recompress_method": int(
            getattr(args, "webtoon_recompress_method", 4)
            if getattr(args, "webtoon_recompress_method", 4) is not None
            else 4
        ),
        "no_sidecar_assets": bool(getattr(args, "no_sidecar_assets", False)),
        # Persist the "user explicitly typed --quality <100" sentinel so replay
        # only re-emits --quality when the ORIGINAL run set it. Emitting the
        # default --quality 85 on replay flips the child's _user_set_quality True
        # → disables the CBZ byte-passthrough fast-path → silent lossy q85
        # re-encode of update chapters. S4-1. (The child recomputes this from its
        # own argv at args._user_set_quality; grep that name.)
        "_user_set_quality": bool(getattr(args, "_user_set_quality", False)),
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


def _load_cached_anilist_id(out_dir: str) -> Optional[int]:
    """Read anilist_id from an existing .aio_series.json in `out_dir`.

    Returns the cached AniList ID as int, or None when absent /
    malformed / unparseable. Tolerant to every failure mode (no file,
    bad JSON, missing key, non-numeric value) since this is a best-
    effort fast path — callers fall through to a fresh AniList search
    when this returns None.

    Cross-file: consumed by the --metadata-source=anilist enrichment
    hook in main() right after allocate_series_output_dir. Written by
    the .aio_series.json writer at the end of main(). The key name
    `anilist_id` must match the writer (grep '"anilist_id"').
    """
    if not out_dir:
        return None
    path = os.path.join(out_dir, ".aio_series.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    value = data.get("anilist_id")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _append_saved_update_options(child_cmd: List[str], params: Dict[str, Any]) -> None:
    """Replay saved per-series options for --update-all child runs.

    Cross-file: paired with _save_download_params which writes the dict.
    Old download_params.json files written before a field was added will
    silently lack the key — `params.get(...)` returns None and the
    corresponding branch is a no-op, so this is forward-compatible
    without an explicit migration.
    """
    if params.get("site"):
        child_cmd.extend(["--site", str(params["site"])])
    if params.get("epub_layout"):
        child_cmd.extend(["--epub-layout", str(params["epub_layout"])])
    # --modernize rides the CBZ byte-passthrough fast-path and HARD-errors at
    # parse time on an explicit --width / --aspect-ratio / --quality (grep
    # '--modernize compatibility checks'). When replaying a modernize series we
    # must NOT re-emit those — the saved values are just the cbz defaults the
    # original run never set explicitly (e.g. width=1500), and emitting them
    # would flip the child's _user_set_* sentinels and make it self-reject. The
    # original run required scaling==100, so the saved scaling is 100 and
    # emitting it stays compatible.
    _replay_modernize = bool(params.get("modernize"))
    if params.get("width") and not _replay_modernize:
        child_cmd.extend(["--width", str(params["width"])])
    if params.get("aspect_ratio") and not _replay_modernize:
        child_cmd.extend(["--aspect-ratio", str(params["aspect_ratio"])])
    if not params.get("no_processing"):
        # Only replay --quality when the ORIGINAL run had the user explicitly set
        # it (_user_set_quality). Emitting the default --quality 85 flips the
        # child's _user_set_quality sentinel True → disables the CBZ byte-
        # passthrough fast-path → silent lossy q85 re-encode of update chapters
        # while the rest of the byte-preserved library stays lossless. Old
        # download_params.json lacking the key → get() falsy → skipped (byte-
        # preserve), the safe default. S4-1 review finding.
        if params.get("_user_set_quality") and not _replay_modernize:
            child_cmd.extend(["--quality", str(params.get("quality", 85))])
        child_cmd.extend(["--scaling", str(params.get("scaling", 100))])
    if params.get("cookies"):
        child_cmd.extend(["--cookies", str(params["cookies"])])
    groups = params.get("group") or []
    if isinstance(groups, str):
        groups = [groups]
    for group in groups:
        child_cmd.extend(["--group", str(group)])
    # Same shape as --group. Without replaying these, an --update-all child
    # reverts to the defaults and the series slowly accumulates chapters from
    # groups the user explicitly rejected (the S4-2 bug class).
    excluded = params.get("exclude_group") or []
    if isinstance(excluded, str):
        excluded = [excluded]
    for group in excluded:
        child_cmd.extend(["--exclude-group", str(group)])
    mtl_policy = params.get("mtl")
    if mtl_policy and mtl_policy != "avoid":
        child_cmd.extend(["--mtl", str(mtl_policy)])
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
        # Chapter-set / layout flags now persisted by _save_download_params.
        # collapse_splits changes the chapter SET (drops fragments) — replaying
        # it stops --update-all re-downloading collapsed .1/.2 as "new" (S4-2);
        # komikku re-coerces format/keep/no-final in the child so the flag alone
        # restores the per-chapter layout (S2-2/S4-3); no_sidecar_assets honors
        # the user's opt-out on updates.
        ("collapse_splits", "--collapse-splits"),
        ("komikku", "--komikku"),
        ("no_sidecar_assets", "--no-sidecar-assets"),
    ):
        if params.get(key):
            child_cmd.append(flag)
    # LINE Webtoon WebP recompression + its quality/method knobs (store_true +
    # two ints). Emit non-default sub-values only, mirroring the modernize block
    # below. Without this, newly downloaded webtoon chapters ship as full-size
    # PNG instead of the chosen WebP. S2-2 review finding.
    if params.get("webtoon_recompress"):
        child_cmd.append("--webtoon-recompress")
        wq = params.get("webtoon_recompress_quality")
        if isinstance(wq, int) and wq != 85:
            child_cmd.extend(["--webtoon-recompress-quality", str(wq)])
        wm = params.get("webtoon_recompress_method")
        if isinstance(wm, int) and wm != 4:
            child_cmd.extend(["--webtoon-recompress-method", str(wm)])
    # External metadata enrichment (--metadata-source family). Saved by
    # _save_download_params; absence in older download_params.json files
    # silently degrades to the default-off behavior (the get returns None
    # and the gate skips). Without these branches, the original
    # download's --metadata-source anilist intent is lost on every
    # subsequent --update-all child — the child defaults to
    # metadata_source=none and the newly downloaded chapters ship with
    # site-only metadata, diverging from the parent series' enriched
    # ComicInfo. metadata_tag_min_rank is only meaningful when
    # enrichment is on; skip emitting "--metadata-source none" entirely
    # (no-op but adds spawn-line noise). metadata_refresh persists the
    # user's original intent — if they originally passed --metadata-
    # refresh they wanted cache-bypassing fuzzy re-match on every
    # update; the cached anilist_id in .aio_series.json still short-
    # circuits when the flag was NOT saved (common case → fast).
    metadata_source = params.get("metadata_source")
    if metadata_source and metadata_source != "none":
        child_cmd.extend(["--metadata-source", str(metadata_source)])
        saved_rank = params.get("metadata_tag_min_rank")
        if isinstance(saved_rank, int) and saved_rank != 50:
            child_cmd.extend(["--metadata-tag-min-rank", str(saved_rank)])

    # Content-aware JXL/AVIF transcode (--modernize family). Mirrors the
    # metadata_source replay above: saved by _save_download_params, absent in
    # older download_params.json (get → None → skipped, forward-compatible).
    # Emit the master flag + only NON-default knobs (argparse defaults:
    # format=auto, quality=90, distance=1.0, min-saving=0.92, effort=7,
    # avif-speed=6) to keep the spawn line clean. The child re-runs the
    # parse-time compat checks; because the
    # width/aspect/quality emissions above are suppressed under _replay_modernize
    # and --format cbz / --scaling 100 are replayed, it satisfies the fast-path.
    if _replay_modernize:
        child_cmd.append("--modernize")
        mfmt = params.get("modernize_format")
        if mfmt and mfmt != "auto":
            child_cmd.extend(["--modernize-format", str(mfmt)])
        mq = params.get("modernize_quality")
        if isinstance(mq, (int, float)) and int(mq) != 90:
            child_cmd.extend(["--modernize-quality", str(int(mq))])
        md = params.get("modernize_distance")
        if isinstance(md, (int, float)) and float(md) != 1.0:
            child_cmd.extend(["--modernize-distance", str(md)])
        mms = params.get("modernize_min_saving")
        if isinstance(mms, (int, float)) and float(mms) != 0.92:
            child_cmd.extend(["--modernize-min-saving", str(mms)])
        me = params.get("modernize_effort")
        if isinstance(me, (int, float)) and int(me) != 7:
            child_cmd.extend(["--modernize-effort", str(int(me))])
        # AVIF speed 0 is valid and != default 6, so it emits correctly here.
        ms = params.get("modernize_avif_speed")
        if isinstance(ms, (int, float)) and int(ms) != 6:
            child_cmd.extend(["--modernize-avif-speed", str(int(ms))])
    if params.get("metadata_refresh"):
        child_cmd.append("--metadata-refresh")


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


# ── Multi-source resume cache (run_params.json `multi_source_cache` key) ──
# The direct-URL --multi-source discovery (aio_search_cli.find_alternatives_
# for_direct_url) costs ~30-80s: a cross-site title search + a chapter-list
# fetch per candidate + alignment. On resume that whole probe re-runs even
# though the discovered *sources* (which sites carry this series) essentially
# never change between an interrupted run and its resume minutes/hours later.
# We persist the resolved (site, url) alternatives + a timestamp into
# run_params.json under a TOP-LEVEL `multi_source_cache` key (NOT inside
# `params`, so it never enters gating_hash) and, on resume, feed them through
# aio_search_cli.build_alternatives_from_payload — the same code the UI's
# --multi-source-prefetched file uses. That skips the search but still
# re-fetches chapter lists + re-aligns (cheap, and keeps the alt chapter data
# fresh). A TTL bounds staleness so a long-idle tmp re-probes.
#
# Two write triggers (run_params.json is written ONCE, and discovery is eager
# OR lazy): the eager path runs before the tmp dir exists, so its payload
# rides `_ms_resume_cache_payload` into main()'s run_params write block; the
# lazy path fires mid-run after that block, so it read-modify-writes the file
# via _persist_multi_source_cache. The chapter loop is sequential on the main
# thread (grep `for ch_idx, ch in enumerate(chapters)`), so that write is
# race-free. Cross-file: consumed in _discover_multi_source_alternatives.
_MULTI_SOURCE_CACHE_TTL_SECONDS = 72 * 3600  # 72h; override via AIO_MULTISOURCE_CACHE_TTL_HOURS


def _multi_source_cache_ttl_seconds() -> float:
    """Resolve the multi-source resume-cache TTL in seconds.

    Default 72h. Env override AIO_MULTISOURCE_CACHE_TTL_HOURS (float hours);
    <= 0 disables the cache entirely (every resume re-probes). A malformed
    env value falls back to the default.
    """
    raw = os.environ.get("AIO_MULTISOURCE_CACHE_TTL_HOURS")
    if raw is None or not str(raw).strip():
        return float(_MULTI_SOURCE_CACHE_TTL_SECONDS)
    try:
        return float(raw) * 3600.0
    except (TypeError, ValueError):
        return float(_MULTI_SOURCE_CACHE_TTL_SECONDS)


def _read_multi_source_resume_cache(params_path: str) -> Optional[Dict[str, Any]]:
    """Return the cached multi-source payload from run_params.json if present
    and younger than the TTL, else None.

    The returned dict has the --multi-source-prefetched payload shape
    ({"title", "year"?, "alternatives": [{"site","url",...}, ...]}) plus a
    "saved_at" epoch float, so it can be handed straight to
    aio_search_cli.build_alternatives_from_payload. Best-effort: a missing
    file / malformed JSON / stale-or-future timestamp / empty alt list all
    yield None so the caller falls back to a fresh cross-site search.
    """
    ttl = _multi_source_cache_ttl_seconds()
    if ttl <= 0:
        return None
    try:
        if not params_path or not os.path.exists(params_path):
            return None
        with open(params_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    cache = data.get("multi_source_cache")
    if not isinstance(cache, dict):
        return None
    alts = cache.get("alternatives")
    if not isinstance(alts, list) or not alts:
        return None
    saved_at = cache.get("saved_at")
    if not isinstance(saved_at, (int, float)) or isinstance(saved_at, bool):
        return None
    age = time.time() - float(saved_at)
    # age < 0 → clock moved backwards / future-dated cache; re-probe rather
    # than trust it. age > ttl → stale.
    if age < 0 or age > ttl:
        return None
    return cache


def _persist_multi_source_cache(params_path: str, cache_payload: Optional[Dict[str, Any]]) -> None:
    """Read-modify-write the `multi_source_cache` top-level key into an
    EXISTING run_params.json.

    Used by the lazy-discovery path, which fires AFTER main()'s one-shot
    run_params write block — so the cache must be merged into the file that
    already holds {gating_hash, params}. Deliberately a NO-OP when the file
    doesn't exist yet: the eager path hasn't created the tmp dir at discovery
    time, and that case rides `_ms_resume_cache_payload` into the write block
    instead — creating a params-less run_params.json here would break the
    resume-compat check. Best-effort; never raises.
    """
    if not cache_payload or not cache_payload.get("alternatives"):
        return
    if not params_path or not os.path.exists(params_path):
        return
    try:
        with open(params_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return
        data["multi_source_cache"] = cache_payload
        with open(params_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except (OSError, ValueError):
        pass


def _build_multi_source_cache_payload(ms_result, title, saved_at, year=None):
    """Assemble the persistable `multi_source_cache` dict from a discovery
    result's `resolved_sources` (grep that key in aio_search_cli.py).

    Returns None when there are no usable (site, url) sources — nothing worth
    caching, so the caller leaves run_params.json untouched.
    """
    sources = (ms_result or {}).get("resolved_sources") or []
    clean: List[Dict[str, Any]] = []
    for s in sources:
        if not isinstance(s, dict):
            continue
        site = (s.get("site") or "").strip()
        url = (s.get("url") or "").strip()
        if not site or not url:
            continue
        entry: Dict[str, Any] = {"site": site, "url": url}
        if s.get("title"):
            entry["title"] = s["title"]
        if s.get("cover"):
            entry["cover"] = s["cover"]
        clean.append(entry)
    if not clean:
        return None
    payload: Dict[str, Any] = {
        "saved_at": float(saved_at),
        "title": title or "",
        "alternatives": clean,
    }
    if year:
        payload["year"] = year
    return payload


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
    _process_chapter (watchdog), and _process_chapter_strict (inline
    retry). Without the second
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
    # --no-fast-download: force-disable curl_cffi fast path globally. Read
    # by both the main-path SUPPORTS_FAST_DOWNLOAD gate AND the prefetch
    # worker's SUPPORTS_FAST_DOWNLOAD gate. Module-global so the prefetch
    # worker (which doesn't have args in scope as a closure capture) can
    # read it without parameter threading.
    globals()["_NO_FAST_DOWNLOAD"] = bool(getattr(args, "no_fast_download", False))
    # --max-cpu-percent: scale the CPU-bound image-pool budget (grep
    # _cpu_pool_budget). Clamped [1,100]. Module-global so the three pool sites
    # read it without threading args through. Re-applied on --restore-parameters
    # so a resumed run honors the CURRENT --max-cpu-percent — the resume CLI
    # passes it explicitly (downloader.js resume()), and the restore loop keeps
    # explicit CLI dests. NOT in _RESUME_GATING_DESTS (speed knob, never changes
    # decoded pixels — mirrors modernize_effort).
    _cpu_pct = getattr(args, "max_cpu_percent", 100)
    # `is not None` (not `or`) so an explicit 0 clamps DOWN to the floor of 1
    # rather than being read as falsy and reset to 100 (full).
    globals()["_CPU_POOL_PERCENT"] = max(
        1, min(100, int(_cpu_pct if _cpu_pct is not None else 100))
    )
    # --image-prefetch-parallel: how many concurrent image-prefetch worker
    # threads. Same module-global pattern as _CPU_POOL_PERCENT above, since
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



# Shared stdlib queue for the inter-chapter image-download prefetch pool
# below. (The MangaFire VRF-prefetch machinery that used to live here was
# removed with the 2026 MangaFire REST-API rewrite — chapter payloads are
# plain JSON now, no token capture; see sites/mangafire.py.)
import queue as _stdlib_queue


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
#     this is a single cheap JSON GET now; cheap enough that we don't try
#     to share metadata via sidecar JSON between threads.
#
# Gated by --prefetch-image-workers (default -1 → match --image-workers).
# Set to 0 for full opt-out when the CDN is rate-limiting and the extra
# concurrent burst hurts more than the overlap helps; set to a smaller
# positive number to keep prefetch on but with a lighter footprint.
#
# Phase B (2026-05-13): replaced single-in-flight thread with a
# queue+worker-pool pattern. Multiple chapters
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
# S5-2: chap_labels whose consume-wait TIMED OUT while the worker was still
# running. The foreground then downloads into the same ch_{chap} tdir, so the
# still-running worker must NOT wipe it (partial-fail rm_tree) or write its late
# success marker. Set under _image_prefetch_lock by _consume_image_prefetch on
# timeout; the worker checks + discards it right before its finalize.
_image_prefetch_abandoned: set = set()                  # chap_label
_image_prefetch_lock = threading.Lock()                 # guards _seen/_done/_workers
# Set by _apply_runtime_tunables from --image-prefetch-parallel (default 2).
_image_prefetch_parallel: int = 2


def _image_prefetch_is_abandoned(chap_label: str) -> bool:
    """True once _consume_image_prefetch handed this chapter's tdir to the
    foreground (300s consume timeout). The prefetch worker polls this DURING
    Phase 2 so it stops writing into the tdir the foreground now downloads
    into — otherwise both write the same per-page .pending_<base> tempfile
    (base.py's fast path AND dl_image's legacy path share that name, keyed on
    (folder, base), NOT per-thread), tearing the tempfile and racing the
    atomic rename. Distinct from _chapter_cancelled: that's the FOREGROUND
    chapter's watchdog, which RACE-2 keeps prefetch decoupled from; THIS is a
    per-chapter hand-off signal prefetch DOES honor. Peek only — the finalize
    block still does the discard under the lock. S5-2 write-race follow-up."""
    with _image_prefetch_lock:
        return chap_label in _image_prefetch_abandoned


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
            # A run-level cancel DRAINS the queue rather than working it: the
            # foreground has stopped caring about these chapters, and the job
            # would otherwise spend minutes fetching pages nobody consumes.
            # Note this still falls through to the finally below, so a main
            # thread parked in _consume_image_prefetch unblocks instead of
            # deadlocking on a done-event that never fires.
            if not run_cancelled():
                _run_image_prefetch_job(job)
        finally:
            # Always signal completion (success or failure) so the main
            # thread's _consume_image_prefetch doesn't deadlock.
            evt = _image_prefetch_done.get(job.chap_label)
            if evt is not None:
                evt.set()
            _image_prefetch_queue.task_done()


def _binary_image_page_name(entry: Dict[str, Any], blob, chap_label, page_counter: int) -> str:
    """On-disk filename for a binary_image page: '<chap_label>_<counter:04d><ext>'.

    ext = explicit entry['extension'] -> a suffix on entry['name'] -> magic-sniff
    (blob[:32] + content_type). The chap_label prefix + CONTINUOUS page_counter
    (never the handler's per-chapter 'name') is REQUIRED: a per-chapter name like
    MangaDex's "0001.png" collides when --collapse-splits concatenates parts into
    one tdir — part 2 overwrites part 1, dropping pages, and under --modernize the
    transcode pool races the shared path (winner deletes the source, the dup
    slot's cleanup deletes the winner's .jxl) -> CBZ FileNotFoundError. Shared by
    _process_chapter_impl (foreground) and _run_image_prefetch_job (prefetch) so
    the naming can't drift between the two — the exact bug that forced editing
    both twins (grep bench/collapseSplitsModernize.md). CL-4.
    """
    explicit_ext = entry.get("extension")
    custom_name = entry.get("name")
    if explicit_ext:
        ext = explicit_ext if explicit_ext.startswith(".") else "." + explicit_ext
    elif custom_name and os.path.splitext(custom_name)[1]:
        ext = os.path.splitext(custom_name)[1]
    else:
        ext = _sniff_image_extension(
            blob[:32] if isinstance(blob, (bytes, bytearray)) else b"",
            entry.get("content_type"),
        )
    return f"{chap_label}_{page_counter:04d}{ext}"


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

        # S5-2 (write race): if get_chapter_images (Phase 1, network-bound) ran
        # long enough that the foreground's consume timed out and ADOPTED this
        # tdir, write NOTHING into it — the foreground owns it now and is
        # downloading the same pages. Discard the flag and bail before any
        # classification / binary-blob write. (Phase-2 downloads are gated
        # per-page below too: is_cancelled on the fast path, _bg_prefetch on the
        # legacy path — this covers a slow Phase-1 that finished after adoption.)
        if _image_prefetch_is_abandoned(chap_label):
            with _image_prefetch_lock:
                _image_prefetch_abandoned.discard(chap_label)
            log_verbose(
                f"  [Img Prefetch] Ch {chap_label} abandoned during fetch — "
                f"foreground owns the tdir; not writing"
            )
            return

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
                    # CL-4: shared with the foreground twin so the collapse-safe
                    # naming can't drift (grep _binary_image_page_name).
                    filename = _binary_image_page_name(
                        entry, blob, chap_label, page_counter
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

        # Blame-host for the Phase-D per-host concurrency cap. download_tasks is
        # non-empty here (guarded above), so the first task's netloc stands in
        # for the chapter's CDN in both the fast and ThreadPool paths below.
        prefetch_host = urlparse(download_tasks[0][1]).netloc

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
            fast_conc = _effective_concurrency(prefetch_host, fast_conc)
            fast_timeout = float(globals().get("_HTTP_TIMEOUT", 30.0))
            # Backoff feedback (2026-07-03): prefetch hard-failures dial down
            # the shared per-host concurrency cap. Before this, the prefetch
            # path — which does nearly ALL the downloading in steady state —
            # fed the backoff NOTHING, so a saturated CDN kept getting hit at
            # full concurrency while only the rare foreground failure stepped
            # the cap down (bench/unordinaryLogs.md: 8->3 took 50 minutes,
            # five single steps, each from a foreground chapter failure).
            # Deliberately backoff-ONLY (_record_host_failure_for_backoff,
            # NOT _record_failure): per-chapter poison counts and ghost
            # signatures must stay scoped to the FOREGROUND chapter — this
            # worker runs concurrently with some other chapter's window.
            # 4xx-status failures are skipped (stricter than the foreground's
            # blanket "retryable"): prefetch has no ghost detector downstream,
            # and a prefetched placeholder chapter of uniform 403s must not
            # throttle the whole run's CDN trust. Timeouts/5xx (status None
            # or >= 500) are the congestion signal we want.
            #
            # Still NO poison feedback: if prefetch fails, the partial-failure
            # branch below wipes tdir and main's foreground download retries
            # with full instrumentation.
            def _prefetch_backoff_feedback(
                h: str, u: str, *, status=None, body_size=None
            ) -> None:
                if status is not None and 400 <= int(status) < 500:
                    return
                _record_host_failure_for_backoff(h, "retryable")

            fast_results = handler.fast_download_images(
                download_tasks,
                concurrency=fast_conc,
                timeout=fast_timeout,
                record_host_failure=_prefetch_backoff_feedback,
                # S5-2 (write race): bail per-page once the foreground adopts
                # this tdir (consume timeout) so we stop writing the shared
                # .pending_<base> tempfiles it is now writing too. This is the
                # PREFETCH's own hand-off signal, NOT the foreground chapter's
                # watchdog — RACE-2 keeps us decoupled from _chapter_cancelled,
                # but we DO honor being adopted. fast_download_images re-checks
                # is_cancelled before each attempt and after the semaphore.
                is_cancelled=lambda: _image_prefetch_is_abandoned(chap_label),
                # S5-2 (write race): distinct pending-file name so an in-flight
                # prefetch page write (one that started before is_cancelled
                # fired) can't collide with the foreground's write of the same
                # page into the adopted tdir (grep .bgprefetch / pending_suffix).
                pending_suffix=".bgprefetch",
                # Forward cookies (e.g. age-gate cookies for LineWebtoon)
                # so prefetch can fetch the same content the foreground
                # path would. Base impl filters to host-relevant cookies.
                scraper=scraper,
            )
            failed = sum(1 for _, p in fast_results if not p)
        else:
            workers = max(1, min(image_workers, len(download_tasks)))
            # Phase D: cap prefetch ThreadPool concurrency too.
            workers = max(1, _effective_concurrency(prefetch_host, workers))
            # RACE-2: mark each pool thread as a background prefetch worker so
            # dl_image / _try_download_url don't honor the FOREGROUND chapter's
            # watchdog + poison tally (which would abort this prefetch of a LATER
            # chapter and wipe its tdir). The flag rides a thread-local (grep
            # _in_background_prefetch); dl_image propagates it into its variant
            # sub-threads. Reset per task; the pool's threads die with the block.
            def _bg_prefetch(url, folder, name):
                # S5-2 (write race): once the foreground adopts this tdir
                # (consume timeout), stop scheduling new page writes into it —
                # it downloads the same pages now and shares dl_image's
                # per-(folder,base) .pending_ tempfile, so concurrent writes
                # tear the tempfile / race the rename. Not-yet-started tasks
                # short-circuit here; the ≤workers already inside dl_image
                # finish their current page (bounded). Counts as failed, but the
                # finalize block checks _abandoned FIRST and leaves the dir to
                # the foreground (no rm_tree, no marker).
                if _image_prefetch_is_abandoned(chap_label):
                    return False
                _PREFETCH_TLS.active = True
                try:
                    return dl_image(url, folder, name, scraper, True)
                finally:
                    _PREFETCH_TLS.active = False
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix=f"img-prefetch-{chap_label}"
            ) as pool:
                futures = [
                    pool.submit(_bg_prefetch, url, folder, name)
                    for _, url, folder, name in download_tasks
                ]
                for fut in as_completed(futures):
                    try:
                        if not fut.result():
                            failed += 1
                    except Exception:
                        failed += 1

        # S5-2: if the foreground gave up waiting (consume timed out) it has
        # ADOPTED this tdir and is downloading into it now. Neither write our
        # marker nor wipe the dir — a late rm_tree here deletes the foreground's
        # in-progress pages mid-build (the unordinaryLogs.md 90-300s consume
        # waits made this reachable). Leave ch_{chap} entirely to the foreground.
        with _image_prefetch_lock:
            _abandoned = chap_label in _image_prefetch_abandoned
            _image_prefetch_abandoned.discard(chap_label)
        if _abandoned:
            log_verbose(
                f"  [Img Prefetch] Ch {chap_label} abandoned (foreground took "
                f"over its tdir after consume timeout) — leaving it untouched"
            )
        elif failed == 0:
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
    N daemon workers total.
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
    No-op when depth <= 0 (user opted out).

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
        finished = evt.wait(timeout=300.0)
        if not finished:
            # S5-2: we're giving up the wait but the worker is STILL running.
            # The foreground is about to download into the SAME ch_{chap} tdir,
            # so flag the chapter abandoned — the worker's late finalize then
            # skips both its rm_tree (which would delete our in-progress pages)
            # and its success marker. Grep _image_prefetch_abandoned.
            with _image_prefetch_lock:
                _image_prefetch_abandoned.add(chap_label)
            log_verbose(
                f"  Image prefetch of Ch {chap_label} exceeded 300s; foreground "
                f"taking over its tdir."
            )
    with _image_prefetch_lock:
        # Clean up per-chap state. Keep _image_prefetch_seen entry so a
        # second enqueue for the same chapter (e.g. inline retry) is
        # a no-op — main's foreground download path handles retries.
        _image_prefetch_done.pop(chap_label, None)


# ---------------------------------------------------------------------------
# --refresh-library-metadata mode (in-place AniList re-enrichment)
# ---------------------------------------------------------------------------
# Repairs an existing library WITHOUT re-downloading images: re-pulls AniList
# metadata for each already-downloaded series and rewrites details.json +
# .aio_series.json (and optionally each CBZ's ComicInfo.xml). Exists because
# the genre-normalization fix (external_metadata REPLACE semantics + the
# mangakatana selector scoping, 2026-06-06) only affects FUTURE downloads;
# series grabbed before the fix keep their 50+-tag taxonomy dumps on disk
# until refreshed. Dispatched from main() right after --update-all.

def _serialize_anilist_tag(t: Any) -> Dict[str, Any]:
    """AnilistTag dataclass -> .aio_series.json tag dict.

    Schema parity with the live-download writer (grep '_tag_to_dict' /
    'anilist_tags' near series_meta). Duck-typed via getattr so a plain
    dict already in the right shape would also pass through.
    """
    return {
        "name": getattr(t, "name", "") or "",
        "category": getattr(t, "category", "") or "",
        "rank": int(getattr(t, "rank", 0) or 0),
        "is_media_spoiler": bool(getattr(t, "is_media_spoiler", False)),
        "is_general_spoiler": bool(getattr(t, "is_general_spoiler", False)),
    }


def _anilist_meta_fields(comic_data: Dict[str, Any]) -> Dict[str, Any]:
    """The seven AniList enrichment fields shared VERBATIM by the two
    .aio_series.json writers (live-download near 'series_meta =' and
    --refresh-library-metadata's 'meta[...]' block) AND — as its leading
    block — the details.json reader extras (_build_aio_reader_extras).

    Returns them in canonical order (id, mal_id, country_of_origin,
    media_format, synonyms, tags, spoiler_tags) so every on-disk artifact
    stays key-order-identical. Tags run through the module-level
    _serialize_anilist_tag (AnilistTag dataclass -> plain dict; the dataclass
    itself doesn't survive json.dump); every value reads comic_data.get(...) so
    an unenriched series emits null/[] on a stable schema. Cross-file: three
    consumers, grep _anilist_meta_fields.
    """
    return {
        "anilist_id": comic_data.get("anilist_id"),
        "mal_id": comic_data.get("mal_id"),
        "country_of_origin": comic_data.get("country_of_origin"),
        "media_format": comic_data.get("media_format"),
        "anilist_synonyms": list(comic_data.get("anilist_synonyms") or []),
        "anilist_tags": [
            _serialize_anilist_tag(t)
            for t in (comic_data.get("anilist_tags") or [])
        ],
        "anilist_spoiler_tags": [
            _serialize_anilist_tag(t)
            for t in (comic_data.get("anilist_spoiler_tags") or [])
        ],
    }


def _build_aio_reader_extras(
    comic_data: Dict[str, Any],
    *,
    source_site: Optional[str],
    source_url: Optional[str],
    language: Optional[str],
) -> Dict[str, Any]:
    """Reader-facing enrichment block merged into details.json as flat,
    top-level keys. Returns a dict the caller `.update()`s onto the Komikku
    payload AFTER the six canonical keys, so those stay first on disk.

    WHY this exists: details.json is the only metadata file a *reader*
    (Komikku, or the user's own reader) actually reads. .aio_series.json is
    internal bookkeeping — the update-checker chapter list + the cached
    anilist_id fast path (grep _load_cached_anilist_id) — and no reader ever
    opens it. So everything a reader might surface has to live in
    details.json. Komikku parses details.json with kotlinx
    Json{ignoreUnknownKeys=true} (verified: `git show 1f17a20^:komikkuspec.md`
    §6.1 — "extra keys are tolerated"), so these extra keys are silently
    dropped by Komikku and read by the user's reader; no namespacing needed
    for Komikku-safety.

    Key names deliberately dodge Komikku's reserved set
    (title/author/artist/description/genre/status) AND the TachiyomiSY keys
    Komikku currently ignores but could one day wire up
    (url/lang/tags/categories/alt_titles/thumbnail_url): hence
    source_url/source_site (not bare url/site) and anilist_tags (not tags).
    The anilist_*/country_of_origin/media_format names mirror
    .aio_series.json's schema (grep 'series_meta =') so the two on-disk files
    stay name-consistent for anything that reads both. Rich-tag dict shape ==
    _serialize_anilist_tag == ComicInfo <TagsExtended> (name/category/rank +
    the two spoiler flags), so a reader can render spoiler-aware chips off
    any of the three artifacts identically.

    Every key is emitted unconditionally (null / [] / "" when enrichment was
    off or found no confident match) so the reader can rely on a stable
    schema. comic_data's anilist_tags/anilist_spoiler_tags are AnilistTag
    dataclass instances in both call sites (set by enrich_from_anilist →
    _apply_anilist_match); _serialize_anilist_tag is getattr-based so that
    holds. Cross-file callers: the two details.json writers — live-download
    (grep 'details_payload.update') and --refresh-library-metadata (grep
    'new_details.update').
    """
    return {
        # Seven AniList fields (canonical order) shared with both
        # .aio_series.json writers; grep _anilist_meta_fields.
        **_anilist_meta_fields(comic_data),
        "source_site": source_site or "",
        "source_url": source_url or "",
        "language": language or "",
    }


def _rewrite_cbz_comicinfo(
    folder: str,
    comic_data: Dict[str, Any],
    series_title: str,
    lang_default: str,
) -> int:
    """Rewrite enrichment elements in every chapter CBZ's ComicInfo.xml, in
    place, preserving all per-chapter fields. Returns the count updated.

    Used only by _refresh_library_metadata when --refresh-rewrite-cbz is set.
    For each .cbz: parse the per-chapter fields we must NOT lose
    (Title/Number/Volume/Translator/Web/Year-Month-Day/LanguageISO/PageCount/
    Publisher), then rebuild the whole ComicInfo via
    build_per_chapter_comic_info_xml with the enriched series-level
    comic_data — so <Genre>/<Tags>/<SpoilerTags>/<TagsExtended>/<Summary>/
    <CountryOfOrigin>/<MediaFormat>/<AnilistId>/<MalId> come out normalized,
    byte-identical in shape to a fresh download. Per-member ZIP
    compression is preserved (we don't re-deflate already-compressed art).
    grep caller: _refresh_library_metadata.
    """
    import calendar
    import xml.etree.ElementTree as ET

    updated = 0
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return 0
    for name in names:
        if not name.lower().endswith(".cbz"):
            continue
        path = os.path.join(folder, name)
        try:
            with zipfile.ZipFile(path) as zin:
                infos = zin.infolist()
                blobs = {zi.filename: zin.read(zi.filename) for zi in infos}
        except (OSError, zipfile.BadZipFile):
            continue

        ci_name = next(
            (zi.filename for zi in infos
             if os.path.basename(zi.filename).lower() == "comicinfo.xml"),
            None,
        )

        chap_title = number = volume = translator = web = lang = None
        writer = penciller = None
        page_count = 0
        uploaded_epoch: Optional[int] = None
        publishers: List[str] = []
        # Preserve the embedded aux rollup (<AioChapterResources>) across the
        # ComicInfo rebuild so a metadata refresh doesn't orphan the _aio/ audio
        # still inside this CBZ (grep _materialize_chapter_aux). ET .text is
        # already entity-unescaped, so the blob parses straight as JSON.
        aux_records: Optional[Dict[str, Any]] = None
        if ci_name and blobs.get(ci_name):
            root = None
            try:
                root = ET.fromstring(blobs[ci_name].decode("utf-8", "replace"))
            except ET.ParseError:
                root = None
            if root is not None:
                def _gx(tag: str) -> Optional[str]:
                    el = root.find(tag)
                    return el.text if (el is not None and el.text) else None
                chap_title = _gx("Title")
                number = _gx("Number")
                volume = _gx("Volume")
                translator = _gx("Translator")
                web = _gx("Web")
                lang = _gx("LanguageISO")
                writer = _gx("Writer")
                penciller = _gx("Penciller")
                pub = _gx("Publisher")
                if pub:
                    publishers = [pub]
                pc = _gx("PageCount")
                if pc:
                    try:
                        page_count = int(pc)
                    except ValueError:
                        page_count = 0
                y, m, d = _gx("Year"), _gx("Month"), _gx("Day")
                if y and m and d:
                    try:
                        uploaded_epoch = calendar.timegm(
                            (int(y), int(m), int(d), 0, 0, 0, 0, 0, 0)
                        )
                    except (ValueError, OverflowError):
                        uploaded_epoch = None
                acr = _gx("AioChapterResources")
                if acr:
                    try:
                        parsed_acr = json.loads(acr)
                        if isinstance(parsed_acr, dict):
                            aux_records = parsed_acr
                    except (ValueError, TypeError):
                        aux_records = None

        # Preserve the archive's existing Writer/Penciller (authors/artists).
        # AniList v1 supplies no staff, so the refresh must carry author/artist
        # metadata through unchanged rather than dropping <Penciller> (Codex
        # review on PR #47). Prefer the CBZ's own values; fall back to the
        # series-level comic_data (seeded from details.json in
        # _refresh_library_metadata).
        per_cbz: Dict[str, Any] = dict(comic_data)
        if writer:
            per_cbz["authors"] = [s.strip() for s in writer.split(",") if s.strip()]
        if penciller:
            per_cbz["artists"] = [s.strip() for s in penciller.split(",") if s.strip()]
        new_ci = build_per_chapter_comic_info_xml(
            series_title=series_title,
            chapter_title=chap_title,
            chapter_num=number,
            volume=volume,
            scanlator=translator,
            web_url=web,
            uploaded_epoch=uploaded_epoch,
            comic_info=per_cbz,
            publishers=publishers,
            lang=lang or lang_default or "",
            page_count=page_count,
            aux_records=aux_records,
        )
        target = ci_name or "ComicInfo.xml"
        had_ci = target in blobs
        blobs[target] = new_ci.encode("utf-8")

        tmp = path + ".refresh.tmp"
        try:
            with zipfile.ZipFile(tmp, "w") as zout:
                for zi in infos:
                    # writestr(ZipInfo, ...) keeps the member's original
                    # compress_type — no re-deflating already-compressed art.
                    zout.writestr(zi, blobs[zi.filename])
                if not had_ci:
                    zout.writestr(target, blobs[target])
            os.replace(tmp, path)
            updated += 1
        except OSError:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
    return updated


def _refresh_cover_jpg(
    folder: str, url: str, fallback_url: Optional[str], scraper
) -> bool:
    """Re-download a series cover to <folder>/cover.jpg for
    --refresh-library-metadata's always-normalize-covers path. Returns True
    iff cover.jpg was (re)written.

    dl_image sniffs the real image format and may produce cover_orig.<ext>;
    we copy whatever it produced to the canonical Komikku name cover.jpg
    (Komikku decodes by content, not extension — komikkuspec §5). Tries `url`
    (the AniList cover) first, then `fallback_url` (the stashed site cover)
    when the AniList CDN fetch returns None. On total failure the existing
    cover.jpg is left untouched (never blanked). Download work happens in a
    throwaway temp dir so a partial/aborted fetch can't litter the series
    folder. dl_image is safe here: its watchdog/host-poison checks are no-ops
    outside the chapter loop. grep caller: _refresh_library_metadata.
    """
    import tempfile

    tmp_dir = tempfile.mkdtemp(prefix="aio_cover_")
    try:
        got = dl_image(url, tmp_dir, "cover_orig.jpg", scraper, cleanup=True)
        if got is None and fallback_url and fallback_url != url:
            got = dl_image(
                fallback_url, tmp_dir, "cover_orig.jpg", scraper, cleanup=True
            )
        if got and os.path.exists(got):
            shutil.copy2(got, os.path.join(folder, "cover.jpg"))
            return True
        return False
    except Exception:
        # Best-effort: a cover refresh must never abort the whole library run.
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _refresh_library_metadata(args) -> int:
    """--refresh-library-metadata mode. Returns a process exit code.

    Re-pull AniList metadata for every already-downloaded series under
    --output-dir and rewrite the on-disk metadata sinks WITHOUT
    re-downloading images. Per series folder carrying a .aio_series.json:

      1. Seed comic_data from the existing .aio_series.json (title, site
         genres, authors, status, cached anilist_id, synonyms) + the
         details.json description (so a failed match never blanks it).
      2. enrich_from_anilist — cached-ID fast path; falls back to a title
         search for series predating enrichment. Honors --metadata-refresh
         (cache-bypass) and --metadata-tag-min-rank. Same merge as a live
         download, so genres get the REPLACE normalization.
      3. On a confident match: rewrite details.json + .aio_series.json,
         repoint the cover URL to AniList's, and re-download cover.jpg from
         AniList (normalize the on-disk cover); with --refresh-rewrite-cbz
         also rewrite each CBZ's ComicInfo.xml.
      4. On no match / error: leave the series untouched.

    Cross-file: sites/external_metadata.enrich_from_anilist,
    library_state.scan_library, _rewrite_cbz_comicinfo,
    _komikku_status_to_digit, _serialize_anilist_tag.
    """
    from library_state import scan_library, SERIES_META_FILE
    from sites.external_metadata import enrich_from_anilist

    root = os.path.abspath(args.output_dir)
    all_entries = scan_library(root)

    # Optional positional filters: restrict to series whose folder name or
    # source URL contains any of the given substrings (case-insensitive).
    # Lets the user repair one series — `--refresh-library-metadata "Eleceed"`
    # — instead of the whole library. Empty = every series.
    filters = [
        str(s).strip().lower()
        for s in (getattr(args, "comic_url", None) or [])
        if str(s).strip()
    ]

    def _matches(entry: Dict[str, Any]) -> bool:
        if not filters:
            return True
        hay = (
            str(entry.get("name", ""))
            + " "
            + str((entry.get("series_meta") or {}).get("url", ""))
        ).lower()
        return any(f in hay for f in filters)

    entries = [e for e in all_entries if e.get("series_meta") and _matches(e)]
    no_meta = (
        [e for e in all_entries if not e.get("series_meta")]
        if not filters
        else []
    )
    if not entries:
        scope = f" matching {filters}" if filters else ""
        print(f"No series with .aio_series.json found in {root}{scope}.")
        return 0

    tag_min_rank = int(getattr(args, "metadata_tag_min_rank", 50) or 50)
    force_refresh = bool(getattr(args, "metadata_refresh", False))
    rewrite_cbz = bool(getattr(args, "refresh_rewrite_cbz", False))
    # Cover normalization (user-chosen "always re-download" on refresh): one
    # shared HTTP session reused for every matched series' cover.jpg fetch.
    # cloudscraper when available (a site-cover fallback may sit behind
    # Cloudflare); AniList's own CDN is plain and works with either.
    cover_scraper = None
    try:
        if cloudscraper is not None and sys.version_info >= (3, 7):
            cover_scraper = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "darwin", "mobile": False}
            )
    except Exception:
        cover_scraper = None
    if cover_scraper is None:
        cover_scraper = requests.Session()
    print(
        f"[*] Refreshing AniList metadata for {len(entries)} series in {root}"
        + (" (+CBZ ComicInfo rewrite)" if rewrite_cbz else "")
        + (" (cache-bypass)" if force_refresh else "")
        + " (+cover.jpg re-download)"
    )

    matched: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []

    for e in entries:
        folder = e["folder"]
        meta = dict(e.get("series_meta") or {})
        title = meta.get("title") or e.get("name") or ""
        if not title:
            skipped.append(e.get("name", "?"))
            continue

        comic_data: Dict[str, Any] = {
            "title": title,
            "hid": meta.get("hid", ""),
            "authors": list(meta.get("authors") or []),
            "genres": list(meta.get("genres") or []),
            "status": meta.get("status"),
            "cover": meta.get("cover"),
        }
        syn = list(meta.get("anilist_synonyms") or [])
        if syn:
            comic_data["alt_names"] = syn

        details_path = os.path.join(folder, "details.json")
        existing_details: Dict[str, Any] = {}
        try:
            with open(details_path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                existing_details = loaded
        except (OSError, ValueError):
            existing_details = {}
        if existing_details.get("description"):
            comic_data["desc"] = existing_details["description"]
        # Seed artists from details.json's preserved `artist` field. AniList
        # v1 fetches no staff, so without this the CBZ rewrite would emit an
        # empty <Penciller> and drop artist metadata. _rewrite_cbz_comicinfo
        # still prefers each archive's own <Penciller> when present; this is
        # the fallback (and the recovery path for CBZs whose <Penciller> was
        # already stripped by the pre-fix run). authors come from
        # .aio_series.json above.
        if existing_details.get("artist"):
            comic_data["artists"] = [
                s.strip()
                for s in str(existing_details["artist"]).split(",")
                if s.strip()
            ]

        try:
            enrich_from_anilist(
                comic_data,
                hid=comic_data["hid"],
                handler_name=meta.get("site", ""),
                year=None,
                cover_url=comic_data.get("cover"),
                tag_min_rank=tag_min_rank,
                force_refresh=force_refresh,
                cached_anilist_id=meta.get("anilist_id"),
            )
        except Exception as exc:
            print(f"  [!] {title}: enrichment error {type(exc).__name__}: {exc}")
            failed.append(title)
            continue

        if not comic_data.get("anilist_id"):
            best = comic_data.pop("_anilist_best_score", 0.0) or 0.0
            if comic_data.pop("_anilist_gate_rejected", False):
                # LINE Webtoon corroboration gate: author disagreed + synonym-only
                # hit. NOTE this path does NOT rewrite the existing files, so a
                # previously-poisoned series stays poisoned on disk — the gate only
                # stops re-applying. Full repair = re-download the series (metadata
                # is re-fetched from the site; the .aio_series.json writer nulls the
                # anilist fields via _anilist_meta_fields).
                print(
                    f"  [-] {title}: AniList match rejected — site author "
                    f"disagrees and title {best:.0f} was a synonym-only hit "
                    f"— left unchanged"
                )
            else:
                print(
                    f"  [-] {title}: no confident AniList match "
                    f"(best {best:.0f}) — left unchanged"
                )
            skipped.append(title)
            continue

        # Rewrite Komikku details.json (preserve extra keys + existing artist).
        new_details = dict(existing_details)
        new_details["title"] = title
        # Author/artist: prefer the (possibly AniList-overwritten) comic_data
        # values — AniList now populates staff on a confident match ("always
        # wins", grep _staff_names_by_role in external_metadata.py). Fall back to
        # the previously-recorded value when this match carried no staff
        # (comic_data keeps the seeded credit then — see the always-wins guard in
        # _apply_anilist_match), so a blank-source refresh never wipes a good
        # author/artist. Finding S3-1.
        new_authors = ", ".join(comic_data.get("authors") or [])
        new_details["author"] = new_authors or existing_details.get("author", "")
        new_artists = ", ".join(comic_data.get("artists") or [])
        new_details["artist"] = new_artists or existing_details.get("artist", "")
        new_details["description"] = (
            comic_data.get("desc") or existing_details.get("description", "")
        )
        new_details["genre"] = list(comic_data.get("genres") or [])
        new_details["status"] = _komikku_status_to_digit(comic_data.get("status"))
        # Reader-facing extension keys (flat top-level; Komikku ignores
        # unknown keys). Mirrors the live-download writer (grep
        # 'details_payload.update'). Provenance comes from the existing
        # .aio_series.json (meta), authoritative on an in-place refresh;
        # language falls back to any value the prior details.json carried.
        new_details.update(
            _build_aio_reader_extras(
                comic_data,
                source_site=meta.get("site"),
                source_url=meta.get("url"),
                language=meta.get("language") or existing_details.get("language"),
            )
        )
        try:
            with open(details_path, "w", encoding="utf-8") as f:
                json.dump(new_details, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            print(f"  [!] {title}: details.json write failed: {exc}")
            failed.append(title)
            continue

        # Rewrite .aio_series.json enrichment fields (preserve the rest).
        meta["genres"] = list(comic_data.get("genres") or [])
        # authors: AniList overwrites these on a confident match ("always wins",
        # grep _staff_names_by_role). comic_data["authors"] is AniList's staff, or
        # the seeded meta value when the match carried no staff — never blank, so
        # the unconditional assign is safe and keeps the live-download writer's
        # behavior (aio-dl.py:12375). (.aio_series.json has no "artists" key by
        # schema; the artist credit lives in details.json + ComicInfo only.)
        meta["authors"] = list(comic_data.get("authors") or [])
        if comic_data.get("status"):
            meta["status"] = comic_data["status"]
        # Repoint the cover URL to AniList's when enrichment supplied one so
        # .aio_series.json + the UI thumbnail (library.js keys on
        # seriesMeta.cover) normalize too. comic_data["cover"] is the AniList
        # URL after enrich, or the unchanged site cover when AniList had none
        # — safe to assign unconditionally.
        meta["cover"] = comic_data.get("cover")
        # Same seven AniList fields the live-download .aio_series.json writer
        # emits; .update overwrites the loaded meta's existing keys in place
        # (order preserved), matching the prior sequential assignments byte-for-
        # byte. grep _anilist_meta_fields.
        meta.update(_anilist_meta_fields(comic_data))
        try:
            with open(
                os.path.join(folder, SERIES_META_FILE), "w", encoding="utf-8"
            ) as f:
                json.dump(meta, f, indent=2)
        except OSError as exc:
            print(f"  [!] {title}: .aio_series.json write failed: {exc}")
            failed.append(title)
            continue

        # Re-download cover.jpg from AniList (user chose always-normalize on
        # refresh). Guarded on anilist_cover so we ONLY overwrite the on-disk
        # cover when AniList actually supplied one — never clobber a custom
        # cover.jpg with a re-fetched site cover in the rare matched-but-no-
        # AniList-cover case. Falls back to the stashed site cover if the
        # AniList CDN fetch fails. grep _refresh_cover_jpg.
        cover_note = ""
        if comic_data.get("anilist_cover"):
            if _refresh_cover_jpg(
                folder,
                comic_data["cover"],
                comic_data.get("site_cover"),
                cover_scraper,
            ):
                cover_note = ", cover.jpg"

        cbz_note = ""
        if rewrite_cbz:
            n = _rewrite_cbz_comicinfo(
                folder,
                comic_data,
                title,
                meta.get("language") or existing_details.get("language") or "",
            )
            cbz_note = f", {n} CBZ rewritten"

        matched.append(title)
        print(
            f"  [+] {title}: matched id={comic_data['anilist_id']} "
            f"({len(comic_data.get('anilist_tags', []))} tags, "
            f"{len(comic_data.get('genres', []))} genres) "
            f"— details.json + .aio_series.json{cover_note}{cbz_note}"
        )

    print(
        f"\nRefresh complete: {len(matched)} updated, "
        f"{len(skipped)} skipped, {len(failed)} failed"
        + (
            f", {len(no_meta)} folders without .aio_series.json ignored"
            if no_meta
            else ""
        )
    )
    return 1 if failed else 0


# Dests declared with action="extend" + default=None in main()'s parser (grep
# the --group / --exclude-group add_argument calls for why the default can't be
# a shared []). Listed here so the post-parse [] normalization right after
# parse_args() can never drift from the declarations when a third such flag is
# added.
_EXTEND_LIST_DESTS = ("group", "exclude_group")


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
    # ── CPU budget (2026-07-05: Settings → Resource Limits → Max CPU usage) ──
    # Scales the CPU-BOUND image pools only (grep _cpu_pool_budget). Speed knob,
    # NOT image-affecting → deliberately NOT in _RESUME_GATING_DESTS (mirrors
    # modernize_effort). Resume passes the CURRENT value explicitly so it wins
    # over the persisted one (see downloader.js resume()).
    p.add_argument(
        "--max-cpu-percent",
        type=int,
        default=int(os.getenv("AIO_MAX_CPU_PERCENT", "100")),
        help="Cap CPU-bound image processing (modernize/webp transcode, final "
             "encode, PDF/image decode) to roughly this percentage of logical "
             "cores (1-100; default 100 = prior behaviour). Lower values run "
             "fewer parallel page-encode workers AND fewer threads per encoder, "
             "so the whole pipeline stays under the budget — useful to keep the "
             "machine responsive during a download. Does NOT affect network "
             "concurrency (see --image-concurrency et al.). Clamped to [1,100]. "
             "Env: AIO_MAX_CPU_PERCENT.",
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
             "the slower Playwright-driven handlers (e.g. comix) that need "
             "browser warmup + page navigation. Pure-HTTP handlers (incl. "
             "MangaFire) complete in <2s. Slow sites self-select out and the "
             "probe-failure cache suppresses them for 1h after 2 timeouts.",
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
        help="Emit the --search candidate list as JSON on stdout. This is "
             "already the default when --auto-pick is NOT set; the flag "
             "exists so UI integrations can request JSON explicitly. NOTE: "
             "combined with --auto-pick the JSON is suppressed (the run "
             "proceeds straight to the picked download) — the UI never "
             "combines the two. See aio_search_cli.run_search_mode's "
             "json_output gate.",
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
        "--disable-sites",
        default=None,
        help="Comma-separated handler names to exclude from cross-site search, "
             "the image-quality probe, AND multi-source download alternatives. "
             "Names match handler.name (the JSON 'site' field, e.g. "
             "'mangakatana,zeroscans'). The UI's Search tab writes this from the "
             "user's disabled-sites list — sites that are unreachable from their "
             "connection or chronically slow. A directly-downloaded URL is never "
             "filtered (an explicit pick overrides the block); only search "
             "candidates and multi-source alternatives are dropped. Parsed by "
             "aio_search_cli.parse_disable_sites; excluded sites also get no "
             "site_health entry in --search-json (they're already opted out).",
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
    # --- External metadata enrichment (opt-in, single-source AniList) ---
    # When enabled, queries https://graphql.anilist.co for ranked
    # categorized tags + plaintext description + country/format +
    # MAL cross-reference per series. Results merge into comic_data
    # and propagate to ComicInfo.xml (new <Tags>/<SpoilerTags>/
    # <TagsExtended>/<CountryOfOrigin>/<MediaFormat>/<AnilistId>/
    # <MalId> elements) and .aio_series.json (cached IDs so resume
    # short-circuits the fuzzy title-match step). Default off; opt-in
    # via flag or AIO_METADATA_SOURCE env var. The flags are NOT in
    # _RESUME_GATING_DESTS — they affect metadata only, not image
    # bytes. Cross-file: sites/external_metadata.py owns the client.
    p.add_argument(
        "--metadata-source",
        choices=["none", "anilist"],
        default=os.environ.get("AIO_METADATA_SOURCE", "none"),
        help="Enrich tags + description + country/format from an "
             "external API. 'anilist' uses the free AniList GraphQL "
             "API (graphql.anilist.co, 90 req/min, no auth). Default: "
             "none. Honors AIO_METADATA_SOURCE env var. The matched "
             "AniList + MAL IDs are cached in .aio_series.json so "
             "resume + --update-all runs skip the fuzzy title-match "
             "search step.",
    )
    p.add_argument(
        "--metadata-tag-min-rank",
        type=int,
        default=50,
        help="When --metadata-source=anilist, only include tags whose "
             "AniList relevance rank (0-100) meets this threshold in "
             "ComicInfo.xml <Tags>/<SpoilerTags>/<TagsExtended> and "
             ".aio_series.json. Default: 50 (moderately relevant). Set "
             "to 0 to include every tag; 80 for very-relevant-only.",
    )
    p.add_argument(
        "--metadata-refresh",
        action="store_true",
        help="Force re-fetch from the configured --metadata-source "
             "even when an external ID is already cached in "
             ".aio_series.json. Use after AniList re-tags a series, "
             "or to backfill an existing library where the cached ID "
             "is known stale.",
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
    p.add_argument(
        "--multi-source-lazy",
        action="store_true",
        help="Defer the --multi-source cross-site alternatives discovery "
             "until a chapter download actually fails, instead of running "
             "it upfront before the first chapter. The upfront discovery "
             "(title search across handlers + chapter-list fetch per "
             "candidate + alignment) costs ~30-80+ s — longer than a "
             "typical 1-5 chapter update download takes end to end. With "
             "this flag the run "
             "starts downloading immediately; the first chapter that "
             "raises a strict-mode failure pays the discovery cost once, "
             "and every later chapter (including the end-of-run missed "
             "retry pass) reuses the result. The Electron UI injects "
             "this for EVERY multi-source download by default — it is an "
             "opt-out nested in the multi-source opt-in (untick it under "
             "Settings → Default Multi-source Fallback or per-job in the "
             "New tab). Tradeoffs vs eager: (a) the collapse-splits "
             "cross-source consensus refinement can't apply — the chapter "
             "list is grouped before any failure can fire discovery; "
             "(b) ghost-chapter classification runs without the "
             "primary-only corroboration signal until discovery has "
             "fired. Both degrade to exactly the multi-source-OFF "
             "behavior, never worse. No effect without --multi-source, "
             "in --search mode (alternatives already come from the "
             "search), or with --multi-source-prefetched (reading the "
             "prefetched JSON skips the expensive search anyway).",
    )
    p.add_argument("--cookies", default="")
    # action="extend" (NOT plain nargs="+") so a REPEATED flag accumulates
    # instead of overwriting — bare nargs="+" keeps only the LAST occurrence,
    # which silently dropped every group but one on the --update-all replay
    # path (grep 'child_cmd.extend(["--group"' in _append_saved_update_options:
    # it emits one flag per saved group). Comma-joining the replay into a
    # single argument is NOT an alternative — group names may contain commas,
    # and main() later splits every value on "," (grep 'group_string.split').
    # default=None (NOT []) because argparse hands the extend default object
    # itself back to the caller when the flag is absent — a shared mutable list
    # one in-place mutation away from leaking groups between parses in the same
    # process. Restored to [] right after parse_args (grep _EXTEND_LIST_DESTS)
    # so consumers and the resume gating_hash still see [] for "not passed".
    p.add_argument(
        "--group",
        nargs="+",
        action="extend",
        default=None,
        help="One or more preferred scanlation groups, in order of priority. "
        'Can be a single quoted string with commas (e.g., "A, B"), '
        'multiple arguments (e.g., "A" "B"), or a repeated flag '
        '(e.g., --group "A" --group "B"); the forms compose.',
    )
    p.add_argument(
        "--mix-by-upvote",
        action="store_true",
        help="When multiple --group args are used, ignore priority order and "
        "rank across the union of the specified groups instead. (Upvotes are "
        "now only a weak tiebreak inside that ranking — most sites report no "
        "vote count at all — so this is 'best of my groups' rather than "
        "'most upvoted'. Flag name kept for back-compat.)",
    )
    p.add_argument(
        "--no-group-fallback",
        action="store_true",
        help="When --group is set, skip chapters missing all preferred groups instead of falling back to another group.",
    )
    p.add_argument(
        "--mtl",
        choices=("avoid", "allow", "exclude"),
        default="avoid",
        help="How to treat machine-translated (MTL) chapter versions, detected "
        "from the scanlation group's name and self-description (grep "
        "sites/group_quality.py). 'avoid' (default) ranks them below every "
        "human translation but still downloads one when it is the ONLY "
        "version of that chapter — you never silently lose a chapter. "
        "'allow' ignores the signal entirely. 'exclude' skips a chapter whose "
        "every version is confirmed MTL (heuristic 'suspect' matches are never "
        "excluded, only demoted). To force a specific group regardless of this "
        "setting, name it in --group.",
    )
    # extend/default=None for the same reason as --group above — this one is
    # what the Codex review on PR #68 actually caught: _append_saved_update_
    # options emits one --exclude-group per saved group, so under bare
    # nargs="+" an --update-all child honored only the LAST exclusion and
    # re-downloaded everything else the user had rejected.
    p.add_argument(
        "--exclude-group",
        nargs="+",
        action="extend",
        default=None,
        help="Scanlation groups to avoid. Same input shape as --group (repeat "
        "the flag, pass several values at once, or pass one comma-separated "
        "string — the forms compose). Excluded versions rank below everything "
        "else but are still used when no alternative exists; "
        "add --no-group-fallback to skip those chapters instead.",
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
        "--refresh-library-metadata",
        action="store_true",
        help="Re-pull AniList metadata for every already-downloaded series "
             "under --output-dir and rewrite details.json + .aio_series.json "
             "in place, WITHOUT re-downloading images. Pass a positional "
             "substring (folder name or URL) to restrict to one series, e.g. "
             "--refresh-library-metadata \"Eleceed\". Honors "
             "--metadata-tag-min-rank and --metadata-refresh (cache-bypass). "
             "Repairs libraries grabbed before the genre-normalization fix. "
             "Add --refresh-rewrite-cbz to also rewrite chapter CBZ ComicInfo. "
             "Then exits.",
    )
    p.add_argument(
        "--refresh-rewrite-cbz",
        action="store_true",
        help="With --refresh-library-metadata, also rewrite the enrichment "
             "elements inside each chapter CBZ's ComicInfo.xml "
             "(<Genre>/<Tags>/<SpoilerTags>/<TagsExtended>/<Summary>/"
             "<CountryOfOrigin>/<MediaFormat>/<AnilistId>/<MalId>). Per-chapter "
             "fields are preserved. I/O-heavy (repackages every CBZ); "
             "off by default.",
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
    # ── Content-aware JXL/AVIF transcode (opt-in via --modernize) ──
    # CBZ-only storage optimizer: re-encode JPEG/PNG pages to JXL (B&W line
    # art) or AVIF (color) before the byte-passthrough fast-path packages them.
    # Hard-gated below (grep '--modernize compatibility checks') to the CBZ
    # fast-path: every flag that would force the slow save_final_images path
    # (which re-encodes .jxl/.avif to PNG) is rejected with p.error(). Pairs
    # with recompress_chapter_images_modern(); the --modernize* dests are in
    # _RESUME_GATING_DESTS so changing them re-transcodes on resume.
    p.add_argument(
        "--modernize",
        action="store_true",
        help="CBZ ONLY: transcode downloaded JPEG/PNG pages to JXL (grayscale "
             "line art) or AVIF (color) before packaging, for visually-lossless "
             "storage savings on a reader that decodes them. Per-page format "
             "choice; WebP/AVIF/GIF/already-JXL sources are left untouched (no "
             "headroom). A page is replaced only if the new file is smaller by "
             "the --modernize-min-saving margin, else the original bytes are "
             "kept. Rides the CBZ byte-passthrough fast-path and is therefore "
             "rejected at startup with --format other than cbz, --no-processing, "
             "--no-cbz-preserve-originals, --quality, --width, --aspect-ratio, "
             "or --scaling != 100; use --keep-images to retain the original "
             "downloads. Needs pillow-jxl-plugin for JXL (AVIF is built into "
             "Pillow >= 12).",
    )
    p.add_argument(
        "--modernize-format",
        choices=["auto", "jxl", "avif", "jxl+avif"],
        default="auto",
        help="Codec routing for --modernize (default: auto). auto = JXL for "
             "grayscale pages, AVIF for color. jxl / avif = force one codec. "
             "jxl+avif = encode both per page and keep the smaller (slower). "
             "Oversized pages (> 8192 px) route to JXL regardless (AVIF "
             "large-dimension decode is less portable), except under 'avif' "
             "where they are left untouched.",
    )
    p.add_argument(
        "--modernize-quality",
        type=int,
        default=90,
        choices=range(1, 101),
        metavar="[1-100]",
        help="AVIF quality for color pages under --modernize (default: 90 ~ "
             "visually lossless; 85 = aggressive, smaller, artifacts only under "
             "pixel-peeping). Ignored for grayscale (JXL) pages.",
    )
    p.add_argument(
        "--modernize-distance",
        type=float,
        default=1.0,
        help="JXL Butteraugli distance for grayscale pages under --modernize "
             "(default: 1.0 ~ visually lossless; lower = higher quality/larger; "
             "0.0 selects JXL mathematically-lossless mode). Ignored for color "
             "(AVIF) pages. Sub-1.0 lossy distances are a trap on JPEG "
             "sources: 0.5 measured 101-129%% of the already-lossy source "
             "(bigger than the original) — for archival fidelity use 0.0 "
             "(bit-exact JPEG reconstruction), not a lower lossy distance.",
    )
    p.add_argument(
        "--modernize-min-saving",
        type=float,
        default=0.92,
        help="Keep a transcoded page only if its size is below this fraction "
             "of the original (default: 0.92 = must save at least 8%%). Guards "
             "against bloating already-dense pages and auto-skips low-headroom "
             "sources; the original bytes are kept otherwise.",
    )
    # effort/speed are the pure CPU<->size knobs for the two encoders — they
    # change encode time and file size ONLY, never the decoded pixels, so they
    # are intentionally NOT in _RESUME_GATING_DESTS (grep that set for the
    # matching note). The axes are INVERSE (higher JXL effort = slower+smaller;
    # higher AVIF speed = faster+larger). Defaults 7 / 6 are the measured sweet
    # spot; see recompress_chapter_images_modern()'s docstring + the memory note
    # modernize-effort9-cpu-trap for the benchmark rationale.
    p.add_argument(
        "--modernize-effort",
        type=int,
        default=7,
        choices=range(1, 10),
        metavar="[1-9]",
        help="JXL encode effort for grayscale pages under --modernize "
             "(default: 7 = sweet spot). Higher = slower encode, smaller files, "
             "SAME pixels. 8 matches 9's size at ~1.5x the speed; 9 is a CPU "
             "trap (~7.5x slower than 7 for only ~5%% smaller). Ignored for "
             "color (AVIF) pages.",
    )
    p.add_argument(
        "--modernize-avif-speed",
        type=int,
        default=6,
        choices=range(0, 11),
        metavar="[0-10]",
        help="AVIF encode speed for color pages under --modernize (default: 6 "
             "= sweet spot). INVERSE of JXL effort: higher = faster encode, "
             "LARGER files, same pixels; lower = slower, smaller. 4 is ~5x "
             "slower than 6 for ~2%% smaller; speeds <=3 measured <=0.5%% "
             "smaller than 4 for 2.3-3.9x the time; 10 encodes fast but "
             "bloats ~13%%. Ignored for grayscale (JXL) pages.",
    )
    # ── Auxiliary chapter assets (audio / motion-toon archival) ──
    # tapas.io + webtoons.com carry .mp3/.m4a audio (motion sounds + episode
    # BGM), a motion timeline manifest, and SoundCloud embeds. By default those
    # ride INSIDE the chapter CBZ under the reserved _aio/ prefix (renumber-
    # exempt) and are indexed in the per-chapter ComicInfo.xml <AioChapterResources>
    # + a series-level rollup in details.json, for a custom reader. CBZ-only
    # (EPUB/PDF can't embed them → skipped, logged once). Zero cost on every
    # site that emits no aux. See _materialize_chapter_aux + sites.base.AssetSpec.
    p.add_argument(
        "--no-sidecar-assets",
        action="store_true",
        help="Disable capture of auxiliary chapter assets (webtoons motion-toon "
             "manifests + audio + episode BGM, tapas SoundCloud references). By "
             "default these are embedded INSIDE each chapter CBZ under _aio/ and "
             "indexed in ComicInfo.xml / details.json for a custom reader; this "
             "flag skips all of that (images are unaffected). Not a resume-gating "
             "flag — toggling it doesn't invalidate downloaded images.",
    )
    # ── comix.to browser controls ──────────────────────────────────────────
    # comix is the only handler that drives a real browser for the DOWNLOAD path
    # (its chapter list, page URLs and search are all behind a signed +
    # encrypted API). These three flags surface the parts of that a user may
    # legitimately need to steer. All of them just set the env vars sites/comix.py
    # already reads, so the CLI and the env knobs share exactly one code path and
    # neither can drift. None are resume-gating: they change HOW bytes are
    # obtained, not WHICH bytes land (same reasoning as --no-fast-download).
    p.add_argument(
        "--comix-headless",
        action="store_true",
        help="Run comix.to's browser headless instead of showing a window. "
             "NOT recommended: comix's reader defers roughly every 10th page "
             "until it scrolls into view, which needs a live rendering "
             "lifecycle, so headless chapters can come out short. Use this only "
             "for unattended runs (cron/CI/headless servers). Equivalent to "
             "AIO_COMIX_HEADLESS=1.",
    )
    p.add_argument(
        "--comix-allow-gapped-chapters",
        action="store_true",
        help="Keep a comix.to chapter even when some pages could not be "
             "captured, instead of skipping it. The shortfall is logged and the "
             "missing page numbers are recorded in the chapter's ComicInfo.xml, "
             "so the gap is never silent. Off by default — a skipped chapter "
             "that --multi-source can refill from another site beats an archive "
             "with invisible holes. Equivalent to AIO_COMIX_ALLOW_GAPPED=1.",
    )
    p.add_argument(
        "--comix-login",
        action="store_true",
        help="Open comix.to in the downloader's own browser window and wait "
             "while you sign in, then exit. The session is saved to the "
             "persistent profile and reused by every later run. Your "
             "credentials are typed into the real site — the downloader never "
             "sees, stores or transmits them.",
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
    # Undo the default=None the action="extend" list flags need. Every consumer
    # (select_best_chapter_version, _build_group_selection_policy,
    # _save_download_params) and — critically — the resume gating_hash expect []
    # for "flag not passed": these dests are in _RESUME_GATING_DESTS, so a None
    # would hash as null and invalidate every in-progress partial download on an
    # otherwise unchanged command line. Must stay ahead of get_resumable_params.
    for _list_dest in _EXTEND_LIST_DESTS:
        if getattr(args, _list_dest, None) is None:
            setattr(args, _list_dest, [])
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

    # comix browser flags -> the env vars sites/comix.py already reads. Setting
    # the env rather than threading args through means the CLI flag and the env
    # knob cannot drift apart, and the handler stays usable from the search
    # subprocess and the API server, neither of which sees `args`. Must run
    # before any handler is constructed.
    if getattr(args, "comix_headless", False):
        os.environ["AIO_COMIX_HEADLESS"] = "1"
    if getattr(args, "comix_allow_gapped_chapters", False):
        os.environ["AIO_COMIX_ALLOW_GAPPED"] = "1"

    if getattr(args, "comix_login", False):
        # Standalone action: open the window, wait for the user to sign in, exit.
        # Deliberately its own mode rather than a pre-step on a download — a
        # login is a one-off that then persists in the profile for every later
        # run, and blocking a download on it would be the wrong default.
        from sites.comix import _COMIX_BROWSER_BRIDGE

        outcome = _COMIX_BROWSER_BRIDGE.open_login_window() or {}
        if outcome.get("signed_in"):
            print("\n[*] comix.to sign-in saved. Future runs will reuse it.")
            return
        print(
            f"\n[!] comix.to sign-in was not completed "
            f"({outcome.get('reason') or 'unknown'}).",
            file=sys.stderr,
        )
        sys.exit(2)

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
        _no_spawn = _self_spawn_unavailable()
        if _no_spawn:
            sys.exit(f"--update-all cannot run here: {_no_spawn}")
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

        def _classify_update_result(title: str, returncode: int, stdout: str, stderr: str) -> None:
            # Mirror the child's stdout/stderr, then bucket the run: rc 0 -> updated
            # (implicit), rc != 0 with a "nothing new" marker -> up-to-date, else
            # failed. Shared by the parallel + serial loops so the up-to-date
            # heuristic can't drift between them.
            sys.stdout.write(stdout)
            sys.stderr.write(stderr)
            if returncode != 0:
                combined = stdout + stderr
                if "No chapters selected" in combined or "Filtered list down to 0 chapters" in combined:
                    up_to_date.append(title)
                    print("  Already up to date.")
                else:
                    failed.append(title)

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
                    _classify_update_result(title, returncode, stdout, stderr)
        else:
            for title, cmd in child_procs:
                print(f"\n{'=' * 60}")
                print(f"Updating: {title}")
                print(f"{'=' * 60}")
                _, returncode, stdout, stderr = _run_saved_update(title, cmd)
                _classify_update_result(title, returncode, stdout, stderr)
        print(f"\n{'=' * 60}")
        updated = len(child_procs) - len(failed) - len(up_to_date)
        print(f"Update complete: {updated} updated, {len(up_to_date)} up-to-date, {len(failed)} failed")
        if failed:
            print(f"Failed: {', '.join(failed)}")
            sys.exit(1)
        return

    if getattr(args, "refresh_library_metadata", False):
        # In-place AniList re-enrichment of an existing library; no
        # downloads. Defined just above main(). Exits with its own code.
        sys.exit(_refresh_library_metadata(args))

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

    # --modernize compatibility checks. Like --webtoon-recompress these run
    # early (before a multi-hour download), but ALL fast-path-disabling
    # combinations are HARD errors, not warnings: --modernize emits .jxl/.avif,
    # and unlike .webp those are NOT understood by the slow save_final_images
    # auto-format path (it would re-encode them to PNG, and epub/none force
    # JPEG). So --modernize is only correct on the CBZ byte-passthrough
    # fast-path; we reject anything that would disable it. The seven checks
    # mirror the seven cbz_fast_path conditions (grep 'cbz_fast_path =') using
    # the same _user_set_* sentinels so they track that gate if it ever changes.
    if getattr(args, "modernize", False):
        if args.format != "cbz":
            p.error(
                "--modernize requires --format cbz: it transcodes pages into "
                "the CBZ byte-passthrough fast-path. Other formats re-encode the "
                "pages (epub/none -> JPEG, pdf -> /DCTDecode) and would discard "
                "the JXL/AVIF."
            )
        if getattr(args, "no_cbz_preserve_originals", False):
            p.error(
                "--modernize is incompatible with --no-cbz-preserve-originals: "
                "it disables the fast-path, so the .jxl/.avif pages would be "
                "decoded and re-encoded to PNG by save_final_images."
            )
        if args.no_processing:
            p.error(
                "--modernize is incompatible with --no-processing, which "
                "bypasses the transcode stage entirely."
            )
        if args.scaling != 100:
            p.error(
                f"--modernize is incompatible with --scaling={args.scaling}: "
                "resizing forces the slow decode-resize-encode path, which "
                "re-encodes the .jxl/.avif pages to PNG. Use --scaling 100."
            )
        if getattr(args, "_user_set_width", False):
            p.error(
                "--modernize is incompatible with --width: it forces the slow "
                "decode-resize-encode path (which re-encodes pages to PNG)."
            )
        if getattr(args, "_user_set_aspect_ratio", False):
            p.error(
                "--modernize is incompatible with --aspect-ratio: it forces the "
                "slow decode-resize-encode path (which re-encodes pages to PNG)."
            )
        if getattr(args, "_user_set_quality", False):
            p.error(
                "--modernize is incompatible with an explicit --quality: it "
                "forces the slow re-encode path. Set modernize quality via "
                "--modernize-quality (AVIF) and --modernize-distance (JXL)."
            )
        # Fail fast on missing encoders rather than mid-download. JXL needs the
        # optional pillow-jxl-plugin; AVIF is native in Pillow >= 12. An
        # avif-only policy never emits JXL (oversized pages are skipped), so it
        # doesn't require the JXL plugin.
        _mpolicy = args.modernize_format
        if _mpolicy != "avif":
            try:
                import pillow_jxl  # noqa: F401  (registers JXL in PIL.Image.SAVE)
            except ImportError:
                p.error(
                    f"--modernize-format {_mpolicy} needs the JXL encoder. "
                    "Install it: pip install pillow-jxl-plugin "
                    "(or use --modernize-format avif for AVIF-only)."
                )
        if _mpolicy in ("auto", "avif", "jxl+avif"):
            # AVIF is native in Pillow >= 12 (Image.init() registers it). On
            # OLDER Pillow the pillow-avif-plugin fallback advertised in the
            # error below registers AVIF only when its module is IMPORTED —
            # Image.init() does not load it — so try that import first or we'd
            # reject a working install. Best-effort, mirrors the pillow_jxl
            # import above. The same import is repeated in
            # recompress_chapter_images_modern so the encoder is present at
            # encode time too (grep 'import pillow_avif').
            try:
                import pillow_avif  # noqa: F401  (registers AVIF in PIL.Image.SAVE)
            except ImportError:
                pass
            Image.init()  # native AVIF plugin registers lazily
            if "AVIF" not in Image.SAVE:
                p.error(
                    f"--modernize-format {_mpolicy} needs AVIF write support. "
                    "Pillow >= 12 has it natively; otherwise: "
                    "pip install pillow-avif-plugin."
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
    # Closure-scope multi-source state. Dict mapping chapter_num_float → list
    # of alternative source dicts (each with handler/scraper/context/chapter).
    # Three writers: --search --multi-source --auto-pick (right below), the
    # direct-URL discovery (grep _discover_multi_source_alternatives — eager
    # by default, deferred to first chapter failure under
    # --multi-source-lazy), and the prefetched-JSON path inside that same
    # closure. Consumed by _process_chapter_strict for per-chapter fallback.
    # Empty/None means single-source mode (existing behavior unchanged).
    _multi_source_alternatives: Dict[float, List[Dict[str, Any]]] = {}
    # consensus_set for the refined collapse-splits Rule 2 / 3b / 6 drops at
    # group_chapters_for_download (2026-05-27). Populated alongside
    # _multi_source_alternatives from the same three carriers (auto-pick,
    # prefetched JSON, direct-URL discovery). None = no peer signal; the
    # group helper falls through to original in-source-only heuristics.
    _multi_source_consensus_set: Optional[Set[float]] = None

    # Persistable multi-source resume cache (run_params.json `multi_source_cache`).
    # Set by _discover_multi_source_alternatives on a successful discovery. The
    # run_params write block folds it into the freshly-written file (EAGER path —
    # the tmp dir doesn't exist yet at discovery time), while the LAZY path writes
    # it directly via _persist_multi_source_cache. grep this name + the module
    # helpers _read/_persist/_build_multi_source_cache*.
    _ms_resume_cache_payload: Optional[Dict[str, Any]] = None

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
        # The supervisor below drives children via subprocess.Popen. Where
        # that's impossible (embedded interpreter), refuse with a real reason
        # instead of spawning something that can't work — an embedder should
        # queue one URL per run, which is what the Android side does.
        _no_spawn = _self_spawn_unavailable()
        if _no_spawn:
            sys.exit(f"Multiple URLs cannot be run here: {_no_spawn}")
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

    # Process the group arguments to handle comma-separated strings
    if args.group:
        # Flatten the list of strings, splitting each by comma, and stripping whitespace.
        args.group = [
            g.strip()
            for group_string in args.group
            for g in group_string.split(",")
        ]
    if getattr(args, "exclude_group", None):
        args.exclude_group = [
            g.strip()
            for group_string in args.exclude_group
            for g in group_string.split(",")
            if g.strip()
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

    # --- The ONLY opt-in for interactive Cloudflare solving in the codebase ---
    #
    # Below this line a human is waiting on THIS run, so a challenge may open a
    # browser window (desktop zendriver) or a phone ChallengeActivity (the
    # Android WebView backend) and block until it is solved. Everything else —
    # cross-site search, the image-quality probe, library update-checks, the
    # Android update sweep — runs at the default of False and gets only the
    # headless tiers. See sites/crawlee_utils.py's "TWO SEPARATE QUESTIONS"
    # block for why this is opt-in rather than opt-out.
    #
    # PLACED HERE, before fetch_comic_context, because the defect this rescues
    # (PARITY.md D2) is a challenged CHAPTER LIST at least as often as a
    # challenged image — granting it later, around the download loop only, would
    # leave the run failing at "No chapters selected." with the solver idle.
    # One grant covers fetch_comic_context -> get_chapters -> get_chapter_images.
    #
    # --list-chapters is excluded and that exclusion is load-bearing: it is the
    # entry point BOTH the desktop UI's update-check subprocess and Android's
    # in-process update sweep use, and neither has anyone watching to solve a
    # CAPTCHA. Reaching this line at all means a URL was resolved, so a
    # search-only run never gets here.
    #
    # Unscoped grant, not a `with`: main() has no enclosing scope to hang a
    # finally on (it runs to EOF and returns from many points). Desktop is a
    # fresh process per run so there is nothing to restore, and on Android
    # aio_android wraps every main() call in interactive_solving(...) whose
    # reset() restores the pre-call value regardless of what is set inside —
    # grep _cf_interactive_solving there. The helper's own docstring records
    # that this is its only sanctioned caller.
    if not getattr(args, "list_chapters", False):
        from sites.crawlee_utils import allow_interactive_solving_for_this_run

        allow_interactive_solving_for_this_run()

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
    _emit("series", title=title, hid=hid, url=getattr(args, "comic_url", None))

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
            # _CPU_POOL_PERCENT, etc.) honor the user's original
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

    # Advisory early-stop hint for handlers with expensive paginated chapter
    # listing (comix's browser DOM scrape walks up to ~360 pager pages on a
    # long multi-group series). Set on the INSTANCE just before the call; see
    # BaseSiteHandler.chapter_floor_hint for the contract. None whenever the
    # floor isn't provably safe, so this can never drop a wanted chapter — the
    # real --chapters filter still runs over the returned pool below either way.
    chapter_floor = _chapter_range_floor(getattr(args, "chapters", "all"))
    try:
        handler.chapter_floor_hint = chapter_floor
    except Exception:
        # Handlers are plain objects, but never let a hint break the run.
        chapter_floor = None

    if getattr(args, "download_volumes", False):
        pool = handler.get_volumes(context, scraper, args.language, make_request)
        if not pool:
            sys.exit("This site handler does not expose volume listing.")
    else:
        pool = handler.get_chapters(context, scraper, args.language, make_request)

    # A floored listing is a PARTIAL view of the series by design, so the
    # "how many chapters existed at download time" stat below must not report
    # it as the total. Volumes never take the hint.
    pool_is_partial = bool(chapter_floor is not None) and not getattr(
        args, "download_volumes", False
    )

    # ── Direct-URL multi-source: find alternatives for fallback ──
    # When --multi-source is set and we got here via a direct URL (not via
    # --search --auto-pick which already populated _multi_source_alternatives),
    # search for the series title across other handlers and pre-fetch their
    # chapter lists so per-chapter fallback in _process_chapter_strict has
    # alternatives to try. Skipped when --list-chapters is set (read-only
    # mode, no downloads happening, alternatives discovery would just waste
    # time).
    #
    # The discovery lives in a closure so it can run at either of two times:
    #   - eagerly, right below (CLI default — unchanged legacy behavior), or
    #   - lazily, from _process_chapter_strict's first-failure path when
    #     --multi-source-lazy is set (grep _ms_lazy_pending). The Electron
    #     UI injects that flag for every multi-source download by default
    #     (opt-out chokepoint in UI-source/electron/downloader.js, grep
    #     multiSourceLazy) because the upfront discovery costs more
    #     wall-clock than a typical 1-5 chapter delta download.
    # It reads handler/context/pool from main()'s scope at CALL time; at the
    # lazy call site those still hold the primary source (the strict
    # wrapper's alt-swap happens after, and its finally restores primary),
    # and `pool` is never reassigned after get_chapters.
    def _discover_multi_source_alternatives() -> None:
        """Populate _multi_source_alternatives / _multi_source_consensus_set
        for a direct-URL --multi-source run. Never raises: any discovery
        failure degrades to standard single-source behavior."""
        nonlocal _multi_source_alternatives, _multi_source_consensus_set
        nonlocal _ms_resume_cache_payload
        # Source of alternatives, in priority order:
        #   1. UI-supplied --multi-source-prefetched file (authoritative for
        #      THIS spawn; the search-tab writes it just before launch, saving
        #      the redundant ~80s search).
        #   2. Resume cache persisted by a PRIOR run of this series into
        #      run_params.json (< TTL old) — reused via the SAME payload path
        #      as the prefetched file, skipping the ~30-80s cross-site search
        #      but still re-fetching chapter lists so the alt data stays fresh.
        #   3. Fresh cross-site search (find_alternatives_for_direct_url).
        # Anything discovered fresh (2/3) is re-persisted so the NEXT resume is
        # cheap. grep _read/_persist/_build_multi_source_cache*.
        prefetched_path = getattr(args, "multi_source_prefetched", None)
        _run_params_path = os.path.join(main_tmp_dir, "run_params.json")
        cache_from_disk = (
            None if prefetched_path
            else _read_multi_source_resume_cache(_run_params_path)
        )
        _cache_hit = False
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
            elif cache_from_disk is not None:
                from aio_search_cli import build_alternatives_from_payload
                _cache_hit = True
                _age_h = max(
                    0.0,
                    (time.time() - float(cache_from_disk.get("saved_at", 0))) / 3600.0,
                )
                print(
                    f"[*] Multi-source: reusing "
                    f"{len(cache_from_disk.get('alternatives') or [])} cached "
                    f"alternative source(s) from run_params.json "
                    f"(discovered {_age_h:.1f}h ago); skipping cross-site search",
                    file=sys.stderr,
                )
                _ms_result = build_alternatives_from_payload(
                    cache_from_disk,
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
            # Guard-filter (2026-07-13): drop the user's disabled sites from the
            # assembled alternatives. This is the CORRECTNESS net for the
            # download-scope disable — it catches prefetched/disk-cached alts
            # that predate the disable (the live find_alternatives path also
            # passes exclude_sites, but the build_alternatives_from_prefetched /
            # _from_payload carriers don't). Because _ms_alts IS
            # _ms_result["alternatives_by_chap_num"] (same object), this also
            # scrubs the payload persisted to run_params.json below, so a
            # disabled site can't get re-cached. The primary anchor is never in
            # this dict, so it's never filtered. grep parse_disable_sites.
            from aio_search_cli import parse_disable_sites as _parse_disable_sites
            _disable_set = _parse_disable_sites(args)
            if _disable_set and _ms_alts:
                for _cf in list(_ms_alts):
                    _ms_alts[_cf] = [
                        _a for _a in _ms_alts[_cf]
                        if (_a.get("site", "") or "").lower() not in _disable_set
                    ]
                    if not _ms_alts[_cf]:
                        del _ms_alts[_cf]
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
                # Persist the discovered sources so a future resume of this
                # series skips the search. On a cache HIT the payload is already
                # on disk and unchanged — just retain it for the write block /
                # post-wipe rewrite (keeps the original saved_at, so the TTL
                # counts from first discovery, not from each resume). On a fresh
                # search or prefetched-file run, build a fresh payload
                # (saved_at=now) and BOTH stash it (eager path: run_params write
                # block folds it in) AND write it now (lazy path: that block has
                # already run). grep _persist_multi_source_cache.
                if _cache_hit:
                    _ms_resume_cache_payload = cache_from_disk
                else:
                    _cache_year = None
                    try:
                        if comic_data.get("year"):
                            _cache_year = int(comic_data["year"])
                    except (TypeError, ValueError, AttributeError):
                        _cache_year = None
                    _new_cache = _build_multi_source_cache_payload(
                        _ms_result, title, time.time(), year=_cache_year,
                    )
                    if _new_cache:
                        _ms_resume_cache_payload = _new_cache
                        _persist_multi_source_cache(_run_params_path, _new_cache)
        except Exception as exc:
            # Don't let alternatives discovery block the main download. If it
            # fails, the user gets standard single-source behavior.
            print(
                f"[!] Multi-source alternatives discovery failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    # Armed = discovery deferred; _process_chapter_strict flips it False and
    # fires the closure on the first ChapterSkippedError (one-shot, even when
    # discovery fails or yields nothing — re-running per failed chapter would
    # just repeat an expensive no-op). Prefetched mode stays eager: reading
    # the UI's JSON skips the expensive search, and its temp file lifetime is
    # tied to this spawn (downloader.js unlinks on close), so defer-reading
    # it buys nothing.
    _ms_lazy_pending = False
    if (
        getattr(args, "multi_source", False)
        and not getattr(args, "search", None)
        and not getattr(args, "list_chapters", False)
        and not getattr(args, "download_volumes", False)
        and not _multi_source_alternatives  # not already populated
    ):
        if (
            getattr(args, "multi_source_lazy", False)
            and not getattr(args, "multi_source_prefetched", None)
        ):
            _ms_lazy_pending = True
            print(
                "[*] Multi-source ON (lazy): alternatives discovery deferred "
                "until a chapter fails",
                file=sys.stderr,
            )
        else:
            _discover_multi_source_alternatives()

    # ── --list-chapters: print metadata + chapter list as JSON, then exit ──
    # Used by the UI to check for new chapters without downloading anything.
    # Only needs series metadata + the chapter list — no image fetching, no downloads.
    # IMPORTANT: This runs BEFORE allocate_series_output_dir so it doesn't
    # create empty folders in manga/ just for checking.
    #
    # collapse-aware: when --collapse-splits is set, the emitted chapter list
    # mirrors what `group_chapters_for_download` would actually surface for
    # download (same function used post-filtering at the `groups = ...` call
    # below this block). Without this, fragment-shaped decimals (mangafire's
    # 52.1 next to 52, sequential split clusters, etc.) leak into the UI's
    # diff against meta.chapters_downloaded and stick as "+N new" forever
    # because no download path produces them under collapse. Cross-file: the
    # Electron-side caller is UI-source/electron/main.js:_checkSeriesUpdates;
    # the Library "Check All" path forwards settings.collapseSplits into the
    # spawn so this branch fires exactly when the user has collapse on.
    if getattr(args, "list_chapters", False):
        # Deduplicate by normalized chapter label. The regular flow at
        # `chapters_by_num` normalizes float/str chap fields with `:g` so
        # handlers that emit 4.0 (mangathemesia subclasses) don't split
        # from "4"-emitting peers; we mirror that here so the dedupe and
        # the optional collapse pass see the same canonical keys.
        list_chapters_by_num: Dict[str, List[Dict[str, Any]]] = {}
        deduped_pool: List[Dict[str, Any]] = []
        # Premium/locked placeholders (tapas.get_chapters emits them for
        # WAIT_OR_MUST_PAY episodes) are surfaced SEPARATELY as locked_chapters
        # so the UI can badge them + nudge multi-source — but they are NEVER
        # folded into `chapters`, which drives the UI's "+N new" update diff.
        # They don't download without --multi-source (and even with it, only if
        # an alt carries them), so counting them there would show a perpetual
        # "+N new". Cross-file: consumed by UI-source (grep locked_chapters).
        locked_labels: List[str] = []
        for ch in pool:
            if ch.get("_locked"):
                lk = ch.get("chap")
                if isinstance(lk, (int, float)):
                    lk = f"{lk:g}"
                elif lk is not None:
                    lk = str(lk)
                if lk is not None and _chap_as_float(lk) is not None:
                    locked_labels.append(lk)
                continue
            num = ch.get("chap")
            if num is None:
                continue
            if isinstance(num, (int, float)):
                num = f"{num:g}"
            else:
                num = str(num)
            # Mirror the download path's "Oneshot"→"1" remap (grep
            # chapters_by_num, ~line 9237) so --list-chapters and the
            # .aio_series.json writer agree on the label. manganato's
            # _chapter_number returns the raw title as its no-numeric-token
            # fallback, so a chapter literally titled "Oneshot" would otherwise
            # LIST as "Oneshot" but RECORD as "1" → the UI update-check shows it
            # as a perpetual "+1 new". Must run BEFORE the seen_nums dedup so a
            # real Ch.1 + a "Oneshot" collapse to one "1" (as the download path
            # buckets them). XF-1 follow-up.
            if num.lower() in ("oneshot", "one-shot"):
                num = "1"
            # Mirror the download path's numeric gate: bucketing (grep
            # chapters_by_num) does float(num_str) and DROPS non-numeric labels,
            # so best_chapters — everything the download actually fetches — is
            # numeric-only. --list-chapters must drop them too, else the UI
            # update-check surfaces a "Special"/"Extra"/"Omake" that never
            # downloads as a perpetual "+N new". Extends the Oneshot fix above to
            # ALL non-numeric labels. Residual follow-up.
            if _chap_as_float(num) is None:
                continue
            # Carry the dict shape (group_chapters_for_download needs it) but
            # with the normalized label injected so its `_extract_chapter_num`
            # parse and our diff target both see the same string.
            list_chapters_by_num.setdefault(num, []).append({**ch, "chap": num})

        # Collapse each number to ONE version with the SAME selector the
        # download path uses. This used to be a plain first-wins `seen_nums`
        # dedupe, which diverged from the download in two ways: it ignored
        # --group entirely, and under --no-group-fallback it would LIST a
        # chapter the download then skipped (a permanent "+1 new" in the UI).
        # The winner choice never changes which NUMBERS exist, so the UI's
        # update diff is unaffected — only the per-chapter group metadata below.
        _list_policy = _build_group_selection_policy(handler, list_chapters_by_num, args)
        for num in sorted(list_chapters_by_num.keys(), key=float):
            best = handler.select_best_chapter_version(
                list_chapters_by_num[num],
                args.group,
                args.mix_by_upvote,
                allow_group_fallback=not getattr(args, "no_group_fallback", False),
                selection_policy=_list_policy,
            )
            if best:
                deduped_pool.append(best)

        collapse_splits_enabled = bool(getattr(args, "collapse_splits", False))
        if collapse_splits_enabled:
            # Same function the actual download path uses. consensus_set is
            # None for single-URL --list-chapters mode (no multi-source peer
            # data); group_chapters_for_download falls through to its
            # in-source-only Rule 2 / 3b / 6 heuristics in that case — same
            # behavior the download would produce. Cross-file: see
            # sites/chapter_merger.py:group_chapters_for_download for the
            # full cluster-rule table.
            groups = group_chapters_for_download(
                deduped_pool,
                collapse_splits=True,
                consensus_set=_multi_source_consensus_set,
            )
            unique_chapters = [g.label for g in groups]
        else:
            unique_chapters = [ch["chap"] for ch in deduped_pool]

        # Sort numerically
        try:
            unique_chapters.sort(key=lambda x: float(x))
        except (ValueError, TypeError):
            pass

        locked_chapters = sorted(set(locked_labels), key=lambda x: float(x))

        # Scanlation groups available for this series, most-prolific first, so
        # the UI can show what exists and let the user pick instead of typing a
        # name blind. Purely additive — the UI ignores unknown keys, and none of
        # this participates in the "+N new" diff.
        group_rows: List[Dict[str, Any]] = []
        _group_display: Dict[str, str] = {}
        _group_flags: Dict[str, Dict[str, Any]] = {}
        for _versions in list_chapters_by_num.values():
            for _v in _versions:
                for _info in handler.get_group_infos(_v):
                    _key = handler.get_group_match_key(_info.name)
                    if not _key:
                        continue
                    _group_display.setdefault(_key, _info.name)
                    _flags = _group_flags.setdefault(
                        _key, {"is_official": False, "mtl": "none"}
                    )
                    _flags["is_official"] = _flags["is_official"] or _info.is_official
                    if _flags["mtl"] == "none":
                        _verdict, _ = classify_mtl(
                            _info.name, description=_info.description
                        )
                        _flags["mtl"] = _verdict
        # Which chapter numbers each group does NOT cover — the one thing a
        # user actually needs before picking one ("if I force Dusk, what do I
        # lose?"). Deliberately NOT a full per-chapter group map: on a site
        # where every chapter has 3-4 competing versions (atsumaru: 201
        # chapters x 4 groups) that map is a second copy of the chapter list,
        # and the UI runs --list-chapters for EVERY library series on an
        # update check. This inverted form is near-empty in the common case.
        _covered: Dict[str, set] = {}
        for _num, _versions in list_chapters_by_num.items():
            for _v in _versions:
                for _info in handler.get_group_infos(_v):
                    _k = handler.get_group_match_key(_info.name)
                    if _k:
                        _covered.setdefault(_k, set()).add(_num)
        _all_nums = set(list_chapters_by_num.keys())
        for _key, _count in sorted(
            (_list_policy.census or {}).items(), key=lambda kv: (-kv[1], kv[0])
        ):
            _missing = sorted(_all_nums - _covered.get(_key, set()), key=float)
            group_rows.append({
                "name": _group_display.get(_key, _key),
                "key": _key,
                "chapters": _count,
                "is_official": _group_flags.get(_key, {}).get("is_official", False),
                "mtl": _group_flags.get(_key, {}).get("mtl", "none"),
                "missing_count": len(_missing),
                # Capped: a group covering 3 of 800 chapters would otherwise
                # inline the other 797.
                "missing_sample": _missing[:50],
            })

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
            # Premium/locked placeholders surfaced for the UI (badge + multi-source
            # nudge). Empty for every non-tapas site today. NOT part of `total`
            # or the "+N new" diff — see the locked_labels comment above.
            "locked_chapters": locked_chapters,
            "collapse_applied": collapse_splits_enabled,
            "groups": group_rows,
        }
        print(json.dumps(result))
        sys.exit(0)

    # Download path only (--list-chapters exited above): drop premium/locked
    # placeholders unless --multi-source can fill them. tapas.get_chapters emits
    # WAIT_OR_MUST_PAY episodes as bare `_locked` placeholders so the multi-
    # source alt-rescue can fetch them from another site; with multi-source OFF
    # there is no rescue path, so each would only attempt → short-circuit
    # ("locked") → clean-skip, and be re-attempted every run. Dropping them here
    # restores the clean single-source behavior (matches the pre-feature "N
    # locked skipped" outcome). Kept when multi-source is on so the alignment
    # includes their chapter numbers and _process_chapter_strict can rescue
    # them (grep aux_veto / _PERMANENT_SKIP_REASONS). Non-tapas pools have no
    # `_locked` entries, so this is a no-op there.
    if not getattr(args, "multi_source", False):
        _locked_dropped = sum(1 for c in pool if c.get("_locked"))
        if _locked_dropped:
            pool = [c for c in pool if not c.get("_locked")]
            print(
                f"[i] {_locked_dropped} premium/locked chapter(s) skipped — "
                f"they need a site login we don't have. Enable --multi-source "
                f"to fetch them from an alternative site.",
                file=sys.stderr,
            )

    # Output goes into a per-title folder under ./manga (title, with hid only on collision)
    # Capture the user-facing ROOT before mutating args.output_dir / args.epub_dir to the
    # per-series folder. Downstream chapter writes, final-file writes, cover writes etc.
    # expect the per-series value (grep `getattr(args, "output_dir"`), so the mutation is
    # intentional for the rest of the current run; but get_resumable_params (grep that name)
    # reads args.output_dir to persist run_params.json, and on resume the restore block
    # (grep `if args.restore_parameters:`) setattr's it back onto args BEFORE this line
    # runs again, which would feed the per-series path back into allocate_series_output_dir
    # and nest (manga/Tekyuu → manga/Tekyuu/Tekyuu). The override after get_resumable_params
    # restores the root in the saved dict. Cross-file: UI-source/electron/downloader.js
    # builds the resume CLI without --output-dir, so the persisted root is the only source
    # of truth on resume.
    _output_dir_root_for_resume = args.output_dir
    _epub_dir_root_for_resume = getattr(args, "epub_dir", None)
    out_dir = allocate_series_output_dir(title, hid, root=args.output_dir)
    setattr(args, "output_dir", out_dir)
    epub_dir_base = getattr(args, "epub_dir", None)
    if epub_dir_base:
        epub_out_dir = allocate_series_output_dir(title, hid, root=epub_dir_base)
        setattr(args, "epub_dir", epub_out_dir)

    # --- External metadata enrichment (--metadata-source anilist) ---
    # Runs AFTER allocate_series_output_dir so the cache lookup uses the
    # final per-series path (.aio_series.json lives there). The fields
    # enrichment writes into comic_data propagate to every downstream
    # sink: ComicInfo.xml builders (build_comic_info_xml +
    # build_per_chapter_comic_info_xml), Komikku details.json writer
    # (genres REPLACED by AniList's curated set on a confident match — see
    # _apply_anilist_match), and .aio_series.json writer (full ID + tag
    # persist).
    # Failures are swallowed — site-only metadata is the fallback path.
    # Cross-file: sites/external_metadata.py owns the client; CLI flags
    # registered near --enable-ml-rating; cache key in .aio_series.json
    # is "anilist_id" (must match _load_cached_anilist_id reader).
    if getattr(args, "metadata_source", "none") == "anilist":
        try:
            from sites.external_metadata import enrich_from_anilist
            cached_id = _load_cached_anilist_id(out_dir)
            year_hint: Optional[int] = None
            try:
                if comic_data.get("year"):
                    year_hint = int(comic_data["year"])
            except (TypeError, ValueError):
                year_hint = None
            comic_data = enrich_from_anilist(
                comic_data,
                hid=hid,
                handler_name=handler.name,
                year=year_hint,
                cover_url=comic_data.get("cover"),
                tag_min_rank=int(getattr(args, "metadata_tag_min_rank", 50)),
                force_refresh=bool(getattr(args, "metadata_refresh", False)),
                cached_anilist_id=cached_id,
            )
            if comic_data.get("anilist_id"):
                refresh = bool(getattr(args, "metadata_refresh", False))
                if cached_id and not refresh:
                    cache_note = " (cache hit by AniList ID)"
                elif refresh:
                    cache_note = " (refresh forced)"
                else:
                    cache_note = ""
                log_verbose(
                    f"  AniList enrichment: matched id="
                    f"{comic_data['anilist_id']} "
                    f"mal_id={comic_data.get('mal_id', 'n/a')} "
                    f"country={comic_data.get('country_of_origin', '?')} "
                    f"format={comic_data.get('media_format', '?')} "
                    f"({len(comic_data.get('anilist_tags', []))} tags, "
                    f"{len(comic_data.get('anilist_spoiler_tags', []))} "
                    f"spoiler-tags){cache_note}"
                )
            else:
                best_score = comic_data.pop("_anilist_best_score", 0.0) or 0.0
                if comic_data.pop("_anilist_gate_rejected", False):
                    # LINE Webtoon corroboration gate rejected a title-plausible
                    # candidate (author disagreed AND it was a synonym-only hit —
                    # the unOrdinary poison class). best_score is ABOVE 75 here.
                    log_verbose(
                        f"  AniList enrichment: rejected best candidate for "
                        f"'{comic_data.get('title', '?')}' (title score "
                        f"{best_score:.1f} cleared 75 but the site author "
                        f"disagreed and the match was synonym-only) — "
                        f"continuing with site-only metadata"
                    )
                else:
                    log_verbose(
                        f"  AniList enrichment: no confident match for "
                        f"'{comic_data.get('title', '?')}' "
                        f"(best rapidfuzz score {best_score:.1f} < 75 "
                        f"threshold) — continuing with site-only metadata"
                    )
        except Exception as exc:
            log_verbose(
                f"  AniList enrichment failed (continuing with "
                f"site-only data): {type(exc).__name__}: {exc}"
            )

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
            # Propagate the normalized label back onto the dict (was: append the
            # raw `ch`). Bucketing keys on num_str — "Oneshot"→"1", float 4.0→"4"
            # (:g above), numeric 0→"0" — but ch["chap"] kept the RAW value, so
            # the downstream float(c["chap"]) filters (--no-partials / --chapters)
            # crashed on "Oneshot" and the .aio_series.json writer recorded "4.0"
            # where --list-chapters emits "4" (perpetual "+N new"). Rewriting the
            # dict here fixes all three at the single chokepoint. C1 review finding.
            chapters_by_num[num_str].append({**ch, "chap": num_str})
        except (ValueError, TypeError):
            log_verbose(f"  Skipping chapter with invalid number: {num_str}")
            continue

    # 2. For each chapter number, select the best version
    selection_policy = _build_group_selection_policy(handler, chapters_by_num, args)
    best_chapters = []
    skipped_mtl_labels: List[str] = []
    sorted_chap_nums = sorted(chapters_by_num.keys(), key=float)
    for num in sorted_chap_nums:
        versions = chapters_by_num[num]
        best_version = handler.select_best_chapter_version(
            versions,
            args.group,
            args.mix_by_upvote,
            allow_group_fallback=not getattr(args, "no_group_fallback", False),
            log_debug_fn=log_debug,
            selection_policy=selection_policy,
        )
        if best_version:
            best_chapters.append(best_version)
        elif selection_policy.mtl == "exclude" and len(versions) > 0:
            # Distinguish an MTL-policy skip from a --no-group-fallback skip so
            # the count below can't over-report. A chapter is only attributed to
            # the MTL policy when every version was confirmed machine-translated.
            if all(
                _version_is_confirmed_mtl(handler, v) for v in versions
            ):
                skipped_mtl_labels.append(num)
    if skipped_mtl_labels:
        # Counted + aggregated, never a silent drop. Mirrors the
        # "N premium/locked chapter(s) skipped" notice below.
        preview = ", ".join(skipped_mtl_labels[:8])
        more = f" (+{len(skipped_mtl_labels) - 8} more)" if len(skipped_mtl_labels) > 8 else ""
        print(
            f"[i] {len(skipped_mtl_labels)} chapter(s) skipped — every available "
            f"version is machine-translated and --mtl exclude is set: "
            f"{preview}{more}. Use --mtl avoid to take them anyway."
        )

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
    # Skip-set for the UI update-check (Option 2, 2026-07-07). A consensus-armed
    # collapse DROPS source-only fragment-shaped decimals (mangafire's duplicate
    # 52.1 next to 52, this series' lone .3 == its integer counterpart) via Rule
    # 2/3b/6. But the Library update-check spawns `--list-chapters` with NO peer
    # data — discovery is skipped there (grep the list_chapters gate above
    # _discover_multi_source_alternatives), so its consensus-free collapse KEEPS
    # those labels, and the main.js diff (siteChapters − chapters_downloaded)
    # flags them as a perpetual "+N new" that a "Download Missing" click then
    # refetches as duplicates. Record exactly the labels THIS run dropped-under-
    # consensus so the UI can subtract them. free_labels ⊇ consensus_labels
    # always (consensus only ever removes more), so the difference is precisely
    # the consensus-gated drops — Rule 3a/5 sequential-split merges collapse
    # identically with or without consensus and never appear here. Empty unless
    # collapse is ON and consensus actually fired (single-source / lazy runs
    # contribute nothing; the UNION at the .aio_series.json write below preserves
    # an earlier eager run's set). Cross-file: UI-source/electron/main.js:
    # _checkSeriesUpdates (grep chapters_skipped_fragments).
    _skipped_fragment_labels: Set[str] = set()
    if collapse_splits_enabled and _multi_source_consensus_set:
        _consensus_labels = {g.label for g in groups}
        _free_labels = {
            g.label
            for g in group_chapters_for_download(
                chapters, collapse_splits=True, consensus_set=None,
            )
        }
        _skipped_fragment_labels = _free_labels - _consensus_labels
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
        # Guard float(c["chap"]) against a non-numeric group.label (collapse-
        # splits can relabel to "?"/"" — grep _chap_as_float). A non-numeric
        # label isn't a numeric ".5" partial, so keep it rather than crash.
        def _is_numeric_partial(c) -> bool:
            cf = _chap_as_float(c.get("chap"))
            return cf is not None and cf != int(cf)
        chapters = [c for c in chapters if not _is_numeric_partial(c)]
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
            # Guard float(c["chap"]) against a non-numeric group.label (grep
            # _chap_as_float): a numbered range can't match a non-numeric label,
            # so drop it rather than crash the run.
            def _wanted(c) -> bool:
                cf = _chap_as_float(c.get("chap"))
                return cf is not None and is_chapter_wanted(cf, args.chapters)
            chapters = [c for c in chapters if _wanted(c)]
            log_verbose(
                f"  --chapters '{args.chapters}': Filtered list down to {len(chapters)} chapters."
            )

    if not chapters:
        sys.exit("No chapters selected.")
    # Always print the final chapter count so the UI can show a progress bar.
    # This is parsed by the Electron app to determine the total for the
    # "Chapter X/Y" progress indicator (regex: /Selected \d+ chapters/).
    print(f"  Selected {len(chapters)} chapters.")
    _emit("chapters_selected", total=len(chapters))
    # --- End of Chapter Selection Logic ---

    # Ensure output folder exists (shared by chapter files, final book, and split parts)
    out_dir = getattr(args, "output_dir", DEFAULT_OUTPUT_DIR)
    os.makedirs(out_dir, exist_ok=True)

    if getattr(args, "save_params", False):
        _save_download_params(out_dir, args.comic_url, args, title)

    resume_mode = False
    params_path = os.path.join(main_tmp_dir, "run_params.json")
    current_params = get_resumable_params(args, p, width, aspect_ratio_str)
    # Persist the user-facing ROOT (captured pre-mutation; grep `_output_dir_root_for_resume`),
    # not the per-series folder args.output_dir / args.epub_dir hold by now. Without this,
    # the resumed run restores the per-series value and the allocate_series_output_dir call
    # at the top of this section nests one level deeper every resume (manga/Tekyuu →
    # manga/Tekyuu/Tekyuu → manga/Tekyuu/Tekyuu/Tekyuu…). Neither dest is in
    # _RESUME_GATING_DESTS, so gating_hash is unaffected by this override.
    current_params["output_dir"] = _output_dir_root_for_resume
    if "epub_dir" in current_params:
        current_params["epub_dir"] = _epub_dir_root_for_resume
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
        _run_params_obj: Dict[str, Any] = {
            "gating_hash": current_hash, "params": current_params,
        }
        # Fold in the multi-source resume cache discovered earlier this run.
        # EAGER discovery ran before this tmp dir existed, so
        # _persist_multi_source_cache no-op'd and its payload waited here; the
        # LAZY path writes the file itself later (grep _ms_resume_cache_payload).
        # Top-level key, NOT in `params`, so gating_hash is unaffected.
        if _ms_resume_cache_payload:
            _run_params_obj["multi_source_cache"] = _ms_resume_cache_payload
        with open(params_path, "w") as f:
            json.dump(_run_params_obj, f, indent=4)

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
            # AniList cover normalization (--metadata-source=anilist): the
            # enrichment step (sites/external_metadata.py:_apply_anilist_match)
            # overwrote comic_data["cover"] with the AniList cover and stashed
            # the site's own cover under `site_cover`. If the AniList CDN fetch
            # failed (dl_image → None), fall back to the site cover so an
            # enriched run is never worse than an un-enriched one.
            if original_cover_path is None:
                site_cover = comic_data.get("site_cover")
                if site_cover and site_cover != cover_url:
                    log_verbose(
                        "  AniList cover fetch failed; falling back to site cover"
                    )
                    original_cover_path = dl_image(
                        site_cover, main_tmp_dir, "cover_orig.jpg", scraper,
                        cleanup=not args.no_cleanup,
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
            # Reader-facing extension keys (flat, top-level). Komikku parses
            # details.json with Json{ignoreUnknownKeys=true} (git show
            # 1f17a20^:komikkuspec.md §6.1), so these are dropped by Komikku
            # and read by the user's own reader. The AniList rich tags /
            # ids / format / synonyms + source provenance only reach a reader
            # via this file — .aio_series.json is never read by one. Field
            # contract + name-collision rationale: _build_aio_reader_extras.
            # Provenance values mirror the .aio_series.json writer below
            # (grep '"site": handler.name').
            details_payload.update(
                _build_aio_reader_extras(
                    comic_data,
                    source_site=handler.name,
                    source_url=args.comic_url,
                    language=args.language,
                )
            )
            details_path = os.path.join(out_dir, "details.json")
            with open(details_path, "w", encoding="utf-8") as f:
                json.dump(details_payload, f, ensure_ascii=False, indent=2)
            log_verbose(
                f"  Komikku: wrote details.json (status={details_payload['status']}, "
                f"{len(details_payload['genre'])} genre tags, "
                f"{len(details_payload['anilist_tags'])} anilist tags)"
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

    # --- Delta run: coerce --keep-chapters so the run has somewhere to land ---
    #
    # A DELTA run is one whose selected chapters do not cover the combined
    # archive already on disk — `--chapters 51-53` against a `<Title>.cbz`
    # holding 1-50, which is exactly what the library's "download missing
    # chapters" flow emits (UI-source/src/lib/downloadArgs.js, and the Android
    # twin core/DownloadForm.kt). The end-of-run guard REFUSES to rebuild that
    # archive from the delta (grep _final_file_would_shrink) because doing so
    # truncated 50 chapters to 3. But refusing leaves the chapters just
    # downloaded with nowhere durable to go: without --keep-chapters they exist
    # only inside tmp_<hid>/, so the run spends the bandwidth and produces
    # nothing the user can open.
    #
    # Coerced HERE, in the engine, rather than in the two arg builders: only the
    # engine knows the guard will fire, and asking every caller to compensate
    # for a decision it cannot see is how those two builders drift apart. Same
    # pattern and same one-line notice as --komikku's coercion (grep
    # "[Komikku] Forcing").
    #
    # PLACED AFTER THE --split RESET ABOVE, not next to the chapter selection,
    # because that reset is what decides whether the series-wide archive gets
    # written at all: with missed-chapter retry on (the default) a --split run
    # falls back to building it, so a check upstream of here would read a stale
    # split state and skip the coercion for a run that does overwrite.
    #
    # Predicted from the SELECTED chapters, before any of them download, so it
    # can be wrong in one direction: a run that covers everything now but loses
    # a chapter to a failure later declines the rebuild without having coerced.
    # That case is benign — the archive it declined to overwrite is precisely
    # the one that still holds the chapter that failed.
    if (
        not getattr(args, "keep_chapters", False)
        and not getattr(args, "no_final_file", False)
        and args.format != "none"
        and split_size_bytes <= 0
        and split_chapter_count <= 0
    ):
        _delta_out_dir = getattr(args, "epub_dir", None) if args.format == "epub" else None
        _delta_final_path = os.path.join(
            _delta_out_dir or out_dir, f"{base_filename}.{args.format}"
        )
        if _final_file_would_shrink(
            [c.get("chap") for c in chapters],
            _load_series_meta(out_dir),
            final_file_exists=os.path.exists(_delta_final_path),
            fmt=args.format,
        ):
            args.keep_chapters = True
            print(
                f"[Delta] Forcing --keep-chapters: this run does not cover all of "
                f"{os.path.basename(_delta_final_path)}, which is being kept as is. "
                f"Per-chapter files are where this run's chapters will land."
            )

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
                # default=str is a belt-and-suspenders: _record_missed already
                # strips the non-serializable _aux* keys (grep _strip_aux_for_log),
                # but any future non-JSON value must NOT silently blow away the
                # whole missed log via the except:pass below. S5-1 review finding.
                json.dump(entries, f, ensure_ascii=False, indent=2, default=str)
        except Exception:
            pass

    def _strip_aux_for_log(d: Dict[str, Any]) -> Dict[str, Any]:
        # AssetSpec instances (_aux_assets) and raw audio bytes (_aux_members)
        # aren't JSON-serializable, and _aux_members can be MB of audio — storing
        # them in the missed log made json.dump raise TypeError, swallowed by
        # _save_missed's except:pass, so the WHOLE run's missed_chapters.json went
        # missing/stale (the end-of-run copy then shipped nothing). Drop every
        # _aux* key; recurse into _merged_parts which carry their own. S5-1.
        out = {k: v for k, v in d.items() if not k.startswith('_aux')}
        parts = out.get('_merged_parts')
        if isinstance(parts, list):
            out['_merged_parts'] = [
                _strip_aux_for_log(p) if isinstance(p, dict) else p for p in parts
            ]
        return out

    def _record_missed(ch: Dict[str, Any], grp_name: str, reason: str, err: str, *, insert_list_index: int, insert_chapter_index: int, insert_marker_index: int, insert_page_index: int, host: str = "", pages_ok: int = 0, pages_total: int = 0) -> None:
        entry = {
            'key': _chapter_key(ch),
            'ch': _strip_aux_for_log(ch),
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

    # Run-level flags: did ANY chapter this run carry audio / motion aux? Read
    # at end-of-run to OR the has_audio/has_motion booleans into details.json AND
    # gate the (Komikku-only) chapter_assets rebuild scan. Per-chapter detail now
    # lives INSIDE each CBZ's ComicInfo (<AioChapterResources>) — the rollup is
    # rebuilt from those CBZs (grep _scan_chapter_cbz_aux), so we no longer
    # accumulate a per-chapter map here. All-False for every normal site.
    aux_seen: Dict[str, bool] = {"audio": False, "motion": False}

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
            # resumed=True is the signal the ETA estimator needs: these ticks
            # cost no network time and would otherwise poison the moving
            # average. Mirrors downloader.js:applyChapterEta's skip rule.
            _emit("chapter_start", chapter=n, resumed=True)
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
                    chapter_content = [
                        {"type": "cbz_cache", "path": cached_cbz_path, "chap": n}
                    ]
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
            # 2026-07-03: the REAL (blocking) join now happens in
            # _process_chapter BEFORE the watchdog timer is armed, so queue
            # wait no longer burns the chapter's deadline; this call is a
            # no-op backstop for any future direct-impl caller.
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

            # Group credit + (when more than one version existed) the tier that
            # actually decided the pick. `_group_selection` is written by
            # base.select_best_chapter_version; grep it there for the tuple.
            _gsel = ch.get("_group_selection") or {}
            _why = _gsel.get("why")
            _avail = _gsel.get("available") or ""
            _sel_note = ""
            if _why and _avail and "," in _avail:
                _sel_note = f" — chosen on {_why} from: {_avail}"
            print(f"\nChapter {n} ({grp_name or 'No Group'}){_sel_note}")
            _emit("chapter_start", chapter=n, group=grp_name or None, resumed=False)
            _t0_imageurls = time.monotonic()
            # Phase 8 (2026-05-08): split-cluster collapse — when this chapter
            # was synthesized by group_chapters_for_download from multiple
            # parts (rule 5: X.1/X.2/X.3/X.4 with no integer X), `_merged_parts`
            # carries the original chapter dicts. Fetch each part's media
            # entries in order and concatenate, so downstream processing sees
            # ONE long chapter with all parts' pages stitched together. The
            # chap label was already replaced with group.label at synthesis
            # time, so tdir / output filename use the floor (e.g., "1") not
            # any individual part's label.
            # Reset aux-asset state before (re)fetching so a retry /
            # force_redownload of the same ch dict can't duplicate sidecars.
            # Non-merged: get_chapter_images(ch) re-assigns ch["_aux_assets"]
            # fresh (or leaves it absent for the ~all normal handlers). Merged:
            # each part's aux is merged up below, so ch must start clean.
            ch.pop("_aux_assets", None)
            ch.pop("_aux_records", None)
            ch.pop("_aux_members", None)
            # Same reason: the end-of-run retry re-feeds the SAME dict, so a
            # stale count from the previous attempt would mislabel a fresh
            # miss. Grep decode_dropped_pages.
            ch.pop("_decode_dropped", None)
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
                    # Merge this part's aux assets up into the synthesized
                    # parent chapter — the sidecar writer reads ch["_aux_assets"]
                    # once for the whole collapsed chapter (grep AssetSpec).
                    part_aux = part.get("_aux_assets")
                    if part_aux:
                        ch.setdefault("_aux_assets", []).extend(part_aux)
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
            _timing["imageurls"] += time.monotonic() - _t0_imageurls

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
                        # CL-4: ext-determination + collapse-safe naming now live
                        # in the shared _binary_image_page_name so this and the
                        # prefetch twin (_run_image_prefetch_job) can't drift —
                        # the per-chapter-name collision they both guard against
                        # (MangaDex "0001.png" overwriting across merged parts,
                        # racing the modernize pool → CBZ FileNotFoundError) is
                        # documented in the helper. bench/collapseSplitsModernize.md.
                        filename = _binary_image_page_name(entry, blob, n, page_counter)
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
            # Host attribution, shared by three consumers: the AIMD clean-
            # chapter credit (accept branch below), the failure diagnostic +
            # skip reason (elif branch), and the cap-aware prefetch chain
            # clamp (grep 'chain depth'). download_tasks is empty for
            # handlers that return all binary_image entries (e.g. MangaDex's
            # resilient pipeline) AND for prefetch-adopted chapters (every
            # page arrives as immediate_images) — fall back to the first
            # media_entries URL, then the chapter's source URL, so every
            # consumer sees a concrete host instead of ''.
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
            # A COMPLETE chapter is never discarded (2026-07-03). The watchdog
            # deadline and the host-poison counter exist to stop WAITING on
            # failing downloads; once every page has landed there is nothing
            # left to protect, and wiping finished work turns a slow-but-
            # successful chapter into a spurious multi-source rescue that can
            # REPLACE it with an inferior alt copy. Real case
            # (bench/unordinaryLogs.md): chs 130/135/143 finished 141/141,
            # 121/121, 124/124 via the background prefetch, but the deadline
            # had expired while the main thread was blocked in
            # _consume_image_prefetch, so the old `or deadline_hit` gate wiped
            # them and the atsumaru rescues delivered 119/107/122 pages —
            # strictly worse archives. Deadline/poison still fail the chapter
            # when pages ARE missing (the elif below is the pre-existing gate).
            if pages_total > 0 and not incomplete:
                if deadline_hit or poisoned_hosts:
                    why = (
                        "time budget elapsed"
                        if deadline_hit
                        else f"host failures on {poisoned_hosts[0]}"
                    )
                    print(
                        f"  [i] Chapter {n} complete ({pages_ok}/{pages_total} "
                        f"pages) — accepting despite {why}."
                    )
                if deadline_hit and _CHAPTER_CANCEL is not None:
                    # Stand the watchdog down for the rest of this chapter's
                    # pipeline (recompress/modernize/aux/CBZ). The one-shot
                    # timer has already fired; leaving the Event set would
                    # make _respect_rate_limit skip politeness sleeps and any
                    # remaining cancel-aware helper fast-fail for no reason.
                    _CHAPTER_CANCEL.clear()
                # AIMD additive-increase: a gate-accepted chapter is the
                # "clean" signal that earns a capped host its concurrency
                # back (grep _record_host_clean_chapter). Credited even on
                # accept-despite-deadline/poison — those failure events
                # already reset the streak themselves; the credit just marks
                # "a full chapter's bytes landed from this host".
                _record_host_clean_chapter(_resolve_host_blame())
            elif incomplete or deadline_hit or poisoned_hosts:
                # Ghost-chapter check FIRST. The detector uses pages_ok=0 +
                # uniform signatures across all recorded failures, which is
                # disjoint from "any pages succeeded" — so a chapter that
                # had even one successful download will never classify here.
                # primary_only feeds the threshold knob: when the alignment
                # data shows no non-primary source for this chapter, drop
                # the pages_total floor from 5 to 3 (independent corroboration
                # the chapter is fake). Grep anchors:
                # _is_ghost_chapter_signature (this file), and the alignment
                # dict writers under _discover_multi_source_alternatives /
                # take_latest_multi_source_state. Under --multi-source-lazy
                # the dict is empty until the first failure fires discovery,
                # so this classification runs with primary_only=None (same
                # as multi-source off) for that first chapter; the strict
                # wrapper re-derives primary_only afterwards for the
                # ChapterGhostError it raises.
                primary_only_for_ghost: Optional[bool] = None
                if _multi_source_alternatives:
                    # CL-1: use the canonical extractor the aligner keyed the
                    # alternatives_by_chap_num dict with (grep _extract_chapter_num)
                    # so the float lookup can't drift from an inline regex reimpl.
                    _cf = _extract_chapter_num(ch.get("chap"))
                    if _cf is not None:
                        primary_only_for_ghost = (
                            not _multi_source_alternatives.get(_cf)
                        )
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
            # 2026-07-03: deliberately fires BEFORE the sidecar-aux block
            # below — a slow BGM/motion fetch (network) then overlaps with
            # the next chapters' background image downloads instead of
            # stalling them (the aux insertion for PR #56 had landed above
            # this block and pushed the chain later by the aux fetch
            # duration).
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
            # Cap-aware prefetch pressure (2026-07-03): while this chapter's
            # image host is under a reduced concurrency cap, push at most ONE
            # chapter ahead. Depth-N pushes against a struggling CDN grow the
            # prefetch queue backlog (slow jobs, busy workers) — exactly what
            # produced the 90-300s consume waits behind the unordinaryLogs.md
            # time-budget failures. Depth 1 keeps the pipeline overlap alive
            # without stacking pressure; the AIMD recovery (grep
            # _record_host_clean_chapter) lifts the cap — and with it this
            # clamp — once the host runs clean again.
            if depth > 1 and _host_concurrency_capped(_resolve_host_blame()):
                log_verbose(
                    f"  [Img Prefetch] host under backoff cap — "
                    f"chain depth {depth} -> 1"
                )
                depth = 1
            if (
                effective_prefetch_workers > 0
                and depth > 0
                and not force_redownload
                and not is_alt_source
            ):
                # Prefer the windowed upcoming_chapters list, falling
                # back to [next_chapter]
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

            # Sidecar auxiliary assets (audio / motion-toon manifest / layer
            # map) — faithful-archival feature. Materialize the handler's
            # _aux_assets into in-memory CBZ members (audio bytes + motion
            # manifest) + a ComicInfo record; the members are EMBEDDED into the
            # chapter CBZ at build time under _aio/ (grep 'cached_cbz_path'),
            # never a loose _assets/ file. CBZ-only: EPUB/PDF can't hold
            # per-chapter sidecars, so aux is skipped there (logged once). Inside
            # `if process_this_chapter:` so a resume-collect skips it — the prior
            # run's CBZ already carries the aux. Handler-scoped: an alt source
            # that set no _aux_assets is inert. Opt-out: --no-sidecar-assets. See
            # _materialize_chapter_aux + sites.base.AssetSpec; the record feeds
            # the per-chapter ComicInfo (_aux_records) + the series has_audio/
            # has_motion flags (aux_seen, patched into details.json at run end).
            if (
                ch.get("_aux_assets")
                and not getattr(args, "no_sidecar_assets", False)
            ):
                if args.format == "cbz":
                    try:
                        _aux_rec, _aux_members = _materialize_chapter_aux(
                            ch["_aux_assets"], scraper, make_request
                        )
                    except Exception as _aux_exc:
                        # Never let an aux-asset failure abort the chapter — the
                        # images are the deliverable; sidecars are a bonus.
                        log_verbose(
                            f"  [assets] aux materialize failed for Ch {n}: "
                            f"{type(_aux_exc).__name__}: {_aux_exc}"
                        )
                        _aux_rec, _aux_members = None, []
                    if _aux_rec:
                        ch["_aux_records"] = _aux_rec
                        ch["_aux_members"] = _aux_members
                        if (
                            _aux_rec.get("audio")
                            or _aux_rec.get("audio_refs")
                            or _aux_rec.get("has_bgm")
                        ):
                            aux_seen["audio"] = True
                        if _aux_rec.get("motion_manifest") or _aux_rec.get("layers"):
                            aux_seen["motion"] = True
                        log_verbose(
                            f"  [assets] Ch {n}: {len(_aux_rec.get('audio') or [])} "
                            f"audio, motion={bool(_aux_rec.get('motion_manifest'))}, "
                            f"{len(_aux_rec.get('audio_refs') or [])} ref(s)"
                        )
                else:
                    _warn_aux_needs_cbz_once(args.format)

            _t0_proc = time.monotonic()
            os.makedirs(processed_tdir, exist_ok=True)

            chapter_content = []

            processed_page_images: List[str] = []
            # For PDF format, track which images were NOT modified during
            # processing so _build_images_pdf can embed the original
            # download bytes (smaller and better quality than re-encoding).
            _pdf_source_paths: Optional[List[Optional[str]]] = None
            # Downloaded pages the PIL decode below could not open. Filled by the
            # three decode paths, reconciled once after them — grep
            # decode_dropped_pages. Empty on the CBZ fast path and under
            # --no-processing, neither of which decodes anything.
            _decode_dropped: List[str] = []

            if raw_image_paths:
                # Phase B (2026-05-07): CBZ fast-path. When the user is on
                # default --scaling 100 with no width/aspect/quality override,
                # we put the original wire bytes straight into the archive —
                # no PIL decode, no recombine, no JPEG re-encode. The archive
                # layer (build_cbz / build_cbz_from_content) preserves
                # per-file extensions on the arcname, so .webp downloads stay
                # .webp, .png stay .png, etc. Phase A made raw_image_paths
                # land with correct extensions which is what makes this work.
                # Computed BEFORE the --modernize block so modernize gates on the
                # exact same condition (see below).
                cbz_fast_path = (
                    args.format == "cbz"
                    and not args.no_processing
                    and not getattr(args, "no_cbz_preserve_originals", False)
                    and scale_factor == 1.0
                    and not getattr(args, "_user_set_width", False)
                    and not getattr(args, "_user_set_aspect_ratio", False)
                    and not getattr(args, "_user_set_quality", False)
                )
                # --modernize (opt-in, CBZ-only): content-aware JXL/AVIF
                # transcode of the downloaded pages, in place, BEFORE the CBZ
                # fast-path consumes raw_image_paths — so the new .jxl/.avif
                # bytes flow straight into build_cbz with correct extensions and
                # never reach the slow save_final_images path (which would
                # re-encode them to PNG). Gated on cbz_fast_path itself, NOT just
                # format==cbz: the parse-time hard errors (grep '--modernize
                # compatibility checks') are SKIPPED on a --restore-parameters
                # resume (the bare resume CLI omits --modernize, so that block
                # never runs, then run_params.json restores modernize=True). A
                # resume that re-adds a fast-path-breaking override
                # (--no-processing / --width / --scaling) would otherwise smuggle
                # .jxl/.avif into the slow path; riding cbz_fast_path closes that
                # hole. Runs after the webtoon-recompress block above (so .webp
                # pages are skipped) and after the prefetch kickoff (CPU encode
                # overlaps the next downloads). See
                # recompress_chapter_images_modern().
                if getattr(args, "modernize", False) and cbz_fast_path:
                    _t0_modernize = time.monotonic()
                    log_verbose(
                        f"  [modernize] Transcoding {len(raw_image_paths)} pages "
                        f"({args.modernize_format})..."
                    )
                    raw_image_paths = recompress_chapter_images_modern(
                        raw_image_paths,
                        policy=args.modernize_format,
                        gray_quality=args.modernize_distance,
                        color_quality=args.modernize_quality,
                        min_saving=args.modernize_min_saving,
                        effort=args.modernize_effort,
                        speed=args.modernize_avif_speed,
                    )
                    log_verbose(
                        f"  [modernize] Done in "
                        f"{time.monotonic() - _t0_modernize:.1f}s."
                    )
                elif getattr(args, "modernize", False):
                    # Requested but the fast-path is disabled — almost always a
                    # resume with a fast-path-breaking override. Skip (don't feed
                    # .jxl/.avif into the re-encode path) and say so, rather than
                    # hard-erroring and aborting the whole resume.
                    log_verbose(
                        "  [modernize] skipped: effective settings disable the "
                        "CBZ byte-passthrough fast-path (e.g. a "
                        "--restore-parameters resume adding --no-processing, "
                        "--width, or --scaling). Pages left as-is."
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
                    # Flatten guard (warn-only leg): this branch decodes each
                    # page to a single PIL frame (process_chapter_images /
                    # save_final_images), so any animated GIF/APNG loses its
                    # animation here. Tell the user once. The fast-path +
                    # recompress/modernize legs above already preserve it.
                    _warn_animated_flatten_once(raw_image_paths, args.format)
                    if args.format == "cbz" or (
                        args.format == "epub" and not text_blocks
                    ):
                        pages_in_memory = process_chapter_images(
                            raw_image_paths,
                            width,
                            recombine_target_height,
                            dropped=_decode_dropped,
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
                                _decode_dropped.append(path)
                        log_verbose(f"  Loaded {len(pages_in_memory)} pages in memory.")
                    else:
                        pages_in_memory = resize_chapter_images(
                            raw_image_paths, width, dropped=_decode_dropped
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

            # ── Post-decode page reconciliation (reason=decode_dropped_pages) ──
            # pages_ok / pages_total are measured at DOWNLOAD time (grep
            # `pages_total = len(download_tasks)`) and the zero-tolerance
            # completeness gate runs there too — but the PIL decode happens HERE,
            # hundreds of lines later. A page that downloads fine and then fails
            # to decode is dropped by the decode helpers (grep "Skipping corrupted
            # image"), so the chapter logged N/N and shipped SHORT. Nothing
            # downstream could catch it either: on the recombine path the output
            # page count is 1:N by design, so a count comparison proves nothing —
            # which is why the signal is the list of DROPPED SOURCES.
            #
            # Live cause: Chaquopy's Pillow ships no WebP decoder, so a
            # mixed-format chapter loses exactly its WebP pages
            # (android/PARITY.md §2 D4). Pre-fix, an all-WebP chapter was caught
            # (empty content → `empty_content`) while a mixed one silently shipped
            # short — the loud/silent split this closes.
            #
            # RAISED, not returned. A bare `return None, …` returns NORMALLY, and
            # _process_chapter_strict only catches ChapterSkippedError — so the
            # alt-source loop, the lazy-discovery trigger and the inline retry
            # were all skipped and this landed in the main loop's weak
            # `if not chapter_content:` branch. That threw away the one rescue
            # that actually fixes this: the live cause is a missing WebP decoder,
            # so an ALTERNATIVE SOURCE serving JPEG succeeds where no amount of
            # retrying the primary can. Multi-source was off the table for
            # precisely the chapters it was built for.
            #
            # reason='decode_dropped_pages' is in _PERMANENT_SKIP_REASONS, which
            # places it exactly right in _process_chapter_strict's ladder: it
            # runs AFTER the alt-source loop (so alternates are tried) but
            # BEFORE the inline retry (skipped — re-downloading the same bytes
            # cannot make a codec appear, and those two long waits were pure
            # cost), and it records-missed-and-continues rather than aborting the
            # run, since the failure is environmental rather than a host fault.
            #
            # The count still rides on `ch` because the main loop's missed-reason
            # branch reads it for the message; grep _decode_dropped there.
            #
            # Unreachable on the CBZ byte-passthrough fast path and under
            # --no-processing: neither decodes anything, which is what keeps
            # Android's stock configuration untouched.
            if _decode_dropped:
                print(
                    f"  [!] Chapter {n}: {len(_decode_dropped)} of "
                    f"{len(raw_image_paths)} downloaded page(s) could not be "
                    f"decoded, so the output would be silently short. Trying an "
                    f"alternate source if one is available, then recording the "
                    f"chapter as missed rather than shipping it incomplete."
                )
                ch["_decode_dropped"] = len(_decode_dropped)
                rm_tree(tdir)
                raise ChapterSkippedError(
                    reason="decode_dropped_pages",
                    # No host blame ON PURPOSE: every page DOWNLOADED fine and
                    # the failure is local (no codec). Naming a host here would
                    # feed a blameless CDN into the diagnostics and read as a
                    # server fault in the missed log.
                    host="",
                    pages_ok=0,
                    pages_total=len(raw_image_paths),
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
                        # Aux sidecars (audio / motion manifest) ride INSIDE the
                        # CBZ under the reserved _aio/ prefix — renumber-exempt
                        # (build_cbz_from_content preserves _aio/ members instead
                        # of turning them into bogus pages). Empty for every
                        # normal site. Grep _materialize_chapter_aux.
                        for _arc, _data in ch.get("_aux_members") or []:
                            zf.writestr(_arc, _data)
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
                                aux_records=ch.get("_aux_records"),
                                missing_pages=ch.get("_missing_pages"),
                            )
                            zf.writestr(
                                "ComicInfo.xml",
                                per_chap_xml,
                                compress_type=zipfile.ZIP_DEFLATED,
                            )
                    chapter_content = [
                        {"type": "cbz_cache", "path": cached_cbz_path, "chap": n}
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
                    _emit("chapter_saved", chapter=n, format="cbz", path=ch_out_path)
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
                            aux_records=ch.get("_aux_records"),
                            missing_pages=ch.get("_missing_pages"),
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
                            extra_members=ch.get("_aux_members"),
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
                _emit("chapter_saved", chapter=n, format="pdf", path=ch_out_path)

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
        # Join any in-flight background prefetch for this chapter BEFORE
        # arming the watchdog (2026-07-03). The join can block up to 300s on
        # a backlogged prefetch queue (grep _consume_image_prefetch) — time
        # spent on the pool's other jobs, not on this chapter's own work.
        # When the join ran inside the deadline window (the impl's old call
        # site), a slow prefetch consumed the whole budget before the chapter
        # even started: unordinaryLogs.md ch 141 opened with "0/114
        # (reason=time_budget)" in the same second, and ch 143's fully
        # prefetched 124/124 pages were wiped at the completeness gate. The
        # impl's own _consume_image_prefetch call stays as a no-op backstop
        # (this join pops the done-event).
        _consume_image_prefetch(ch.get("chap"))
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
          2. Try alternative sources first (cheap — no sleep) — EXCEPT
             aux-bearing chapters (webtoons BGM/motion, tapas BGM): those are
             never alt-rescued because alt sites lack the audio/motion content
             (2026-07-03 directive; grep _chapter_carries_aux). They go
             straight to the inline retry on the primary.
          3. If all sources fail: long inline retry on primary (CDN recovery).
          4. If inline retries don't recover: stop the run with a clear error.

        Backoff schedule: --inline-chapter-backoff seconds, doubling each
        retry. With defaults (base=30s, retries=2): waits 30s, then 60s.

        Cross-file: cooperates with _process_chapter (watchdog wrapper) which
        bounds each individual attempt's wall-clock time. Multi-source state
        comes from `_multi_source_alternatives`, populated in main() by
        --search --multi-source --auto-pick, by the eager direct-URL
        discovery, or — under --multi-source-lazy — by the first-failure
        hook right below (grep _ms_lazy_pending).
        """
        nonlocal handler, scraper, context, comic_data, _ms_lazy_pending
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

        # Faithful-archival veto (2026-07-03): a chapter that carries sidecar
        # aux content on the primary (webtoons BGM / motion-toon, tapas BGM —
        # grep _chapter_carries_aux) is NEVER rescued from an alternative
        # source. Alt sites only mirror the flattened pages, so a rescue
        # silently drops the audio/motion. Recovery for these chapters is
        # inline-retry on the primary only (below). The veto also skips the
        # lazy-discovery trigger: no point paying the one-time cross-site
        # search for a chapter that won't consult the result —
        # _ms_lazy_pending stays armed for the next aux-free failure.
        # Scoped to runs where the sidecar would actually be embedded: aux is
        # CBZ-only and --no-sidecar-assets opts out entirely (mirrors the
        # gating at _materialize_chapter_aux's call site) — when aux wouldn't
        # land in the archive anyway, a rescue loses nothing and stays allowed.
        aux_veto = (
            args.format == "cbz"
            and not getattr(args, "no_sidecar_assets", False)
            and _chapter_carries_aux(ch)
        )
        if aux_veto and (_ms_lazy_pending or _multi_source_alternatives):
            print(
                f"  [Multi-source] Chapter {n_for_log} carries sidecar assets "
                f"(BGM/motion) that alt sources can't provide; skipping "
                f"alt-source rescue, will inline-retry on "
                f"{primary_state[0].name}."
            )

        # --multi-source-lazy: the upfront cross-site discovery was deferred
        # at startup; the first failed chapter pays for it here, once, so the
        # alts lookup below (and every later chapter, including the missed-
        # retry pass which also routes through this wrapper) sees the result.
        # Disarm BEFORE running so a discovery that fails or finds nothing
        # isn't re-attempted on every subsequent failure. Runs outside the
        # per-attempt watchdog (_process_chapter cancelled its timer before
        # this except arm), so the chapter deadline can't kill the discovery.
        if _ms_lazy_pending and not aux_veto:
            _ms_lazy_pending = False
            print(
                f"  [Multi-source] Chapter {n_for_log} failed on "
                f"{primary_state[0].name}; running deferred alternatives "
                f"discovery now (--multi-source-lazy)..."
            )
            _discover_multi_source_alternatives()

        # Phase 4b: try alternative sources before the inline-retry sleep.
        # Look up alternatives by chapter number (float). Numeric extraction
        # mirrors chapter_merger._extract_chapter_num.
        alts: List[Dict[str, Any]] = []
        if not aux_veto:
            # CL-1: canonical extractor (matches the aligner's dict keys) — grep
            # _extract_chapter_num.
            _cf = _extract_chapter_num(ch.get("chap"))
            if _cf is not None:
                alts = list(_multi_source_alternatives.get(_cf, []))

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
                    # "filled/rescued" line for the two reasons where the
                    # in-chapter log didn't already make the rescue obvious:
                    #   - locked: a premium tapas placeholder the primary can
                    #     NEVER serve — this fill IS the feature the user asked
                    #     for ("paid chapter → next highest rated site"), so
                    #     name it plainly.
                    #   - ghost: the in-chapter log claimed "every failure had
                    #     identical signature"; close the loop visually.
                    # For other reasons (host_poison / time_budget / incomplete)
                    # the existing "[Multi-source] -> X" line + the per-chapter
                    # "CBZ saved" already make the rescue obvious.
                    multi_source_rescues.append({
                        "chap": n_for_log,
                        "alt_site": alt_handler.name,
                        "primary_site": primary_state[0].name,
                        "primary_reason": primary_err.reason,
                    })
                    if primary_err.reason == "locked":
                        print(
                            f"    [Multi-source] ✓ Chapter {n_for_log} "
                            f"(premium/locked on {primary_state[0].name}) "
                            f"filled from {alt_handler.name}"
                        )
                    elif primary_err.reason == "ghost_chapter":
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
                # CL-1: canonical extractor (matches the aligner's dict keys) —
                # grep _extract_chapter_num.
                _cf = _extract_chapter_num(ch.get("chap"))
                if _cf is not None:
                    primary_only_for_ghost = (
                        not _multi_source_alternatives.get(_cf)
                    )
            raise ChapterGhostError(
                chap=n_for_log,
                host=primary_err.host,
                pages_total=primary_err.pages_total,
                primary_only=primary_only_for_ghost,
            ) from primary_err

        # Deterministic per-chapter gate (mature/age/login interstitial): no
        # inline retry or alt source clears it, and for an aux chapter the veto
        # above already skipped alt-rescue — so the OLD path exhausted retries
        # and then ABORTED the whole run on a single login-gated tapas BGM
        # episode. Skip + continue instead (the main loop records it missed),
        # WITHOUT the ghost path's consecutive-host-block escalation (a mature
        # series legitimately has many consecutive gated episodes). Grep
        # _PERMANENT_SKIP_REASONS / ChapterPermanentSkipError.
        if primary_err.reason in _PERMANENT_SKIP_REASONS:
            raise ChapterPermanentSkipError(
                chap=n_for_log,
                reason=primary_err.reason,
                host=primary_err.host,
                pages_total=primary_err.pages_total,
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
            cancelled_during_wait = False
            while waited < wait:
                # The 2s chunking exists so this wait CAN be interrupted; a run
                # cancel has to actually do so. Without this check a user who
                # hits Cancel while a CDN is being retried waits out the full
                # backoff (up to 60s on the last attempt, and the retry then
                # redownloads the whole chapter anyway) — on Android that reads
                # as "Cancel is broken". Verified on-device 2026-08-08.
                if run_cancelled():
                    cancelled_during_wait = True
                    break
                chunk = min(2.0, wait - waited)
                time.sleep(chunk)
                waited += chunk
            if cancelled_during_wait:
                # Give up retrying and report the chapter as skipped. The caller
                # records it and the main loop breaks, so everything already on
                # disk stays resumable.
                raise last_err
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
    _timing = {"imageurls": 0.0, "download": 0.0, "processing": 0.0}
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

    # Chapters this run actually REACHED, as normalized labels.
    #
    # The .aio_series.json writer used to derive "what did we download" by
    # subtracting the recorded misses from the SELECTED list (`chapters`). That
    # is only true of a run that ran to completion. The cancel checkpoint below
    # BREAKs, and the abort path exits the loop too — neither records a miss for
    # the chapters it never reached, so every un-attempted chapter was being
    # written into `chapters_downloaded` as downloaded. The library update-check
    # then saw nothing missing, which is the self-concealing half of the
    # archive-overwrite hazard (android/PARITY.md D1): cancel a `--chapters
    # 51-53` update after 51 and the metadata claimed 52 and 53, which exist
    # nowhere on disk and would never be offered for repair.
    #
    # Fixed at the source rather than by the caller compensating: for any run
    # that finishes the loop this set EQUALS the selection, so complete
    # downloads, split runs, komikku, --format none and resume are all
    # byte-identical. Only a run that stopped early differs, and only in the
    # direction of claiming less. Three doors close at once — the cancel break,
    # the `aborted_remaining` exit, and a PDF/EPUB run cancelled before any
    # chapter finished (that one never even enters the final-file gate, so the
    # gate's own `final_build_stranded` guard could not have caught it).
    attempted_chapter_labels: Set[str] = set()

    for ch_idx, ch in enumerate(chapters):
        # Run-level cancel checkpoint. BREAK, don't raise: everything after this
        # loop (details.json asset patch, .aio_series.json write, temp-dir
        # retention, the timing/missed summaries) is what makes a partial run
        # resumable, and an exception here would skip all of it. The chapters
        # already on disk stay on disk. Grep request_run_cancel.
        if run_cancelled():
            print(f"\n[!] Cancelled — stopping before chapter {ch.get('chap')}.")
            break
        # Recorded AFTER the cancel check, so the chapter this run stopped
        # before is not claimed. Recorded BEFORE any work, so a chapter that
        # fails still counts as attempted and is removed by the missed-entry
        # subtraction instead — the two mechanisms stay disjoint.
        attempted_chapter_labels.add(_chap_label_str(ch.get("chap")))
        grp_name = handler.get_group_name(ch)
        insert_list_index = len(current_book_content)
        insert_chapter_index = len(current_book_chapters)
        insert_marker_index = len(current_epub_markers)
        insert_page_index = _running_page_count if args.format == 'epub' else 0
        # Look ahead so _process_chapter_impl can image-prefetch upcoming
        # chapters while downloading images for this one. next_ch retained
        # for the inter-chapter image prefetch (Phase G7) which is
        # single-chapter.
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
        except ChapterPermanentSkipError as cpse:
            # Deterministic per-chapter gate — record missed and continue, with
            # NO consecutive-host-block escalation (unlike the ghost soft-skip
            # above). See ChapterPermanentSkipError + _PERMANENT_SKIP_REASONS;
            # producers are sites/tapas.py's mature_login_required and "locked",
            # sites/comix.py's comix_pages_stalled, and this file's
            # decode_dropped_pages (grep _decode_dropped).
            # By the time we're here the alt-rescue loop already ran and failed
            # (or multi-source was off), so a "locked" chapter means no alt site
            # could supply it either. Fixes the old abort-on-one-gated-episode.
            if cpse.reason == "locked":
                _gate_note = (
                    "premium/locked episode — no alternative source could "
                    "supply it (enable/broaden --multi-source to fill it)"
                )
                _gate_detail = (
                    "deterministic per-chapter gate; no retry/alt source can clear it"
                )
            elif cpse.reason == "decode_dropped_pages":
                # NOT a gate, and not the host's fault. Naming the cause matters
                # here more than anywhere else in this handler: the remedy is a
                # local build/flag change, and the previous message ("content is
                # gated (needs a site login we don't have)") pointed at the one
                # thing that is definitely not wrong.
                _gate_note = (
                    "pages downloaded but could not be DECODED by this build's "
                    "Pillow (typically a WebP source with no WebP codec) — no "
                    "alternative source could supply a readable format either. "
                    "Fixes: download as CBZ without --quality/--width/--scaling "
                    "(that path never decodes), or use a build whose Pillow has "
                    "the codec"
                )
                _gate_detail = (
                    "downloaded pages failed to decode; retrying the same source "
                    "cannot help, alt sources were tried"
                )
            else:
                _gate_note = "content is gated (needs a site login we don't have)"
                _gate_detail = (
                    "deterministic per-chapter gate; no retry/alt source can clear it"
                )
            print(
                f"\n[!] Chapter {cpse.chap}: {cpse.reason}"
                f"{f' on {cpse.host}' if cpse.host else ''} — {_gate_note}. "
                f"Recorded as missed; the run continues."
            )
            _record_missed(
                ch, grp_name, cpse.reason,
                _gate_detail,
                insert_list_index=insert_list_index,
                insert_chapter_index=insert_chapter_index,
                insert_marker_index=insert_marker_index,
                insert_page_index=insert_page_index,
                host=cpse.host,
                pages_ok=0,
                pages_total=cpse.pages_total,
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
            # Two distinct causes land here, and the reason string is the only
            # place they can be told apart in the missed report:
            #   empty_content        — nothing downloadable at all;
            #   decode_dropped_pages — pages DID download, then failed to decode.
            #
            # The decode branch is a BACKSTOP, not the live path. Decode drops
            # now RAISE ChapterSkippedError('decode_dropped_pages') so they reach
            # the alt-source rescue (grep _decode_dropped), and a chapter that
            # exhausts that exits via ChapterPermanentSkipError above — which
            # `continue`s before reaching here. `_decode_dropped` is also popped
            # before every fetch, so an alt source's own empty return cannot
            # carry the primary's flag in. Kept because mislabelling a decode
            # failure as "No downloadable content" is exactly the diagnosis this
            # detector exists to prevent, and the cost is four lines.
            if _dropped_pages:
                _miss_reason = 'decode_dropped_pages'
                _miss_error = (
                    f'{_dropped_pages} downloaded page(s) failed to decode; '
                    f'chapter withheld rather than shipped short'
                )
            else:
                _miss_reason = 'empty_content'
                _miss_error = 'No downloadable content'
            _record_missed(ch, grp_name, _miss_reason, _miss_error, insert_list_index=insert_list_index, insert_chapter_index=insert_chapter_index, insert_marker_index=insert_marker_index, insert_page_index=insert_page_index)
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
        _emit("missed", count=len(missed_entries))
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
            _emit("recovered", chapter=n)

        missed_entries = remaining
        _save_missed(missed_entries)
        if missed_entries:
            print(f"[!] Still missed {len(missed_entries)} chapter(s). A log was saved to: {missed_log_path}")
            _emit("still_missed", count=len(missed_entries), log_path=missed_log_path)
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

    # Read the on-disk series metadata ONCE, here, before anything below can
    # rewrite it: the combined-archive overwrite guard needs to know what the
    # existing archive covers, and the .aio_series.json writer further down needs
    # the same dict for its chapters_downloaded union. Grep _load_series_meta.
    existing_series_meta = _load_series_meta(out_dir)

    # Set to the labels actually written into the combined archive, and ONLY when
    # a build really happened — it becomes .aio_series.json's `final_file_chapters`
    # below. Stays None for --no-final-file / --format none / komikku / split /
    # skipped runs, in which case the previous value is carried forward untouched
    # rather than replaced with a lie.
    final_file_built_labels: Optional[List[str]] = None

    # True when the overwrite guard declined the build AND --keep-chapters is off,
    # i.e. this run's chapters exist ONLY as the per-chapter caches inside
    # tmp_<hid>/. Two consequences below, and they are the other half of D1 (the
    # metadata concealment): the tmp dir is KEPT rather than wiped, and this run's
    # labels are withheld from the chapters_downloaded union — claiming chapters
    # we are about to delete is exactly how the library update-check stopped
    # noticing the damage. Only reachable from the guard branch, so komikku /
    # --no-final-file / --format none / split can never set it.
    final_build_stranded = False

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
            # Two guards stand between "this run produced content" and the
            # TRUNCATING write of the series-wide archive. Both live in this
            # branch only, so --no-final-file / --format none / split mode keep
            # their prior behaviour byte for byte. See the block comment above
            # _final_file_would_shrink for what they protect against.
            active_out_dir = getattr(args, "epub_dir", None) if args.format == "epub" else None
            final_path = os.path.join(active_out_dir or out_dir, f"{base_filename}.{args.format}")
            run_chapter_labels = [c.get("chap") for c in current_book_chapters]
            if run_cancelled():
                # A cancelled run holds a PREFIX of the series at best, so it can
                # never be the authority on the combined archive. tmp_<hid>/ is
                # deliberately kept for exactly this case (see the cleanup branch
                # at the end of main()) and the resume re-collects every finished
                # chapter before rebuilding.
                final_skip: Optional[Dict[str, Any]] = {
                    "reason": "cancelled",
                    "run_chapters": len(run_chapter_labels),
                    "existing_chapters": len(
                        _final_file_recorded_coverage(
                            existing_series_meta, args.format
                        )
                    ),
                    "dropped_chapters": 0,
                    "sample": "",
                }
            else:
                final_skip = _final_file_would_shrink(
                    run_chapter_labels,
                    existing_series_meta,
                    final_file_exists=os.path.exists(final_path),
                    fmt=args.format,
                )

            if final_skip:
                _skip_name = os.path.basename(final_path)
                if final_skip["reason"] == "cancelled":
                    print(
                        f"\n[!] Cancelled — not rebuilding {_skip_name} from this "
                        f"partial run ({final_skip['run_chapters']} chapter(s) "
                        f"assembled). Temporary files are kept; resume to finish "
                        f"the series and rebuild it."
                    )
                elif final_skip["reason"] == "no_chapters":
                    print(
                        f"\n[!] No chapters were assembled this run — keeping the "
                        f"existing {_skip_name} untouched."
                    )
                else:
                    print(
                        f"\n[!] Keeping the existing "
                        f"{final_skip['existing_chapters']}-chapter {_skip_name} — "
                        f"this run covers only {final_skip['run_chapters']} "
                        f"chapter(s), so rebuilding would drop "
                        f"{final_skip['dropped_chapters']}: "
                        f"{final_skip['sample']}."
                    )
                    # NOT --build-final-file for cbz/epub: that mode globs *.pdf
                    # and merges per-chapter PDFs only (grep
                    # build_final_pdf_from_chapter_folder). Re-running the URL with
                    # the full range is the only recombine path for the others.
                    print(
                        "    Re-run this URL with --chapters all to rebuild the "
                        "combined file from every chapter."
                    )
                    if args.format == "pdf":
                        print(
                            "    (Or --build-final-file <folder>, if the "
                            "per-chapter PDFs are still on disk.)"
                        )
                _emit(
                    "final_file_skipped",
                    reason=final_skip["reason"],
                    format=args.format,
                    path=final_path,
                    run_chapters=final_skip["run_chapters"],
                    existing_chapters=final_skip["existing_chapters"],
                    dropped_chapters=final_skip["dropped_chapters"],
                )
                final_build_stranded = not getattr(args, "keep_chapters", False)
                if final_build_stranded and run_chapter_labels:
                    print(
                        f"    This run's {len(run_chapter_labels)} chapter(s) are "
                        f"only in the temp folder (no --keep-chapters), so it is "
                        f"being kept instead of cleaned up."
                    )
            else:
                print("\nBuilding final file...")
                _emit("phase", phase="building_final")
                if active_out_dir:
                    os.makedirs(active_out_dir, exist_ok=True)
                # Recorded as `final_file_chapters` only where a file is really
                # written (the PDF branch below can no-op when no chapter PDFs
                # survived), so the key never claims coverage that isn't on disk.
                built_labels = sorted(
                    {_chap_label_str(x) for x in run_chapter_labels},
                    key=lambda x: (_chap_as_float(x) is None, _chap_as_float(x) or 0.0),
                )
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
                    final_file_built_labels = built_labels
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
                    final_file_built_labels = built_labels
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
                        _emit("file_saved", format="pdf", path=final_path)
                        final_file_built_labels = built_labels
                    for item in current_book_content:
                        if item.get("type") == "pdf" and item.get("path"):
                            try:
                                os.remove(item["path"])
                            except OSError:
                                pass

    # --- Patch details.json with the sidecar-asset rollup (Komikku mode) ---
    # details.json was written once up front (before the chapter loop), so the
    # has_motion/has_audio/chapter_assets rollup is merged in now that every
    # chapter's CBZ (with its embedded _aio/ aux) has landed. The helper rebuilds
    # the map from the on-disk CBZ ComicInfo scan (so an incremental/resume run
    # stays complete) and skips the scan entirely on every aux-free series.
    if getattr(args, "komikku", False):
        _patch_details_json_with_assets(out_dir, aux_seen)

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
        # Normalize labels to --list-chapters' f"{num:g}" form (grep
        # _chap_label_str) so the UI's update-check diff matches — a float-
        # emitting handler otherwise records "4.0" here vs "4" from
        # --list-chapters and the series sticks at "+N new". Safe sort key so a
        # stray non-numeric label can't crash the whole .aio_series.json write
        # (it lives in a log-only except → silent metadata loss). XF-1/PYP-2.
        # ATTEMPTED, not SELECTED — see _run_claimed_chapter_labels and
        # attempted_chapter_labels at the chapter loop. Equal to the selection
        # for any run that finished the loop, so complete downloads are
        # unchanged; a cancelled or aborted run now claims only what it reached.
        actually_downloaded = _run_claimed_chapter_labels(
            attempted_chapter_labels, missed_entries
        )

        # If a previous .aio_series.json exists (from an earlier download),
        # merge the chapter lists so partial/split downloads accumulate. Loaded
        # once above the final-file gate (grep existing_series_meta) because the
        # overwrite guard has to read it BEFORE the build.
        existing_meta = existing_series_meta

        # Re-normalize any legacy "4.0"-style entries from an older writer so a
        # once-polluted file self-heals to "4" on this write (else the union
        # keeps BOTH "4.0" and "4"). Safe sort key (grep _chap_as_float).
        prev_downloaded = set(
            _chap_label_str(x) for x in existing_meta.get("chapters_downloaded", [])
        )
        # Withhold this run's labels when its chapters never reached a durable
        # file (grep final_build_stranded): the combined build was declined and
        # --keep-chapters is off, so the only copies live in tmp_<hid>/. Recording
        # them would make the library update-check report the series complete
        # while the bytes sit in a temp folder — the concealment half of D1.
        # Prior labels are still preserved, so this only ever withholds, never
        # forgets.
        merged_downloaded = sorted(
            prev_downloaded
            if final_build_stranded
            else prev_downloaded | set(actually_downloaded),
            key=lambda x: (_chap_as_float(x) is None, _chap_as_float(x) or 0.0),
        )

        # Fragment labels this (or a prior eager) run dropped under multi-source
        # consensus — the UI update-check subtracts these so duplicate .1-.4
        # fragments don't show as a perpetual "+N new" (see _skipped_fragment_labels
        # at the group_chapters_for_download site). UNION with any prior set so a
        # later lazy "Download Missing" run (no consensus → empty THIS run) can't
        # wipe an eager run's set; MINUS merged_downloaded so a force-downloaded
        # fragment counts as present rather than skipped (the two on-disk sets stay
        # disjoint). Same normalized :g labels as chapters_downloaded so the main.js
        # Set difference matches. grep chapters_skipped_fragments.
        prev_skipped = set(
            _chap_label_str(x)
            for x in existing_meta.get("chapters_skipped_fragments", [])
        )
        merged_skipped = sorted(
            (prev_skipped | {_chap_label_str(x) for x in _skipped_fragment_labels})
            - set(merged_downloaded),
            key=lambda x: (_chap_as_float(x) is None, _chap_as_float(x) or 0.0),
        )

        # What each combined archive on disk actually contains, as of the last
        # build of THAT FORMAT that really ran. DISTINCT from
        # chapters_downloaded, which is a running UNION across runs ON PURPOSE
        # (split/incremental downloads depend on that) and therefore can never
        # answer "would rebuilding lose something" — the very question the
        # overwrite guard asks. Read back by _final_file_recorded_coverage,
        # which falls back to chapters_downloaded so pre-2026-08 files without
        # the key still work, and migrates the pre-map single-list shape.
        #
        # Carried forward unchanged when this run built nothing (--no-final-file,
        # --format none, komikku, split, or a guard-skipped build): recording the
        # current run's chapters there would assert coverage no file has.
        # KEYED BY FORMAT. A series folder holds one archive per format and one
        # metadata file for all of them, so an unqualified list let an EPUB
        # export describe the CBZ and the guard cleared a truncating rebuild.
        # This run may only ever replace ITS OWN format's entry; the others are
        # carried through untouched, because they still describe files that are
        # still on disk. See _final_file_recorded_coverage for the read rules.
        final_file_chapters = {}
        _prev_ffc = existing_meta.get("final_file_chapters")
        if isinstance(_prev_ffc, dict):
            for _fmt, _labels in _prev_ffc.items():
                if isinstance(_labels, (list, tuple)) and isinstance(_fmt, str):
                    final_file_chapters[_fmt] = [
                        _chap_label_str(x) for x in _labels
                    ]
        elif isinstance(_prev_ffc, list):
            # Legacy single-format shape, migrated in place. meta["format"] is
            # the format that wrote it; without that we cannot say which archive
            # it described, so it is dropped rather than misfiled onto this run's
            # format (the reader then falls back to chapters_downloaded, which
            # overstates — the safe direction).
            _prev_fmt = existing_meta.get("format")
            if isinstance(_prev_fmt, str) and _prev_fmt:
                final_file_chapters[_prev_fmt] = [
                    _chap_label_str(x) for x in _prev_ffc
                ]
        if final_file_built_labels is not None:
            final_file_chapters[args.format] = list(final_file_built_labels)
        if not final_file_chapters:
            final_file_chapters = None

        # Serialize AniList tags via the module-level _serialize_anilist_tag —
        # the same serializer the refresh writer + _build_aio_reader_extras use,
        # so the on-disk schema stays identical across all three writers (the
        # AnilistTag dataclass itself doesn't survive json.dump). Empty lists
        # when enrichment was off / no confident match keep the field always
        # present. Cross-file: sites/external_metadata.py:AnilistTag is the
        # source; aio-dl.py:_load_cached_anilist_id reads "anilist_id" back on resume.
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
            # Consensus-dropped duplicate fragments; subtracted by the UI
            # update-check (main.js:_checkSeriesUpdates). Empty for single-source
            # / lazy / collapse-off series. grep chapters_skipped_fragments.
            "chapters_skipped_fragments": merged_skipped,
            # Absent (not null) when no build has ever been recorded — readers
            # must treat "missing" as "unknown", never as "covers nothing".
            **(
                {"final_file_chapters": final_file_chapters}
                if final_file_chapters is not None
                else {}
            ),
            # None when the chapter listing was deliberately cut short by
            # chapter_floor_hint (grep pool_is_partial): reporting a floored
            # pool as the series total would be a lie, and "unknown" is the
            # honest answer. Nothing reads this field today; keep it truthful
            # anyway so anything that starts to can trust it.
            "total_available_at_download": None if pool_is_partial else len(pool),
            "last_downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            # --- AniList enrichment fields (--metadata-source=anilist) ---
            # Populated when enrichment matched a series confidently;
            # null/empty when no match or feature disabled. The cached
            # IDs let resume + --update-all runs short-circuit the
            # fuzzy title-match step. See sites/external_metadata.py.
            # Same seven fields as the refresh writer + _build_aio_reader_extras
            # (grep _anilist_meta_fields), appended in canonical order so the
            # on-disk key sequence is unchanged.
            **_anilist_meta_fields(comic_data),
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

    _timing_other = max(0.0, _timing_total - _timing["imageurls"] - _timing["download"] - _timing["processing"])
    print(f"\n--- Timing Summary ---")
    print(f"  Image URL fetch  : {_fmt_time(_timing['imageurls'])}")
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
        # Emit BEFORE sys.exit: SystemExit unwinds straight past an embedder's
        # try/finally-free call site, and a UI that never hears a terminal event
        # leaves the download stuck "running" forever.
        _emit(
            "done",
            ok=False,
            aborted=True,
            output_dir=out_dir,
            temp_dir=main_tmp_dir,
            cancelled=run_cancelled(),
        )
        sys.exit(1)
    elif not args.no_cleanup and not run_cancelled() and not final_build_stranded:
        rm_tree(main_tmp_dir)
        print("\nDone.")
        _emit("done", ok=True, aborted=False, output_dir=out_dir, cancelled=False)
    else:
        # --no-cleanup, a CANCELLED run, or a run whose combined-file build the
        # overwrite guard declined while --keep-chapters was off (grep
        # final_build_stranded) — in that last case the per-chapter caches in here
        # are the ONLY copy of what was just downloaded, so wiping them would turn
        # a refusal-to-destroy into a different data loss.
        #
        # A cancelled run MUST keep tmp_<hid>/ — it is the entire input to a
        # resume, and the completed chapter dirs in it are bytes the user
        # already paid for. Same reasoning as the abort branch above.
        #
        # This only ever fires for an EMBEDDED caller: cancellation is
        # cooperative (`request_run_cancel`, whose one caller is
        # aio_android.cancel), so a cancelled run returns normally and falls
        # through to here. The desktop cancels by KILLING the process, so
        # main() never reaches this block at all and its tmp dir survives for
        # free — which is why the desktop's resume bar has always worked and
        # why this guard was not needed until Android.
        print(f"\nDone. Temporary files kept at: {main_tmp_dir}")
        _emit(
            "done",
            ok=True,
            aborted=False,
            output_dir=out_dir,
            temp_dir=main_tmp_dir,
            cancelled=run_cancelled(),
        )


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
        # comix's WAF / truncated-scrape failures are USER-actionable, not
        # bugs: the message already carries the remediation, and a traceback
        # buries it in noise the user can do nothing with. Everything else
        # still re-raises with the full traceback. Imported lazily so a
        # renamed/missing handler can never break the CLI's own error path.
        # Cross-file: sites/comix.py (grep ComixWafChallengeError).
        try:
            from sites.comix import (
                ComixChapterScrapeError,
                ComixWafChallengeError,
            )
            _actionable = (ComixWafChallengeError, ComixChapterScrapeError)
        except Exception:
            _actionable = ()
        if _actionable and isinstance(e, _actionable):
            print(f"\n[!] {e}", file=sys.stderr, flush=True)
            sys.exit(2)
        raise
