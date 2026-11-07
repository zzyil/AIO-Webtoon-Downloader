"""Coverage for aio_android's resume API — the Python half of M7.

WHY OFFLINE TESTS EARN THEIR KEEP HERE: resume is the one feature whose failure
mode is invisible at the moment it happens. A wrong `--format` on the resume CLI
does not error — it silently converts a CBZ library to EPUB, and you find out
when the reader cannot open the file. A containment bug in `delete_resumable`
does not error either; it deletes the wrong directory. Neither shows up in a
device smoke test that only checks "the download continued".

The three rules these pin, all of which have a real trap behind them:

  1. `--format` is ALWAYS emitted. argparse defaults it to "epub"
     (aio-dl.py:8209), and aio-dl.py deliberately omits format from
     run_params.json so a resume can change it — which means an omitted flag is
     not "keep what it was", it is "switch to EPUB".
  2. The throttle flags come from the CURRENT settings, never the saved run.
  3. Deletion is confined to `<work_dir>/tmp_*`. The working directory also
     holds the browser profiles and the vrf token cache; a malformed path must
     not be able to take those with it.

Cross-file: android/.../core/ResumeRepository.kt (the caller),
UI-source/electron/downloader.js:scanResumable + resume() (the behaviour being
ported), aio-dl.py (main_tmp_dir, get_resumable_params).
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

import aio_android
from aio_android import (
    build_resume_argv,
    build_resume_argv_json,
    delete_resumable,
    probe_library_root,
    scan_resumable,
)


# --------------------------------------------------------------------------
# The rule that makes resume possible at all
# --------------------------------------------------------------------------


def test_a_cancelled_run_keeps_its_tmp_dir():
    """aio-dl.py must NOT wipe tmp_<hid>/ when the run was cancelled.

    THIS IS THE WHOLE FEATURE'S INPUT. Found on device: the end-of-run block
    used to be `elif not args.no_cleanup: rm_tree(main_tmp_dir)`, which fires
    for a cancelled run too — so every cancelled Android download deleted
    exactly the chapters a resume would have reused, and the Unfinished section
    was permanently empty.

    Desktop never hit it because its cancel KILLS the process, so main() never
    reaches that block. Android's cancel is cooperative (`request_run_cancel`,
    one caller: aio_android.cancel), so the run returns normally and walks
    straight into the cleanup.

    Asserted structurally rather than by running a download: reaching this code
    needs a complete run against a live site. The guard is one token, and its
    absence is silent, so a text assertion is worth more than no assertion.
    """
    import re

    source = (
        pathlib.Path(__file__).resolve().parent.parent / "aio-dl.py"
    ).read_text(encoding="utf-8")

    # The cleanup branch must test run_cancelled(). Anchoring on the whole
    # condition keeps this from passing on some unrelated run_cancelled()
    # nearby, and the tail is restricted to further `and not <name>` terms —
    # which can only ever make the branch keep the folder MORE often.
    # `final_build_stranded` is one such term (the overwrite guard declined the
    # combined build, so the only copies of this run's chapters are in
    # tmp_<hid>/ — grep it in aio-dl.py).
    #
    # The tail used to be `[^:\n]*`, which was described in this very comment as
    # rejecting anything that made the branch delete more. It did not: `[^:\n]*`
    # excludes only `:` and newline, and `or` contains neither, so
    # `... and not run_cancelled() or force_wipe:` matched — the exact
    # regression this test exists to catch, passing. Verified by running the old
    # pattern against that string and three other harmful variants.
    assert re.search(
        r"elif not args\.no_cleanup and not run_cancelled\(\)"
        r"(?:\s+and\s+not\s+[A-Za-z_][A-Za-z_0-9.]*(?:\(\))?)*"
        r":\s*\n\s*rm_tree\(main_tmp_dir\)",
        source,
    ), "the end-of-run cleanup no longer guards on run_cancelled() — a cancelled run will delete its own resume data"


def test_cancellation_is_only_ever_requested_by_the_android_shim():
    """The guard above is Android-only in EFFECT, and this is why.

    `request_run_cancel` has exactly one caller (aio_android.cancel), so
    run_cancelled() cannot be true in a desktop run and the new condition cannot
    change desktop behaviour. If a desktop caller ever appears, that reasoning
    needs revisiting — which is what this test is here to force.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    callers = []
    for path in root.glob("*.py"):
        if path.name == "aio-dl.py":
            continue  # defines it
        if "request_run_cancel()" in path.read_text(encoding="utf-8"):
            callers.append(path.name)
    assert callers == ["aio_android.py"], f"unexpected cancellation callers: {callers}"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A configured work dir + library root. Yields (work, library) as Paths.

    Patches _CONFIGURED rather than calling configure(), which would os.chdir()
    the whole test session into tmp_path and leave it there.
    """
    work = tmp_path / "work"
    library = tmp_path / "manga"
    work.mkdir()
    library.mkdir()
    monkeypatch.setitem(aio_android._CONFIGURED, "work_dir", str(work))
    monkeypatch.setitem(aio_android._CONFIGURED, "output_dir", str(library))
    monkeypatch.setitem(aio_android._CONFIGURED, "cache_dir", str(tmp_path / "cache"))
    return work, library


def make_tmp_dir(
    work,
    hid,
    *,
    params=None,
    meta=None,
    done_chapters=0,
    pending_chapters=0,
    marker=".processed_complete",
    page_bytes=100,
):
    """One `tmp_<hid>/` shaped the way aio-dl.py leaves one mid-run."""
    folder = work / f"tmp_{hid}"
    folder.mkdir(parents=True, exist_ok=True)
    if params is not None:
        (folder / "run_params.json").write_text(json.dumps(params), encoding="utf-8")
    if meta is not None:
        (folder / "run_meta.json").write_text(json.dumps(meta), encoding="utf-8")

    for index in range(done_chapters + pending_chapters):
        chapter = folder / f"ch_{index + 1}"
        chapter.mkdir(exist_ok=True)
        (chapter / "0001.jpg").write_bytes(b"x" * page_bytes)
        if index < done_chapters:
            (chapter / marker).write_text("", encoding="utf-8")
    return folder


# --------------------------------------------------------------------------
# scan_resumable
# --------------------------------------------------------------------------


def test_reports_a_resumable_run_with_its_metadata(workspace):
    work, _ = workspace
    make_tmp_dir(
        work,
        "abc123",
        params={"gating_hash": "h", "params": {"quality": 85, "scaling": 90, "language": "ja"}},
        meta={"url": "https://site.test/x", "format": "cbz", "title": "Some Series"},
        done_chapters=2,
        pending_chapters=1,
    )

    payload = json.loads(scan_resumable())
    assert payload["root"] == str(work)
    assert len(payload["items"]) == 1

    item = payload["items"][0]
    assert item["hid"] == "abc123"
    assert item["folderName"] == "tmp_abc123"
    assert item["url"] == "https://site.test/x"
    assert item["title"] == "Some Series"
    assert item["format"] == "cbz"
    assert item["cachedChapters"] == 2
    assert item["language"] == "ja"
    assert item["quality"] == 85
    assert item["scaling"] == 90
    assert item["sizeBytes"] > 0
    assert item["modifiedAt"] > 0


def test_a_folder_without_run_params_is_not_resumable(workspace):
    """No params means nothing for --restore-parameters to read, so "resuming"
    it would be a fresh download wearing a resume button."""
    work, _ = workspace
    make_tmp_dir(work, "noparams", meta={"url": "https://site.test/x"}, done_chapters=1)
    assert json.loads(scan_resumable())["items"] == []


def test_non_tmp_directories_and_files_are_ignored(workspace):
    work, _ = workspace
    (work / "mangafire-profile").mkdir()
    (work / "vrf-cache.json").write_text("{}", encoding="utf-8")
    make_tmp_dir(work, "real", params={"params": {}})
    assert [i["hid"] for i in json.loads(scan_resumable())["items"]] == ["real"]


def test_both_completion_markers_count(workspace):
    """`--no-processing` runs write `.download_complete` instead. Reading only
    the other name would report a resumable run as having cached nothing, and
    the card would offer to restart work that is already on disk."""
    work, _ = workspace
    make_tmp_dir(
        work,
        "raw",
        params={"params": {}},
        done_chapters=3,
        marker=".download_complete",
    )
    assert json.loads(scan_resumable())["items"][0]["cachedChapters"] == 3


def test_the_legacy_flat_params_schema_still_reads(workspace):
    """Pre-2026-05 run_params.json was a flat dict, not {gating_hash, params}.
    Both shapes are on disk in the wild — see aio-dl.py:get_resumable_params."""
    work, _ = workspace
    make_tmp_dir(work, "legacy", params={"quality": 70, "language": "ko"})
    item = json.loads(scan_resumable())["items"][0]
    assert item["quality"] == 70
    assert item["language"] == "ko"


def test_unreadable_metadata_does_not_hide_a_resumable_run(workspace):
    """A corrupt sidecar means "no information", never "not resumable" — the
    chapters on disk are still real and still worth continuing."""
    work, _ = workspace
    folder = make_tmp_dir(work, "corrupt", params={"params": {}}, done_chapters=1)
    (folder / "run_meta.json").write_text("{not json", encoding="utf-8")

    item = json.loads(scan_resumable())["items"][0]
    assert item["hid"] == "corrupt"
    assert item["url"] == ""
    assert item["cachedChapters"] == 1


def test_items_are_newest_first(workspace):
    work, _ = workspace
    older = make_tmp_dir(work, "older", params={"params": {}})
    newer = make_tmp_dir(work, "newer", params={"params": {}})
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))
    assert [i["hid"] for i in json.loads(scan_resumable())["items"]] == ["newer", "older"]


def test_scan_survives_a_missing_work_dir(tmp_path, monkeypatch):
    monkeypatch.setitem(aio_android._CONFIGURED, "work_dir", str(tmp_path / "gone"))
    assert json.loads(scan_resumable())["items"] == []


# --------------------------------------------------------------------------
# build_resume_argv
# --------------------------------------------------------------------------


def test_format_is_always_emitted():
    """THE test in this file. --format is deliberately absent from
    run_params.json so it can be changed on resume; argparse then defaults it to
    "epub", so an omitted flag silently converts a CBZ run."""
    argv = build_resume_argv("https://site.test/x", "cbz")
    assert argv[0] == "--restore-parameters"
    assert argv[argv.index("--format") + 1] == "cbz"
    assert argv[-1] == "https://site.test/x"


def test_an_unknown_format_falls_back_to_cbz_not_to_argparses_epub():
    """A tmp folder with no run_meta.json reports format "". Falling through to
    argparse's default would hand a phone an EPUB; CBZ is what the platform's
    readers want and what this app defaults to everywhere else."""
    for value in ("", "   ", "zzz", None):
        argv = build_resume_argv("https://site.test/x", value)
        assert argv[argv.index("--format") + 1] == "cbz"


def test_epub_layout_rides_along_only_for_epub():
    assert "--epub-layout" in build_resume_argv("u", "epub", "page")
    assert "--epub-layout" not in build_resume_argv("u", "cbz", "page")
    # An unknown layout is dropped rather than forwarded — argparse `choices`
    # would reject it and the resume would die at parse time.
    assert "--epub-layout" not in build_resume_argv("u", "epub", "sideways")


def test_the_current_throttle_wins_over_the_saved_one():
    """Concrete values for all five knobs, ALWAYS — that is what makes "current
    wins" hold in both directions, including was-limited-now-unlimited where an
    omitted flag would let the persisted low value stand."""
    limited = build_resume_argv("u", "cbz", "", {"networkLimit": "low", "cpuLimit": "low"})
    assert limited[limited.index("--image-concurrency") + 1] == "2"
    assert limited[limited.index("--max-cpu-percent") + 1] == "25"

    unlimited = build_resume_argv("u", "cbz", "", {})
    assert unlimited[unlimited.index("--image-concurrency") + 1] == "8"
    assert unlimited[unlimited.index("--max-cpu-percent") + 1] == "100"


def test_verbose_is_not_emitted():
    """The desktop hardcodes --verbose because its stdout parser needs it.
    Android reads structured _emit events, which are not gated on the flag, so
    all it would do is bury the Logs screen under a 200-chapter resume."""
    assert "--verbose" not in build_resume_argv("u", "cbz")


def test_the_url_stays_last():
    """aio-dl.py takes the series URL positionally. Anything appended after it
    would be parsed as a second positional and rejected."""
    argv = build_resume_argv("https://site.test/x", "epub", "vertical", {"networkLimit": "low"})
    assert argv[-1] == "https://site.test/x"
    assert argv.count("https://site.test/x") == 1


def test_json_wrapper_round_trips():
    argv = json.loads(build_resume_argv_json("u", "cbz", "", json.dumps({"networkLimit": "low"})))
    assert argv == build_resume_argv("u", "cbz", "", {"networkLimit": "low"})
    # An empty settings string is the "no settings yet" case, not an error.
    assert json.loads(build_resume_argv_json("u", "cbz", "", "")) == build_resume_argv("u", "cbz")


# --------------------------------------------------------------------------
# delete_resumable — the destructive one
# --------------------------------------------------------------------------


def test_deletes_a_tmp_folder(workspace):
    work, _ = workspace
    folder = make_tmp_dir(work, "gone", params={"params": {}}, done_chapters=2)
    assert json.loads(delete_resumable(str(folder)))["ok"] is True
    assert not folder.exists()


def test_refuses_the_work_dir_itself(workspace):
    """The working directory holds the browser profiles and the vrf cache.
    "Discard this download" must never be able to mean "discard the browser
    session every site is relying on"."""
    work, _ = workspace
    assert json.loads(delete_resumable(str(work)))["error"] == "refused_root"
    assert work.exists()


def test_refuses_a_path_outside_the_work_dir(workspace):
    work, library = workspace
    series = library / "Some Series"
    series.mkdir()
    assert json.loads(delete_resumable(str(series)))["error"] == "outside_work_dir"
    assert series.exists()


def test_refuses_a_sibling_that_is_not_a_tmp_folder(workspace):
    work, _ = workspace
    profile = work / "mangafire-profile"
    profile.mkdir()
    assert json.loads(delete_resumable(str(profile)))["error"] == "not_a_tmp_dir"
    assert profile.exists()


def test_refuses_a_nested_tmp_folder(workspace):
    """Only DIRECT children are eligible. A `tmp_x` nested inside a profile
    directory is that profile's business, not a resumable download."""
    work, _ = workspace
    nested = work / "mangafire-profile" / "tmp_nested"
    nested.mkdir(parents=True)
    assert json.loads(delete_resumable(str(nested)))["error"] == "outside_work_dir"
    assert nested.exists()


def test_missing_folder_reports_not_found(workspace):
    work, _ = workspace
    assert json.loads(delete_resumable(str(work / "tmp_never")))["error"] == "not_found"


def test_delete_without_configuration_refuses(monkeypatch):
    monkeypatch.setitem(aio_android._CONFIGURED, "work_dir", "")
    assert json.loads(delete_resumable("/anything"))["error"] == "not_configured"


# --------------------------------------------------------------------------
# probe_library_root
# --------------------------------------------------------------------------


def test_probe_creates_and_reports_a_usable_root(tmp_path):
    target = tmp_path / "shared" / "Manga"
    payload = json.loads(probe_library_root(str(target)))
    assert payload["ok"] is True
    assert payload["path"] == str(target)
    assert payload["freeBytes"] > 0
    assert payload["seriesCount"] == 0
    assert target.is_dir()


def test_probe_leaves_no_trace(tmp_path):
    """The write probe has to actually write — os.access reports permission
    bits, and under scoped storage those routinely disagree with what the
    sandbox does. It must clean up after itself all the same."""
    target = tmp_path / "root"
    json.loads(probe_library_root(str(target)))
    assert list(target.iterdir()) == []


def test_probe_counts_existing_series(tmp_path):
    """The reassurance half: pointed at a real library it says so, which is how
    a user tells "the right folder" from "a plausible empty one"."""
    target = tmp_path / "root"
    for name in ("A", "B"):
        series = target / name
        series.mkdir(parents=True)
        (series / ".aio_series.json").write_text("{}", encoding="utf-8")
    (target / "NotASeries").mkdir()
    assert json.loads(probe_library_root(str(target)))["seriesCount"] == 2


def test_probe_rejects_an_empty_path():
    assert json.loads(probe_library_root("  "))["error"] == "empty_path"


def test_probe_reports_a_path_that_cannot_be_created(tmp_path):
    """A file where a directory should be. On device the realistic version is a
    revoked MANAGE_EXTERNAL_STORAGE grant, which surfaces the same way."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    payload = json.loads(probe_library_root(str(blocker / "child")))
    assert payload["error"] in ("cannot_create", "not_a_directory")
