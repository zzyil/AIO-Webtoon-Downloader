"""Image-format sniffing + atomic-finalize helpers, shared between aio-dl.py
and per-site handlers that implement their own image fetcher.

What this module owns:
  - Magic-byte detection for JPEG / PNG / GIF / WebP / AVIF / HEIC.
  - Content-Type fallback when magic is ambiguous.
  - `IMAGE_ACCEPT` / `IMAGE_ACCEPT_HEADERS`: the request-side counterpart —
    the Accept header an image fetch must send so content-negotiating hosts
    return the image and not an HTML wrapper page. See its comment below.
  - Header-only pixel-dimension sniffing (`sniff_image_dimensions`) + a
    `looks_like_real_image()` validity predicate that rescues legitimately tiny
    images from the download/probe byte-size gate (see that function's docstring)
    while rejecting HTML/JSON error documents served under an image URL.
  - `finalize_pending_image()`: validate + atomic-rename a `.pending_<base>`
    tempfile to `<folder>/<base><ext>` once bytes have landed.

What reads from it:
  - `aio-dl.py:dl_image` (the main download path) — uses both helpers.
  - `aio-dl.py:_start_image_prefetch._worker` and Phase 1/2 binary classification.
  - `sites/base.py:BaseSiteHandler.fast_download_images` (the shared curl_cffi
    async path; mangafire/linewebtoon/etc. inherit it) — uses
    `finalize_pending_image` per page and `looks_like_real_image` as its 200-OK
    body gate; the quality-probe + cover fetchers use `looks_like_real_image` too.

Why a separate module: `aio-dl.py` is at the top of the import graph (it
imports from `sites/`); `sites/mangafire.py` cannot import from `aio-dl.py`
without a circular dep. Pulling the helpers out into a leaf module is the
minimum-blast-radius refactor.

Originally lived in aio-dl.py at lines 808-881 (Phase A, 2026-05-07). Module
extracted 2026-05-09 to share with MangaFire's fast download path.
"""
from __future__ import annotations

import os
import struct
from typing import Optional, Tuple

# Magic-byte prefixes. Hex-readable comments inline.
JPEG_MAGIC = b"\xff\xd8"           # SOI marker
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"   # ISO 15948 §5.2 file signature
GIF_MAGIC = b"GIF8"                # both GIF87a and GIF89a
# WebP/AVIF/HEIC use ISO BMFF / RIFF containers — checked via byte ranges.


# --- Request side: how to ASK for an image ----------------------------------
# Byte-for-byte what Chrome sends on an <img> subresource request, as opposed to
# the document Accept a session default carries. This is NOT cosmetic
# fingerprinting — some hosts content-negotiate on it and serve a *different
# resource* for the same URL:
#
#   WordPress.com (`<sub>.files.wordpress.com/...jpg`, and the
#   `<sub>.wordpress.com/wp-content/uploads/...` form it redirects to) answers
#   an image URL with `200 text/html` + a ~19 KB attachment WRAPPER PAGE
#   (`x-orig-src: 0_wrapper`) whenever text/html outranks image/* in Accept, and
#   with the real JPEG (`x-orig-src: 01_mogdir`) otherwise. Both responses are
#   200, and the response carries `Vary: Accept`.
#
# cloudscraper seeds every session with the *document* Accept
# (`text/html,...;q=0.9,image/webp,image/apng,*/*;q=0.8`), and curl_cffi's
# `impersonate` does the same — so before this constant existed, every image
# fetch in the tree asked for HTML and WordPress-hosted chapters (manhuaplus'
# entire pre-~ch.1000 back catalogue) downloaded 19 KB of markup per page.
# `looks_like_real_image` below correctly rejected them, which is why the
# symptom read as a dead host rather than a bad request.
#
# Pass PER REQUEST, never onto `session.headers` — the same session fetches
# HTML pages, and a session-level image Accept would break those instead.
# Callers: aio-dl.py:_try_download_url, sites/base.py (_fast_dl_build_headers,
# _fetch_probe_item_bytes_ex, _probe_cover_image), sites/mangadex.py
# (_fetch_image_blob). grep IMAGE_ACCEPT.
IMAGE_ACCEPT = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"

# Ready-made dict for `requests`/`curl_cffi` `headers=` kwargs. Module-level and
# shared, so treat it as read-only: `dict(IMAGE_ACCEPT_HEADERS)` before mutating.
IMAGE_ACCEPT_HEADERS = {"Accept": IMAGE_ACCEPT}


def content_type_to_ext(content_type: str) -> Optional[str]:
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


def image_magic_extension(head: bytes) -> Optional[str]:
    """Extension implied by `head`'s magic bytes, or None when no known raster
    signature matches.

    Split out of `sniff_image_extension` so callers can ask "are these bytes
    RECOGNIZABLY an image?" — `sniff_image_extension` always returns something
    (`.jpg` fallback) and therefore can never answer that question. Both share
    this one magic table; `sniff_image_extension`'s return contract is
    unchanged. Cross-file: `looks_like_real_image` below is the only consumer
    in-tree (grep image_magic_extension)."""
    if not head:
        return None
    if head.startswith(JPEG_MAGIC):
        return ".jpg"
    if head.startswith(PNG_MAGIC):
        return ".png"
    if head.startswith(GIF_MAGIC):
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
    return None


def sniff_image_extension(head: bytes, content_type: Optional[str] = None) -> str:
    """Return the most accurate file extension (with leading dot) for an image
    given its first ≥12 bytes and an optional Content-Type. Magic bytes are
    primary; Content-Type is consulted only when magic is ambiguous. Falls
    back to `.jpg` so callers always get a usable extension (matches prior
    blanket-`.jpg` behavior for unknown content) — which is exactly why it must
    NOT be used as a validity test; use `looks_like_real_image` for that."""
    magic = image_magic_extension(head)
    if magic:
        return magic
    fallback = content_type_to_ext(
        (content_type or "").split(";", 1)[0]
    )
    return fallback or ".jpg"


# --- Dimension sniffing + image-validity predicate --------------------------
# WHY this exists: the fast-download path and the search-quality probes used to
# reject any HTTP-200 body under _MIN_IMAGE_BYTES as junk. That false-positives
# on legitimately tiny images — an 800x40 LINE-Webtoon divider bar compresses to
# ~128 bytes, and one such page tripping the gate aborted a whole 216-chapter
# run (bench/webtoonCanvasShelterLogs.md, Shelter ch.45: 50/51 pages, host
# swebtoon-phinf, 3 futile inline retries, then FATAL). A valid, decodable image
# with sane dimensions is real regardless of byte size. Callers: grep
# looks_like_real_image across sites/base.py.
_MIN_IMAGE_BYTES = 256  # bodies >= this accept without a decode (prior behavior; zero-regression)


def sniff_image_dimensions(head: bytes) -> Optional[Tuple[int, int]]:
    """Best-effort (width, height) in pixels from an image's leading header
    bytes, WITHOUT decoding pixel data or importing Pillow. Returns None when
    the bytes are not a recognized raster image or the header is too short /
    malformed to parse.

    Formats: PNG, GIF, JPEG, WebP (VP8 / VP8L / VP8X), BMP — the set served on
    manga/webtoon image CDNs. AVIF/HEIC (ISO-BMFF) are intentionally NOT parsed:
    their container overhead means they never appear as sub-256-byte bodies, so
    the byte-size fast-accept in looks_like_real_image() already covers them.
    """
    try:
        n = len(head)
        if n < 10:  # shortest parseable header is GIF's 10 bytes
            return None
        # PNG: 8-byte signature, then the IHDR chunk — width/height are the
        # first two big-endian uint32s of its data (offsets 16 and 20).
        if head[:8] == PNG_MAGIC:
            if n >= 24 and head[12:16] == b"IHDR":
                w, h = struct.unpack(">II", head[16:24])
                return (int(w), int(h))
            return None
        # GIF: logical-screen width/height as little-endian uint16 at offset 6.
        if head[:4] == GIF_MAGIC:
            w, h = struct.unpack("<HH", head[6:10])
            return (int(w), int(h))
        # BMP: BITMAPINFOHEADER width/height as little-endian int32 at 18/22
        # (height may be negative for top-down bitmaps → abs()).
        if head[:2] == b"BM" and n >= 26:
            w, h = struct.unpack("<ii", head[18:26])
            return (abs(int(w)), abs(int(h)))
        # WebP: RIFF container; the fourcc at offset 12 selects the codec, each
        # of which packs the canvas dimensions differently.
        if n >= 30 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            fourcc = head[12:16]
            if fourcc == b"VP8 ":  # lossy keyframe: 14-bit dims after the 0x9d012a start code
                w = struct.unpack("<H", head[26:28])[0] & 0x3FFF
                h = struct.unpack("<H", head[28:30])[0] & 0x3FFF
                return (int(w), int(h))
            if fourcc == b"VP8L":  # lossless: 14-bit (w-1, h-1) packed after the 0x2f signature byte
                b0, b1, b2, b3 = head[21], head[22], head[23], head[24]
                w = (((b1 & 0x3F) << 8) | b0) + 1
                h = (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6)) + 1
                return (int(w), int(h))
            if fourcc == b"VP8X":  # extended: 24-bit (w-1, h-1) canvas size at offset 24
                w = (head[24] | (head[25] << 8) | (head[26] << 16)) + 1
                h = (head[27] | (head[28] << 8) | (head[29] << 16)) + 1
                return (int(w), int(h))
            return None
        # JPEG: walk marker segments to the first Start-Of-Frame (it carries dims).
        if head[:2] == JPEG_MAGIC:
            return _sniff_jpeg_dimensions(head)
    except Exception:
        return None
    return None


def _sniff_jpeg_dimensions(head: bytes) -> Optional[Tuple[int, int]]:
    """Scan JPEG marker segments for the Start-Of-Frame that carries the image
    dimensions (precision:1, height:2, width:2 after the segment length).
    Returns None if no SOF is reached within the available bytes (e.g. a body
    truncated before the frame header)."""
    n = len(head)
    i = 2  # skip the SOI marker (0xFFD8)
    while i + 9 <= n:
        if head[i] != 0xFF:
            i += 1
            continue
        marker = head[i + 1]
        if marker == 0xFF:  # padding fill bytes between segments
            i += 1
            continue
        # Standalone markers with no length payload: SOI/EOI, RSTn, TEM.
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", head[i + 2:i + 4])[0]
        # SOF0..SOF15 carry dimensions; exclude DHT(C4), JPG(C8), DAC(CC), which
        # share the 0xC0..0xCF range but are not frame headers.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h = struct.unpack(">H", head[i + 5:i + 7])[0]
            w = struct.unpack(">H", head[i + 7:i + 9])[0]
            return (int(w), int(h))
        if seg_len < 2:
            return None  # malformed length would loop forever; bail
        i += 2 + seg_len
    return None


# Prefixes that mark a body as a text document rather than a raster image. No
# raster signature starts with any of these (JPEG=0xFFD8, PNG=0x89, GIF='G',
# RIFF='R', BMP='B', ISO-BMFF has 'ftyp' at offset 4), so matching one is a
# zero-false-positive reject — which is what makes the size-independent
# rejection below safe.
_MARKUP_PREFIXES = (
    b"<!doctype", b"<html", b"<head", b"<body", b"<?xml", b"{", b"[",
)
_ASCII_SPACE = b" \t\r\n\f\v"

# Bare Content-Types that promise a text document. A body carrying one of these
# AND no recognizable image bytes is junk regardless of size.
_NON_IMAGE_CONTENT_TYPES = frozenset({
    "application/json",
    "application/xml",
    "application/xhtml+xml",
})


def _looks_like_markup(head: bytes) -> bool:
    """Do these leading bytes open an HTML/XML/JSON document? Byte-oriented on
    purpose — a `.decode()` here would raise on real image bytes."""
    if not head:
        return False
    probe = head.lstrip(_ASCII_SPACE)
    # A BOM can precede the first tag. UTF-16 additionally interleaves NULs
    # between ASCII characters, so drop those before prefix-matching.
    for bom, utf16 in ((b"\xef\xbb\xbf", False), (b"\xff\xfe", True), (b"\xfe\xff", True)):
        if probe.startswith(bom):
            probe = probe[len(bom):]
            if utf16:
                probe = probe.replace(b"\x00", b"")
            probe = probe.lstrip(_ASCII_SPACE)
            break
    return probe[:16].lower().startswith(_MARKUP_PREFIXES)


def _is_non_image_content_type(content_type: Optional[str]) -> bool:
    bare = (content_type or "").split(";", 1)[0].strip().lower()
    if not bare:
        return False
    return bare.startswith("text/") or bare in _NON_IMAGE_CONTENT_TYPES


def _head_rejects_image(
    head: bytes, size: int, content_type: Optional[str] = None
) -> Optional[str]:
    """Head-only rejection shared by `looks_like_real_image` (whole body in
    memory) and `finalize_pending_image` (body on disk, only the head read).
    Returns a short reason, or None when the head gives no grounds to reject —
    the caller still applies its own size/dimension rules."""
    if size <= 0:
        return "empty body"
    if _looks_like_markup(head):
        return "markup"
    if _is_non_image_content_type(content_type):
        if image_magic_extension(head) is None and sniff_image_dimensions(head) is None:
            return "declared non-image"
    return None


def describe_invalid_image(
    head: bytes, content_type: Optional[str] = None, size: int = 0
) -> str:
    """One-line human reason for a rejected body, naming the Content-Type and
    byte size. Used verbatim in aio-dl.py:dl_image's per-page verbose log (grep
    "Rejected page")."""
    bare = (content_type or "").split(";", 1)[0].strip().lower()
    if bare:
        return f"server returned {bare} ({size} bytes), not an image"
    if _looks_like_markup(head):
        return f"server returned an HTML/JSON document ({size} bytes), not an image"
    return f"body is not a recognized image ({size} bytes)"


def looks_like_real_image(
    data: bytes,
    min_bytes: int = _MIN_IMAGE_BYTES,
    content_type: Optional[str] = None,
) -> bool:
    """Does `data` look like a real, downloadable image, as opposed to a CDN
    error stub, an HTML/JSON error body, or a 1x1 tracking pixel?

    `content_type` is optional and third so every existing positional caller
    (`looks_like_real_image(body)`, `(body, 512)`) is unaffected — grep
    looks_like_real_image across sites/base.py.

    Policy, in order:
      - Empty -> False.
      - Opens like HTML/XML/JSON -> False REGARDLESS OF SIZE. A dead host can
        answer 200 + `Content-Type: text/html` + a 19 KB error page for an
        image URL (manhuaplus' retired `*.files.wordpress.com` pages), and the
        old size-only fast-accept below waved those straight into the CBZ as
        `0001.jpg`.
      - Declared text/* or JSON/XML AND no recognizable image bytes -> False.
      - len >= min_bytes -> True. Preserves the historical `len(body) >= 256`
        accept threshold verbatim, so nothing that used to download is newly
        rejected (no regression on large/unusual formats we don't dimension-parse,
        e.g. AVIF/HEIC/JXL).
      - len < min_bytes -> True ONLY if the bytes decode (by header) to a
        recognized image larger than a single pixel. This rescues legitimately
        tiny images (divider bars, thin spacers) while still rejecting sub-256-byte
        junk: truncated bodies fail the format sniff and 1x1 tracking pixels fail
        the area check.

    See bench/webtoonCanvasShelterLogs.md for the tiny-divider run this rescues.
    """
    if not data:
        return False
    if _head_rejects_image(data[:64], len(data), content_type) is not None:
        return False
    if len(data) >= min_bytes:
        return True
    dims = sniff_image_dimensions(data)
    if dims is None:
        return False
    w, h = dims
    return (w * h) >= 2  # reject the 1x1 tracking-pixel / error-stub shape


def finalize_pending_image(
    pending_path: str,
    folder: str,
    base: str,
    content_type: Optional[str],
    *,
    validate: bool = True,
    on_reject=None,
) -> Optional[str]:
    """Validate a successfully-downloaded pending file, sniff its first bytes,
    atomic-rename it to `<folder>/<base><ext>`, and return the final path.

    Returns None — the established "this page failed" contract every caller
    already understands — when the pending file is missing, OR when `validate`
    and the bytes are not an image (in which case the pending file is DELETED
    so no half-written junk survives, and `on_reject(reason)` is invoked with a
    `describe_invalid_image` string). `on_reject` fires ONLY for a validation
    reject, so a caller can still distinguish that from a missing tempfile.

    `os.replace` is atomic on both POSIX and NT when source/dest share a
    volume — pending and final live in the same folder, so this is safe.
    Callers: aio-dl.py:dl_image (5 sites, grep _finalize_downloaded_image) and
    sites/base.py:fast_download_images."""
    if not os.path.exists(pending_path):
        return None
    try:
        # 64 bytes: every magic signature plus the markup sniff fit; WebP VP8X
        # dimensions need 30. Was 32, which could not see a BOM-prefixed
        # `<!doctype html>` past its leading whitespace.
        with open(pending_path, "rb") as fh:
            head = fh.read(64)
    except Exception:
        head = b""
    if validate:
        try:
            size = os.path.getsize(pending_path)
        except OSError:
            size = len(head)
        if not _pending_file_is_image(pending_path, head, size, content_type):
            if on_reject is not None:
                try:
                    on_reject(describe_invalid_image(head, content_type, size))
                except Exception:
                    pass
            try:
                os.remove(pending_path)
            except OSError:
                pass
            return None
    ext = sniff_image_extension(head, content_type)
    final_path = os.path.join(folder, base + ext)
    os.replace(pending_path, final_path)
    return final_path


def _pending_file_is_image(
    path: str, head: bytes, size: int, content_type: Optional[str]
) -> bool:
    """`looks_like_real_image`'s policy against a file we've only read the head
    of. Deliberately NOT `looks_like_real_image(head)`: a 64-byte head of a real
    900 KB JPEG is under min_bytes and its SOF may sit past byte 64, which would
    reject every large page on the site."""
    if _head_rejects_image(head, size, content_type) is not None:
        return False
    if size >= _MIN_IMAGE_BYTES:
        return True
    # Sub-256-byte body: the divider-bar rescue needs the whole file to
    # dimension-sniff. Bounded read — the file is smaller than _MIN_IMAGE_BYTES.
    try:
        with open(path, "rb") as fh:
            return looks_like_real_image(fh.read(_MIN_IMAGE_BYTES), content_type=content_type)
    except OSError:
        return False
