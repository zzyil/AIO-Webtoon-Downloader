"""The combined series archive must never be replaced by a partial run.

Pins android/PARITY.md §2 D1 ("a partial run overwrites the complete series
archive, and the metadata hides it") and the accounting half of §2 D4.

WHAT BROKE. aio-dl.py's end-of-run gate was `if current_book_content and not
aborted_remaining:` — no cancellation term, and no check that this run's chapter
set covers what the archive on disk already holds. Both writers TRUNCATE
(`ZipFile(out_path, "w")` in build_cbz_from_content; `open(..., "wb")` under
merge_pdf_files) and the target is the same `<base_filename>.<format>` a complete
run produced, so two routine paths destroyed a finished library:

  - DELTA RUN. The update-check flow passes only the missing range as
    `--chapters` (UI-source/src/lib/downloadArgs.js:buildLibraryDownloadArgs,
    both platforms), so a 3-chapter run rewrote a 50-chapter Series.cbz. Note
    the run's set is DISJOINT from what is on disk, not a subset of it — a
    strict-subset predicate would sail straight past the headline case, which is
    why _final_file_would_shrink asks "does the archive cover chapters this run
    does not".
  - COOPERATIVE CANCEL. The chapter loop breaks and falls through to the same
    gate. Desktop escapes only because its cancel kills the process outright.

The floor was ONE PAGE, not one chapter: for CBZ the cover is appended to
`current_book_content` before the loop, so the gate was already true with zero
chapters downloaded. And `.aio_series.json`'s `chapters_downloaded` is UNIONed
with the prior file's on every write, so the library update-check then saw
nothing missing and nothing ever offered to repair it.

D4's half: `pages_ok`/`pages_total` are measured at DOWNLOAD time and the
completeness gate runs there, but the PIL decode happens much later. A page that
downloads and then fails to decode was dropped silently, so the chapter logged
N/N and shipped short. On the recombine path the output page count is 1:N by
design, so counting outputs proves nothing — the decode helpers now report the
DROPPED SOURCE PATHS instead.

Offline only: every test drives module-level pure helpers or real zipfile/JSON
round-trips in a tmp_path. Nothing here touches the network or PIL.

Cross-file:
  - aio-dl.py — `_final_file_would_shrink`, `_final_file_recorded_coverage`,
    `_load_series_meta`, `final_file_built_labels`, `final_build_stranded`,
    `decode_dropped_pages`.
  - UI-source/src/lib/downloadArgs.js — produces the delta arg shape.
  - android/PARITY.md §2 D1 / §2 D4.
"""

from __future__ import annotations

import importlib
import inspect
import io
import json
import os
import re
import zipfile

import pytest

aio = importlib.import_module("aio-dl")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_meta(out_dir, **fields):
    """Write a `.aio_series.json` into out_dir and return its path."""
    path = os.path.join(str(out_dir), ".aio_series.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fields, f)
    return path


def _make_archive(path, members=("0000.jpg", "0001.jpg")):
    """A stand-in for a complete series CBZ. Returns its bytes."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        for name in members:
            z.writestr(name, b"x" * 32)
    with open(path, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# _final_file_recorded_coverage — what the archive on disk is believed to hold
# ---------------------------------------------------------------------------

def test_coverage_prefers_final_file_chapters_over_the_union():
    """`chapters_downloaded` is a running union across runs and therefore cannot
    answer "would rebuilding lose something"; `final_file_chapters` is written
    only by a build that really ran, so it wins whenever present."""
    meta = {
        "chapters_downloaded": ["1", "2", "3", "4"],
        "final_file_chapters": ["1", "2"],
    }
    assert aio._final_file_recorded_coverage(meta) == {"1", "2"}


def test_coverage_falls_back_to_chapters_downloaded_when_key_absent():
    """Absent-tolerant: files written before final_file_chapters existed must
    still produce a usable (deliberately over-stated) coverage set."""
    meta = {"chapters_downloaded": ["1", "2", "3"]}
    assert aio._final_file_recorded_coverage(meta) == {"1", "2", "3"}


def test_coverage_normalizes_legacy_float_labels():
    """"4.0" from a float-emitting handler must compare equal to "4" — otherwise
    a genuine superset reads as a shrink and every rebuild is refused."""
    meta = {"final_file_chapters": ["4.0", "5.00", "Oneshot"]}
    assert aio._final_file_recorded_coverage(meta) == {"4", "5", "Oneshot"}


@pytest.mark.parametrize("meta", [None, {}, {"final_file_chapters": "nope"}, 42])
def test_coverage_is_empty_for_junk_metadata(meta):
    assert aio._final_file_recorded_coverage(meta) == set()


# ---------------------------------------------------------------------------
# _final_file_would_shrink — the guard itself
# ---------------------------------------------------------------------------

def test_delta_run_is_blocked_even_though_it_is_not_a_subset():
    """THE HEADLINE CASE. `--chapters 51-53` against an archive holding 1-50.
    The two sets are disjoint, so this is precisely what a strict-subset test
    would have missed."""
    meta = {"final_file_chapters": [str(i) for i in range(1, 51)]}
    verdict = aio._final_file_would_shrink(
        ["51", "52", "53"], meta, final_file_exists=True
    )
    assert verdict is not None
    assert verdict["reason"] == "partial_coverage"
    assert verdict["run_chapters"] == 3
    assert verdict["existing_chapters"] == 50
    assert verdict["dropped_chapters"] == 50


def test_strict_subset_run_is_blocked():
    meta = {"final_file_chapters": ["1", "2", "3", "4", "5"]}
    verdict = aio._final_file_would_shrink(["1", "2"], meta, final_file_exists=True)
    assert verdict is not None
    assert verdict["reason"] == "partial_coverage"
    assert verdict["dropped_chapters"] == 3


def test_equal_coverage_rebuilds():
    meta = {"final_file_chapters": ["1", "2", "3"]}
    assert aio._final_file_would_shrink(
        ["1", "2", "3"], meta, final_file_exists=True
    ) is None


def test_superset_run_rebuilds():
    """A full re-run that ADDS chapters must not be blocked — that is the very
    operation the skip message tells the user to perform."""
    meta = {"final_file_chapters": ["1", "2", "3"]}
    assert aio._final_file_would_shrink(
        ["1", "2", "3", "4", "5"], meta, final_file_exists=True
    ) is None


def test_float_labelled_superset_is_not_mistaken_for_a_shrink():
    """The "4.0" vs "4" trap: without normalization on BOTH sides this reads as
    a shrink and a legitimate rebuild is refused forever."""
    meta = {"chapters_downloaded": ["4.0", "5.0"]}
    assert aio._final_file_would_shrink(
        [4.0, 5, 6], meta, final_file_exists=True
    ) is None


def test_no_existing_file_always_rebuilds():
    """Nothing on disk means no loss is possible. This is also what keeps the
    guard from misfiring on split / --no-final-file / komikku histories, whose
    chapters_downloaded describes output that never had a combined archive."""
    meta = {"chapters_downloaded": [str(i) for i in range(1, 101)]}
    assert aio._final_file_would_shrink(["7"], meta, final_file_exists=False) is None


def test_first_ever_run_rebuilds():
    assert aio._final_file_would_shrink(["1", "2"], {}, final_file_exists=False) is None


def test_one_page_floor_cover_only_run_never_overwrites():
    """For CBZ the cover is appended to current_book_content before the chapter
    loop, so the build gate is true with ZERO chapters. Blocked even with no
    metadata at all, because there is no reading of "0 chapters" that justifies
    replacing an existing archive."""
    verdict = aio._final_file_would_shrink([], {}, final_file_exists=True)
    assert verdict is not None
    assert verdict["reason"] == "no_chapters"
    assert verdict["run_chapters"] == 0


def test_cover_only_run_with_metadata_reports_partial_coverage():
    meta = {"final_file_chapters": ["1", "2"]}
    verdict = aio._final_file_would_shrink([], meta, final_file_exists=True)
    assert verdict is not None
    assert verdict["dropped_chapters"] == 2


def test_sample_is_numerically_sorted_and_truncated():
    """The printed sample must read as chapters, not as sorted strings — "10"
    must not precede "2"."""
    sample = aio._format_chapter_label_sample(["10", "2", "1"], limit=6)
    assert sample == "1, 2, 10"
    long = aio._format_chapter_label_sample([str(i) for i in range(1, 20)], limit=3)
    assert long.startswith("1, 2, 3")
    assert "+16 more" in long


def test_sample_tolerates_non_numeric_labels():
    """_chap_as_float returns None for "Oneshot"; the sort key must not raise."""
    sample = aio._format_chapter_label_sample(["Oneshot", "2", "1"])
    assert sample == "1, 2, Oneshot"


# ---------------------------------------------------------------------------
# _load_series_meta
# ---------------------------------------------------------------------------

def test_load_series_meta_round_trips(tmp_path):
    _write_meta(tmp_path, chapters_downloaded=["1"], final_file_chapters=["1"])
    assert aio._load_series_meta(str(tmp_path))["final_file_chapters"] == ["1"]


@pytest.mark.parametrize("body", ["", "{not json", "[1, 2, 3]", "null"])
def test_load_series_meta_degrades_to_empty_on_junk(tmp_path, body):
    """A corrupt metadata file must degrade to "no prior knowledge" — never
    abort a finished download, and never be mistaken for a dict."""
    (tmp_path / ".aio_series.json").write_text(body, encoding="utf-8")
    assert aio._load_series_meta(str(tmp_path)) == {}


def test_load_series_meta_missing_file_and_empty_dir():
    assert aio._load_series_meta(str(os.path.join(os.sep, "nope", "nope"))) == {}
    assert aio._load_series_meta("") == {}


# ---------------------------------------------------------------------------
# End-to-end byte check: the guard's decision drives a real truncating write
# ---------------------------------------------------------------------------

def _simulate_gate(final_path, run_labels, meta, cancelled=False):
    """Mirror of main()'s combined-archive branch, minus the download engine.

    Reproduces the ORDER that matters: compute final_path, ask the guard, and
    only then perform the truncating write. Returns (built, verdict).
    """
    if cancelled:
        return False, {"reason": "cancelled"}
    verdict = aio._final_file_would_shrink(
        run_labels, meta, final_file_exists=os.path.exists(final_path)
    )
    if verdict:
        return False, verdict
    with zipfile.ZipFile(final_path, "w", zipfile.ZIP_STORED) as z:
        for label in run_labels:
            z.writestr(f"{label}.jpg", b"new")
    return True, None


def test_subset_run_leaves_the_existing_archive_byte_identical(tmp_path):
    final_path = str(tmp_path / "Series.cbz")
    before = _make_archive(final_path, [f"{i:04d}.jpg" for i in range(50)])
    meta = {"final_file_chapters": [str(i) for i in range(1, 51)]}

    built, verdict = _simulate_gate(final_path, ["51", "52", "53"], meta)

    assert built is False
    assert verdict["reason"] == "partial_coverage"
    with open(final_path, "rb") as f:
        assert f.read() == before, "the complete archive must not be touched"


def test_cover_only_run_leaves_the_existing_archive_byte_identical(tmp_path):
    final_path = str(tmp_path / "Series.cbz")
    before = _make_archive(final_path)
    built, verdict = _simulate_gate(final_path, [], {})
    assert built is False and verdict["reason"] == "no_chapters"
    with open(final_path, "rb") as f:
        assert f.read() == before


def test_cancelled_run_does_not_build_at_all(tmp_path):
    final_path = str(tmp_path / "Series.cbz")
    built, verdict = _simulate_gate(final_path, ["1", "2"], {}, cancelled=True)
    assert built is False and verdict["reason"] == "cancelled"
    assert not os.path.exists(final_path), (
        "a cancelled run must not create the combined archive either — its "
        "tmp_<hid>/ is kept and the resume builds it properly"
    )


def test_superset_run_really_rebuilds(tmp_path):
    final_path = str(tmp_path / "Series.cbz")
    _make_archive(final_path, ["0000.jpg"])
    meta = {"final_file_chapters": ["1"]}
    built, verdict = _simulate_gate(final_path, ["1", "2", "3"], meta)
    assert built is True and verdict is None
    with zipfile.ZipFile(final_path) as z:
        assert z.namelist() == ["1.jpg", "2.jpg", "3.jpg"]


# ---------------------------------------------------------------------------
# Immune modes — the guard lives in ONE branch and must not reach the others
# ---------------------------------------------------------------------------

def _final_gate_source():
    """The source text of main()'s combined-archive gate, from the
    `if current_book_content` line to the details.json patch that follows it."""
    src = inspect.getsource(aio.main)
    start = src.index("if current_book_content and not aborted_remaining:")
    end = src.index("_patch_details_json_with_assets", start)
    return src[start:end]


def test_guard_sits_below_the_three_immune_branches():
    """--no-final-file, --format none and split mode are checked BEFORE the
    guard, so none of them can reach it. Order is the whole contract here."""
    block = _final_gate_source()
    i_nff = block.index("args.no_final_file")
    i_none = block.index('args.format == "none"')
    i_split = block.index("split_size_bytes > 0 or split_chapter_count > 0")
    i_guard = block.index("_final_file_would_shrink")
    assert i_nff < i_none < i_split < i_guard


def test_split_mode_still_calls_build_book_part_unguarded():
    """Split writes a differently-named part file, so it was never the hazard;
    keeping it out of the guard keeps that behaviour byte-for-byte."""
    block = _final_gate_source()
    split_branch = block[
        block.index("split_size_bytes > 0 or split_chapter_count > 0"):
        block.index("_final_file_would_shrink")
    ]
    assert "build_book_part(" in split_branch
    assert "run_cancelled" not in split_branch


def test_komikku_forces_no_final_file():
    """Komikku's immunity is structural: it coerces --no-final-file, which is the
    first branch above. Guarded here so a future refactor of that coercion
    cannot silently expose komikku runs to the combined-file path."""
    src = inspect.getsource(aio.main)
    block = src[src.index('if getattr(args, "komikku", False):'):]
    block = block[: block.index("if args.no_final_file and (not args.keep_chapters)")]
    assert "args.no_final_file = True" in block
    assert 'args.format = "cbz"' in block
    assert "args.keep_chapters = True" in block


def test_cancel_term_is_inside_the_combined_branch_only():
    """The bug was a missing run_cancelled() term at this gate. It must now be
    present — and only in the combined-archive branch."""
    block = _final_gate_source()
    assert "run_cancelled()" in block


# ---------------------------------------------------------------------------
# final_file_chapters — written on a real build, absent-tolerant on read
# ---------------------------------------------------------------------------

def test_final_file_chapters_is_written_only_when_a_build_happened():
    """The writer keys on `final_file_built_labels is not None`, which only the
    three build branches set. A skipped / --no-final-file / komikku / split run
    must carry the previous value forward instead of recording a lie."""
    src = inspect.getsource(aio.main)
    assert "if final_file_built_labels is not None:" in src
    # This run may only ever touch ITS OWN format's entry; every other format's
    # entry describes a file that is still on disk and must survive untouched.
    assert "final_file_chapters[args.format] = list(final_file_built_labels)" in src
    # Both prior shapes are carried forward: the {format: labels} map, and the
    # legacy single list (migrated under meta["format"]).
    assert 'isinstance(_prev_ffc, dict)' in src
    assert 'isinstance(_prev_ffc, list)' in src
    # Exactly three assignment sites: epub, cbz, and the pdf branch's inner
    # `if pdf_inputs:` (a PDF run with no chapter PDFs writes no file at all).
    assert src.count("final_file_built_labels = built_labels") == 3


def test_final_file_chapters_survives_a_run_that_built_nothing():
    """Absent-tolerant in the other direction: carrying the key forward is what
    lets a later run still know what the archive holds."""
    prior = {"final_file_chapters": ["1", "2", "3"]}
    assert aio._final_file_recorded_coverage(prior) == {"1", "2", "3"}
    # ...and a file that never had the key is not treated as "covers nothing".
    assert aio._final_file_recorded_coverage(
        {"chapters_downloaded": ["1", "2", "3"]}
    ) == {"1", "2", "3"}


def test_stranded_run_withholds_labels_from_the_downloaded_union():
    """The concealment half of D1. When the build is declined AND --keep-chapters
    is off, the chapters live only in tmp_<hid>/ — recording them would make the
    update-check report a series complete whose bytes are in a temp folder."""
    src = inspect.getsource(aio.main)
    assert "final_build_stranded = not getattr(args, \"keep_chapters\", False)" in src
    assert "if final_build_stranded" in src
    # ...and that same flag keeps the temp folder alive.
    assert (
        "not args.no_cleanup and not run_cancelled() and not final_build_stranded"
        in src
    )


# ---------------------------------------------------------------------------
# D4 — decode drops must be accounted for, not silently shipped
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Coverage is PER FORMAT
#
# One .aio_series.json serves the series folder, but there is one archive per
# FORMAT in it. An unqualified coverage list meant the last build of any format
# described every archive — export 3 chapters as EPUB into a folder holding a
# 50-chapter CBZ, re-run CBZ over any superset of those 3, and the guard saw
# nothing dropped and truncated the CBZ. Same hazard, side door.
# ---------------------------------------------------------------------------


def test_an_epub_export_cannot_describe_the_cbz_archive():
    """THE regression. Without the format key this returns None (= rebuild) and
    the 50-chapter CBZ is replaced by a 5-chapter one."""
    meta = {
        "format": "cbz",
        "final_file_chapters": {
            "cbz": [str(n) for n in range(1, 51)],
            "epub": ["1", "2", "3"],
        },
        "chapters_downloaded": [str(n) for n in range(1, 51)],
    }
    skip = aio._final_file_would_shrink(
        ["1", "2", "3", "4", "5"], meta, final_file_exists=True, fmt="cbz"
    )
    assert skip is not None
    assert skip["reason"] == "partial_coverage"
    assert skip["existing_chapters"] == 50
    assert skip["dropped_chapters"] == 45


def test_each_format_is_judged_against_its_own_archive():
    """The other direction, and why the fix is a map rather than a veto: a
    legitimate EPUB rebuild covering the EPUB's own 3 chapters must still
    proceed even though the CBZ holds 50."""
    meta = {
        "format": "cbz",
        "final_file_chapters": {
            "cbz": [str(n) for n in range(1, 51)],
            "epub": ["1", "2", "3"],
        },
    }
    assert (
        aio._final_file_would_shrink(
            ["1", "2", "3"], meta, final_file_exists=True, fmt="epub"
        )
        is None
    )


def test_an_unrecorded_format_falls_back_to_the_overstating_union():
    """No entry for this format means unknown coverage, and unknown must not
    read as "covers nothing". chapters_downloaded overstates, which only ever
    skips a build the user can redo."""
    meta = {
        "format": "cbz",
        "final_file_chapters": {"cbz": ["1", "2"]},
        "chapters_downloaded": ["1", "2", "3", "4"],
    }
    assert aio._final_file_recorded_coverage(meta, "pdf") == {"1", "2", "3", "4"}


def test_a_legacy_list_is_honoured_only_for_the_format_that_wrote_it():
    """Files written before the map carry one list and meta["format"] says which
    archive it described. Applied to a different format it would understate —
    the direction that destroys data."""
    meta = {
        "format": "cbz",
        "final_file_chapters": ["1", "2", "3"],
        "chapters_downloaded": ["1", "2", "3", "4", "5"],
    }
    assert aio._final_file_recorded_coverage(meta, "cbz") == {"1", "2", "3"}
    assert aio._final_file_recorded_coverage(meta, "epub") == {
        "1", "2", "3", "4", "5",
    }


def test_coverage_without_a_format_argument_keeps_the_legacy_reading():
    """Back-compat for callers that predate the qualifier."""
    meta = {"format": "cbz", "final_file_chapters": ["1", "2"]}
    assert aio._final_file_recorded_coverage(meta) == {"1", "2"}


def test_a_junk_format_map_degrades_to_the_union_not_to_empty():
    meta = {
        "final_file_chapters": {"cbz": "not-a-list"},
        "chapters_downloaded": ["7"],
    }
    assert aio._final_file_recorded_coverage(meta, "cbz") == {"7"}


def test_a_cancelled_run_claims_only_the_chapters_it_reached():
    """THE metadata-concealment fix. A `--chapters 51-53` update cancelled after
    51 must record 51 and nothing else — claiming 52 and 53 makes the library
    update-check report a series as complete while two chapters exist nowhere."""
    assert aio._run_claimed_chapter_labels({"51"}, []) == ["51"]


def test_a_complete_run_claims_exactly_what_it_selected():
    """The no-regression half, and the reason this fix is safe: for any run that
    finishes the loop, attempted == selection, so the recorded set is identical
    to what the old selection-minus-misses arithmetic produced."""
    assert aio._run_claimed_chapter_labels(
        {"51", "52", "53"}, []
    ) == ["51", "52", "53"]


def test_a_failed_chapter_is_still_subtracted():
    """Attempted-but-failed and never-attempted are different states and both
    must be excluded — via the missed list and via absence respectively."""
    assert aio._run_claimed_chapter_labels(
        {"51", "52", "53"}, [{"chap": "52", "reason": "empty_content"}]
    ) == ["51", "53"]


def test_a_run_that_reached_no_chapter_claims_nothing():
    """The door the final-file gate could NOT close: a PDF/EPUB run cancelled
    before chapter 1 never seeds current_book_content (only CBZ appends the
    cover), so it skips the gate entirely and `final_build_stranded` stays
    False. The claim has to be right at the source instead."""
    assert aio._run_claimed_chapter_labels(set(), []) == []
    assert aio._run_claimed_chapter_labels(None, None) == []


def test_claimed_labels_normalize_on_both_sides():
    """A float-emitting handler must not leave "4.0" claimed and "4" missed."""
    assert aio._run_claimed_chapter_labels({4.0, "5"}, [{"chap": 5.0}]) == ["4"]


def test_claimed_labels_tolerate_non_numeric_labels():
    """Same safe sort key as everywhere else here — this runs inside a log-only
    except, so a raise means silent metadata loss."""
    assert aio._run_claimed_chapter_labels(
        {"Oneshot", "2", "10"}, []
    ) == ["2", "10", "Oneshot"]


def test_the_attempt_is_recorded_after_the_cancel_check_not_before():
    """ORDERING IS THE FIX. Recording the attempt above the cancel checkpoint
    would put the chapter the run stopped BEFORE back into the claimed set and
    undo this entirely — and nothing else in the suite would notice."""
    src = inspect.getsource(aio.main)
    cancel = src.index("[!] Cancelled — stopping before chapter")
    record = src.index("attempted_chapter_labels.add(")
    assert record > cancel, (
        "attempted_chapter_labels.add must sit BELOW the run-cancel break"
    )


def test_the_metadata_writer_no_longer_derives_from_the_selected_list():
    """The old expression was `set(... for ch in chapters)` — the SELECTION.
    Pinned because reintroducing it is a one-word edit that re-opens the hole
    while every other test here still passes."""
    src = inspect.getsource(aio.main)
    assert "_run_claimed_chapter_labels(" in src
    assert 'set(_chap_label_str(ch["chap"]) for ch in chapters)' not in src


def test_process_chapter_images_reports_dropped_sources(tmp_path):
    """A page that downloads fine and then fails to decode is skipped by
    _assemble. The `dropped` out-parameter is the only reliable signal: the
    return length is 1:N by design (fill-the-gap recombine), so comparing counts
    cannot tell "recombined" from "lost pages"."""
    good = tmp_path / "good.png"
    from PIL import Image

    Image.new("RGB", (40, 40), "red").save(good)
    bad = tmp_path / "bad.webp"
    bad.write_bytes(b"not an image at all")

    dropped = []
    pages = aio.process_chapter_images(
        [str(good), str(bad)], 40, 4000, dropped=dropped
    )
    assert dropped == [str(bad)]
    assert pages, "the decodable page still comes through"


def test_resize_chapter_images_reports_dropped_sources(tmp_path):
    from PIL import Image

    good = tmp_path / "good.png"
    Image.new("RGB", (40, 40), "blue").save(good)
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n truncated")

    dropped = []
    out = aio.resize_chapter_images([str(good), str(bad)], 40, dropped=dropped)
    assert dropped == [str(bad)]
    assert len(out) == 1


def test_decode_helpers_default_to_no_out_param(tmp_path):
    """`dropped` is keyword-only with a None default, so every pre-existing
    caller (and the probe paths in sites/) is unaffected."""
    for fn in (aio.process_chapter_images, aio.resize_chapter_images):
        param = inspect.signature(fn).parameters["dropped"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is None


def test_decode_drop_withholds_the_chapter_instead_of_shipping_it_short():
    """The reconciliation must run BEFORE the per-chapter archive is built, and
    must return no content so the main loop records the chapter missed."""
    src = inspect.getsource(aio.main)
    i_reconcile = src.index("if _decode_dropped:")
    # Anchor on the per-chapter cache WRITE, not on `cached_cbz_path = ...`,
    # which also appears in the resume-collect branch far earlier in main().
    write_anchor = 'cached_cbz_path, "w", zipfile.ZIP_STORED'
    assert src.count(write_anchor) == 1, "anchor is no longer unique"
    assert i_reconcile < src.index(write_anchor), (
        "reconciling after the archive is written would leave a short .cbz on "
        "disk for resume to adopt"
    )
    tail = src[i_reconcile: i_reconcile + 1600]
    assert 'ch["_decode_dropped"] = len(_decode_dropped)' in tail
    # RAISES rather than returning. A bare `return None, …` returns normally,
    # and _process_chapter_strict catches only ChapterSkippedError — so the old
    # shape skipped the alt-source loop, the lazy-discovery trigger and the
    # inline retry, and fell into the main loop's weak empty-content branch.
    assert 'raise ChapterSkippedError(' in tail
    assert 'reason="decode_dropped_pages"' in tail
    assert "return None, grp_name, n, 0" not in tail, (
        "the decode drop returns normally again — alt-source rescue is the ONLY "
        "thing that fixes a missing codec, and a normal return bypasses it"
    )


def test_decode_drop_reaches_alt_sources_but_not_the_inline_retry():
    """Placement in the strict wrapper's ladder IS the fix.

    _PERMANENT_SKIP_REASONS is checked AFTER the alt-source loop and BEFORE the
    inline retry, which is exactly right here: an alternate source serving a
    format this build can read is the only real rescue, while re-downloading the
    same bytes cannot make a codec appear (two long waits of pure cost), and the
    failure is environmental so it must not abort the run.
    """
    assert "decode_dropped_pages" in aio._PERMANENT_SKIP_REASONS

    strict = inspect.getsource(aio.main)
    i_alt = strict.index("# Phase 4b: try alternative sources before the inline-retry sleep.")
    i_perm = strict.index("if primary_err.reason in _PERMANENT_SKIP_REASONS:")
    i_inline = strict.index("for retry_attempt in range(max_retries):")
    assert i_alt < i_perm < i_inline, (
        "permanent-skip must sit between the alt loop and the inline retry"
    )


def test_decode_drop_gets_its_own_missed_reason_and_names_the_cause():
    """Distinct from empty_content so the missed report can tell "nothing
    downloaded" from "downloaded, then undecodable".

    The message must also name the CAUSE and a remedy. Routed through the
    permanent-skip handler it previously inherited that branch's "content is
    gated (needs a site login we don't have)" — pointing at the one thing that
    is definitely not wrong.
    """
    src = inspect.getsource(aio.main)
    assert "'decode_dropped_pages'" in src or '"decode_dropped_pages"' in src
    handler = src[src.index("except ChapterPermanentSkipError as cpse:"):]
    handler = handler[: handler.index("except ChapterAbortedError")]
    assert 'cpse.reason == "decode_dropped_pages"' in handler
    # Its own branch, with its own wording — the login message must belong to
    # the `else`, not be inherited by a decode failure.
    decode_branch = handler.split('elif cpse.reason == "decode_dropped_pages":')[1]
    decode_branch = decode_branch[: decode_branch.index("            else:")]
    # Comments stripped first: the branch legitimately QUOTES the old wrong
    # message to record why it exists, and a raw substring check matches the
    # explanation rather than the emitted string.
    code_only = "\n".join(
        line for line in decode_branch.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "could not be DECODED" in code_only
    assert "needs a site login" not in code_only


def test_decode_drop_state_is_reset_before_each_fetch():
    """The end-of-run retry re-feeds the SAME chapter dict, so a stale count
    would mislabel a fresh miss."""
    src = inspect.getsource(aio.main)
    assert 'ch.pop("_decode_dropped", None)' in src


def test_complete_chapter_still_accepted_despite_deadline_or_poison():
    """The 2026-07-03 fix (grep "accepting despite") must survive: the download
    completeness gate accepts any pages_ok == pages_total chapter regardless of
    deadline/poison. D4's reconciliation is a SEPARATE, later check."""
    src = inspect.getsource(aio.main)
    assert "accepting despite" in src
    i_gate = src.index("if pages_total > 0 and not incomplete:")
    i_reconcile = src.index("if _decode_dropped:")
    assert i_gate < i_reconcile


def test_fast_paths_never_populate_the_dropped_list():
    """Android's stock configuration (CBZ byte-passthrough) and --no-processing
    do not decode at all, so the reconciliation is unreachable there. Verified
    structurally: both branches only copy raw_image_paths."""
    src = inspect.getsource(aio.main)
    block = src[src.index("if cbz_fast_path:\n                    processed_page_images"):]
    block = block[: block.index("else:\n                    log_verbose(")]
    assert "_decode_dropped" not in block
    assert "processed_page_images = list(raw_image_paths)" in block


# ---------------------------------------------------------------------------
# UI arg-builder coupling
# ---------------------------------------------------------------------------

def test_delta_runs_coerce_keep_chapters_in_the_ENGINE_not_the_arg_builders():
    """With the combined rebuild declined for delta runs, per-chapter files are
    the only durable home for the chapters just downloaded — and the engine is
    what arranges that, because it is the only component that knows the guard
    will fire.

    An earlier revision forced `keepChapters` in UI-source/src/lib/downloadArgs.js
    instead. That was the wrong layer twice over: it needed a second copy in the
    Android twin (core/DownloadForm.kt) to cover the same case, and a hardcoded
    `true` there cannot be turned off. Both arg builders are therefore asserted
    to stay PLAIN, so a future change puts the logic back where it belongs
    rather than re-splitting it.
    """
    source = inspect.getsource(aio.main)

    # The coercion, and the notice that makes it visible in the log panel.
    assert re.search(r"^\s*args\.keep_chapters = True\b", source, re.M), (
        "the delta-run coercion is gone — a delta download now strands its "
        "chapters in tmp_<hid>/ with nothing readable produced"
    )
    assert "[Delta] Forcing --keep-chapters" in source, (
        "the coercion must announce itself, like --komikku's does"
    )

    # It must be driven by the SAME predicate as the end-of-run guard, or the
    # two can disagree and the run strands anyway.
    coercion = source[source.index("--- Delta run: coerce"):]
    assert "_final_file_would_shrink(" in coercion[:4000]

    # ...and it must sit AFTER the --split reset, whose outcome decides whether
    # the series-wide archive is written at all. Upstream of that reset the
    # split state is stale (missed-chapter retry, on by default, disables split).
    assert source.index("--split is disabled while missed-chapter retry") < source.index(
        "--- Delta run: coerce"
    ), "the coercion moved above the --split reset and now reads a stale split state"

    # The desktop arg builder stays plain — no force, no bespoke opt-out key.
    js = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "UI-source", "src", "lib", "downloadArgs.js",
    )
    text = open(js, encoding="utf-8").read()
    assert "if (def.keepChapters) args.keepChapters = true;" in text
    assert not re.search(r"^\s*args\.keepChapters = true;", text, re.M)
    assert "keepChaptersOnUpdate" not in text
    # noFinalFile stays defaulted: a fresh series with no archive yet must still
    # be allowed to build one.
    assert "if (def.noFinalFile) args.noFinalFile = true;" in text
