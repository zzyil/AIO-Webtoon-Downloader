"""WebP→PDF encoding comparison.

Compares ways of getting atsumaru's WebP chapter pages into a PDF without
quality loss or PDF bloat. Tests each option on real images, measures size,
speed, and quality, then projects 86-page chapter cost.

Approaches tested:
  A. raw_pixels_flate    — current behavior; decode WebP to RGB pixels,
                           zlib-compress, embed via /FlateDecode. Lossless.
  B. webp_native_filter  — embed the original WebP bytes with a custom
                           /Filter /WebP marker. PDF spec doesn't define
                           this filter, so verifies whether *any* common
                           reader (Adobe, Chrome PDFium, Edge, Firefox PDF.js,
                           SumatraPDF/MuPDF) renders it.
  C. jpeg_qN             — decode WebP, re-encode as JPEG quality N, embed
                           via /DCTDecode (universal PDF reader support).
                           Lossy but quality≥95 is generally imperceptible.
  D. jpeg2000            — decode WebP, encode as JPEG 2000 (JPX), embed
                           via /JPXDecode. PDF-spec-blessed, lossless or
                           visually-lossless modes available, slower
                           encode/decode.

Quality metric: PSNR (higher = better; >40 dB is "visually identical").
                SSIM (1.0 = identical; >0.97 is "indistinguishable").
"""
from __future__ import annotations

import io
import math
import os
import time
import zlib
from typing import Dict, List, Tuple

import numpy as np
import requests
from PIL import Image


# ---------------------------------------------------------------- #
# Sample fetch                                                     #
# ---------------------------------------------------------------- #

ATSUMARU_PAGES = [
    # Talentless Nana ch1 — diverse: title page, action, dialogue
    "https://atsu.moe/static/pages/RNwbs/HsFYtUMR/0.webp",
    "https://atsu.moe/static/pages/RNwbs/HsFYtUMR/10.webp",
    "https://atsu.moe/static/pages/RNwbs/HsFYtUMR/30.webp",
    "https://atsu.moe/static/pages/RNwbs/HsFYtUMR/50.webp",
    "https://atsu.moe/static/pages/RNwbs/HsFYtUMR/80.webp",
]


def fetch_samples(out_dir: str) -> List[str]:
    """Download the test pages once; reuse on re-runs."""
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    sess = requests.Session()
    sess.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    )
    sess.headers["Referer"] = "https://atsu.moe/"
    for url in ATSUMARU_PAGES:
        name = os.path.basename(url)
        path = os.path.join(out_dir, name)
        if not os.path.exists(path):
            r = sess.get(url, timeout=30)
            r.raise_for_status()
            with open(path, "wb") as f:
                f.write(r.content)
        paths.append(path)
    return paths


# ---------------------------------------------------------------- #
# Quality metrics                                                  #
# ---------------------------------------------------------------- #

def psnr(a: np.ndarray, b: np.ndarray) -> float:
    """Peak signal-to-noise ratio (dB). >40 dB = visually identical."""
    if a.shape != b.shape:
        raise ValueError("shape mismatch for PSNR")
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    if mse == 0:
        return float("inf")
    return 20.0 * math.log10(255.0 / math.sqrt(mse))


def ssim(a: np.ndarray, b: np.ndarray) -> float:
    """Single-scale SSIM (Wang 2004) on luminance.

    Manual implementation since scikit-image isn't installed. Uses 8x8
    block averaging with the standard k1=0.01, k2=0.03 constants.
    """
    if a.ndim == 3:
        a = (0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2])
    if b.ndim == 3:
        b = (0.299 * b[..., 0] + 0.587 * b[..., 1] + 0.114 * b[..., 2])
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    K1, K2, L = 0.01, 0.03, 255.0
    C1 = (K1 * L) ** 2
    C2 = (K2 * L) ** 2
    # block reduce by 8 with mean
    h, w = a.shape
    h, w = h - h % 8, w - w % 8
    a = a[:h, :w].reshape(h // 8, 8, w // 8, 8).mean(axis=(1, 3))
    b = b[:h, :w].reshape(h // 8, 8, w // 8, 8).mean(axis=(1, 3))
    mu_a = a.mean()
    mu_b = b.mean()
    var_a = a.var()
    var_b = b.var()
    cov_ab = ((a - mu_a) * (b - mu_b)).mean()
    num = (2 * mu_a * mu_b + C1) * (2 * cov_ab + C2)
    den = (mu_a ** 2 + mu_b ** 2 + C1) * (var_a + var_b + C2)
    return float(num / den)


# ---------------------------------------------------------------- #
# Encoders                                                         #
# ---------------------------------------------------------------- #

def encode_raw_flate(img: Image.Image) -> Tuple[bytes, str, str]:
    """Raw RGB pixels, zlib level 9. Lossless. Current behavior."""
    rgb = img.convert("RGB").tobytes()
    data = zlib.compress(rgb, 9)
    return data, "/FlateDecode", "/DeviceRGB"


def encode_webp_native(webp_path: str) -> Tuple[bytes, str, str]:
    """Pass the original WebP bytes through. Uses /Filter /WebP — not in
    the PDF spec; we're testing reader support."""
    with open(webp_path, "rb") as f:
        return f.read(), "/WebP", "/DeviceRGB"


def encode_jpeg(img: Image.Image, quality: int) -> Tuple[bytes, str, str]:
    """JPEG via /DCTDecode. Universally supported."""
    buf = io.BytesIO()
    img.convert("RGB").save(
        buf,
        format="JPEG",
        quality=quality,
        optimize=True,
        subsampling=2,  # 4:2:0 standard for photos; manga still ok
        progressive=False,
    )
    return buf.getvalue(), "/DCTDecode", "/DeviceRGB"


def encode_jpeg2000(img: Image.Image, quality_layers: int) -> Tuple[bytes, str, str]:
    """JPEG 2000 via /JPXDecode. quality_layers=1 ≈ visually-lossless,
    higher = more compression."""
    buf = io.BytesIO()
    img.convert("RGB").save(
        buf,
        format="JPEG2000",
        quality_mode="rates",
        quality_layers=[quality_layers],
        irreversible=True,
    )
    return buf.getvalue(), "/JPXDecode", "/DeviceRGB"


# ---------------------------------------------------------------- #
# Minimal PDF builder (mirror of aio-dl.py:_build_images_pdf)      #
# ---------------------------------------------------------------- #

def build_pdf(
    pages: List[Tuple[bytes, str, str, int, int]],
    out_path: str,
) -> int:
    """Write a one-image-per-page PDF. Each page tuple:
       (stream_bytes, filter, colorspace, width, height).
    Returns total file size."""
    objects: List[bytes | None] = [None]

    def reserve():
        objects.append(None)
        return len(objects) - 1

    def setobj(num, data):
        objects[num] = data

    catalog_obj = reserve()
    pages_obj = reserve()
    page_objs = []

    for stream, filt, cs, w, h in pages:
        img_obj = reserve()
        hdr = (
            f"<< /Type /XObject /Subtype /Image "
            f"/Width {w} /Height {h} "
            f"/ColorSpace {cs} "
            f"/BitsPerComponent 8 "
            f"/Filter {filt} "
            f"/Length {len(stream)} >>\nstream\n"
        ).encode("latin-1")
        setobj(img_obj, hdr + stream + b"\nendstream")

        content = f"q {w} 0 0 {h} 0 0 cm /Im0 Do Q".encode("latin-1")
        content_obj = reserve()
        setobj(
            content_obj,
            f"<< /Length {len(content)} >>\nstream\n".encode("latin-1")
            + content
            + b"\nendstream",
        )
        page_obj = reserve()
        setobj(
            page_obj,
            (
                f"<< /Type /Page /Parent {pages_obj} 0 R "
                f"/MediaBox [0 0 {w} {h}] "
                f"/Resources << /XObject << /Im0 {img_obj} 0 R >> >> "
                f"/Contents {content_obj} 0 R >>"
            ).encode("latin-1"),
        )
        page_objs.append(page_obj)

    kids = " ".join(f"{p} 0 R" for p in page_objs)
    setobj(
        pages_obj,
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_objs)} >>".encode("latin-1"),
    )
    setobj(
        catalog_obj,
        f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode("latin-1"),
    )

    with open(out_path, "wb") as f:
        f.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for idx, obj in enumerate(objects[1:], start=1):
            if obj is None:
                obj = b"<<>>"
            offsets.append(f.tell())
            f.write(f"{idx} 0 obj\n".encode("latin-1"))
            f.write(obj)
            f.write(b"\nendobj\n")
        xref_off = f.tell()
        f.write(f"xref\n0 {len(objects)}\n".encode("latin-1"))
        f.write(b"0000000000 65535 f \n")
        for off in offsets:
            f.write(f"{off:010d} 00000 n \n".encode("latin-1"))
        f.write(
            f"trailer\n<< /Size {len(objects)} /Root {catalog_obj} 0 R >>\n"
            f"startxref\n{xref_off}\n%%EOF\n".encode("latin-1")
        )

    return os.path.getsize(out_path)


# ---------------------------------------------------------------- #
# Per-image benchmark                                              #
# ---------------------------------------------------------------- #

def benchmark_one(webp_path: str) -> Dict:
    """Run every encoder on one WebP. Returns per-method timings + sizes
    + quality. Includes a single-page PDF size for each method."""
    img = Image.open(webp_path)
    img.load()
    w, h = img.size
    rgb = np.asarray(img.convert("RGB"))
    orig_size = os.path.getsize(webp_path)

    methods = {}

    def measure(name: str, encoder, *args):
        # Run twice — first warms PIL caches, second is the timed run
        encoder(*args)
        t0 = time.perf_counter()
        stream, filt, cs = encoder(*args)
        elapsed = time.perf_counter() - t0
        # Decode the stream back to verify quality vs original.
        if filt == "/DCTDecode":
            decoded = np.asarray(Image.open(io.BytesIO(stream)).convert("RGB"))
        elif filt == "/JPXDecode":
            decoded = np.asarray(Image.open(io.BytesIO(stream)).convert("RGB"))
        elif filt == "/FlateDecode":
            # Lossless — same as input
            decoded = rgb
        elif filt == "/WebP":
            # Original bytes, decode to compare; should match WebP roundtrip
            decoded = np.asarray(Image.open(io.BytesIO(stream)).convert("RGB"))
        else:
            decoded = rgb
        # Quality vs the canonical decoded WebP (rgb)
        p = psnr(rgb, decoded)
        s = ssim(rgb, decoded)
        # Single-page PDF
        pdf_path = os.path.join(
            os.path.dirname(webp_path),
            f"_pdf_{os.path.basename(webp_path)}_{name}.pdf",
        )
        pdf_size = build_pdf([(stream, filt, cs, w, h)], pdf_path)
        os.remove(pdf_path)
        methods[name] = {
            "stream_bytes": len(stream),
            "pdf_bytes": pdf_size,
            "encode_s": elapsed,
            "psnr_db": p,
            "ssim": s,
        }

    # Reference: raw WebP file size
    methods["webp_original_bytes"] = {
        "stream_bytes": orig_size,
        "pdf_bytes": None,
        "encode_s": 0.0,
        "psnr_db": float("inf"),
        "ssim": 1.0,
        "_note": "reference: original WebP file size",
    }

    measure("flate_lossless", encode_raw_flate, img)
    measure("webp_native", encode_webp_native, webp_path)
    for q in (85, 90, 95, 100):
        measure(f"jpeg_q{q}", encode_jpeg, img, q)
    measure("jp2000_visually_lossless", encode_jpeg2000, img, 5)
    measure("jp2000_high", encode_jpeg2000, img, 10)
    measure("jp2000_max", encode_jpeg2000, img, 1)

    return {"path": webp_path, "width": w, "height": h, "methods": methods}


# ---------------------------------------------------------------- #
# Multi-page PDF projections (86-page chapter scale)               #
# ---------------------------------------------------------------- #

def build_chapter_pdfs(webp_paths: List[str], out_dir: str) -> Dict[str, Dict]:
    """For methods that produce sensible PDFs, build 86-page replicas to
    measure end-to-end build cost + final file size at chapter scale."""
    # Replicate the 5 sample images to fill 86 pages (close enough — pages
    # are uniform-ish on atsumaru so this approximates real chapter cost)
    fill = (webp_paths * 18)[:86]

    def build_for(name: str, encoder_fn):
        t0 = time.perf_counter()
        pages = []
        for p in fill:
            img = Image.open(p)
            img.load()
            stream, filt, cs = encoder_fn(img, p)
            pages.append((stream, filt, cs, img.width, img.height))
        encode_t = time.perf_counter() - t0
        pdf_path = os.path.join(out_dir, f"chapter_{name}.pdf")
        t0 = time.perf_counter()
        size = build_pdf(pages, pdf_path)
        write_t = time.perf_counter() - t0
        return {
            "pdf_path": pdf_path,
            "pdf_bytes": size,
            "encode_s": encode_t,
            "write_s": write_t,
            "total_s": encode_t + write_t,
        }

    out = {}
    out["flate_lossless"] = build_for(
        "flate_lossless",
        lambda img, p: encode_raw_flate(img),
    )
    out["webp_native"] = build_for(
        "webp_native",
        lambda img, p: encode_webp_native(p),
    )
    for q in (85, 90, 95, 100):
        out[f"jpeg_q{q}"] = build_for(
            f"jpeg_q{q}",
            lambda img, p, q=q: encode_jpeg(img, q),
        )
    out["jp2000_vl"] = build_for(
        "jp2000_vl",
        lambda img, p: encode_jpeg2000(img, 5),
    )
    return out


# ---------------------------------------------------------------- #
# Main                                                             #
# ---------------------------------------------------------------- #

def fmt_bytes(n: float | None) -> str:
    if n is None:
        return "    -    "
    if n < 1024:
        return f"{n:>7d} B"
    if n < 1024 * 1024:
        return f"{n/1024:>6.1f} KB"
    return f"{n/(1024*1024):>6.2f} MB"


def main():
    work = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_webp_test")
    os.makedirs(work, exist_ok=True)
    print(f"[*] Workspace: {work}")
    paths = fetch_samples(work)
    print(f"[*] Fetched {len(paths)} sample images.")

    print()
    print("=" * 100)
    print("PER-IMAGE BENCHMARK (averaged across samples)")
    print("=" * 100)

    # Aggregate
    method_aggregate: Dict[str, List[Dict]] = {}
    for path in paths:
        result = benchmark_one(path)
        for name, m in result["methods"].items():
            method_aggregate.setdefault(name, []).append(m)

    # Print table
    print(
        f"\n{'method':<28} {'stream':>11} {'pdf/img':>11} {'encode':>9} "
        f"{'psnr (dB)':>10} {'ssim':>8}"
    )
    print("-" * 90)
    for name in [
        "webp_original_bytes",
        "flate_lossless",
        "webp_native",
        "jpeg_q85",
        "jpeg_q90",
        "jpeg_q95",
        "jpeg_q100",
        "jp2000_visually_lossless",
        "jp2000_high",
        "jp2000_max",
    ]:
        rows = method_aggregate[name]
        sb = sum(r["stream_bytes"] for r in rows) / len(rows)
        pdf_bs = [r["pdf_bytes"] for r in rows if r["pdf_bytes"] is not None]
        pb = sum(pdf_bs) / len(pdf_bs) if pdf_bs else None
        en = sum(r["encode_s"] for r in rows) / len(rows)
        ps = sum(r["psnr_db"] for r in rows) / len(rows)
        ss = sum(r["ssim"] for r in rows) / len(rows)
        print(
            f"{name:<28} {fmt_bytes(int(sb)):>11} {fmt_bytes(int(pb) if pb else None):>11} "
            f"{en*1000:>7.1f}ms {('inf' if math.isinf(ps) else f'{ps:>9.2f}'):>10} {ss:>8.4f}"
        )

    print()
    print("=" * 100)
    print("86-PAGE CHAPTER PDF PROJECTIONS")
    print("=" * 100)
    chap = build_chapter_pdfs(paths, work)
    print(
        f"\n{'method':<28} {'pdf size':>11} {'encode':>9} {'write':>9} {'total':>9}"
    )
    print("-" * 80)
    for name, info in chap.items():
        print(
            f"{name:<28} {fmt_bytes(info['pdf_bytes']):>11} "
            f"{info['encode_s']:>7.2f}s {info['write_s']:>7.2f}s {info['total_s']:>7.2f}s"
        )

    print()
    print("=" * 100)
    print("READER COMPATIBILITY NOTES")
    print("=" * 100)
    print("""
  - /DCTDecode (JPEG):    universal — every PDF reader.
  - /JPXDecode (JPEG2000): in the PDF spec since 1.5. Adobe Acrobat ✓,
                          Edge/Chrome PDFium ✓, Firefox PDF.js ✓ (since 2018),
                          MuPDF/SumatraPDF ✓. Safari ✓. Should work everywhere.
  - /FlateDecode raw:     universal — every PDF reader.
  - /WebP custom filter:  NOT in PDF 1.7 or 2.0 spec. Test the produced
                          chapter_webp_native.pdf in your readers manually
                          to confirm — likely shows nothing or an error.
""")
    print(f"[*] Test PDFs written to {work}/")
    print(f"[*] Open chapter_webp_native.pdf to verify reader support claim.")


if __name__ == "__main__":
    main()
