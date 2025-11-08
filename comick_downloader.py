#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ------------------------------------------------------------------
# Multi-site comic downloader  →  PDF, EPUB, or CBZ
# -----------------------------------------------------------
import argparse
import glob
import json
import math
import os
import re
import shutil
import sys
import time
import textwrap
import xml.sax.saxutils
import zipfile
from typing import Any, Dict, List, Optional

from sites import get_handler_by_name, get_handler_for_url
from sites.base import SiteComicContext

# cloudscraper is optional; fall back to requests.Session if unavailable
try:
    import cloudscraper  # type: ignore
except Exception:  # pragma: no cover
    cloudscraper = None

import requests

from PIL import Image, ImageDraw, ImageFont

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
    try:
        r = scraper.get(url)
        # Some sites (like madarascans.com) return 403 but still serve content
        # Only fail if we got a real error (4xx/5xx) AND no content
        if r.status_code >= 400:
            if not r.text or len(r.text) < 100:
                r.raise_for_status()
            # Otherwise, log but continue (it's likely Cloudflare sending content with 403)
            log_verbose(f"  Warning: Got status {r.status_code} but response has content, continuing...")
        return r
    except requests.exceptions.RequestException as e:
        sys.exit(f"Request failed: {e}")


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
def _try_download_url(url, pth, name, scraper, max_retries, retry_delay):
    """Attempts to download a single URL with retries. Returns path or None."""
    for attempt in range(max_retries):
        try:
            r = scraper.get(url, stream=True, timeout=30)
            r.raise_for_status()
            with open(pth, "wb") as fh:
                for chunk in r.iter_content(8192):
                    fh.write(chunk)
            return pth  # Success
        except requests.exceptions.RequestException as e:
            log_verbose(
                f"  Warning: Attempt {attempt + 1}/{max_retries} failed for {os.path.basename(name)} ({os.path.basename(url)}): {e}"
            )
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
    return None


def dl_image(url: str, folder: str, name: str, scraper) -> str:
    """
    Downloads an image using a sophisticated fallback chain.
    Returns the file path on success, or None on failure.
    """
    max_retries = 2
    retry_delay = 0.5  # seconds
    extensions_to_try = [".webp", ".png", ".jpg", ".jpeg", ".avif"]

    os.makedirs(folder, exist_ok=True)
    pth = os.path.join(folder, name)

    # 1. Generate the list of potential URLs to try
    urls_to_try = []
    base_url, original_ext = os.path.splitext(url)

    # Add original URL and its extension variants
    urls_to_try.append(url)
    for ext in extensions_to_try:
        urls_to_try.append(base_url + ext)

    # Add '-m' variant and its extension variants
    modified_base_url = base_url + "-m"
    urls_to_try.append(modified_base_url + original_ext)
    for ext in extensions_to_try:
        urls_to_try.append(modified_base_url + ext)

    # De-duplicate the list while preserving order
    unique_urls_to_try = list(dict.fromkeys(urls_to_try))

    # 2. Loop through the generated URLs and attempt to download
    has_failed_a_variant = False
    for attempt_url in unique_urls_to_try:
        if has_failed_a_variant:
            log_verbose(f"  Trying next variant: {os.path.basename(attempt_url)}")
        else:
            log_debug(f"  Trying URL variant: {os.path.basename(attempt_url)}")

        if _try_download_url(
            attempt_url, pth, name, scraper, max_retries, retry_delay
        ):
            success_message = f"  Successfully downloaded {os.path.basename(name)} using this variant."
            if has_failed_a_variant:
                # If in a retry scenario, show success in verbose mode.
                log_verbose(success_message)
            else:
                # Otherwise, only show success in debug mode.
                log_debug(success_message)
            return pth  # Success!
        else:
            has_failed_a_variant = True

    # 3. If all attempts failed
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
    """
    final_pages = []
    page_buffer = []
    buffer_height = 0

    for i, path in enumerate(input_paths):
        try:
            current_image = Image.open(path).convert("RGB")
            if current_image.width != target_w:
                scale = target_w / current_image.width
                current_image = current_image.resize(
                    (target_w, int(current_image.height * scale)),
                    Image.LANCZOS,
                )
        except Exception as e:
            print(f"  Warning: Skipping corrupted image {path}: {e}")
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
) -> List[str]:
    """Saves a list of final PIL images to disk."""
    os.makedirs(output_dir, exist_ok=True)
    output_paths = []
    log_verbose(f"  Saving {len(images)} final pages...")
    for i, img in enumerate(images):
        out_path = os.path.join(output_dir, f"{prefix}_{i+1:04d}.jpg")
        img.save(out_path, optimize=True, quality=quality)
        output_paths.append(out_path)
        log_debug(f"    Saved -> {os.path.basename(out_path)}")
    return output_paths


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
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, image_path in enumerate(slices):
            arcname = f"{i:04d}{os.path.splitext(image_path)[1]}"
            zf.write(image_path, arcname)
        zf.writestr("ComicInfo.xml", xml_content)
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
                zf.write(file_path, arcname)

    shutil.rmtree(temp_dir)
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
    part_suffix = f"Ch_{start_chap}-{end_chap}"
    part_filename = f"{base_filename}_{part_suffix}"
    out_dir = "comics"
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
        cbz_images = [
            item["path"]
            for item in book_content
            if item.get("type") == "image"
        ]
        build_cbz(
            cbz_images,
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
    """Creates a dictionary of parameters that affect image processing."""
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


# -----------------------------------------------------------
# main
# -----------------------------------------------------------
def main():
    p = argparse.ArgumentParser("comic downloader")
    p.add_argument("comic_url")
    p.add_argument(
        "--site",
        type=str,
        default=None,
        help="Explicitly select the site handler (auto-detected by URL when omitted).",
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
    args = p.parse_args()

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

    def sanitize_filename(name):
        return re.sub(r'[\\/*?:"<>|]', "", name).replace(" ", "_")

    safe_title = sanitize_filename(title)
    safe_site = sanitize_filename(handler.name)
    base_filename = f"{safe_title}_{safe_site}" if safe_site else safe_title
    if args.group:
        safe_group = sanitize_filename("_".join(args.group))
        base_filename = f"{base_filename}_{safe_group}"

    pool = handler.get_chapters(context, scraper, args.language, make_request)

    # --- Chapter Selection Logic ---
    log_verbose("Filtering chapters based on preferences...")

    # 1. Group all available chapter versions by chapter number
    chapters_by_num = {}
    for ch in pool:
        num_str = ch.get("chap")
        if num_str is None:
            continue
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
    # --- End of Chapter Selection Logic ---

    out_dir = "comics"
    os.makedirs(out_dir, exist_ok=True)

    resume_mode = False
    params_path = os.path.join(main_tmp_dir, "run_params.json")
    current_params = get_processing_params(args, width, aspect_ratio_str)

    if os.path.isdir(main_tmp_dir):
        print("Temporary directory found. Checking for resume compatibility...")
        if os.path.exists(params_path):
            try:
                with open(params_path, "r") as f:
                    old_params = json.load(f)
                if old_params == current_params:
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

    current_book_content = []
    current_book_chapters = []
    current_book_scan_groups = set()
    current_book_size = 0
    current_epub_markers = []

    original_cover_path = None
    if args.format in ["epub", "cbz"]:
        cover_url = None
        if context.soup:
            cover_tag = context.soup.find("meta", property="og:image")
            if cover_tag and cover_tag.get("content"):
                cover_url = cover_tag["content"]
        if not cover_url:
            cover_url = comic_data.get("cover") or comic_data.get("thumb")
        if cover_url:
            original_cover_path = dl_image(
                cover_url, main_tmp_dir, "cover_orig.jpg", scraper
            )
            if args.format == "cbz" and original_cover_path:
                current_book_content.append(
                    {"type": "image", "path": original_cover_path}
                )
                current_book_size += os.path.getsize(original_cover_path)

    for ch in chapters:
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

        if resume_mode and os.path.exists(marker_path):
            print(f"\nChapter {n} (already processed, collecting files)")
            if args.format in {"epub", "pdf", "none"}:
                log_verbose(
                    "  Resume mode not supported for this format; re-processing."
                )
                rm_tree(tdir)
            else:
                if args.no_processing:
                    raw_images = glob.glob(os.path.join(tdir, f"{n}_*.jpg"))
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
                    source_images = sorted(
                        glob.glob(os.path.join(processed_tdir, "*.jpg"))
                    )

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
            if os.path.isdir(tdir):
                log_verbose(
                    f"  Found incomplete temporary directory for Ch {n}. Cleaning before re-download."
                )
                rm_tree(tdir)

            print(f"\nChapter {n} ({grp_name or 'No Group'})")
            media_entries = handler.get_chapter_images(
                ch, scraper, make_request
            ) or []
            raw_image_paths: List[str] = []
            text_blocks: List[Dict[str, Any]] = []
            page_counter = 1
            log_verbose(
                f"  Fetching {len(media_entries)} media item(s)..."
            )
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
                        ext = entry.get("extension") or ".jpg"
                        if not ext.startswith("."):
                            ext = "." + ext
                        custom_name = entry.get("name")
                        filename = (
                            custom_name
                            if custom_name
                            else f"{n}_{page_counter:04d}{ext}"
                        )
                        pth = os.path.join(tdir, filename)
                        with open(pth, "wb") as fh:
                            fh.write(blob)
                        raw_image_paths.append(pth)
                        page_counter += 1
                        continue

                full_url = entry if isinstance(entry, str) else entry.get("url")
                if not full_url:
                    continue
                filename = f"{n}_{page_counter:04d}.jpg"
                pth = dl_image(
                    full_url,
                    tdir,
                    filename,
                    scraper,
                )
                if pth:
                    raw_image_paths.append(pth)
                    page_counter += 1

            if not raw_image_paths and not text_blocks:
                print(
                    f"  Warning: No media downloaded for Chapter {n}. Skipping."
                )
                continue

            if args.keep_images and raw_image_paths:
                dest_dir = os.path.join(out_dir, safe_title, f"Chapter_{n}")
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

            os.makedirs(processed_tdir, exist_ok=True)

            chapter_content = []

            processed_page_images: List[str] = []
            if raw_image_paths:
                if args.no_processing:
                    processed_page_images = list(raw_image_paths)
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
                    else:
                        pages_in_memory = resize_chapter_images(
                            raw_image_paths, width
                        )

                    log_verbose(f"  Applying {args.scaling}% scaling...")
                    scaled_images_in_mem = [
                        img.resize(
                            (
                                int(img.width * scale_factor),
                                int(img.height * scale_factor),
                            ),
                            Image.LANCZOS,
                        )
                        for img in pages_in_memory
                    ]

                    images_to_save = scaled_images_in_mem
                    if (
                        args.scaling < 100
                        and args.format in ["epub", "cbz"]
                        and recombine_target_height > 0
                    ):
                        images_to_save = recombine_scaled_images(
                            scaled_images_in_mem, recombine_target_height
                        )

                    processed_page_images = save_final_images(
                        images_to_save, processed_tdir, f"p_{n}", args.quality
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

                chapter_content = [
                    {"type": "image", "path": p} for p in processed_page_images
                ]
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
                    sheets = [
                        Image.open(p).convert("RGB")
                        for p in processed_page_images
                    ]
                    if sheets:
                        sheets[0].save(
                            image_pdf_path,
                            save_all=True,
                            append_images=sheets[1:],
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
                    if len(pdf_parts) == 1:
                        final_pdf_path = pdf_parts[0]
                    else:
                        final_pdf_path = os.path.join(
                            main_tmp_dir, f"{base_filename}_Ch_{n}.pdf"
                        )
                        merge_pdf_files(pdf_parts, final_pdf_path, None)
                        for part_path in pdf_parts:
                            if part_path != final_pdf_path:
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

        if not chapter_content:
            continue

        if args.keep_chapters:
            ch_suffix = f"Ch_{n}"
            ch_filename = f"{base_filename}_{ch_suffix}.{args.format}"
            ch_out_path = os.path.join(out_dir, ch_filename)
            ch_title = f"{title} ({ch_suffix})"
            log_verbose(f"  Saving individual chapter file...")

            if args.format == "epub":
                chapter_marker = [{"ch": ch, "page_index": 0}]
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
                cbz_images = [
                    item["path"]
                    for item in chapter_content
                    if item.get("type") == "image"
                ]
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
                    shutil.copy(chapter_content[0]["path"], ch_out_path)
                print(f"PDF Chapter saved → {os.path.basename(ch_out_path)}")

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

        if args.format == "epub":
            start_page_index = _epub_page_count(current_book_content)
        current_book_content.extend(chapter_content)
        current_book_chapters.append(ch)
        if grp_name:
            current_book_scan_groups.add(grp_name)
        current_book_size += chapter_content_size
        if args.format == "epub" and _epub_page_count(chapter_content) > 0:
            current_epub_markers.append(
                {"ch": ch, "page_index": start_page_index}
            )

    if current_book_content:
        if args.format == "none":
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
                cbz_images_all = [
                    item["path"]
                    for item in current_book_content
                    if item.get("type") == "image"
                ]
                build_cbz(
                    cbz_images_all,
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

    if not args.no_cleanup:
        rm_tree(main_tmp_dir)
        print("\nDone.")
    else:
        print(f"\nDone. Temporary files kept at: {main_tmp_dir}")


if __name__ == "__main__":
    main()
