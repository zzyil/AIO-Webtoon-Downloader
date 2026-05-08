"""Per-image probe-data calibration script for the v4 quality scorer.

Phase H1c (2026-05-08): empirical validation step before locking in the
``_bpp_decode_quality`` curve constants in sites/search_orchestrator.py.
The current starting points are working hypotheses, not measurements:

  grayscale: (bpp - 0.03) / 0.20
  color:     (bpp - 0.08) / 0.40

This script samples real probe data per-image (not aggregated) across a
fixed test set of sources, dumps per-page bpp + grayscale flags, and
prints ASCII histograms so the curve constants can be fitted to actual
data instead of guesses.

Cross-file:
  - Reuses sites/base.py:_pick_sample_indices,
    sites/<handler>.py:fetch_comic_context / get_chapters / get_chapter_images,
    sites/search_orchestrator.py:_score_image_blob (which now emits per-image
    bpp + is_grayscale in metadata as of Phase H3 helpers).
  - Doesn't touch the long-term cache at ~/.aio-dl/cache/img_quality.json;
    its own output goes to _quality_calibration.json next to this script.

Usage:
    python calibrate_quality_probe.py
    # or override the test list:
    python calibrate_quality_probe.py --series-list my_series.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import requests

try:
    import cloudscraper
except Exception:
    cloudscraper = None

from sites import get_handler_by_name
from sites.base import SearchHit
from sites.search_orchestrator import _score_image_blob


# Default test list. Mix of atsumaru (color webtoon) + MangaFire + MangaDex
# + others. User can override via --series-list <file.json>.
DEFAULT_SERIES = [
    # atsumaru — heavily-lossy color webtoon (the canonical "low-quality"
    # baseline used throughout this codebase's Phase G/H work).
    ("atsumaru", "https://atsu.moe/manga/RNwbs"),  # Talentless Nana
    # MangaFire — JPEG, typically q=85 territory.
    ("mangafire", "https://mangafire.to/manga/talentless-nana.nzmj"),
    # User may add more entries — at least 3-5 series across 3+ sources
    # gives the histograms enough data to validate or recalibrate.
]


def _build_scraper():
    """Mirror of aio-dl.py:main()'s scraper construction. Single-source
    dependency — change there too if the policy moves."""
    if cloudscraper is not None and sys.version_info >= (3, 7):
        try:
            return cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "darwin", "mobile": False}
            )
        except Exception:
            pass
    return requests.Session()


def _make_request(url, scraper):
    """Fast-fail request shim. The orchestrator's own version (in
    aio_search_cli.py) does retries + cooldown coordination; for a
    one-shot calibration we just want the single request to either
    succeed quickly or fail honestly."""
    return scraper.get(url, timeout=15)


def _pick_representative_chapter(chapters: List[Dict]) -> Optional[Dict]:
    """Same heuristic the production probe uses: prefer the median-index
    chapter (avoids cover-only chapter-0 quirks and avoids the very
    last chapter which can be a partial / TBA placeholder).
    """
    if not chapters:
        return None
    return chapters[len(chapters) // 2]


def _pick_sample_indices(n: int) -> List[int]:
    """5 evenly-spaced indices: [0, n//4, n//2, 3n//4, n-1] dedup'd.
    For n < 5 returns all indices. Same shape as
    sites/base.py:_pick_sample_indices."""
    if n <= 0:
        return []
    if n < 5:
        return list(range(n))
    return sorted(set([0, n // 4, n // 2, (3 * n) // 4, n - 1]))


def _fetch_image_bytes(handler, image_item, scraper) -> Optional[bytes]:
    """Probe an image and return its bytes, or None on failure. Handlers
    can return either a URL string or a {'data': bytes} dict for inline
    blobs (rare; some sites embed first page as data: URI). Mirrors the
    logic in BaseSiteHandler._fetch_probe_item_bytes."""
    if isinstance(image_item, dict):
        if image_item.get("data"):
            data = image_item["data"]
            return data if isinstance(data, (bytes, bytearray)) else None
        url = image_item.get("url")
    else:
        url = image_item
    if not url or not isinstance(url, str):
        return None
    try:
        r = scraper.get(url, timeout=20)
        if r.status_code >= 400:
            return None
        return r.content
    except Exception:
        return None


def probe_series(site: str, url: str) -> List[Dict]:
    """Run the probe pipeline against (site, url) and return per-image
    metadata for the 5 sampled pages. Each entry: {site, series_url,
    chapter_label, page_index, width, height, format, bpp, is_grayscale,
    decode_quality_v4} — enough to plot histograms and fit curves.

    Failures are silently absorbed (the calibration set will be sparse
    if a site is down today, but that's diagnostic in itself).
    """
    handler = get_handler_by_name(site)
    if handler is None:
        print(f"  [{site}] handler not found", file=sys.stderr)
        return []
    scraper = _build_scraper()
    try:
        handler.configure_session(scraper, argparse.Namespace(cookies=""))
    except Exception:
        pass
    try:
        ctx = handler.fetch_comic_context(url, scraper, _make_request)
    except Exception as exc:
        print(f"  [{site}] fetch_comic_context failed: {exc}", file=sys.stderr)
        return []
    try:
        chapters = handler.get_chapters(ctx, scraper, "en", _make_request)
    except Exception as exc:
        print(f"  [{site}] get_chapters failed: {exc}", file=sys.stderr)
        return []
    chapter = _pick_representative_chapter(chapters)
    if chapter is None:
        print(f"  [{site}] no chapters returned", file=sys.stderr)
        return []
    chapter_label = chapter.get("chap") or chapter.get("title") or "<unknown>"
    try:
        items = handler.get_chapter_images(chapter, scraper, _make_request)
    except Exception as exc:
        print(f"  [{site}] get_chapter_images failed: {exc}", file=sys.stderr)
        return []
    if not items:
        return []
    indices = _pick_sample_indices(len(items))
    out: List[Dict] = []
    for idx in indices:
        blob = _fetch_image_bytes(handler, items[idx], scraper)
        if not blob:
            continue
        result = _score_image_blob(blob)
        if result is None:
            continue
        score, metadata = result
        out.append({
            "site": site,
            "series_url": url,
            "chapter_label": str(chapter_label),
            "page_index": idx,
            "page_total": len(items),
            "width": metadata["width"],
            "height": metadata["height"],
            "format": metadata["format"],
            "size_bytes": metadata["size_bytes"],
            "bpp": metadata.get("bpp"),
            "is_grayscale": metadata.get("is_grayscale"),
            "outlier": metadata.get("outlier"),
            "decode_quality_v4": round(score, 4),
        })
    return out


def histogram(values: List[float], bins: List[Tuple[float, float]]) -> List[int]:
    """Bucket a list of floats into [lo, hi) buckets. Last bucket is
    closed on both sides to avoid losing the maximum."""
    counts = [0] * len(bins)
    for v in values:
        for i, (lo, hi) in enumerate(bins):
            if i == len(bins) - 1:
                if lo <= v <= hi:
                    counts[i] += 1
                    break
            elif lo <= v < hi:
                counts[i] += 1
                break
    return counts


def render_histogram(values: List[float], label: str) -> None:
    """ASCII histogram for bpp values. Bins span 0.00..0.60 in 0.05
    steps (covers the practical range for manga images)."""
    if not values:
        print(f"  {label}: (no data)")
        return
    bins = [(round(0.05 * i, 2), round(0.05 * (i + 1), 2)) for i in range(12)]
    counts = histogram(values, bins)
    n = len(values)
    mn, mx = min(values), max(values)
    median = sorted(values)[len(values) // 2]
    print(f"  {label} (n={n}, min={mn:.3f}, median={median:.3f}, max={mx:.3f})")
    bar_max = max(counts) or 1
    for (lo, hi), c in zip(bins, counts):
        bar = "█" * int(40 * c / bar_max)
        print(f"    {lo:.2f}-{hi:.2f} | {bar:<40} {c}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--series-list",
        default=None,
        help="Optional JSON file with [[site, url], ...]. If omitted, uses "
        "the DEFAULT_SERIES list at the top of this script.",
    )
    parser.add_argument(
        "--out",
        default="_quality_calibration.json",
        help="Output path for per-image probe data (default: _quality_calibration.json)",
    )
    args = parser.parse_args()

    series_list: List[Tuple[str, str]] = DEFAULT_SERIES
    if args.series_list:
        with open(args.series_list, "r", encoding="utf-8") as f:
            data = json.load(f)
        series_list = [(s, u) for s, u in data]

    print(f"[*] Probing {len(series_list)} series across {len(set(s for s, _ in series_list))} sources...")
    all_records: List[Dict] = []
    for site, url in series_list:
        print(f"  [{site}] {url}")
        t0 = time.time()
        records = probe_series(site, url)
        print(f"    → {len(records)} samples in {time.time() - t0:.1f}s")
        all_records.extend(records)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(all_records, f, indent=2)
    print(f"[*] Wrote {len(all_records)} records to {args.out}")

    # Group by (site, content_type) and render histograms.
    print()
    print("=" * 70)
    print("HISTOGRAMS — bpp distribution per source × content type")
    print("=" * 70)
    grouped: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    for rec in all_records:
        if rec.get("bpp") is None:
            continue
        ct = "grayscale" if rec.get("is_grayscale") else "color"
        grouped[(rec["site"], ct)].append(rec["bpp"])

    for (site, ct), values in sorted(grouped.items()):
        render_histogram(values, f"{site} / {ct}")
        print()

    # Suggested curve constants based on observed data: floor at 5th
    # percentile of the worst-quality source's distribution, ceiling at
    # 95th percentile of the best-quality source's distribution. The
    # numbers below are illustrative — review the histograms manually
    # before changing the production constants.
    print("=" * 70)
    print("CURVE-CONSTANT SUGGESTIONS")
    print("=" * 70)
    print("Inspect the histograms above. The bpp curve should:")
    print("  - floor at ~5th percentile of poorest source (= 0.0 quality)")
    print("  - ceiling at ~95th percentile of best source (= 1.0 quality)")
    print()
    for ct in ("color", "grayscale"):
        all_vals = sorted(
            v for (s, c), values in grouped.items() if c == ct for v in values
        )
        if not all_vals:
            continue
        p5 = all_vals[max(0, len(all_vals) * 5 // 100)]
        p95 = all_vals[min(len(all_vals) - 1, len(all_vals) * 95 // 100)]
        floor = round(p5, 3)
        spread = round(p95 - p5, 3)
        print(f"  {ct}: suggested floor={floor:.3f}, spread={spread:.3f}")
        print(f"    → curve: (bpp - {floor:.3f}) / {spread:.3f}")


if __name__ == "__main__":
    main()
