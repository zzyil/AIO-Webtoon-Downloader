"""Official-publisher catalog loader and lookup.

Reads sites/official_publishers.json (a hand-curated catalog of MangaDex
scanlation_group UUIDs that represent licensed publishing platforms) and
provides a single lookup function for handlers to annotate chapter dicts
with is_official + publisher fields.

What this module owns:
  - Lazy load + cache of the catalog (loaded once per process).
  - `lookup_publisher(group_id, group_name)` → (is_official, canonical_name)
    matching by UUID first (definitive) then case-insensitive name + aliases.
  - Overdue-warning emission on first lookup if the catalog is past its
    review cadence (90 days by default).

What reads from it:
  - sites/mangadex.py — annotates each chapter's group_name UUID.
  - Future: any other handler that exposes per-chapter publisher info
    (e.g., MangaFire publisher tag scraping, if/when that ships).

Cross-file:
  - Catalog at sites/official_publishers.json. Schema documented in the
    file's _meta block.
  - Reused by sites/chapter_merger.py via the is_official field on
    chapter dicts (no direct dependency — the merger reads the field
    that this module's consumers populate).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import threading
from typing import Dict, List, Optional, Tuple


_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "official_publishers.json")

# Loaded once, cached. Two views are kept:
#   _BY_UUID: UUID (lowercased) -> publisher entry. Primary match key.
#   _BY_NAME: name/alias (lowercased) -> publisher entry. Secondary fallback.
_LOAD_LOCK = threading.Lock()
_LOADED = False
_BY_UUID: Dict[str, Dict] = {}
_BY_NAME: Dict[str, Dict] = {}
_OVERDUE_WARNED = False


def _load_catalog() -> None:
    """Lazy load the catalog. Idempotent + thread-safe.

    Failure modes:
      - File missing / parse error → empty catalog (lookup always returns
        is_official=False). Logs to stderr once.
      - Past review cadence → logs a one-time warning to stderr.
    """
    global _LOADED, _BY_UUID, _BY_NAME, _OVERDUE_WARNED
    if _LOADED:
        return
    with _LOAD_LOCK:
        if _LOADED:
            return
        try:
            with open(_CATALOG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"[!] _publishers: failed to load official_publishers.json: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            _LOADED = True
            return

        publishers = data.get("publishers") or []
        for entry in publishers:
            if not isinstance(entry, dict):
                continue
            uuid = (entry.get("mangadex_group_id") or "").strip().lower()
            if uuid:
                _BY_UUID[uuid] = entry
            for nm in [entry.get("name")] + (entry.get("name_aliases") or []):
                if isinstance(nm, str) and nm.strip():
                    _BY_NAME[nm.strip().lower()] = entry

        # Overdue-review warning. Cadence-aware so the user is nudged to
        # re-verify UUIDs after 90 days (corporate events: rebrands,
        # shutdowns, new publishers). Won't block runs.
        last_reviewed = data.get("last_reviewed")
        cadence_days = int(data.get("review_cadence_days") or 90)
        if last_reviewed and not _OVERDUE_WARNED:
            try:
                reviewed_dt = _dt.datetime.strptime(last_reviewed, "%Y-%m-%d").date()
                age_days = (_dt.date.today() - reviewed_dt).days
                if age_days > cadence_days:
                    print(
                        f"[!] official_publishers.json overdue for review: "
                        f"last reviewed {last_reviewed} ({age_days} days ago, "
                        f"cadence {cadence_days}d). Re-verify UUIDs via "
                        f"https://api.mangadex.org/group?name=<publisher> .",
                        file=sys.stderr,
                    )
                    _OVERDUE_WARNED = True
            except (ValueError, TypeError):
                pass

        _LOADED = True


def lookup_publisher(
    group_id: Optional[str], group_name: Optional[str]
) -> Tuple[bool, Optional[str]]:
    """Match a (group_id, group_name) pair against the official-publishers
    catalog.

    Returns:
        (is_official, canonical_name)
        - is_official: True if either the UUID or the name (case-insensitive,
          including aliases) matches a non-defunct catalog entry.
        - canonical_name: the catalog's `name` field on match, else None.
          Callers should use this as the `publisher` field on chapter dicts
          rather than the raw group_name (the catalog name is the canonical
          form across rebrands).

    UUID match is preferred over name match — UUIDs are stable across
    rebrands; names drift. If both are None or no match, returns (False, None).

    Defunct entries (status="defunct") still match for historical chapters
    — they were once official, the chapters they signed are still the
    licensed translation. Caller decides whether to treat them differently
    from active publishers.
    """
    _load_catalog()
    entry = None
    if group_id:
        entry = _BY_UUID.get(group_id.strip().lower())
    if entry is None and group_name:
        entry = _BY_NAME.get(group_name.strip().lower())
    if entry is None:
        return False, None
    return True, entry.get("name")


__all__ = ["lookup_publisher"]
