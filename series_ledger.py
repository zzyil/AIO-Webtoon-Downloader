"""The `.aio_series.json` ledger: per-kind namespaces and a locked read-modify-write.

What this module owns
---------------------
1. **The namespaces.** Chapter state stays where it has always been, at the top
   level: ``chapters_downloaded``, ``chapters_skipped_fragments``,
   ``chapters_ignored``, ``final_file_chapters``, ``format``, ``language`` and
   ``total_available_at_download``. Volume state lives in one optional
   sub-object::

       "volumes": {"schema": 1, "downloaded": [...], "ignored": [...],
                   "format": "cbz", "language": "en",
                   "total_available_at_download": 117,
                   "last_downloaded_at": "..."}

   Why a sub-object rather than "v3"-style labels in the chapter list: older
   builds, including the synced Android engine, read this file. An unknown
   sub-object is invisible to them, but a "v3" label would land in their
   chapter counts, shrink guard and update-check diff. It also means existing
   files need no migration. There is no volume ``skipped_fragments`` or
   ``final_file`` field because volumes have neither state.

2. **The write path.** ``update_series_meta`` takes a cross-process lock (kept
   in the system temp dir, not the library; see ``_lock_for``), re-reads it, applies the caller's mutation, and replaces the file
   atomically. A chapter job and a volume job on one series can finish at the
   same moment (the UI runs a queue, and ``--update-all`` runs a child per
   kind). The old writer built the dict from a snapshot read minutes earlier,
   so the slower job silently dropped the faster one's labels.

Who reads it
------------
- aio-dl.py: the end-of-run writer (grep ``series_ledger.update_series_meta``)
  and the AniList refresh writer.
- library_state.py: ``tracked_kinds`` and ``kind_view`` for scan-library and
  ``--update-all``.
- aio_android.py: update check.
- JS twin: UI-source/electron/series-ledger.js (``trackedKinds``,
  ``kindView``). Shared cases live in tests/fixtures/series_ledger_cases.json.

Depends on: stdlib, item_kinds, sites.profile_lock (the OS lock; it has no
mangafire-specific code despite its origin).
"""
from __future__ import annotations

import json
import math
import os
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from item_kinds import CHAPTER, VOLUME

SERIES_META_FILE = ".aio_series.json"
VOLUMES_KEY = "volumes"
VOLUME_SCHEMA = 1

# Keys describing the SERIES, not one kind's progress. Every writer, of either
# kind, may refresh these. Anything else at the top level belongs to the
# chapter namespace, and a volume run must never touch it. Notably that
# includes `format` and `language`: `buildLibraryDownloadArgs` replays them on
# the next chapter update, so a volume run writing them would silently change
# the chapter format.
SHARED_KEYS = (
    "url", "hid", "title", "site", "status", "authors", "cover", "genres",
)

# How long a writer waits for a peer before writing without the lock. A run
# that has finished downloading must never lose its ledger to a stuck peer, so
# on timeout it goes ahead unlocked; that is the pre-lock behaviour, not worse.
_LOCK_TIMEOUT_S = 30.0


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------

def label_str(value: Any) -> str:
    """Canonical label: ``f"{x:g}"`` for finite numbers, else ``str()``.

    Mirrors aio-dl.py ``_chap_label_str``. Kept separate so library_state and
    the Android engine can use it without importing aio-dl.py.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{f:g}" if math.isfinite(f) else str(value)


def label_sort_key(label: str):
    try:
        f = float(label)
        if math.isfinite(f):
            return (False, f)
    except (TypeError, ValueError):
        pass
    return (True, 0.0)


def sorted_labels(labels: Iterable[Any]) -> List[str]:
    return sorted({label_str(x) for x in labels}, key=label_sort_key)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def load_series_meta(folder: str) -> Dict[str, Any]:
    """Parsed ledger of *folder*, or {} when absent, unreadable or not a dict.

    Tolerant on purpose: a corrupt ledger must degrade to "no prior
    knowledge", never abort a finished download.
    """
    if not folder:
        return {}
    path = os.path.join(folder, SERIES_META_FILE)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def volume_block(meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    block = meta.get(VOLUMES_KEY) if isinstance(meta, dict) else None
    return block if isinstance(block, dict) else None


def kind_view(meta: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """One kind's progress as ``{downloaded, ignored, format, language}``.

    For the volume kind, ``format`` and ``language`` fall back to the series
    language and to None (the caller picks a default format) when the block
    lacks them. That covers a block rebuilt by the self-heal path.
    """
    meta = meta if isinstance(meta, dict) else {}
    if kind == CHAPTER:
        return {
            "downloaded": [label_str(x) for x in meta.get("chapters_downloaded") or []],
            "ignored": [label_str(x) for x in meta.get("chapters_ignored") or []],
            "format": meta.get("format"),
            "language": meta.get("language"),
        }
    if kind == VOLUME:
        block = volume_block(meta) or {}
        return {
            "downloaded": [label_str(x) for x in block.get("downloaded") or []],
            "ignored": [label_str(x) for x in block.get("ignored") or []],
            "format": block.get("format"),
            "language": block.get("language") or meta.get("language"),
        }
    raise ValueError(f"unknown item kind: {kind!r}")


def tracked_kinds(meta: Dict[str, Any], *, volume_files_present: bool = False) -> Set[str]:
    """Which kinds the update check should list and diff for this series.

    - chapter: when ``chapters_downloaded`` is non-empty, OR when there is no
      volume block at all. A legacy ledger, or a folder with no ledger, has
      always meant a chapter series.
    - volume: when the volume block exists, OR when volume files are on disk.
      The second case is the self-heal for an older build that rebuilt the
      ledger and dropped the block.

    So a volume-only series never shows "+300 new chapters" and nobody has to
    flag it by hand. A volume block with an empty ``downloaded`` list still
    tracks volumes: the user chose volume mode for this series.
    """
    meta = meta if isinstance(meta, dict) else {}
    kinds: Set[str] = set()
    has_volumes = volume_block(meta) is not None or volume_files_present
    if meta.get("chapters_downloaded") or not has_volumes:
        kinds.add(CHAPTER)
    if has_volumes:
        kinds.add(VOLUME)
    return kinds


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def _lock_dir() -> str:
    import tempfile

    return os.path.join(tempfile.gettempdir(), "aio-series-locks")


def _lock_for(folder: str):
    # The lock file lives OUTSIDE the library, in the system temp dir, keyed
    # by a hash of the folder's normalized absolute path. "X instead of a
    # sibling .aio_series.lock" because the series folder is read by other
    # tools that inventory every file in it:
    #   - series-merge.js counts an unknown file as a leftover, which blocks
    #     removing the merged-away folder;
    #   - device sync mirrors folders;
    #   - on Android the library can sit on shared storage where locks are
    #     unreliable;
    #   - Komikku reads the folder.
    # The lock only has to coordinate processes on THIS machine, so it doesn't
    # need to sit beside the file it guards.
    # Imported lazily: `sites` package init registers every handler (~1.5 s),
    # and this module's READ path is used by light callers that must not pay
    # that cost. Every WRITER (aio-dl.py, aio_android.py) has `sites` loaded
    # already.
    try:
        from sites import profile_lock
    except Exception:
        return None
    import hashlib

    key = os.path.normcase(os.path.abspath(folder)).encode("utf-8", "surrogatepass")
    digest = hashlib.sha1(key).hexdigest()[:20]
    # profile_lock.get(X) locks "X.lock" and keeps one lock object per path in
    # this process, which is what makes two threads share it.
    return profile_lock.get(os.path.join(_lock_dir(), digest))


def _write_atomic(path: str, data: Dict[str, Any]) -> None:
    """Write via temp file + os.replace, so a reader never sees a half file.

    On Windows os.replace fails with PermissionError while another process
    holds the destination open (the Electron app reads ledgers during a
    library scan). That window is short, so retry briefly, then fall back to
    an in-place write. A truncated-read risk is better than losing the
    ledger.
    """
    tmp = f"{path}.{os.getpid()}.tmp"
    payload = json.dumps(data, indent=2)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1 * (attempt + 1))
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def update_series_meta(
    folder: str,
    mutate: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Locked read-modify-write of *folder*'s ledger. Returns what was written.

    *mutate* gets a fresh copy of the ledger as it is on disk NOW (re-read
    under the lock) and returns the full dict to write. Returning None means
    "write nothing", and the on-disk ledger is returned unchanged. Raises
    whatever *mutate* or the write raises. Callers already wrap the call in
    their own log-only except, because a ledger failure must not fail a run.
    """
    lock = _lock_for(folder)
    held = False
    if lock is not None:
        try:
            held = lock.acquire(_LOCK_TIMEOUT_S)
        except Exception:
            held = False
    try:
        existing = load_series_meta(folder)
        new = mutate(dict(existing))
        if new is None:
            return existing
        _write_atomic(os.path.join(folder, SERIES_META_FILE), new)
        return new
    finally:
        if held:
            lock.release()


def carry_other_namespaces(existing: Dict[str, Any], rebuilt: Dict[str, Any]) -> Dict[str, Any]:
    """Copy namespaces the CHAPTER writer doesn't own from *existing* into *rebuilt*.

    The chapter writer rebuilds the top level from scratch on purpose: it is
    how stale and legacy keys get dropped. Without this step it would also drop
    the volume block. Appended last, and only when present, so a chapter-only
    ledger stays byte-identical to the old writer's output.
    """
    block = volume_block(existing)
    if block is not None:
        rebuilt[VOLUMES_KEY] = block
    return rebuilt


def merge_volume_run(
    existing: Dict[str, Any],
    *,
    shared: Dict[str, Any],
    downloaded: Iterable[Any],
    fmt: str,
    language: str,
    total_available: Optional[int],
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """Ledger after a volume run: the shared keys are refreshed, the volume
    block is unioned, and the chapter namespace is left exactly as it was.

    Starts from *existing* rather than rebuilding, because the top level holds
    the chapter namespace, which this run does not own. *shared* may hold only
    SHARED_KEYS plus the AniList fields (the caller passes
    ``_anilist_meta_fields``); a stray chapter key in it raises ValueError.
    That makes the "volume run rewrote my chapter format" bug a crash in tests
    rather than a silent change.

    ``ignored`` loses anything now downloaded: once a volume is on disk there
    is nothing left to withhold. This is the same rule as the chapter writer's
    ``merged_ignored``.
    """
    chapter_owned = {
        "format", "language", "chapters_downloaded", "chapters_ignored",
        "chapters_skipped_fragments", "final_file_chapters",
        "total_available_at_download", "last_downloaded_at", "download_volumes",
    }
    bad = chapter_owned.intersection(shared)
    if bad:
        raise ValueError(f"volume run may not write chapter-namespace keys: {sorted(bad)}")

    out = dict(existing)
    out.update(shared)
    prev = volume_block(existing) or {}
    merged_downloaded = sorted_labels(list(prev.get("downloaded") or []) + list(downloaded))
    merged_ignored = sorted(
        {label_str(x) for x in prev.get("ignored") or []} - set(merged_downloaded),
        key=label_sort_key,
    )
    block: Dict[str, Any] = {
        "schema": VOLUME_SCHEMA,
        "downloaded": merged_downloaded,
        "format": fmt,
        "language": language,
        "total_available_at_download": total_available,
        "last_downloaded_at": now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if merged_ignored:
        block["ignored"] = merged_ignored
    out[VOLUMES_KEY] = block
    return out


def set_ignored(meta: Dict[str, Any], kind: str, labels: Iterable[Any]) -> Dict[str, Any]:
    """Replace one kind's user cross-outs. Python twin of main.js
    set-chapters-ignored. Used by the Android engine. An empty list removes the
    key, matching the writers' "absent means empty" rule."""
    out = dict(meta)
    clean = sorted_labels(labels)
    if kind == CHAPTER:
        if clean:
            out["chapters_ignored"] = clean
        else:
            out.pop("chapters_ignored", None)
        return out
    if kind == VOLUME:
        # An empty list never CREATES the block: a block alone makes the series
        # track volumes (tracked_kinds). Same rule as series-ledger.js applyIgnored.
        existing = volume_block(meta)
        if existing is None and not clean:
            return out
        block = dict(existing or {"schema": VOLUME_SCHEMA, "downloaded": []})
        if clean:
            block["ignored"] = clean
        else:
            block.pop("ignored", None)
        out[VOLUMES_KEY] = block
        return out
    raise ValueError(f"unknown item kind: {kind!r}")
