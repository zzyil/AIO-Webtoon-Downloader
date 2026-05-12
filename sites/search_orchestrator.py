"""Cross-site search orchestrator.

Owns: fan-out across all search-capable handlers, title-match scoring,
candidate dedupe, ranking. Phase 1a per the plan
(C:\\Users\\legoc\\.claude\\plans\\snappy-forging-waffle.md).

Reads:
  - sites.iter_search_capable_handlers() — handler registry filtered to those
    with an overridden search() method. Sites that fail to load this filter
    just don't participate in search; matches the "no reliability tracking"
    principle.
  - sites/quality_seed.json — per-site image-quality priors used ONLY as a
    tiebreaker when title-match scores fall within 0.10 of each other. The
    seed never adds to the title-match score.

Called by: aio_search_cli.run_search_mode (which is invoked by aio-dl.py
when --search is passed).

Cross-file:
  - SearchHit dataclass lives in sites/base.py.
  - The probe-failure cache hooks into make_request's _record_rate_limit so a
    timed-out host suppresses itself for 1h via the existing cooldown machinery
    (aio-dl.py:589, aio-dl.py:628). No parallel cooldown system.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .base import BaseSiteHandler, SearchHit


# --- Tunables -----------------------------------------------------------
# Title-match floor: hits below this rapidfuzz similarity are discarded.
# 0.55 keeps "Frieren" matching "Sousou no Frieren" via alt-titles but kicks
# out unrelated series that share a single word.
DEFAULT_MIN_MATCH = 0.55

# Tiebreaker window: when two sources for the same candidate score within
# this much on title-match, the quality_seed prior breaks the tie. Above this
# delta, title-match is fully decisive. See plan section "G. --search wired
# from day one (with seed-prior tiebreaker)".
TIEBREAKER_WINDOW = 0.10

# Per-site search timeout. Dead/unresponsive sites just take this long to
# self-select out per-run; combined with the probe-failure cache they cost
# zero on subsequent searches within the cache window.
DEFAULT_PER_SITE_TIMEOUT_S = 8.0

# Per-host probe-failure cache: after this many consecutive failures, suppress
# the host for PROBE_FAILURE_TTL_S via the existing _record_rate_limit
# coordinator entry. Self-healing — re-tries after the entry expires.
PROBE_FAILURE_THRESHOLD = 2
PROBE_FAILURE_TTL_S = 3600  # 1 hour

# Image-quality cache TTL (Phase 2). Covers/CDN policies change slowly; 30 days
# means we re-probe a (site, series) at most once a month under normal use.
IMG_QUALITY_TTL_S = 30 * 24 * 3600

# Chapter-image probe is heavier than cover probe (3-4 HTTP requests vs 1).
# We only run it for sites with seed_quality at or above this threshold —
# the long tail of Madara/MangaThemesia extras (default seed 0.50) gets
# cover probe to keep the total probe phase bounded. Sites with seed >= 0.65
# are the ones explicitly listed in sites/quality_seed.json — those are the
# candidates that actually compete for "best source", and the same threshold
# powers the --multi-source-quality-min default. Low-seed sites' image scores
# only matter for tiebreaking within the title-match window during search
# ranking; cover-based scores are good enough for that. This is a deliberate
# accuracy/speed tradeoff — see search_system.md "chapter-image probe" section.
CHAPTER_PROBE_MIN_SEED = 0.65

# 2026-05-08: when an "expensive-probe" handler (one that requires Playwright
# / VRF capture per chapter — currently only MangaFire) hits a result whose
# title_match falls below this threshold, the orchestrator clamps the probe
# to a SINGLE image instead of the usual 5 samples. Rationale: low-match
# results are usually noise (spinoffs, doujinshi, unrelated series sharing a
# token), and the 4 extra image fetches per low-confidence source add up —
# 4 wrong matches × 5 images = 16 wasted requests per search. We still
# probe one image to keep some signal for ranking; we just don't aggressively
# pay the bandwidth on the long tail. Strong matches (title_match ≥ this)
# still get the full 5-sample aggregate probe. Pure-HTTP handlers ignore
# this knob entirely (their EXPENSIVE_PROBE class attr is False).
#
# Threshold rationale: rapidfuzz WRatio on a clean main-series alt-title
# match typically lands ≥0.95; doujinshi with the literal English title in
# their slug land ~0.85-0.95; spinoffs / wrong-series-sharing-a-word land
# ~0.55-0.75. 0.85 keeps real-but-imperfect matches on the full probe path
# while quick-probing the noise.
EXPENSIVE_PROBE_QUICK_THRESHOLD = 0.85

# Hard cap on the entire probe phase. After this, we abandon any still-running
# probes (their threads keep going but we don't wait — the cache only persists
# completed probes, so unfinished ones get retried next search). Bounds worst-
# case latency from a hung handler (e.g., a site whose cover-fetch internally
# blocks past the per-request timeout, or a Playwright-VRF call stuck in a bad
# state).
#
# Bumped to 120s when the multi-page aggregate probe shipped (2026-05-07): each
# high-seed probe now does up to 5 sequential image fetches (~10-15s typical)
# instead of 1 (~2s). At parallelism=6, 22 high-seed sites take ~22/6 * 12s =
# ~44s in the new path vs ~7s in the old single-page path. 120s gives headroom
# for one fully-throttled site (5x15s = 75s upper bound per probe) without
# guillotining the rest.
PROBE_PHASE_DEADLINE_S = 120.0


# --- Data shapes --------------------------------------------------------
@dataclass
class SourceEntry:
    """One source's data for a particular SeriesCandidate.

    `composite_score` is what the candidate's `sources` list is sorted by.
    In Phase 1a it's just title_match (with the seed prior tie-break baked
    in via stable sort). In later phases img_quality_score, user_modifier,
    and feedback flow into composite_score directly.

    `img_quality_score=None` means "not yet measured" — the comparator falls
    back to seed_quality. A measured 0.0 (e.g., aggregate probe got 0/5
    successful samples — CDN-poisoned site like rizzchoros.cloud during a
    bad period) is distinct from un-measured and IS used by the comparator,
    so the broken-CDN signal isn't camouflaged by a high seed prior.
    """
    site: str
    url: str
    title: str
    cover: Optional[str]
    title_match: float
    seed_quality: float
    img_quality_score: Optional[float] = None  # populated in Phase 2; None = un-measured
    # Aggregate metadata from _probe_chapter_aggregate / _score_image_blob —
    # carries bpp / decode_quality / is_grayscale / outlier / format / etc. so
    # the UI can render a "why this score?" tooltip without recomputing. Shape
    # is the dict returned by _probe_chapter_aggregate (or _score_image_blob
    # in the cover-fallback path). None = un-measured.
    img_quality_metadata: Optional[Dict] = None
    user_modifier: float = 0.0      # populated in Phase 5
    composite_score: float = 0.0
    chapter_count_hint: Optional[int] = None
    actual_chapter_count: Optional[int] = None
    dmca_likely: bool = False
    raw_score: float = 0.0
    # True when the handler is the publisher's own platform (e.g. linewebtoon
    # = webtoons.com), not an aggregator re-hosting other publishers' content.
    # Populated in search_all from handler.OFFICIAL_PUBLISHER. Consumed by
    # _cmp as the top tiebreaker within a SeriesCandidate, above quality.
    is_official: bool = False


@dataclass
class SeriesCandidate:
    canonical_title: str
    canonical_year: Optional[int]
    sources: List[SourceEntry] = field(default_factory=list)

    def to_json(self) -> Dict:
        return {
            "canonical_title": self.canonical_title,
            "canonical_year": self.canonical_year,
            "sources": [
                {
                    "site": s.site,
                    "url": s.url,
                    "title": s.title,
                    "cover": s.cover,
                    "title_match": round(s.title_match, 4),
                    "seed_quality": round(s.seed_quality, 4),
                    "img_quality_score": round(s.img_quality_score, 4) if s.img_quality_score is not None else None,
                    "img_quality_metadata": s.img_quality_metadata,
                    "composite_score": round(s.composite_score, 4),
                    "chapter_count_hint": s.chapter_count_hint,
                    "actual_chapter_count": s.actual_chapter_count,
                    "dmca_likely": s.dmca_likely,
                    "is_official": s.is_official,
                }
                for s in self.sources
            ],
        }


# --- Quality-seed loading ----------------------------------------------
_SEED_LOCK = threading.Lock()
_SEED_CACHE: Optional[Dict[str, float]] = None


def _load_quality_seed() -> Dict[str, float]:
    """Load and cache sites/quality_seed.json. Missing file or parse error
    just yields an empty dict — the orchestrator degrades to pure title-match
    ranking, which is correct (no tiebreaker means no preference between
    equally-matching sites)."""
    global _SEED_CACHE
    with _SEED_LOCK:
        if _SEED_CACHE is not None:
            return _SEED_CACHE
        path = os.path.join(os.path.dirname(__file__), "quality_seed.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}
        seed: Dict[str, float] = {}
        for k, v in (data or {}).items():
            if k.startswith("_"):
                continue
            try:
                seed[str(k).lower()] = max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                continue
        _SEED_CACHE = seed
        return _SEED_CACHE


# --- Probe-failure cache (in-process + JSON snapshot) ------------------
class ProbeFailureCache:
    """Track per-host search-time failures to skip dead sites cheaply.

    Persists to ~/.aio-dl/cache/probe_failures.json so back-to-back invocations
    share state. Integrates with aio-dl.py's _record_rate_limit by accepting
    a callable that records a long cooldown when a host crosses the failure
    threshold — that's how dead sites stay out of make_request's eyeballs
    without a separate suppression layer.

    Self-healing: entries expire after PROBE_FAILURE_TTL_S so a recovered site
    naturally rejoins on the next search.
    """

    def __init__(
        self,
        ttl_s: float = PROBE_FAILURE_TTL_S,
        threshold: int = PROBE_FAILURE_THRESHOLD,
        record_cooldown: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        self.ttl_s = ttl_s
        self.threshold = threshold
        # record_cooldown: (host, seconds) -> None. Plumbs into
        # aio-dl.py:_record_rate_limit so the existing rate-limit machinery
        # honors our suppression. Optional — if None, we still maintain the
        # in-process count and the search loop checks is_blocked().
        self.record_cooldown = record_cooldown
        self._lock = threading.Lock()
        # host -> (consecutive_failures, expires_at_epoch)
        # Both partial-failure and blocked states have an expires_at so the
        # snapshot loader can prune stale entries with one rule. is_blocked
        # is "fails >= threshold AND expires_at > now"; everything else is
        # a partial-failure counter that disappears once the entry expires.
        self._state: Dict[str, Tuple[int, float]] = {}
        self._snapshot_path = os.path.join(
            os.path.expanduser("~"), ".aio-dl", "cache", "probe_failures.json"
        )
        self._load_snapshot()

    def _load_snapshot(self) -> None:
        try:
            with open(self._snapshot_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                now = time.time()
                cleaned: Dict[str, Tuple[int, float]] = {}
                for host, entry in data.items():
                    if not isinstance(entry, list) or len(entry) != 2:
                        continue
                    fails, until = entry
                    try:
                        fails = int(fails)
                        until = float(until)
                    except (TypeError, ValueError):
                        continue
                    if until > now:
                        cleaned[str(host)] = (fails, until)
                self._state = cleaned
        except (OSError, json.JSONDecodeError):
            return

    def _save_snapshot(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._snapshot_path), exist_ok=True)
            payload = {h: list(v) for h, v in self._state.items()}
            tmp = self._snapshot_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, self._snapshot_path)
        except OSError:
            pass

    def is_blocked(self, host: str) -> bool:
        if not host:
            return False
        with self._lock:
            entry = self._state.get(host)
            if not entry:
                return False
            fails, expires_at = entry
            if expires_at <= time.time():
                self._state.pop(host, None)
                return False
            # Only block when threshold actually crossed — partial-failure
            # entries survive as counters but don't suppress the host yet.
            return fails >= self.threshold

    def record_failure(self, host: str) -> None:
        if not host:
            return
        with self._lock:
            fails, _ = self._state.get(host, (0, 0.0))
            fails += 1
            expires_at = time.time() + self.ttl_s
            self._state[host] = (fails, expires_at)
            if fails >= self.threshold and self.record_cooldown:
                try:
                    self.record_cooldown(host, self.ttl_s)
                except Exception:
                    pass
            # Persist on every change so back-to-back aio-dl.py invocations
            # accumulate consecutive failures across runs. Without this, a
            # threshold of 2 never trips because each run resets the counter.
            self._save_snapshot()

    def record_success(self, host: str) -> None:
        if not host:
            return
        with self._lock:
            if host in self._state:
                self._state.pop(host, None)
                self._save_snapshot()


# --- Image-quality probe + cache (Phase 2) -----------------------------
class ImageQualityCache:
    """Persistent (site, series_url) -> measured image-quality score cache.

    Used by search_all to replace seed_quality with measured img_quality_score
    in the rank composite once a (site, series) has been probed. Per-pair TTL
    so CDN/recompression policy changes are detected without manual cache
    invalidation.

    Schema in ~/.aio-dl/cache/img_quality.json:
      { "<site>:<series_url>": {
          "score": float,
          "metadata": {...},
          "expires_at": float-epoch,
          "schema_version": 2,
        } }

    schema_version bumps:
      - v1 (implicit, missing field): cover-image probe. Biased per-site
        because covers and chapter pages have different CDN policies.
      - v2: single-page chapter-image probe (middle page of a representative
        chapter). Better than v1 but couldn't detect CDN throttling under
        bulk fetch — a site that throttles on consecutive requests scored
        identically to a healthy site.
      - v3: multi-page aggregate probe (5 sequential pages: start,
        start-mid, mid, mid-last, last). Failed fetches count as 0 in the
        aggregate, so a flaky CDN scores proportional to its failure rate.
      - v4 (2026-05-08): content-aware decode_quality. Adds:
          * Hybrid median/mean aggregation in _probe_chapter_aggregate
            (sites/base.py:483) — median when all 5 succeed (suppresses
            color/B&W content variance), mean if any failed (preserves
            throttle-detection signal).
          * Lossless format short-circuit (_detect_lossless_blob): PNG
            and WebP-VP8L score decode_quality = 1.0 honestly instead of
            the prior monolithic 0.85.
          * Grayscale detection (_is_grayscale_pil) + dual bpp curves
            (_bpp_decode_quality) for lossy WebP/AVIF — calibrated B&W
            band, plausible-shaped color band.
          * Sanity-clip outliers: WebP < 0.05 bpp clamped to 0.1
            (broken-encoding flag); JPEG > 0.6 bpp clamped to 1.0
            (high-quality JPEG that the QT estimate undersells).
        Pre-v4 entries are dropped at load so they get re-probed.

    No threading concerns beyond `_lock` because the orchestrator's parallel
    probe phase reads/writes from worker threads.
    """

    SCHEMA_VERSION = 4

    def __init__(self, ttl_s: float = IMG_QUALITY_TTL_S) -> None:
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._state: Dict[str, Dict] = {}
        self._snapshot_path = os.path.join(
            os.path.expanduser("~"), ".aio-dl", "cache", "img_quality.json"
        )
        self._load_snapshot()

    @staticmethod
    def _key(site: str, series_url: str) -> str:
        return f"{site}:{series_url}"

    def _load_snapshot(self) -> None:
        try:
            with open(self._snapshot_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                now = time.time()
                cleaned: Dict[str, Dict] = {}
                for k, v in data.items():
                    if not isinstance(v, dict):
                        continue
                    # Pre-v2 entries are cover-based (biased per-site). Drop
                    # them so the next search re-probes with the chapter-
                    # image path. See ImageQualityCache class docstring.
                    if v.get("schema_version") != self.SCHEMA_VERSION:
                        continue
                    expires_at = v.get("expires_at")
                    try:
                        expires_at = float(expires_at) if expires_at is not None else 0.0
                    except (TypeError, ValueError):
                        continue
                    if expires_at > now and isinstance(v.get("score"), (int, float)):
                        cleaned[str(k)] = {
                            "score": float(v["score"]),
                            "metadata": v.get("metadata") or {},
                            "expires_at": expires_at,
                            "schema_version": self.SCHEMA_VERSION,
                        }
                self._state = cleaned
        except (OSError, json.JSONDecodeError):
            return

    def _save_snapshot(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._snapshot_path), exist_ok=True)
            tmp = self._snapshot_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
            os.replace(tmp, self._snapshot_path)
        except OSError:
            pass

    def get(self, site: str, series_url: str) -> Optional[float]:
        if not site or not series_url:
            return None
        key = self._key(site, series_url)
        with self._lock:
            entry = self._state.get(key)
            if not entry:
                return None
            if entry.get("expires_at", 0.0) <= time.time():
                self._state.pop(key, None)
                self._save_snapshot()
                return None
            score = entry.get("score")
            return float(score) if isinstance(score, (int, float)) else None

    def get_with_metadata(
        self, site: str, series_url: str
    ) -> Optional[Tuple[float, Dict]]:
        """Like get(), but also returns the cached metadata dict so the
        orchestrator can populate src.img_quality_metadata from cache hits
        (otherwise the UI would lose bpp / is_grayscale / outlier on every
        repeat search). Returns None on miss / expiry / type mismatch.
        """
        if not site or not series_url:
            return None
        key = self._key(site, series_url)
        with self._lock:
            entry = self._state.get(key)
            if not entry:
                return None
            if entry.get("expires_at", 0.0) <= time.time():
                self._state.pop(key, None)
                self._save_snapshot()
                return None
            score = entry.get("score")
            if not isinstance(score, (int, float)):
                return None
            metadata = entry.get("metadata") or {}
            return float(score), metadata

    def set(self, site: str, series_url: str, score: float, metadata: Optional[Dict] = None) -> None:
        if not site or not series_url:
            return
        key = self._key(site, series_url)
        with self._lock:
            self._state[key] = {
                "score": max(0.0, min(1.0, float(score))),
                "metadata": metadata or {},
                "expires_at": time.time() + self.ttl_s,
                "schema_version": self.SCHEMA_VERSION,
            }
            self._save_snapshot()


# --- Image-quality scoring ---------------------------------------------
# Format ranking: avif (modern, lossy with high quality) > webp (modern,
# typical quality > jpeg-equivalent at smaller size) > png (lossless,
# overkill for manga) > jpeg (older lossy, often heavily compressed by
# aggregator CDNs) > unknown.
_FORMAT_BONUS = {
    "AVIF": 1.0,
    "WEBP": 0.85,
    "PNG": 0.7,
    "JPEG": 0.55,
    "JPG": 0.55,
    "GIF": 0.4,
}


def _detect_lossless_blob(blob: bytes) -> Optional[bool]:
    """Phase H2 (2026-05-08): detect whether an image blob is a known
    lossless format from its container header — quick byte-twiddle parse,
    no PIL decode needed.

    Returns True for PNG and WebP-VP8L (lossless WebP), False for known
    lossy (JPEG, WebP-VP8), None for ambiguous/unknown formats. Caller
    short-circuits decode_quality=1.0 for True, falls through to
    quantization/bpp estimates otherwise.

    Why bother with byte-twiddling: the prior scorer hardcoded
    decode_quality=0.85 for every non-JPEG, which silently underrated
    actually-lossless WebP/PNG sources by 15%. Detecting the lossless
    case honestly before hitting the bpp curve fixes that.

    Cross-file: called from _score_image_blob below; no other consumers.
    """
    if len(blob) < 16:
        return None
    # PNG: 8-byte magic
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    # WebP RIFF container: bytes 0-3 'RIFF', bytes 8-11 'WEBP',
    # bytes 12-15 chunk type.
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        chunk = blob[12:16]
        if chunk == b"VP8L":
            return True   # lossless WebP
        if chunk == b"VP8 ":
            return False  # lossy WebP
        if chunk == b"VP8X":
            # Extended container — VP8X header is bytes 12-29 (18 bytes:
            # 4-byte chunk id + 4-byte size + 10-byte body). After it,
            # OPTIONAL chunks may appear before the inner VP8/VP8L
            # bitstream: ICCP (color profile — common on Photoshop/Krita
            # exports with sRGB or wider gamut), ANIM (animation params),
            # ALPH (separate alpha for lossy WebP+alpha), EXIF, XMP.
            # We MUST chunk-walk; a naive byte-30 read misclassifies any
            # lossless WebP carrying a color profile, which is a sizeable
            # fraction of high-quality WebP exports in the wild.
            pos = 30  # first chunk after the VP8X header
            while pos + 8 <= len(blob):
                ctype = blob[pos:pos + 4]
                # Chunk size is little-endian 32-bit immediately following
                # the 4-byte chunk id; chunks are word-aligned (1 pad byte
                # if size is odd).
                csize = int.from_bytes(blob[pos + 4:pos + 8], "little")
                if ctype == b"VP8L":
                    return True
                if ctype == b"VP8 ":
                    return False
                pos += 8 + csize + (csize & 1)
            return None
    # JPEG: bytes 0-1 0xFFD8 → always lossy; let the JPEG path handle it.
    if blob[:2] == b"\xff\xd8":
        return False
    return None


def _is_grayscale_pil(img) -> bool:
    """Phase H3 (2026-05-08): detect whether an image is effectively
    grayscale via a sparse-grid chroma probe. ~400 samples (20×20
    thumbnail), <1ms in PIL.

    Uses the **90th-percentile** chroma deviation, not the max:
      - JPEG re-encodes of B&W scans pick up localized chroma noise from
        encoder ringing. A single noisy pixel out of 400 would otherwise
        push max above the threshold and force a B&W page into the color
        curve. The misclassification is much more punishing than the bpp
        curve itself: a clean B&W manga page at 0.10 bpp scores
        (0.10-0.03)/0.20 = 0.35 in the B&W curve vs (0.10-0.08)/0.40 =
        0.05 in the color curve — false-color is the dominant error.
      - Sepia/restoration scans carry consistent low-but-nonzero chroma
        (~10-20/255); max-based detection always declares them color
        even though they're functionally monochromatic. p90 is more
        forgiving and the threshold can be tuned upward (5 → 8 or 10)
        to fold sepia into the grayscale class if desired.

    Cross-file: called from _score_image_blob below to drive
    _bpp_decode_quality's curve selection.
    """
    if getattr(img, "mode", None) in ("L", "1"):
        return True
    try:
        # Local import — keeps the module importable when PIL isn't
        # available (probe phase no-ops in that case anyway).
        from PIL import Image
        sample = img.convert("RGB").resize((20, 20), Image.LANCZOS)
    except Exception:
        return False
    chroma_vals = sorted(
        max(abs(r - g), abs(r - b), abs(g - b))
        for r, g, b in sample.getdata()
    )
    if not chroma_vals:
        return False
    # 90th percentile: tolerates up to ~40 noisy pixels (out of 400).
    p90 = chroma_vals[len(chroma_vals) * 9 // 10]
    return p90 < 5


def _bpp_decode_quality(
    width: int, height: int, size_bytes: int, is_grayscale: bool
) -> float:
    """Phase H3 (2026-05-08): convert bytes-per-pixel into a 0..1
    decode-quality estimate, with separate curves for grayscale (B&W
    manga compresses much smaller at the same encoder quality) vs
    color content. Caller is responsible for detecting grayscale via
    _is_grayscale_pil.

    Curves below come from the calibrate_quality_probe.py run on
    Talentless Nana ch ~60 across atsumaru (WebP) and mangafire (JPEG):

      grayscale (calibrated): (bpp - 0.05) / 0.20 → 0.05 bpp = 0.0,
                                                     0.25 bpp = 1.0
        - atsumaru 0.18 bpp → 0.65    (lossy WebP B&W, the "low" baseline)
        - mangafire 0.26 bpp → 1.0   (mid-range JPEG B&W, doesn't apply
                                       — JPEG uses QT path — but bpp
                                       lands above the ceiling for this
                                       curve, validating the band)
        - clean B&W q=90 WebP at 0.10 bpp → 0.25  (poor)
        - lossless WebP at 0.50+ bpp → handled by H2 short-circuit, 1.0

      color     (starting):   (bpp - 0.08) / 0.40 → 0.08 bpp = 0.0,
                                                     0.48 bpp = 1.0
        - The test set didn't include a color manga, so this curve is
          unvalidated. Plausible-shaped based on expected encoder
          behavior; expect to retune after seeing real color-source
          probe data.

    Both clamped to [0.0, 1.0]. Below the floor = "fundamentally broken
    encoding"; above the ceiling = "near-lossless." The H4 outlier clip
    in _score_image_blob handles WebP < 0.05 bpp separately (clamps to
    0.1 and flags the metadata).

    Cross-file: called from _score_image_blob below for non-JPEG/non-
    lossless formats. JPEG keeps its quantization-table estimate
    (independent of bpp) augmented by H4's high-bpp clip.
    """
    pixels = max(1, width * height)
    bpp = size_bytes / pixels
    if is_grayscale:
        return max(0.0, min(1.0, (bpp - 0.05) / 0.20))
    return max(0.0, min(1.0, (bpp - 0.08) / 0.40))


def _jpeg_quality_estimate(quantization: Optional[Dict]) -> float:
    """Reverse-derive JPEG quality (0-100) from PIL's quantization tables.

    Higher values in the QT correspond to coarser quantization and thus
    lower quality. We use the average of the luminance table (key 0) as a
    proxy for the libjpeg quality factor, then map to bucketed quality.
    Robust enough for ranking — exact reverse-mapping isn't necessary.
    """
    if not quantization or not isinstance(quantization, dict):
        return 50.0
    qt = quantization.get(0)
    if not qt:
        # Some JPEGs only have chroma (key 1); use that as fallback
        qt = quantization.get(1)
    if not qt:
        return 50.0
    avg = sum(qt) / max(1, len(qt))
    if avg < 1.5:
        return 100.0
    if avg < 5.0:
        return 92.0
    if avg < 12.0:
        return 82.0
    if avg < 25.0:
        return 68.0
    if avg < 60.0:
        return 50.0
    return 30.0


def _score_image_blob(blob: bytes) -> Optional[Tuple[float, Dict]]:
    """Score an image blob 0..1; return (score, metadata) or None on failure.

    Component weights:
      0.4 — resolution (image width, normalized 800px..2400px)
      0.3 — format bonus (avif > webp > png > jpeg; macro prior only)
      0.3 — decode quality (content-aware; see Phase H2/H3/H4 below)

    decode_quality v4 logic (2026-05-08):
      - PNG / WebP-VP8L (lossless detected via _detect_lossless_blob) → 1.0
        (honest score for sources with no encoder loss to penalize).
      - JPEG → quantization-table estimate via _jpeg_quality_estimate,
        clipped up to 1.0 when bpp > 0.6 (H4: high-bpp JPEGs are q≥98
        territory and the QT estimate often undersells them at ~0.85).
      - Lossy WebP / AVIF / unknown → bpp-derived curve via
        _bpp_decode_quality with grayscale-vs-color awareness from
        _is_grayscale_pil. WebP with bpp < 0.05 clamps to 0.1 and
        flags the metadata as a broken-encoding outlier (H4).

    Returns None when PIL can't decode (corrupt, truncated, or unsupported
    format). Caller falls back to seed_quality in that case.
    """
    if not blob or len(blob) < 256:
        return None
    try:
        from PIL import Image  # local import to avoid module-load cost
        import io
        img = Image.open(io.BytesIO(blob))
        img.load()
    except Exception:
        return None

    try:
        width, height = img.size
        fmt = (img.format or "UNKNOWN").upper()
    except Exception:
        return None

    if width < 100 or height < 100:
        return None  # likely a placeholder/icon, not a real cover

    # Resolution: 800px = floor (acceptable), 2400px = ceiling (excellent).
    # Width is the dominant axis for manga since most pages are taller than
    # they are wide; covers are usually portrait-orientation too.
    res_score = max(0.0, min(1.0, (width - 800) / 1600))
    format_bonus = _FORMAT_BONUS.get(fmt, 0.4)

    # Phase H2 (2026-05-08): detect lossless containers BEFORE falling
    # into bpp/quantization estimates. PNG and WebP-VP8L get an honest
    # 1.0 instead of the prior monolithic 0.85 default for non-JPEG.
    lossless = _detect_lossless_blob(blob)
    is_grayscale = _is_grayscale_pil(img)
    bpp = len(blob) / max(1, width * height)
    metadata: Dict[str, Any] = {
        "width": width,
        "height": height,
        "format": fmt,
        "size_bytes": len(blob),
        "is_grayscale": is_grayscale,
        "bpp": round(bpp, 4),
    }

    if lossless is True:
        decode_quality = 1.0
    elif fmt in ("JPEG", "JPG"):
        quality_est = _jpeg_quality_estimate(getattr(img, "quantization", None))
        decode_quality = max(0.0, min(1.0, quality_est / 95.0))
        # H4: very high bpp JPEG = practically lossless; QT estimate often
        # caps around 0.85 for these. Clip up so genuinely-pristine JPEGs
        # don't get underranked vs lossless WebP.
        if bpp > 0.6:
            decode_quality = max(decode_quality, 1.0)
    else:
        # Phase H3: lossy WebP / AVIF / unknown — content-aware bpp curve.
        decode_quality = _bpp_decode_quality(width, height, len(blob), is_grayscale)
        # H4: WebP below 0.05 bpp on natural manga is a fundamentally-broken
        # encoder (or a placeholder slice); clamp the score and flag it for
        # downstream auditing without affecting upstream comparator contract.
        if fmt == "WEBP" and bpp < 0.05:
            decode_quality = min(decode_quality, 0.1)
            metadata["outlier"] = "webp_below_floor"

    # Persist decode_quality on the per-image metadata so the aggregate in
    # base.py:_probe_chapter_aggregate can fold it into the per-source view
    # (mean across samples). Useful for the SearchSourceCard tooltip and for
    # calibration tooling that wants to see what the lossy curve actually
    # produced for each probed page.
    metadata["decode_quality"] = round(decode_quality, 4)

    score = 0.4 * res_score + 0.3 * format_bonus + 0.3 * decode_quality
    score = max(0.0, min(1.0, score))
    return score, metadata


# --- Title normalization + canonical key -------------------------------
_PUNCT_RE = re.compile(r"[\W_]+", re.UNICODE)
_PARENS_RE = re.compile(r"\s*\([^)]*\)\s*")


def _normalize_title(title: str) -> str:
    """Canonical key for cross-site dedupe.

    Aggressive: lowercase, drop parentheticals (often '(Doujinshi)' or season
    markers), strip non-alphanumerics, collapse to a token-bag string. False
    merges of distinct series are caught at the candidate level by chapter_count
    deltas in later phases (plan section L); for Phase 1a we accept some
    over-merging since the user picks the source URL anyway.
    """
    s = (title or "").strip().lower()
    s = _PARENS_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    s = " ".join(s.split())
    return s


# --- Title matching -----------------------------------------------------
def _best_title_match(query: str, hit: SearchHit) -> float:
    """Compute rapidfuzz weighted similarity against title + alt_titles.

    Uses WRatio (weighted blend of partial_ratio, token_sort_ratio, and ratio)
    because token_set_ratio alone gives a perfect score to any candidate that
    contains every query token — which means 'Frieren' matches a 60-char doujin
    title equally with the actual main series. WRatio penalizes extra cruft
    so 'Sousou no Frieren' scores 90 vs a doujin with parenthetical markers
    at 60. Returns max similarity across all candidate strings, normalized
    0..1 (rapidfuzz emits 0..100).

    Cross-file: this is the only place query↔hit similarity is computed; if
    we ever migrate scorers, change here and update DEFAULT_MIN_MATCH above
    in the same edit.
    """
    try:
        from rapidfuzz import fuzz
    except ImportError as exc:
        raise RuntimeError(
            "rapidfuzz is required for cross-site search. "
            "Install with: pip install rapidfuzz"
        ) from exc

    candidates = [hit.title]
    candidates.extend(hit.alt_titles or [])
    if not candidates:
        return 0.0

    q = (query or "").strip()
    if not q:
        return 0.0

    best = 0.0
    for cand in candidates:
        if not cand:
            continue
        score = fuzz.WRatio(q, cand) / 100.0
        if score > best:
            best = score
    return best


# --- Public entrypoint --------------------------------------------------
def search_all(
    query: str,
    scraper_factory: Callable[[BaseSiteHandler], object],
    make_request: Callable,
    *,
    language: str = "en",
    parallelism: int = 6,
    per_site_timeout_s: float = DEFAULT_PER_SITE_TIMEOUT_S,
    min_match: float = DEFAULT_MIN_MATCH,
    top_per_site: int = 20,
    probe_failure_cache: Optional[ProbeFailureCache] = None,
    img_quality_cache: Optional[ImageQualityCache] = None,
    seed_hits: Optional[List[SearchHit]] = None,
    on_status: Optional[Callable[[str], None]] = None,
    seeded_only: bool = False,
) -> List[SeriesCandidate]:
    """Fan out search across all search-capable handlers, dedupe, rank.

    Args:
      query: user-provided title text.
      scraper_factory: called once per handler — returns a configured scraper
        for that handler. Lets each handler set its own headers/cookies via
        configure_session without colliding on a shared session. Implemented
        by aio_search_cli.run_search_mode.
      make_request: aio-dl.py's make_request — passing this in (rather than
        importing) keeps the orchestrator a pure module.
      probe_failure_cache: shared cache of host→suppression. Pass None for
        ephemeral runs (used only by tests).
      seed_hits: optional list of SearchHits to inject before the parallel
        search runs. Used for URL-mode --search where the user supplied a
        specific MangaFire URL; the seed hit guarantees that source's
        participation even when its keyword search would have failed (e.g.,
        MangaFire's flaky typeahead). Seeded hits go through the same
        rapidfuzz scoring + union-find merge as parallel-search results, so
        they end up in the right candidate cluster.
      on_status: optional progress callback for human output (e.g., the
        "[*] Probing image quality across N sources..." line in Phase 2).

    Returns:
      List of SeriesCandidate, ranked best-first.
    """
    from . import iter_search_capable_handlers, get_handler_by_name

    cache = probe_failure_cache  # may be None
    img_cache = img_quality_cache  # may be None — probe phase skipped if None
    seed = _load_quality_seed()

    handlers = [h for h in iter_search_capable_handlers()]
    if not handlers and not seed_hits:
        return []

    # Lowercase names of handlers flagged as publisher-owned platforms (vs
    # aggregators). Read off the class attribute once here so the per-source
    # loop below can populate SourceEntry.is_official with a cheap set lookup
    # instead of re-traversing the handler registry per candidate. Empty set
    # is the safe degenerate — falls back to the pre-fix behavior of quality-
    # only tiebreaking. See BaseSiteHandler.OFFICIAL_PUBLISHER for the flag.
    official_sites: set = {
        h.name.lower() for h in handlers
        if getattr(h, "OFFICIAL_PUBLISHER", False)
    }

    # Names of sites already represented by a seed hit (URL-mode --search).
    # Defined here (before the eligibility loop uses it) so seed_hits can
    # short-circuit re-querying those handlers.
    seeded_sites: set = {h.site for h in (seed_hits or [])}

    # Filter out handlers whose primary domain is currently blocked OR whose
    # site is already represented by a URL-mode seed hit. When a seed hit is
    # present we already have authoritative data for that site — no need to
    # also run the parallel search and risk its flakiness (this is the whole
    # reason URL-mode exists for MangaFire).
    #
    # When seeded_only is set (--seeded-only flag), drop any handler whose
    # name doesn't appear in quality_seed.json. This skips the long tail of
    # Madara/MangaThemesia extras that default to seed=0.50 — most are
    # foreign-language and contribute mostly noise to rankings. Cuts search
    # wall time roughly in half on popular queries since ~250 handlers stop
    # contributing parallel I/O. Cross-file: same name lookup as the
    # comparator's seed_quality assignment (`seed.get(site.lower(), 0.5)`),
    # so the filter is consistent with how scoring would have treated them.
    eligible: List[BaseSiteHandler] = []
    skipped_unseeded = 0
    for h in handlers:
        host = (h.domains[0] if getattr(h, "domains", None) else "") or ""
        if h.name in seeded_sites:
            if on_status:
                on_status(f"  skipping {h.name} (seeded from URL)")
            continue
        if cache and host and cache.is_blocked(host):
            if on_status:
                on_status(f"  skipping {h.name} ({host} suppressed)")
            continue
        if seeded_only and h.name.lower() not in seed:
            skipped_unseeded += 1
            continue
        eligible.append(h)
    if seeded_only and skipped_unseeded and on_status:
        on_status(
            f"  --seeded-only: skipped {skipped_unseeded} handler(s) not in "
            f"quality_seed.json"
        )

    # Seed hits enter the scoring pipeline before the parallel search results.
    # If a seed hit's site is also returned by the parallel search, dedupe-
    # within-candidate (per_site_best) keeps whichever scored higher on
    # title-match — but seed hits typically already score 1.0 (title was
    # extracted from the URL's series page itself), so they normally win.
    #
    # Defined BEFORE the early-return below so the `not all_hits` check has
    # something to evaluate. Earlier revisions defined this after the early
    # return, which raised NameError when `eligible` was empty (all sites
    # blocked / --seeded-only filtered everything out / etc.).
    all_hits: List[SearchHit] = list(seed_hits) if seed_hits else []

    if not eligible and not all_hits:
        # No live handlers AND no seed hits — nothing to score.
        return []

    if on_status:
        if eligible:
            on_status(f"[*] Searching {len(eligible)} sites for '{query}'...")
        if seed_hits:
            on_status(f"[*] Including {len(seed_hits)} seeded source(s) from URL")

    def _run_one(handler: BaseSiteHandler) -> List[SearchHit]:
        host = (handler.domains[0] if getattr(handler, "domains", None) else "") or ""
        try:
            scraper = scraper_factory(handler)
        except Exception:
            if cache and host:
                cache.record_failure(host)
            return []
        try:
            hits = handler.search(
                query, scraper, make_request, language=language, limit=top_per_site
            )
            if not isinstance(hits, list):
                hits = []
            if cache and host:
                # Empty results don't count as failures (the site is up but
                # has no match for this query); only exceptions do.
                cache.record_success(host)
            return hits
        except Exception:
            if cache and host:
                cache.record_failure(host)
            return []

    workers = max(1, min(parallelism, len(eligible)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="search") as pool:
        futures = {pool.submit(_run_one, h): h for h in eligible}
        for fut in as_completed(futures, timeout=None):
            try:
                hits = fut.result(timeout=per_site_timeout_s)
            except Exception:
                hits = []
            if hits:
                all_hits.extend(hits)

    if not all_hits:
        return []

    # Score every hit against the query.
    scored: List[Tuple[float, SearchHit]] = []
    for hit in all_hits:
        score = _best_title_match(query, hit)
        if score >= min_match:
            scored.append((score, hit))

    if not scored:
        return []

    # Group by canonical title key. Within a candidate, multiple sources may
    # appear; we rank them per-candidate and emit one candidate per group.
    #
    # Merge strategy: union-find over normalized title-keys.
    # Each hit contributes its primary key + every alt-title key as members of
    # one set. Any two hits whose keysets overlap end up in the same group.
    # This is symmetric — site A returning "Frieren ..." with "Sousou no
    # Frieren" as alt and site B returning "Sousou no Frieren" with no useful
    # alts will still merge, regardless of which arrived first. The earlier
    # one-directional approach (only fold new hit's alts into existing groups)
    # missed that case.
    parent: Dict[str, str] = {}

    def _find(k: str) -> str:
        path: List[str] = []
        while parent.get(k) and parent[k] != k:
            path.append(k)
            k = parent[k]
        for p in path:
            parent[p] = k
        parent.setdefault(k, k)
        return k

    def _union(a: str, b: str) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    # Pass 1: build keysets per hit; union all keys within a hit; track which
    # hits ended up in which root.
    hit_keys: List[Tuple[float, SearchHit, List[str]]] = []
    for score, hit in scored:
        keys: List[str] = []
        primary = _normalize_title(hit.title)
        if primary:
            keys.append(primary)
        for alt in hit.alt_titles or []:
            ak = _normalize_title(alt)
            if ak and ak not in keys:
                keys.append(ak)
        if not keys:
            continue
        for k in keys:
            _find(k)  # ensure node exists
        for k in keys[1:]:
            _union(keys[0], k)
        hit_keys.append((score, hit, keys))

    # Pass 2: bucket by root.
    groups: Dict[str, List[Tuple[float, SearchHit]]] = {}
    for score, hit, keys in hit_keys:
        root = _find(keys[0])
        groups.setdefault(root, []).append((score, hit))

    candidates: List[SeriesCandidate] = []
    for key, members in groups.items():
        # canonical_title: most-common title across the bucket; shortest as
        # tiebreaker. Length-as-primary biases toward edition variants like
        # "One Piece - Digital Colored Comics" over the canonical "One Piece"
        # when 3 of 4 sources agree on the short name.
        from collections import Counter
        title_counts = Counter((h.title or "") for _, h in members if (h.title or ""))
        if title_counts:
            max_freq = max(title_counts.values())
            top_titles = [t for t, c in title_counts.items() if c == max_freq]
            canonical_title = min(top_titles, key=len)
        else:
            canonical_title = ""
        # Year: take the max non-None year (most recent edition).
        year = max(
            (h.year for _, h in members if isinstance(h.year, int)),
            default=None,
        )
        sources: List[SourceEntry] = []
        # Dedupe within the same site — keep the highest title-match per site.
        per_site_best: Dict[str, Tuple[float, SearchHit]] = {}
        for score, hit in members:
            cur = per_site_best.get(hit.site)
            if cur is None or score > cur[0]:
                per_site_best[hit.site] = (score, hit)
        for site, (score, hit) in per_site_best.items():
            seed_q = seed.get(site.lower(), 0.5)
            sources.append(
                SourceEntry(
                    site=site,
                    url=hit.url,
                    title=hit.title,
                    cover=hit.cover,
                    title_match=score,
                    seed_quality=seed_q,
                    composite_score=score,  # Phase 1a: composite = title_match
                    chapter_count_hint=hit.chapter_count_hint,
                    actual_chapter_count=hit.actual_chapter_count,
                    dmca_likely=hit.dmca_likely,
                    raw_score=hit.raw_score,
                    is_official=site.lower() in official_sites,
                )
            )

        # Cross-site DMCA detection: when one source reports a meaningful
        # chapter count (10+) but another source has only a small fraction
        # actually fetchable, the second is likely DMCA-affected even if its
        # own metadata didn't expose lastChapter. Catches:
        #   - Witch Hat Atelier (MangaDex 1 vs MangaFire 96)
        #   - One Piece (MangaDex 7 vs MangaFire 1181)
        #   - Eleceed (MangaDex 102 vs ~400 actual; flagged at 25.5%)
        # Threshold: actual count < 50% of the highest reported count across
        # other sources. Initially I used 25% but Eleceed slipped through at
        # 25.5%; 50% catches the intent (substantially incomplete vs others)
        # without false-positives on series where MD legitimately lags by a
        # chapter or two.
        max_other_count = 0
        for s in sources:
            if isinstance(s.chapter_count_hint, int) and s.chapter_count_hint > max_other_count:
                max_other_count = s.chapter_count_hint
        if max_other_count >= 10:
            for s in sources:
                if s.dmca_likely:
                    continue
                actual = s.actual_chapter_count
                if isinstance(actual, int) and actual < max_other_count * 0.5:
                    s.dmca_likely = True
        # Sort sources within candidate. Order of decision:
        #   1. DMCA-likely sources go to the back. A source with 1/96 chapters
        #      accessible should never beat one with 96/96 regardless of
        #      quality. Fixes the canonical Witch Hat Atelier case where
        #      MangaDex's higher seed_quality (0.92) was beating MangaFire
        #      (0.85) even though MangaDex was DMCA-hollowed.
        #   2. Official publisher wins. When one source is the publisher's own
        #      platform (e.g. linewebtoon = webtoons.com, the literal LINE
        #      publisher) and the other is an aggregator re-hosting that
        #      same content (toonily, asura, etc.), the publisher wins
        #      regardless of title_match spread or measured image quality.
        #      Both sources have already been merged by union-find as the
        #      same series, so we trust the canonical bytes from the
        #      publisher. Fixes the webtoons.com vs toonily case where
        #      vertical-scroll PNG at 720-800px scored below toonily's
        #      upscaled JPEG on the probe's res_score formula (which
        #      treats 800-2400px as the credit band) despite the PNG
        #      being lossless and the JPEG being generation-loss.
        #   3. Title match within TIEBREAKER_WINDOW (0.10) → image-quality
        #      decides. Use img_quality_score when measured (Phase 2 cover
        #      probe); fall back to seed_quality when probe failed or hasn't
        #      run yet. Per the plan, ANY measurement replaces the seed —
        #      cover-bytes are deterministic, not noisy estimates.
        #   4. Otherwise title_match wins.
        # Pairwise comparator gives deterministic behavior at window boundaries.
        def _quality_for(s: SourceEntry) -> float:
            # `is not None` (not `> 0`): a measured 0.0 is meaningful — it's
            # the aggregate probe's "5/5 fetches failed" signal. We want it
            # used as the rank input even though it'll lose to anything seed-
            # based, because a broken CDN should rank LAST among its peers.
            return s.img_quality_score if s.img_quality_score is not None else s.seed_quality

        def _cmp(a: SourceEntry, b: SourceEntry) -> int:
            if a.dmca_likely != b.dmca_likely:
                return 1 if a.dmca_likely else -1
            if a.is_official != b.is_official:
                return -1 if a.is_official else 1
            if abs(a.title_match - b.title_match) <= TIEBREAKER_WINDOW:
                qa, qb = _quality_for(a), _quality_for(b)
                if qa != qb:
                    return -1 if qa > qb else 1
                return 0
            return -1 if a.title_match > b.title_match else 1

        # Capture the comparator for re-use after the probe phase populates
        # img_quality_score; we sort once now (with seed fallback) so output
        # is deterministic even if the probe phase fails entirely.
        sources.sort(key=cmp_to_key(_cmp))
        candidates.append(
            SeriesCandidate(
                canonical_title=canonical_title or key,
                canonical_year=year,
                sources=sources,
            )
        )

    # ---- Image-quality probe phase (Phase 2) ----
    # For each unique (site, url, hit-with-cover) across all candidates'
    # sources, probe a representative image and score it. Cached results
    # serve subsequent searches for the same (site, series). Replaces the
    # seed_quality prior in the comparator below — the seed continues to
    # serve unfamiliar pairs and probe-failures.
    if img_cache is not None and candidates:
        # Index hit-by-url for quick lookup during probe (so we can pass the
        # full SearchHit to handler.probe_sample_image, not just the source).
        hit_by_url: Dict[str, SearchHit] = {h.url: h for _, h in scored}
        # Collect unique (site, url) source entries needing probe.
        # Same-(site,url) appearing in multiple candidates can share the same
        # cached result — but in practice each (site, url) lives in one
        # candidate after the union-find merge, so this is mostly redundant.
        seen_pairs: set = set()
        sources_to_probe: List[SourceEntry] = []
        for c in candidates:
            for src in c.sources:
                key = (src.site, src.url)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                sources_to_probe.append(src)

        # Cache hits short-circuit; only un-cached sources need a fetch.
        # Use get_with_metadata so cached entries restore img_quality_metadata
        # too — without this the UI tooltip loses bpp/is_grayscale/outlier on
        # every repeat search (cache hit path was returning score-only).
        cache_misses: List[SourceEntry] = []
        for src in sources_to_probe:
            cached = img_cache.get_with_metadata(src.site, src.url)
            if cached is not None:
                score, metadata = cached
                src.img_quality_score = score
                src.img_quality_metadata = metadata or None
            else:
                cache_misses.append(src)

        if cache_misses:
            if on_status:
                on_status(f"[*] Probing image quality across {len(cache_misses)} sources...")

            def _probe_one(src: SourceEntry) -> None:
                hit = hit_by_url.get(src.url)
                if hit is None:
                    return
                handler = get_handler_by_name(src.site)
                if handler is None:
                    return
                try:
                    scraper = scraper_factory(handler)
                except Exception:
                    return
                # Tiered probe selection. High-seed sites (those listed in
                # quality_seed.json) get the multi-page aggregate probe (5
                # sequential image fetches scored together — catches CDN
                # throttling AND pixel quality). Low-seed sites stay on the
                # cheap cover probe. See CHAPTER_PROBE_MIN_SEED for rationale.
                if src.seed_quality >= CHAPTER_PROBE_MIN_SEED:
                    # Quick-probe clamp for expensive-probe handlers (mangafire
                    # VRF) when title_match is weak. The full 5-sample probe is
                    # wasted bandwidth on results that probably aren't the right
                    # series — drop to 1 image so the user-perceived score is
                    # still grounded in real chapter data, but stop paying for
                    # 4 extra fetches × N noise results. See the
                    # EXPENSIVE_PROBE_QUICK_THRESHOLD comment for the rationale
                    # behind the 0.85 cutoff.
                    quick_probe = (
                        getattr(handler, "EXPENSIVE_PROBE", False)
                        and src.title_match < EXPENSIVE_PROBE_QUICK_THRESHOLD
                    )
                    try:
                        aggregate = handler._probe_chapter_aggregate(
                            hit, scraper, make_request,
                            max_samples=1 if quick_probe else None,
                        )
                    except Exception:
                        aggregate = None
                    if aggregate is not None:
                        score, metadata = aggregate
                        src.img_quality_score = score
                        src.img_quality_metadata = metadata
                        # Skip caching quick-probe results: the next search may
                        # rank this source higher (different query → different
                        # title_match → wants the full 5-sample probe). A
                        # cached 1-sample result would otherwise serve from
                        # cache and lock the source into the noisier score.
                        # Quick probes are cheap enough to redo on demand.
                        if not quick_probe:
                            img_cache.set(src.site, src.url, score, metadata=metadata)
                        return
                    # Aggregate failed (no chapters / total CDN failure / etc.)
                    # — fall through to cover probe so we still get *some*
                    # signal for ranking.
                try:
                    blob = handler._probe_cover_image(hit, scraper, make_request)
                except Exception:
                    return
                if not blob:
                    return
                result = _score_image_blob(blob)
                if result is None:
                    return
                score, metadata = result
                src.img_quality_score = score
                src.img_quality_metadata = metadata
                img_cache.set(src.site, src.url, score, metadata=metadata)

            # Daemon-thread worker pool with deadline. We can't use
            # ThreadPoolExecutor here because its `with` block calls
            # shutdown(wait=True), which waits for ALL submitted tasks —
            # including any hung probe (e.g., a cover URL on a slow CDN, a
            # site whose internals don't honor scraper.get's timeout, or a
            # Playwright VRF call stuck in a bad state). The previous code
            # could hang the orchestrator for minutes on a single bad site.
            #
            # Fix: spawn daemon worker threads pulling from a queue; bound
            # total wait time at PROBE_PHASE_DEADLINE_S. Daemons don't block
            # process exit; whatever didn't finish gets retried (uncached)
            # next search. Per-probe internal timeouts (scraper.get,
            # search_mr's per-attempt timeout) keep hung probes from leaking
            # bandwidth indefinitely; they finish on their own within ~30s
            # and cache results for next run if they manage to complete.
            import queue
            q: "queue.Queue" = queue.Queue()
            for s in cache_misses:
                q.put(s)
            probe_workers = max(1, min(parallelism, len(cache_misses)))

            def _worker_loop() -> None:
                while True:
                    try:
                        s = q.get_nowait()
                    except queue.Empty:
                        return
                    try:
                        _probe_one(s)
                    except Exception:
                        pass

            workers: List[threading.Thread] = []
            for _ in range(probe_workers):
                t = threading.Thread(
                    target=_worker_loop,
                    name="img-probe",
                    daemon=True,
                )
                t.start()
                workers.append(t)

            phase_deadline = time.time() + PROBE_PHASE_DEADLINE_S
            for t in workers:
                remaining = phase_deadline - time.time()
                if remaining <= 0:
                    break
                t.join(timeout=remaining)
            still_running = sum(1 for t in workers if t.is_alive())
            if still_running and on_status:
                on_status(
                    f"[!] Probe phase: {still_running}/{probe_workers} workers "
                    f"still running at {PROBE_PHASE_DEADLINE_S:.0f}s deadline; "
                    f"abandoning to avoid hang. Their results will populate the "
                    f"cache if the underlying calls return."
                )

        # Re-sort each candidate's sources now that img_quality_score is
        # populated. The comparator uses _quality_for which prefers measured
        # over seed when img_quality_score > 0.
        for c in candidates:
            c.sources.sort(key=cmp_to_key(_cmp))

    # Rank candidates: best title_match across any of their sources.
    candidates.sort(
        key=lambda c: max((s.title_match for s in c.sources), default=0.0),
        reverse=True,
    )
    return candidates


__all__ = [
    "SearchHit",
    "SourceEntry",
    "SeriesCandidate",
    "ProbeFailureCache",
    "ImageQualityCache",
    "search_all",
    "DEFAULT_MIN_MATCH",
    "TIEBREAKER_WINDOW",
    "DEFAULT_PER_SITE_TIMEOUT_S",
    "IMG_QUALITY_TTL_S",
]
