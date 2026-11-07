"""series_ledger: per-kind namespaces in .aio_series.json and the locked write.

Covered here:
  - A legacy (chapter-only) ledger reads as chapters and needs no migration.
  - Neither kind's write touches the other's namespace. In particular a volume
    run never rewrites the top-level `format`/`language`, which chapter updates
    replay.
  - tracked_kinds for every combination, including the self-heal for a
    volume block that an older build dropped.
  - The read-modify-write re-reads under the lock, so a stale snapshot cannot
    erase a peer's labels (checked in-process and across two processes).
  - The shared fixture the JS twin (UI-source/electron/series-ledger.js) also
    reads.
"""
from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

import series_ledger as sl
from item_kinds import CHAPTER, VOLUME

FIXTURE = Path(__file__).parent / "fixtures" / "series_ledger_cases.json"

LEGACY = {
    "url": "https://mangafire.to/title/dkw-one-piece",
    "hid": "dkw",
    "title": "One Piece",
    "site": "mangafire",
    "format": "pdf",
    "language": "en",
    "download_volumes": False,
    "chapters_downloaded": ["1", "2", "3"],
    "chapters_ignored": ["4"],
}


def _write(folder, meta):
    (Path(folder) / sl.SERIES_META_FILE).write_text(json.dumps(meta), encoding="utf-8")


def test_legacy_ledger_reads_as_chapters_only():
    assert sl.tracked_kinds(LEGACY) == {CHAPTER}
    view = sl.kind_view(LEGACY, CHAPTER)
    assert view["downloaded"] == ["1", "2", "3"]
    assert view["ignored"] == ["4"]
    assert view["format"] == "pdf"
    assert sl.kind_view(LEGACY, VOLUME)["downloaded"] == []


def test_volume_run_leaves_the_chapter_namespace_byte_for_byte():
    out = sl.merge_volume_run(
        LEGACY, shared={"title": "One Piece", "status": "Ongoing"},
        downloaded=["1", "2.0"], fmt="cbz", language="en", total_available=117,
        now="2026-11-06T00:00:00Z",
    )
    for key, value in LEGACY.items():
        assert out[key] == value, key
    assert out["status"] == "Ongoing"
    assert out["volumes"] == {
        "schema": 1, "downloaded": ["1", "2"], "format": "cbz", "language": "en",
        "total_available_at_download": 117, "last_downloaded_at": "2026-11-06T00:00:00Z",
    }
    assert sl.tracked_kinds(out) == {CHAPTER, VOLUME}


def test_volume_run_refuses_chapter_keys_in_shared():
    with pytest.raises(ValueError):
        sl.merge_volume_run(LEGACY, shared={"format": "cbz"}, downloaded=[],
                            fmt="cbz", language="en", total_available=None)


def test_volume_run_unions_and_clears_cross_outs_it_now_has():
    first = sl.merge_volume_run({}, shared={}, downloaded=["1"], fmt="cbz",
                                language="en", total_available=3)
    first = sl.set_ignored(first, VOLUME, ["2", "3"])
    second = sl.merge_volume_run(first, shared={}, downloaded=["2"], fmt="cbz",
                                 language="en", total_available=3)
    assert second["volumes"]["downloaded"] == ["1", "2"]
    assert second["volumes"]["ignored"] == ["3"]


def test_chapter_writer_carries_the_volume_block_through_a_rebuild():
    existing = sl.merge_volume_run(LEGACY, shared={}, downloaded=["5"], fmt="cbz",
                                   language="en", total_available=10)
    rebuilt = {"url": "u", "chapters_downloaded": ["1"]}
    out = sl.carry_other_namespaces(existing, rebuilt)
    assert out["volumes"] == existing["volumes"]
    # A chapter-only ledger gains no key, so its bytes match the old writer's.
    assert "volumes" not in sl.carry_other_namespaces(LEGACY, {"url": "u"})


@pytest.mark.parametrize(
    "meta,files,expected",
    [
        ({}, False, {CHAPTER}),
        ({"chapters_downloaded": ["1"]}, False, {CHAPTER}),
        ({"volumes": {"downloaded": ["1"]}}, False, {VOLUME}),
        ({"chapters_downloaded": ["1"], "volumes": {"downloaded": []}}, False, {CHAPTER, VOLUME}),
        # An empty volume block still means the user chose volumes here.
        ({"volumes": {"downloaded": []}}, False, {VOLUME}),
        # Self-heal: an old build dropped the block, but volume files remain.
        ({"chapters_downloaded": []}, True, {VOLUME}),
        ({"chapters_downloaded": ["1"]}, True, {CHAPTER, VOLUME}),
    ],
)
def test_tracked_kinds(meta, files, expected):
    assert sl.tracked_kinds(meta, volume_files_present=files) == expected


def test_set_ignored_per_kind_and_absent_means_empty():
    m = sl.set_ignored(LEGACY, CHAPTER, [])
    assert "chapters_ignored" not in m
    m = sl.set_ignored(m, VOLUME, ["3", "1.0"])
    assert m["volumes"]["ignored"] == ["1", "3"]
    assert "chapters_ignored" not in m
    m = sl.set_ignored(m, VOLUME, [])
    assert "ignored" not in m["volumes"]


def test_set_ignored_empty_never_creates_the_volume_block():
    # A block alone makes a series track volumes, so clearing volume
    # cross-outs on a chapter-only series must not start tracking them.
    m = sl.set_ignored(LEGACY, VOLUME, [])
    assert "volumes" not in m
    assert sl.tracked_kinds(m) == {CHAPTER}


def test_update_rereads_under_the_lock(tmp_path):
    _write(tmp_path, LEGACY)
    stale = sl.load_series_meta(str(tmp_path))
    # A peer volume run lands after `stale` was read...
    sl.update_series_meta(str(tmp_path), lambda m: sl.merge_volume_run(
        m, shared={}, downloaded=["1"], fmt="cbz", language="en", total_available=1))
    # ...and this chapter run's mutate gets the CURRENT ledger, not `stale`.
    seen = {}

    def chapter_mutate(current):
        seen["had_volumes"] = "volumes" in current
        rebuilt = dict(stale)
        rebuilt["chapters_downloaded"] = ["1", "2", "3", "4"]
        return sl.carry_other_namespaces(current, rebuilt)

    sl.update_series_meta(str(tmp_path), chapter_mutate)
    final = sl.load_series_meta(str(tmp_path))
    assert seen["had_volumes"]
    assert final["volumes"]["downloaded"] == ["1"]
    assert final["chapters_downloaded"] == ["1", "2", "3", "4"]


def test_update_returning_none_writes_nothing(tmp_path):
    _write(tmp_path, LEGACY)
    before = (tmp_path / sl.SERIES_META_FILE).read_bytes()
    assert sl.update_series_meta(str(tmp_path), lambda m: None) == LEGACY
    assert (tmp_path / sl.SERIES_META_FILE).read_bytes() == before


def test_update_writes_the_same_bytes_as_json_dump_indent_2(tmp_path):
    # The old writer used json.dump(meta, f, indent=2). A chapter-only ledger
    # must come out byte-identical through the new path.
    sl.update_series_meta(str(tmp_path), lambda m: dict(LEGACY))
    assert (tmp_path / sl.SERIES_META_FILE).read_text(encoding="utf-8") == json.dumps(LEGACY, indent=2)
    # Nothing but the ledger lands in the series folder: no temp file and no
    # lock file. series-merge.js would read a lock file as a leftover and
    # refuse to remove a merged folder.
    assert sorted(p.name for p in tmp_path.iterdir()) == [sl.SERIES_META_FILE]


def _append_labels(folder, kind, labels, barrier):
    import series_ledger as sl_child

    barrier.wait()
    for label in labels:
        if kind == "chapter":
            def mutate(m, label=label):
                m["chapters_downloaded"] = sl_child.sorted_labels(
                    list(m.get("chapters_downloaded") or []) + [label])
                return m
        else:
            def mutate(m, label=label):
                return sl_child.merge_volume_run(
                    m, shared={}, downloaded=[label], fmt="cbz", language="en",
                    total_available=None)
        sl_child.update_series_meta(folder, mutate)


def test_two_processes_writing_both_kinds_lose_nothing(tmp_path):
    _write(tmp_path, {"chapters_downloaded": []})
    labels = [str(i) for i in range(1, 16)]
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    procs = [
        ctx.Process(target=_append_labels, args=(str(tmp_path), "chapter", labels, barrier)),
        ctx.Process(target=_append_labels, args=(str(tmp_path), "volume", labels, barrier)),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    final = sl.load_series_meta(str(tmp_path))
    assert final["chapters_downloaded"] == sl.sorted_labels(labels)
    assert final["volumes"]["downloaded"] == sl.sorted_labels(labels)


def test_shared_fixture_cases():
    """The same cases are asserted by the JS twin (series-ledger.js)."""
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert cases["tracked_kinds"], "fixture must not be empty"
    for case in cases["tracked_kinds"]:
        got = sl.tracked_kinds(case["meta"], volume_files_present=case.get("volume_files_present", False))
        assert sorted(got) == sorted(case["expected"]), case["name"]
    for case in cases["kind_view"]:
        got = sl.kind_view(case["meta"], case["kind"])
        for key, value in case["expected"].items():
            assert got[key] == value, (case["name"], key)
