"""Tests for the v9 ImageQualityCache (sites/search_orchestrator.py).

The cache stores, per (site, series_url), the matched-page probe's VERDICT for
that source and its PAIR EDGES to peers (sites/match_session.py:SourceVerdict /
PairEdge); ratings are recomputed at read time, so nothing here checks a score
number. Covers: round trip of verdicts and edges (edges on both endpoints, each
oriented from its owner), version gate, per-status TTLs, D7 (failure verdicts
survive a reload), the pending-never-replaces-served rule, the one-shot purge
mechanism, corrupt files and concurrent writers.

End-to-end use with a real MatchSession lives in tests/test_match_session.py
(test_cache_roundtrip).
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from sites import match_session as ms
from sites.search_orchestrator import ImageQualityCache, IMG_QUALITY_TTL_S


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """An ImageQualityCache whose file lives in the test's tmp dir (expanduser
    patched so the real ~/.aio-dl/cache/ is never touched)."""
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path))
    return ImageQualityCache()


def _served(key, **kw):
    kw.setdefault("attempted", 6)
    kw.setdefault("succeeded", 6)
    kw.setdefault("edition", "original")
    kw.setdefault("chapters", [4, 9, 15])
    return ms.SourceVerdict(key=key, status="served", **kw)


def _edge(a, b, score=0.2, n=3, same=False, at=None):
    return ms.PairEdge(a, b, score, n, {"sharp": score, "res": 0.0, "blk": 0.0},
                       time.time() if at is None else at, same)


def _write_raw(cache, data):
    os.makedirs(os.path.dirname(cache._snapshot_path), exist_ok=True)
    with open(cache._snapshot_path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def test_schema_version_is_9():
    """v9 (2026-10, plan sync-temp/plans/image-ranker-rework.md) stores verdicts
    and pair edges instead of v8's per-source T1/T2/T3 scores."""
    assert ImageQualityCache.SCHEMA_VERSION == 9


def test_round_trip_verdicts_and_edges(tmp_cache):
    a, b = "atsu:https://atsu.moe/m/1", "mangadex:https://mangadex.org/title/1"
    va = _served(a, coverage=120, chroma=0.0123, truncated=False)
    vb = _served(b, coverage=118, unmatched_against=["x:y"])
    tmp_cache.put_records([va, vb], [_edge(a, b, score=0.4, same=False)])

    again = ImageQualityCache()
    got_a, edges_a = again.get_record("atsu", "https://atsu.moe/m/1")
    got_b, edges_b = again.get_record("mangadex", "https://mangadex.org/title/1")
    assert got_a == va
    assert got_b == vb
    # stored on both entries, each in its owner's favour
    assert edges_a[b].score == pytest.approx(0.4)
    assert edges_b[a].score == pytest.approx(-0.4)
    assert edges_b[a].parts["sharp"] == pytest.approx(-0.4)
    assert edges_a[b].n == 3 and edges_a[b].same is False


def test_identical_flag_round_trips(tmp_cache):
    a, b = "mangapill:https://p/1", "weebcentral:https://w/1"
    tmp_cache.put_records([_served(a), _served(b)], [_edge(a, b, score=0.0, same=True)])
    _, edges = ImageQualityCache().get_record("mangapill", "https://p/1")
    assert edges[b].same is True


def test_pair_psnr_round_trips_and_old_records_read_none(tmp_cache):
    """psnr (the same-scan note's input) persists on both entries; a record
    written before the field existed reads psnr=None (no same-scan claim)."""
    a, b, c = "atsu:https://a/1", "mangapill:https://p/1", "weebcentral:https://w/1"
    e = ms.PairEdge(a, b, 0.1, 3, {"sharp": 0.1, "res": 0.0, "blk": 0.0}, time.time(), False, 31.237)
    tmp_cache.put_records([_served(a), _served(b), _served(c)], [e, _edge(a, c)])
    _, edges_b = ImageQualityCache().get_record("mangapill", "https://p/1")
    assert edges_b[a].psnr == pytest.approx(31.24)
    _, edges_a = ImageQualityCache().get_record("atsu", "https://a/1")
    assert edges_a[c].psnr is None
    assert ms.edge_from_json(a, c, {"score": 0.1, "n": 2, "parts": {}}).psnr is None


def test_miss_and_empty_keys_return_none(tmp_cache):
    assert tmp_cache.get_record("nope", "https://x/1") is None
    assert tmp_cache.get_record("", "https://x/1") is None
    assert tmp_cache.get_record("foo", "") is None


def test_failure_verdicts_survive_reload(tmp_cache):
    """D7: v8 rebuilt entries from scorer fields at load and so dropped every
    failure verdict, re-probing dead sites on each search."""
    dead = ms.SourceVerdict(key="dead:https://d/1", status="unreachable",
                            reason="pages_failed", attempted=4)
    tmp_cache.put_records([dead], [])
    got, edges = ImageQualityCache().get_record("dead", "https://d/1")
    assert got.status == "unreachable" and got.reason == "pages_failed"
    assert edges == {}


@pytest.mark.parametrize("version", [None, 4, 8])
def test_other_versions_are_dropped_on_load(tmp_cache, version):
    entry = {"expires_at": time.time() + 86400,
             "verdict": {"status": "served"}, "pairs": {}}
    if version is not None:
        entry["schema_version"] = version
    _write_raw(tmp_cache, {"old:https://x/1": entry})
    assert ImageQualityCache().get_record("old", "https://x/1") is None


def test_current_version_entry_without_verdict_is_dropped(tmp_cache):
    _write_raw(tmp_cache, {"bad:https://x/1": {
        "schema_version": 9, "expires_at": time.time() + 86400, "score": 0.7}})
    assert ImageQualityCache().get_record("bad", "https://x/1") is None


def test_current_version_entry_is_kept(tmp_cache):
    _write_raw(tmp_cache, {"ok:https://x/1": {
        "schema_version": 9, "expires_at": time.time() + 86400,
        "verdict": {"status": "served", "succeeded": 5}, "pairs": "not a dict"}})
    got, edges = ImageQualityCache().get_record("ok", "https://x/1")
    assert got.status == "served" and got.succeeded == 5
    assert edges == {}   # a malformed pairs field reads as none


@pytest.mark.parametrize("verdict,ttl", [
    (_served("s:u"), ms.TTL_SERVED_S),
    (_served("s:u", truncated=True), ms.TTL_TRUNCATED_S),
    (ms.SourceVerdict(key="s:u", status="unreachable"), ms.TTL_UNREACHABLE_S),
    (ms.SourceVerdict(key="s:u", status="pending"), ms.TTL_PENDING_S),
])
def test_expiry_follows_the_verdict(tmp_cache, verdict, ttl):
    before = time.time()
    tmp_cache.put_records([verdict], [])
    exp = tmp_cache._state["s:u"]["expires_at"]
    assert before + ttl - 1 <= exp <= time.time() + ttl + 1


def test_constructor_ttl_caps_every_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path))
    c = ImageQualityCache(ttl_s=60)
    c.put_records([_served("s:u")], [])
    assert c._state["s:u"]["expires_at"] <= time.time() + 61


def test_expired_entry_reads_as_miss(tmp_cache):
    tmp_cache.put_records([_served("a:u")], [])
    tmp_cache._state["a:u"]["expires_at"] = time.time() - 1
    assert tmp_cache.get_record("a", "u") is None


def test_expired_edge_is_left_out(tmp_cache):
    """Edges expire on their own clock (`at` + IMG_QUALITY_TTL_S), so a peer
    compared long ago stops counting even while the entry itself is fresh."""
    old = time.time() - IMG_QUALITY_TTL_S - 10
    tmp_cache.put_records([_served("a:u"), _served("b:u"), _served("c:u")],
                          [_edge("a:u", "b:u", at=old), _edge("a:u", "c:u")])
    _, edges = tmp_cache.get_record("a", "u")
    assert set(edges) == {"c:u"}


def test_pending_never_replaces_fresh_served(tmp_cache):
    tmp_cache.put_records([_served("a:u", succeeded=6)], [])
    tmp_cache.put_records([ms.SourceVerdict(key="a:u", status="pending")], [])
    got, _ = ImageQualityCache().get_record("a", "u")
    assert got.status == "served" and got.succeeded == 6


def test_a_failed_list_never_replaces_fresh_served(tmp_cache):
    """list_failed is as often this machine as the site (no page evidence):
    a re-probed anchor's one reset must not throw away its measured record."""
    tmp_cache.put_records([_served("a:u", succeeded=6), _served("b:u")], [_edge("a:u", "b:u")])
    tmp_cache.put_records([ms.SourceVerdict(key="a:u", status="unreachable", reason="list_failed")], [])
    got, edges = ImageQualityCache().get_record("a", "u")
    assert got.status == "served" and got.succeeded == 6 and set(edges) == {"b:u"}


def test_pending_replaces_expired_served(tmp_cache):
    tmp_cache.put_records([_served("a:u")], [])
    tmp_cache._state["a:u"]["expires_at"] = time.time() - 1
    tmp_cache.put_records([ms.SourceVerdict(key="a:u", status="pending")], [])
    assert tmp_cache.get_record("a", "u")[0].status == "pending"


def test_new_verdict_keeps_existing_pairs(tmp_cache):
    """A re-probe that did not meet a peer this time keeps the old edge to it."""
    tmp_cache.put_records([_served("a:u"), _served("b:u")], [_edge("a:u", "b:u")])
    tmp_cache.put_records([_served("a:u", succeeded=3)], [])
    got, edges = tmp_cache.get_record("a", "u")
    assert got.succeeded == 3 and "b:u" in edges


def test_shallow_reprobe_keeps_the_deeper_depth(tmp_cache):
    """A top-candidate source (depth 3) re-probed at depth 1 as another
    candidate's anchor must still count as probed at depth 3."""
    tmp_cache.put_records([_served("a:u", depth=3)], [])
    tmp_cache.put_records([_served("a:u", depth=1)], [])
    assert tmp_cache.get_record("a", "u")[0].depth == 3
    # a failure in between resets it: the deep pages are no longer vouched for
    tmp_cache.put_records([ms.SourceVerdict(key="a:u", status="unreachable")], [])
    tmp_cache.put_records([_served("a:u", depth=1)], [])
    assert tmp_cache.get_record("a", "u")[0].depth == 1


def test_edge_to_absent_endpoint_lands_only_on_the_present_one(tmp_cache):
    tmp_cache.put_records([_served("a:u")], [_edge("a:u", "ghost:u")])
    assert "ghost:u" in tmp_cache.get_record("a", "u")[1]
    assert "ghost:u" not in tmp_cache._state


def test_one_shot_purge_runs_once(tmp_cache):
    """Mechanism: the first load with no marker drops the prefix's entries and
    writes the marker; later loads keep entries written by the fixed code."""
    marker = os.path.join(os.path.dirname(tmp_cache._snapshot_path),
                          "img_quality.purged-mangadex-linkouts")
    assert os.path.exists(marker)   # the fixture's load already ran it
    os.remove(marker)
    tmp_cache.put_records([_served("mangadex:https://m/a"), _served("atsu:https://a/a")], [])

    first = ImageQualityCache()
    assert first.get_record("mangadex", "https://m/a") is None
    assert first.get_record("atsu", "https://a/a") is not None
    assert os.path.exists(marker)
    with open(first._snapshot_path, encoding="utf-8") as f:
        assert list(json.load(f)) == ["atsu:https://a/a"]

    first.put_records([_served("mangadex:https://m/a")], [])
    assert ImageQualityCache().get_record("mangadex", "https://m/a") is not None


def test_corrupt_snapshot_starts_empty(tmp_cache):
    os.makedirs(os.path.dirname(tmp_cache._snapshot_path), exist_ok=True)
    with open(tmp_cache._snapshot_path, "w", encoding="utf-8") as f:
        f.write("not json at all { }")
    assert ImageQualityCache().get_record("anything", "anywhere") is None


def test_concurrent_writers_and_readers(tmp_cache):
    errors = []

    def writer(i):
        try:
            k = f"site{i % 5}:https://x/{i}"
            tmp_cache.put_records([_served(k)], [_edge(k, f"site{(i + 1) % 5}:https://x/{i + 1}")])
        except Exception as e:   # pragma: no cover - reported below
            errors.append(e)

    def reader(i):
        try:
            r = tmp_cache.get_record(f"site{i % 5}", f"https://x/{i}")
            assert r is None or r[0].status == "served"
        except Exception as e:   # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=f, args=(i,)) for i in range(60) for f in (writer, reader)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    assert all(tmp_cache.get_record(f"site{i % 5}", f"https://x/{i}") for i in range(60))
