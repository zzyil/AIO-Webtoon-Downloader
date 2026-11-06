from __future__ import annotations

import json
import os
import re
import zipfile
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Set

# Both stdlib-only at import time (series_ledger loads `sites` lazily, and only
# to WRITE), so this module stays cheap for the Android and CLI scanners.
import series_ledger
from item_kinds import CHAPTER, RAW_VOLUME_DIR_RE, VOLUME, parse_volume_number_from_name


SAVED_PARAMS_FILE = "download_params.json"
SERIES_META_FILE = ".aio_series.json"
SUPPORTED_BOOK_EXTS = {".cbz", ".pdf", ".epub"}
SUPPORTED_COVER_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
CHAPTER_FILE_RE = re.compile(
    r"(?:^|[ _])Ch[ _]([0-9]+(?:[.~][0-9]+)?)(?:-([0-9]+(?:[.~][0-9]+)?))?",
    re.IGNORECASE,
)
RAW_IMAGE_DIR_RE = re.compile(r"^Chapter_([0-9]+(?:[.~][0-9]+)?)$", re.IGNORECASE)


def parse_chapter_number(value: object) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.lower() in {"oneshot", "one-shot"}:
        text = "1"
    text = text.replace("~", ".")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def format_chapter_number(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _is_integral(value: Decimal) -> bool:
    return value == value.to_integral_value()


def extract_chapter_numbers_from_name(name: str) -> Set[Decimal]:
    match = CHAPTER_FILE_RE.search(name)
    if not match:
        return set()

    start = parse_chapter_number(match.group(1))
    end = parse_chapter_number(match.group(2))
    if start is None:
        return set()

    values = {start}
    if end is None:
        return values

    if (
        _is_integral(start)
        and _is_integral(end)
        and 0 <= int(end) - int(start) <= 1000
    ):
        for chapter in range(int(start), int(end) + 1):
            values.add(Decimal(chapter))
        return values

    values.add(end)
    return values


def scan_downloaded_chapters(folder: str) -> Set[Decimal]:
    chapter_numbers: Set[Decimal] = set()
    if not os.path.isdir(folder):
        return chapter_numbers

    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            ext = os.path.splitext(name)[1].lower()
            if ext in SUPPORTED_BOOK_EXTS:
                chapter_numbers.update(extract_chapter_numbers_from_name(name))
            continue

        if not os.path.isdir(path):
            continue

        raw_match = RAW_IMAGE_DIR_RE.match(name)
        if raw_match:
            chapter = parse_chapter_number(raw_match.group(1))
            if chapter is not None:
                chapter_numbers.add(chapter)
            continue

        if name != "images":
            continue

        for child in os.listdir(path):
            child_path = os.path.join(path, child)
            if not os.path.isdir(child_path):
                continue
            child_match = RAW_IMAGE_DIR_RE.match(child)
            if not child_match:
                continue
            chapter = parse_chapter_number(child_match.group(1))
            if chapter is not None:
                chapter_numbers.add(chapter)

    return chapter_numbers


def volume_file_base(title: object) -> Optional[str]:
    """The file-name base a volume run writes for *title*, or None.

    Same rule as aio-dl.py main()'s sanitize_filename (grep it there): drop
    Windows-illegal characters, underscores become spaces, whitespace collapses,
    no trailing dots or spaces. A volume run always clears --group (grep
    "[Volumes] Forcing"), so its files are "<this> Vol.NN" with nothing in
    between. Keep the two in step.
    """
    s = re.sub(r'[\\/*?:"<>|]', "", str(title or "")).replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip().rstrip(" .")
    return s or None


def scan_downloaded_volumes(folder: str, base: Optional[str] = None) -> Set[Decimal]:
    """Volume numbers on disk: `<base> Vol.NN.<ext>` archives at the folder
    root, plus `images/Volume_N` raw-image dirs (--keep-images / --format none).

    The volume twin of scan_downloaded_chapters, and disjoint from it by
    construction: a volume name has no "Ch", and the volume pattern is anchored
    at the end of the stem, so Komikku's "Vol.01 Ch.005" is a chapter
    (tests/test_item_kinds.py holds both directions).

    *base* (volume_file_base of the ledger title) should be passed whenever it
    is known. Without it, a combined archive of a series whose TITLE ends in
    "Vol.3" reads as volume 3, and the update check would then start tracking
    volumes for a chapter series.
    """
    numbers: Set[Decimal] = set()
    try:
        names = os.listdir(folder)
    except OSError:
        return numbers
    for name in names:
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            if os.path.splitext(name)[1].lower() in SUPPORTED_BOOK_EXTS:
                label = parse_volume_number_from_name(name, base=base)
                parsed = parse_chapter_number(label) if label else None
                if parsed is not None:
                    numbers.add(parsed)
            continue
        if name != "images" or not os.path.isdir(path):
            continue
        try:
            children = os.listdir(path)
        except OSError:
            continue
        for child in children:
            m = RAW_VOLUME_DIR_RE.match(child)
            if m and os.path.isdir(os.path.join(path, child)):
                parsed = parse_chapter_number(m.group(1))
                if parsed is not None:
                    numbers.add(parsed)
    return numbers


def _volume_format_from_files(folder: str, base: Optional[str] = None) -> Optional[str]:
    # Fallback for a volume block that lacks `format` (rebuilt by the
    # self-heal, or written by hand): the extension the volume files carry.
    try:
        for name in sorted(os.listdir(folder)):
            ext = os.path.splitext(name)[1].lower()
            if ext in SUPPORTED_BOOK_EXTS and parse_volume_number_from_name(name, base=base):
                return ext[1:]
    except OSError:
        pass
    return None


def highest_contiguous_whole_chapter(chapter_numbers: Set[Decimal]) -> int:
    whole_numbers = {
        int(chapter)
        for chapter in chapter_numbers
        if _is_integral(chapter) and chapter >= 0
    }
    highest = 0
    while (highest + 1) in whole_numbers:
        highest += 1
    return highest


def build_update_chapters_arg(chapter_numbers: Set[Decimal]) -> str:
    if not chapter_numbers:
        return "all"

    latest = max(chapter_numbers)
    if _is_integral(latest):
        return f"{int(latest) + 1}-"
    return f"{format_chapter_number(latest)}-"


def volume_update_argv(entry: Dict, output_dir: str) -> Optional[List[str]]:
    """aio-dl.py flags (no interpreter, script or URL) that fetch the volumes a
    scan_library *entry* is missing, or None when Komikku blocks it.

    The one builder for a volume update, shared by aio-dl.py
    _build_update_children (--update-all) and gui.py _update_selected, so the
    two can't drift. Only the volume block's own format/language plus the
    series' site and cookies ride along: groups, split, collapse, multi-source
    and the image-size knobs are chapter-run settings (download_params.json is
    written by chapter runs only), and a volume run coerces its layout flags
    itself (grep "[Volumes] Forcing" in aio-dl.py).

    None under Komikku (the series was saved with --komikku): --komikku turns
    --download-volumes off, so the run would download CHAPTERS 1-3 where the
    user tracked VOLUMES 1-3, silently. Callers say "skipped" instead.
    """
    params = entry.get("params") or {}
    if params.get("komikku"):
        return None
    argv = [
        "--download-volumes",
        "--chapters", entry.get("next_volume_update") or "all",
        "--format", entry.get("volume_format") or "cbz",
        "--language", entry.get("volume_language") or entry.get("language") or "en",
        "--output-dir", output_dir,
    ]
    if params.get("site"):
        argv.extend(["--site", str(params["site"])])
    if params.get("cookies"):
        argv.extend(["--cookies", str(params["cookies"])])
    return argv


def load_saved_params(folder: str) -> tuple[bool, Dict]:
    params_path = os.path.join(folder, SAVED_PARAMS_FILE)
    if not os.path.isfile(params_path):
        return False, {}
    try:
        with open(params_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return True, {}
    return True, data if isinstance(data, dict) else {}


def load_series_meta(folder: str) -> tuple[bool, Dict]:
    meta_path = os.path.join(folder, SERIES_META_FILE)
    if not os.path.isfile(meta_path):
        return False, {}
    try:
        with open(meta_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return True, {}
    return True, data if isinstance(data, dict) else {}


def _cover_sort_key(name: str) -> tuple[int, int, str]:
    normalized = name.replace("\\", "/").lower()
    base = os.path.basename(normalized)
    ext = os.path.splitext(base)[1]
    if ext not in SUPPORTED_COVER_EXTS:
        return (99, len(normalized), normalized)
    if base.startswith(".cover") or base.startswith("cover"):
        return (0, len(normalized), normalized)
    if "/cover" in normalized or "cover" in base:
        return (1, len(normalized), normalized)
    return (2, len(normalized), normalized)


def _find_existing_cover(folder: str) -> Optional[str]:
    candidates = []
    try:
        names = os.listdir(folder)
    except OSError:
        return None

    for name in names:
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        base = name.lower()
        ext = os.path.splitext(base)[1]
        if ext not in SUPPORTED_COVER_EXTS:
            continue
        if base.startswith(".cover") or base.startswith("cover"):
            candidates.append(path)

    if not candidates:
        return None
    return sorted(candidates, key=lambda path: _cover_sort_key(os.path.basename(path)))[0]


def _write_cover_file(folder: str, ext: str, data: bytes) -> Optional[str]:
    if ext not in SUPPORTED_COVER_EXTS:
        ext = ".jpg"
    out_path = os.path.join(folder, f".cover{ext}")
    try:
        with open(out_path, "wb") as handle:
            handle.write(data)
    except OSError:
        return None
    return out_path


def _extract_cover_from_zip(book_path: str, folder: str, write_cache: bool = False) -> Optional[str]:
    try:
        with zipfile.ZipFile(book_path) as archive:
            members = [
                name
                for name in archive.namelist()
                if not name.endswith("/")
                and os.path.splitext(name.lower())[1] in SUPPORTED_COVER_EXTS
            ]
            if not members:
                return None
            member = sorted(members, key=_cover_sort_key)[0]
            data = archive.read(member)
    except (OSError, zipfile.BadZipFile, KeyError):
        return None

    ext = os.path.splitext(member)[1].lower() or ".jpg"
    # MISC-3: only materialize the .cover.* cache when explicitly asked. The
    # read-only library scan must not write (concurrent GET /api/library + GUI
    # refresh + Electron scans raced → torn .cover.* writes). The zip-embedded
    # cover has no standalone on-disk path, so the read path returns None and
    # find_cover_path falls through to the next book / raw-images extractor.
    if not write_cache:
        return None
    return _write_cover_file(folder, ext, data)


def _extract_cover_from_raw_images(folder: str, write_cache: bool = False) -> Optional[str]:
    candidates = []

    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if not os.path.isdir(path):
            continue
        if RAW_IMAGE_DIR_RE.match(name):
            candidates.append(path)
        elif name == "images":
            for child in sorted(os.listdir(path)):
                child_path = os.path.join(path, child)
                if os.path.isdir(child_path) and RAW_IMAGE_DIR_RE.match(child):
                    candidates.append(child_path)

    for chapter_dir in candidates:
        image_names = [
            name
            for name in sorted(os.listdir(chapter_dir))
            if os.path.splitext(name.lower())[1] in SUPPORTED_COVER_EXTS
        ]
        if not image_names:
            continue
        image_path = os.path.join(chapter_dir, image_names[0])
        # MISC-3: read-only scan returns the real on-disk page path directly;
        # only write the .cover.* cache copy when explicitly opted in.
        if not write_cache:
            return image_path
        ext = os.path.splitext(image_path)[1].lower() or ".jpg"
        try:
            with open(image_path, "rb") as handle:
                return _write_cover_file(folder, ext, handle.read())
        except OSError:
            continue

    return None


def find_cover_path(folder: str, write_cache: bool = False) -> Optional[str]:
    # MISC-3: read-only by default. The library scan (scan_library) must NOT
    # write a .cover.* cache file as a side effect — concurrent scanners
    # (GET /api/library + GUI refresh + Electron) raced and produced torn
    # writes. Pass write_cache=True only from a single-owner caller that
    # deliberately wants the cache materialized.
    existing = _find_existing_cover(folder)
    if existing:
        return existing

    book_files = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        names = []

    for name in names:
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext in {".epub", ".cbz"}:
            book_files.append(path)

    for book_path in book_files:
        cover = _extract_cover_from_zip(book_path, folder, write_cache=write_cache)
        if cover:
            return cover

    return _extract_cover_from_raw_images(folder, write_cache=write_cache)


def list_saved_books(folder: str) -> List[str]:
    books = []
    try:
        names = os.listdir(folder)
    except OSError:
        return books

    for name in names:
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in SUPPORTED_BOOK_EXTS:
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        books.append((path, mtime))

    # Combined archive first, then per-volume books, then per-chapter files:
    # callers take books[0] as the series' representative book.
    def _tier(path: str) -> int:
        name = os.path.basename(path)
        if CHAPTER_FILE_RE.search(name):
            return 2
        return 1 if parse_volume_number_from_name(name) else 0

    books.sort(
        key=lambda item: (
            _tier(item[0]),
            -item[1],
            os.path.basename(item[0]).lower(),
        )
    )
    return [path for path, _mtime in books]


# ──────────────────────────────────────────────────────────────────
# SERIES IDENTITY / DUPLICATE GROUPING
#
# Two folders are the SAME SERIES when they name the same page on the same
# site, whatever they happen to be called on disk. This matters because a site
# RENAMING a series used to fork a second folder (aio-dl.py's allocator keyed on
# the title string), splitting the chapters and making the update check
# under-report on both halves — the 59-chapter folder said "5 new" forever while
# its 5-chapter fork said "59 new".
#
# aio-dl.py:allocate_series_output_dir now prevents new forks. These helpers are
# for the ones already on disk: an update check that treats a group as ONE
# series diffs against the union of what the members hold, so a forked library
# reports the truth until the user merges the folders.
#
# MIRROR TWINS — three implementations of one rule, because the desktop scans
# the library in JS and cannot import this:
#   * UI-source/electron/library.js — seriesIdentityKey / groupEntriesBySeries
#   * aio-dl.py                     — _series_identity_matches / _series_folder_sort_key
#   * here                          — the Android + CLI scanner
# Grep seriesIdentityKey if you change the matching or the primary rule.
# ──────────────────────────────────────────────────────────────────


def normalize_series_url(value: object) -> str:
    """Comparable form of a stored series URL; "" when there isn't one.

    Twin of aio-dl.py:_normalize_series_url — a list is reduced to its first
    element (older metadata could hold either shape), scheme/host are case
    folded, a leading www. and a trailing slash are dropped, and the PATH is
    left alone because plenty of sites serve case-sensitive slugs.
    """
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    match = re.match(r"^(https?://)(?:www\.)?([^/]+)(.*)$", text, re.IGNORECASE)
    if not match:
        return text.lower()
    return f"{match.group(1).lower()}{match.group(2).lower()}{match.group(3)}"


def series_identity_key(meta: Optional[Dict]) -> Optional[str]:
    """Grouping key for a series, or None when the metadata can't identify one.

    site+hid first because it survives a URL changing (domain rotation), URL
    second because it survives a site changing its id scheme. A folder with
    neither is ungroupable and must stay on its own — never fall back to the
    title, which is the exact signal that proved untrustworthy.
    """
    if not isinstance(meta, dict):
        return None
    site = str(meta.get("site") or "").strip()
    hid = str(meta.get("hid") or "").strip()
    if site and hid:
        return f"hid:{site}:{hid}"
    url = normalize_series_url(meta.get("url"))
    if url:
        return f"url:{url}"
    return None


def _series_payload_count(folder: str, entry: Dict) -> int:
    """Archives at the folder root plus --format none chapter dirs.

    Computed lazily by the grouper — only groups with more than one member need
    a primary, and those are rare — so the extra listdir is not paid per series.
    """
    count = int(entry.get("files") or 0)
    try:
        images_dir = os.path.join(folder, "images")
        count += sum(
            1
            for name in os.listdir(images_dir)
            if name.lower().startswith(("chapter_", "ch_"))
            and os.path.isdir(os.path.join(images_dir, name))
        )
    except OSError:
        pass
    return count


def _primary_sort_key(entry: Dict) -> tuple:
    # Richest first: a fork's new chapters belong beside the bulk of the series,
    # not in the husk, so the fullest folder is the one the update check reports
    # under and the one a queued download targets. Twin of aio-dl.py's
    # _series_folder_sort_key — keep the three tiers and their order in step.
    meta = entry.get("series_meta") or {}
    n_downloaded = len(meta.get("chapters_downloaded") or [])
    n_files = _series_payload_count(entry.get("folder") or "", entry)
    return (
        -(1 if (n_files or n_downloaded) else 0),
        -n_downloaded,
        -n_files,
        str(entry.get("name") or ""),
    )


def group_entries_by_series(entries: List[Dict]) -> List[Dict]:
    """Collapse scan_library output into one group per series.

    Returns [{key, primary, members}] in the input's order of first appearance.
    An entry with no identity key is its own single-member group, so callers can
    iterate groups uniformly instead of special-casing ungroupable folders.
    """
    order: List[Optional[str]] = []
    buckets: Dict[Optional[str], List[Dict]] = {}
    for index, entry in enumerate(entries):
        key = series_identity_key(entry.get("series_meta"))
        # None is not a shared bucket — every unidentifiable folder gets its own.
        bucket_key = key if key else f"solo:{index}"
        if bucket_key not in buckets:
            buckets[bucket_key] = []
            order.append(bucket_key)
        buckets[bucket_key].append(entry)

    groups: List[Dict] = []
    for bucket_key in order:
        members = buckets[bucket_key]
        primary = members[0] if len(members) == 1 else sorted(members, key=_primary_sort_key)[0]
        groups.append(
            {
                "key": bucket_key,
                "primary": primary,
                "members": members,
            }
        )
    return groups


def scan_library(root: str) -> List[Dict]:
    entries: List[Dict] = []
    if not os.path.isdir(root):
        return entries

    for entry in sorted(os.listdir(root)):
        folder = os.path.join(root, entry)
        if not os.path.isdir(folder) or entry.startswith("."):
            continue

        has_meta, series_meta = load_series_meta(folder)
        has_params, params = load_saved_params(folder)
        saved = dict(params)
        saved.update(series_meta)
        chapter_numbers = scan_downloaded_chapters(folder)
        for number in saved.get("chapters_downloaded") or []:
            parsed = parse_chapter_number(number)
            if parsed is not None:
                chapter_numbers.add(parsed)
        latest = max(chapter_numbers) if chapter_numbers else None

        # Volumes (native volume releases; item_kinds / series_ledger). Kept
        # apart from chapter_numbers: a volume number is not a chapter number.
        vol_base = volume_file_base(series_meta.get("title"))
        volume_files = scan_downloaded_volumes(folder, base=vol_base)
        volume_numbers = set(volume_files)
        volume_view = series_ledger.kind_view(series_meta, VOLUME)
        for number in volume_view["downloaded"]:
            parsed = parse_chapter_number(number)
            if parsed is not None:
                volume_numbers.add(parsed)
        tracked = series_ledger.tracked_kinds(
            series_meta, volume_files_present=bool(volume_files)
        )

        book_files = list_saved_books(folder)
        total_size = 0
        for path in book_files:
            try:
                total_size += os.path.getsize(path)
            except OSError:
                pass

        cover_path = find_cover_path(folder)

        entries.append(
            {
                "name": entry,
                "folder": folder,
                "has_params": has_params or has_meta,
                "params": saved,
                "series_meta": series_meta,
                # Duplicate detection, for callers that don't want to re-derive
                # it: series_key groups forks of the SAME page on the same site
                # (grep seriesIdentityKey), anilist_id is the weaker cross-site
                # hint used only to WARN, never to merge automatically.
                "series_key": series_identity_key(series_meta),
                "anilist_id": series_meta.get("anilist_id"),
                "url": saved.get("url", ""),
                "format": saved.get("format", "?"),
                "language": saved.get("language", "en"),
                "status": saved.get("status"),
                "authors": saved.get("authors", []),
                "genres": saved.get("genres", []),
                "chapters": len(chapter_numbers),
                "highest_contiguous": highest_contiguous_whole_chapter(chapter_numbers),
                "latest_chapter": format_chapter_number(latest) if latest is not None else "",
                "chapter_numbers": chapter_numbers,
                "next_update": build_update_chapters_arg(chapter_numbers),
                # Which kinds an update should fetch, chapter first (grep
                # _build_update_children in aio-dl.py). A volume-only series
                # has no chapter entry here, so it never gets a chapter update.
                "tracked_kinds": [k for k in (CHAPTER, VOLUME) if k in tracked],
                "volumes": len(volume_numbers),
                "volume_numbers": volume_numbers,
                "next_volume_update": build_update_chapters_arg(volume_numbers),
                "volume_format": volume_view["format"] or _volume_format_from_files(folder, vol_base),
                "volume_language": volume_view["language"],
                "files": len(book_files),
                "size": total_size,
                "cover": cover_path,
                "primary_book": book_files[0] if book_files else "",
            }
        )

    return entries


def to_jsonable(value):
    if isinstance(value, Decimal):
        return format_chapter_number(value)
    if isinstance(value, set):
        return sorted((to_jsonable(item) for item in value), key=str)
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    return value
