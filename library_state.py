from __future__ import annotations

import json
import os
import re
import zipfile
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Set


SAVED_PARAMS_FILE = "download_params.json"
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


def _extract_cover_from_zip(book_path: str, folder: str) -> Optional[str]:
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
    return _write_cover_file(folder, ext, data)


def _extract_cover_from_raw_images(folder: str) -> Optional[str]:
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
        ext = os.path.splitext(image_path)[1].lower() or ".jpg"
        try:
            with open(image_path, "rb") as handle:
                return _write_cover_file(folder, ext, handle.read())
        except OSError:
            continue

    return None


def find_cover_path(folder: str) -> Optional[str]:
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
        cover = _extract_cover_from_zip(book_path, folder)
        if cover:
            return cover

    return _extract_cover_from_raw_images(folder)


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

    books.sort(
        key=lambda item: (
            1 if CHAPTER_FILE_RE.search(os.path.basename(item[0])) else 0,
            -item[1],
            os.path.basename(item[0]).lower(),
        )
    )
    return [path for path, _mtime in books]


def scan_library(root: str) -> List[Dict]:
    entries: List[Dict] = []
    if not os.path.isdir(root):
        return entries

    for entry in sorted(os.listdir(root)):
        folder = os.path.join(root, entry)
        if not os.path.isdir(folder) or entry.startswith("."):
            continue

        has_params, params = load_saved_params(folder)
        chapter_numbers = scan_downloaded_chapters(folder)
        latest = max(chapter_numbers) if chapter_numbers else None

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
                "has_params": has_params,
                "params": params,
                "url": params.get("url", ""),
                "format": params.get("format", "?"),
                "chapters": len(chapter_numbers),
                "highest_contiguous": highest_contiguous_whole_chapter(chapter_numbers),
                "latest_chapter": format_chapter_number(latest) if latest is not None else "",
                "chapter_numbers": chapter_numbers,
                "next_update": build_update_chapters_arg(chapter_numbers),
                "files": len(book_files),
                "size": total_size,
                "cover": cover_path,
                "primary_book": book_files[0] if book_files else "",
            }
        )

    return entries
