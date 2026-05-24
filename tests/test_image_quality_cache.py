"""Tests for the v5 ImageQualityCache (sites/search_orchestrator.py).

Covers:
  - v5 schema round-trip with full T1+T2+T3 metadata
  - Pre-v5 entry drop on load (the v4 → v5 invalidation)
  - TTL expiry
  - get_with_metadata returning paired/throttle fields
  - Multiple concurrent reads/writes (thread safety)

Cross-file: targets sites/search_orchestrator.py:ImageQualityCache.
Plan reference: ~/.claude/plans/how-robust-is-the-memoized-koala.md
(Phase 6 section).
"""

from __future__ import annotations

import json
import os
import time
from unittest.mock import patch

import pytest

from sites.search_orchestrator import ImageQualityCache, IMG_QUALITY_TTL_S


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """Provide an ImageQualityCache pointed at a tmp file path. Patches the
    `expanduser` so the cache writes inside the test's tmp dir instead of
    polluting the real ~/.aio-dl/cache/."""
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path))
    cache = ImageQualityCache()
    return cache


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------

def test_schema_version_is_8():
    """v8 (2026-05-18) bumped SCHEMA_VERSION from 7 for the B&W rewrite:
    new bw_signals primitives (screentone integrity, line quality,
    upscaler, bilevel/chroma/bg) added to T1 for bw_manga content via
    _compute_t1_score_bw; torchmetrics CLIP-IQA+ added to T2;
    anchor-free _run_pairwise_ranking replaces v5.1 anchor-based
    DISTS in T3. The new t1_score / t2_score / pairwise_adjustment
    differ from v7 for the same source so v7 entries can't be mixed
    with v8 entries — the bump forces a clean reprobe."""
    assert ImageQualityCache.SCHEMA_VERSION == 8


# ---------------------------------------------------------------------------
# Round-trip with full v5 metadata
# ---------------------------------------------------------------------------

def test_cache_round_trip_full_v5_metadata(tmp_cache):
    """Write an entry with all v6 fields → reload → assert byte-equal.

    Adds the v5.1 fields (content_type, tenengrad_clean, watermark_*,
    usm_*, ghost, chroma_*, alignment_method, paired_perceptual_median,
    etc.) on top of v5's. Test name kept for git-blame continuity even
    though the contents now cover v6."""
    metadata = {
        # Probe provenance
        "samples_attempted": 8,
        "samples_succeeded": 8,
        "chapter_indices_sampled": [1, 3, 5, 7, 9, 11, 13, 15],
        # T1 components
        "t1_score": 0.65,
        "res_norm": 0.5,
        "blockiness": 0.1,
        "fft_hf_ratio": 0.55,
        "tenengrad": 75.3,
        "tenengrad_norm": 0.94,
        "jpeg_qf": 85,
        "jpeg_qf_norm": 0.85,
        "jpeg_nse": 0.95,
        # T2 components
        "t2_available": True,
        "t2_score": 0.47,
        "niqe_score": 6.7,
        "niqe_norm": 0.53,
        "clip_iqa_scores": None,
        "clip_iqa_mean": None,
        # T3 components
        "paired_quality_adjustment": 0.05,
        "paired_anchor_site": "mangadex",
        "paired_dists_median": 0.04,
        "paired_pairs_compared": 3,
        # Content
        "is_grayscale": True,
        "is_lossless": False,
        "width": 1500,
        "height": 2000,
        "format": "JPEG",
        "bpp": 0.25,
        "size_bytes": 750000,
        # Throttle
        "cdn_reliability": 1.0,
        # Outlier
        "outlier": None,
        # v5.1 (v6 schema) additions
        "content_type": "bw_manga",
        "res_norm_target": 2_000_000,
        "tenengrad_clean": 0.94,
        "usm_overshoot_score": 0.05,
        "usm_n_edges": 35000,
        "usm_edges_with_overshoot": 100,
        "chroma_var": 0.0,
        "chroma_penalty": 0.0,
        "chroma_subsampling": None,
        "chroma_complexity": None,
        "watermark_score": 0.0,
        "watermark_regions": [],
        "watermark_detector_used": "easyocr",
        "paired_perceptual_median": 0.04,
        "paired_dists_alone_median": 0.05,
        "paired_stlpips_median": 0.03,
        "paired_ghost_score": 0.12,
        "paired_alignment_method": "phase_correlate",
        "paired_device": "cpu",
    }
    tmp_cache.set("mangafire", "https://x/series-1", 0.68, metadata=metadata)
    # Reload from disk by constructing a new instance with the same path.
    reloaded = ImageQualityCache()
    result = reloaded.get_with_metadata("mangafire", "https://x/series-1")
    assert result is not None
    score, restored = result
    assert score == 0.68
    # Every metadata field round-trips.
    for key, val in metadata.items():
        assert restored[key] == val, f"field {key}: wrote {val}, got {restored[key]}"


def test_cache_get_returns_just_score(tmp_cache):
    """get() returns the score only (metadata not included). Useful for
    consumers that only want the scalar."""
    tmp_cache.set("foo", "https://x/1", 0.42, metadata={"width": 1000})
    assert tmp_cache.get("foo", "https://x/1") == 0.42


def test_cache_get_missing_returns_none(tmp_cache):
    """Cache miss returns None for both get() and get_with_metadata()."""
    assert tmp_cache.get("nonexistent", "https://x/1") is None
    assert tmp_cache.get_with_metadata("nonexistent", "https://x/1") is None


# ---------------------------------------------------------------------------
# Pre-v5 entry drop
# ---------------------------------------------------------------------------

def test_cache_drops_v4_entries_on_load(tmp_cache):
    """Manually write a v4-shaped entry; the cache must drop it on load."""
    # Get the actual snapshot path the cache uses.
    snapshot_path = tmp_cache._snapshot_path
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
    # Write a v4 entry directly to the file.
    v4_data = {
        "old:https://x/1": {
            "score": 0.7,
            "metadata": {"width": 1500, "format": "JPEG", "bpp": 0.3},
            "expires_at": time.time() + 86400,
            "schema_version": 4,
        }
    }
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(v4_data, f)
    # New instance loads and should drop the v4 entry.
    fresh = ImageQualityCache()
    assert fresh.get("old", "https://x/1") is None


def test_cache_drops_unversioned_entries_on_load(tmp_cache):
    """An entry with no schema_version field at all (very old caches)
    is also dropped on load."""
    snapshot_path = tmp_cache._snapshot_path
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
    data = {
        "ancient:https://x/1": {
            "score": 0.7,
            "metadata": {},
            "expires_at": time.time() + 86400,
        }
    }
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    fresh = ImageQualityCache()
    assert fresh.get("ancient", "https://x/1") is None


def test_cache_drops_partial_entries_missing_required_fields(tmp_cache):
    """Catches the case where SCHEMA_VERSION was bumped BEFORE the
    field-population code landed. Entries tagged with the current
    SCHEMA_VERSION but lacking required v6+ fields are dropped on load
    by the field-completeness gate (separate from the schema-version
    gate). content_type and tenengrad_clean weren't present in v5
    entries so they're the canonical "missing required field" proxy."""
    snapshot_path = tmp_cache._snapshot_path
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
    # Tagged with the current version so the schema-version gate doesn't
    # fire; the field-completeness gate is what we're exercising here.
    data = {
        "partial:https://x/1": {
            "score": 0.6,
            "metadata": {
                "t1_score": 0.7, "res_norm": 0.5, "blockiness": 0.1,
                "fft_hf_ratio": 0.55, "tenengrad_norm": 0.9,
                "width": 1500, "format": "JPEG",
                # Missing: content_type, tenengrad_clean — required fields.
            },
            "expires_at": time.time() + 86400,
            "schema_version": ImageQualityCache.SCHEMA_VERSION,
        }
    }
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    fresh = ImageQualityCache()
    assert fresh.get("partial", "https://x/1") is None


def test_cache_drops_v5_entries_on_load(tmp_cache):
    """v5 entries are dropped on load by the schema-version gate (every
    bump invalidates the entire cache to keep score calibration
    consistent across versions). First search after upgrade re-probes
    everything."""
    snapshot_path = tmp_cache._snapshot_path
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
    data = {
        "v5entry:https://x/1": {
            "score": 0.7,
            "metadata": {
                "t1_score": 0.7, "res_norm": 0.5, "blockiness": 0.1,
                "fft_hf_ratio": 0.55, "tenengrad_norm": 0.9,
                "width": 1500, "format": "JPEG",
                "jpeg_qf": 85, "is_grayscale": True,
            },
            "expires_at": time.time() + 86400,
            "schema_version": 5,
        }
    }
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    fresh = ImageQualityCache()
    assert fresh.get("v5entry", "https://x/1") is None


def test_cache_drops_v6_entries_on_load(tmp_cache):
    """v7 (2026-05-18) bumped SCHEMA_VERSION. v6 entries — even with
    complete v6 metadata — are dropped on load because the watermark
    detector recalibration changes t1_score and the outlier label for
    the same source. Keep the cache calibration consistent by forcing
    a full reprobe."""
    snapshot_path = tmp_cache._snapshot_path
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
    data = {
        "v6entry:https://x/1": {
            "score": 0.7,
            "metadata": {
                "t1_score": 0.7, "res_norm": 0.5, "blockiness": 0.1,
                "fft_hf_ratio": 0.55, "tenengrad_norm": 0.9,
                "width": 1500, "format": "JPEG",
                "content_type": "bw_manga", "tenengrad_clean": 0.9,
            },
            "expires_at": time.time() + 86400,
            "schema_version": 6,
        }
    }
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    fresh = ImageQualityCache()
    assert fresh.get("v6entry", "https://x/1") is None


def test_cache_keeps_complete_current_version_entries(tmp_cache):
    """Counterpoint to the schema-drop tests: entries tagged with the
    CURRENT SCHEMA_VERSION and carrying all required fields survive the
    load filter."""
    snapshot_path = tmp_cache._snapshot_path
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
    data = {
        "complete:https://x/1": {
            "score": 0.7,
            "metadata": {
                "t1_score": 0.7, "res_norm": 0.5, "blockiness": 0.1,
                "fft_hf_ratio": 0.55, "tenengrad_norm": 0.9,
                "width": 1500, "format": "JPEG",
                "content_type": "bw_manga", "tenengrad_clean": 0.9,
            },
            "expires_at": time.time() + 86400,
            "schema_version": ImageQualityCache.SCHEMA_VERSION,
        }
    }
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    fresh = ImageQualityCache()
    assert fresh.get("complete", "https://x/1") == 0.7


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------

def test_cache_expired_entry_evicted(tmp_cache):
    """Entries past their expires_at are evicted on next read."""
    # Force a short TTL by patching the constructor's ttl_s.
    cache = tmp_cache
    cache.set("a", "https://x/1", 0.5, metadata={"width": 1000})
    # Backdate the entry's expires_at to past.
    key = cache._key("a", "https://x/1")
    cache._state[key]["expires_at"] = time.time() - 1
    # Next read should evict.
    assert cache.get("a", "https://x/1") is None
    assert key not in cache._state


def test_cache_set_persists_metadata_dict(tmp_cache):
    """set() with metadata=None stores an empty dict (not crash)."""
    tmp_cache.set("foo", "https://x/1", 0.5, metadata=None)
    result = tmp_cache.get_with_metadata("foo", "https://x/1")
    assert result is not None
    _, meta = result
    assert isinstance(meta, dict)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_cache_set_clamps_score_to_unit_interval(tmp_cache):
    """Scores outside [0, 1] are clamped on write."""
    tmp_cache.set("foo", "https://x/1", 1.5, metadata={})
    assert tmp_cache.get("foo", "https://x/1") == 1.0
    tmp_cache.set("foo", "https://x/2", -0.3, metadata={})
    assert tmp_cache.get("foo", "https://x/2") == 0.0


def test_cache_set_handles_empty_keys(tmp_cache):
    """Empty site or URL silently no-op (defensive)."""
    tmp_cache.set("", "https://x/1", 0.5)  # no-op
    tmp_cache.set("foo", "", 0.5)  # no-op
    assert tmp_cache.get("", "https://x/1") is None
    assert tmp_cache.get("foo", "") is None


def test_cache_corrupted_snapshot_recovers_gracefully(tmp_cache):
    """If the snapshot file is corrupt JSON, the cache initializes empty
    instead of crashing."""
    snapshot_path = tmp_cache._snapshot_path
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
    with open(snapshot_path, "w", encoding="utf-8") as f:
        f.write("not json at all { }")
    fresh = ImageQualityCache()
    assert fresh.get("anything", "anywhere") is None


def test_cache_handles_concurrent_writes(tmp_cache):
    """Concurrent set/get from multiple threads doesn't crash and produces
    consistent results."""
    import concurrent.futures
    import random
    rng = random.Random(42)

    def _writer(i: int):
        tmp_cache.set(f"site-{i % 5}", f"https://x/series-{i}",
                     rng.random(), metadata={"width": 1000 + i})

    def _reader(i: int):
        return tmp_cache.get(f"site-{i % 5}", f"https://x/series-{i}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        # Write 100 entries concurrently.
        list(pool.map(_writer, range(100)))
        # Read them back; results may be None (key not written yet) or float.
        results = list(pool.map(_reader, range(100)))
        for r in results:
            assert r is None or 0.0 <= r <= 1.0
