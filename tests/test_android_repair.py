"""Coverage for the two in-place library-repair entry points added in Wave 2:
aio_android.refresh_library_metadata and aio_android.build_final_file.

WHY THEY EXIST: before these, the device had no repair path of ANY kind. A
series enriched against the wrong AniList entry stayed wrong forever (the
documented cure is `--refresh-library-metadata`, which nothing on Android could
invoke), and an archive the overwrite guard declined to rebuild had no
"recombine it now" button.

WHAT IS AND IS NOT EXERCISED HERE. Both entry points really run aio-dl.py's
main() — these are not mock-only tests — but every case is arranged so the run
returns from a filesystem scan without touching the network:

  * refresh runs against a library where NOTHING matches, which is the branch
    that returns before `enrich_from_anilist` is ever called. A matching series
    would fan out to graphql.anilist.co, which has no place in an offline suite.
  * build-final-file is local by nature; it merges PDFs already on disk.

The argv-shape tests take the other route and record `_run_engine`'s argument
instead of running it, because "which flags does this build" is a different
question from "does the mode work" and mixing them makes both harder to read.

Cross-file: aio-dl.py:_refresh_library_metadata and
build_final_pdf_from_chapter_folder (the modes), aio-dl.py's
_final_file_would_shrink (the guard build_final_file is the recovery for).
"""

from __future__ import annotations

import json
import os
import threading

import pytest

import aio_android
from aio_android import ENGINE_BUSY, build_final_file, configure, refresh_library_metadata


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A configured, EMPTY library root. Yields the path."""
    root = tmp_path / "manga"
    root.mkdir()
    monkeypatch.setitem(aio_android._CONFIGURED, "output_dir", str(root))
    monkeypatch.setitem(aio_android._CONFIGURED, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(aio_android._CONFIGURED, "work_dir", str(tmp_path / "work"))
    return root


@pytest.fixture
def recorded_argv(monkeypatch):
    """Replace _run_engine with a recorder. Yields the list it appends to."""
    seen = []

    def _fake(argv, sink=None):
        seen.append(list(argv))
        return 0

    monkeypatch.setattr(aio_android, "_run_engine", _fake)
    return seen


def _hold_engine():
    """Start a thread holding _ENGINE_LOCK. Returns (release_event, thread)."""
    acquired = threading.Event()
    release = threading.Event()

    def hold():
        with aio_android._ENGINE_LOCK:
            acquired.set()
            release.wait(5)

    worker = threading.Thread(target=hold, daemon=True)
    worker.start()
    assert acquired.wait(5)
    return release, worker


# --------------------------------------------------------------------------
# refresh_library_metadata — argv shape
# --------------------------------------------------------------------------


def test_refresh_minimal_argv(library, recorded_argv):
    refresh_library_metadata()
    assert recorded_argv == [["--refresh-library-metadata", "-o", str(library)]]


def test_refresh_targets_the_configured_root_explicitly(library, recorded_argv):
    """Not left to resolve_output_dir's AIO_OUTPUT_DIR fallback. configure()
    sets both to the same value, but the guard above reads _CONFIGURED — and a
    caller that set one without the other would sweep the WRONG library while
    passing the check."""
    refresh_library_metadata()
    argv = recorded_argv[0]
    assert argv[argv.index("-o") + 1] == str(library)


def test_refresh_forwards_its_switches(library, recorded_argv):
    refresh_library_metadata("Eleceed", rewrite_cbz=True, force_refresh=True, tag_min_rank=80)
    assert recorded_argv[0] == [
        "--refresh-library-metadata",
        "-o", str(library),
        "--refresh-rewrite-cbz",
        "--metadata-refresh",
        "--metadata-tag-min-rank", "80",
        "--", "Eleceed",
    ]


def test_refresh_omits_the_default_tag_rank(library, recorded_argv):
    """50 is aio-dl.py's own default; emitting it is noise in a logged line."""
    refresh_library_metadata(tag_min_rank=50)
    assert "--metadata-tag-min-rank" not in recorded_argv[0]


def test_refresh_puts_the_filter_behind_a_double_dash(library, recorded_argv):
    """The filter is free text the user typed. One starting with "-" would
    otherwise be read as an unknown option and kill the run with an argparse
    error instead of simply matching nothing."""
    refresh_library_metadata("-weird")
    assert recorded_argv[0][-2:] == ["--", "-weird"]


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_refresh_omits_an_empty_filter_entirely(library, recorded_argv, blank):
    refresh_library_metadata(blank)
    assert "--" not in recorded_argv[0]


def test_refresh_never_emits_metadata_source(library, recorded_argv):
    """It would be INERT: _refresh_library_metadata imports and calls
    enrich_from_anilist directly and never reads that flag, so a repair works
    even where enrichment is off for live downloads. Emitting it would imply a
    coupling that does not exist."""
    refresh_library_metadata("x", force_refresh=True)
    assert "--metadata-source" not in recorded_argv[0]


# --------------------------------------------------------------------------
# refresh_library_metadata — behaviour
# --------------------------------------------------------------------------


def test_refresh_requires_configuration(monkeypatch):
    monkeypatch.setitem(aio_android._CONFIGURED, "output_dir", "")
    assert json.loads(refresh_library_metadata())["error"] == "not_configured"


def test_refresh_reports_engine_busy_rather_than_blocking(library):
    """A repair that silently queued behind a 40-minute download would look
    like a dead button."""
    release, worker = _hold_engine()
    try:
        assert json.loads(refresh_library_metadata())["error"] == ENGINE_BUSY
    finally:
        release.set()
        worker.join(5)


def test_refresh_runs_the_real_mode_on_an_empty_library(library):
    """A real _run_engine call — no network, because the mode returns as soon
    as it finds no series with a .aio_series.json."""
    payload = json.loads(refresh_library_metadata())
    assert payload["ok"] is True
    assert (payload["matched"], payload["skipped"], payload["failed"]) == (0, 0, 0)
    assert "No series with .aio_series.json" in payload["output"]
    assert payload["exitCode"] == 0
    # It swept the CONFIGURED root, not whatever AIO_OUTPUT_DIR happened to
    # hold. Without this the assertion above would pass just as happily
    # against an unrelated empty directory.
    assert str(library) in payload["output"]


def test_refresh_filter_really_reaches_the_mode(library):
    """The filter narrows the sweep, and a non-matching one is what keeps this
    test offline: with a series present but excluded, the mode returns before
    enrich_from_anilist is called."""
    series = library / "Eleceed"
    series.mkdir()
    (series / ".aio_series.json").write_text(
        json.dumps({"title": "Eleceed", "url": "https://x/y"}), encoding="utf-8"
    )

    payload = json.loads(refresh_library_metadata("NoSuchSeries"))
    assert payload["ok"] is True
    assert payload["matched"] == 0
    # The mode echoes the normalized filter, which proves the positional was
    # not swallowed on the way in.
    assert "matching ['nosuchseries']" in payload["output"]


def test_refresh_releases_the_engine_lock(library):
    refresh_library_metadata()
    assert aio_android._ENGINE_LOCK.acquire(blocking=False)
    aio_android._ENGINE_LOCK.release()


def test_refresh_reports_an_engine_exception_instead_of_raising(library, monkeypatch):
    def _boom(argv, sink=None):
        raise RuntimeError("anilist exploded")

    monkeypatch.setattr(aio_android, "_run_engine", _boom)
    payload = json.loads(refresh_library_metadata())
    assert payload["error"] == "refresh_failed"
    assert "anilist exploded" in payload["detail"]
    # And the lock is still released, so the next attempt is not engine_busy.
    assert aio_android._ENGINE_LOCK.acquire(blocking=False)
    aio_android._ENGINE_LOCK.release()


def test_refresh_parses_the_modes_summary_line(library, monkeypatch):
    def _fake(argv, sink=None):
        print("Refresh complete: 7 updated, 2 skipped, 1 failed, 3 folders ignored")
        return 1

    monkeypatch.setattr(aio_android, "_run_engine", _fake)
    payload = json.loads(refresh_library_metadata())
    assert (payload["matched"], payload["skipped"], payload["failed"]) == (7, 2, 1)
    assert payload["exitCode"] == 1


def test_refresh_output_is_capped(library, monkeypatch):
    """A device UI should not have to receive an unbounded string across JNI.
    The TAIL is kept, because the summary line is at the end."""
    def _fake(argv, sink=None):
        print("x" * (aio_android._REPAIR_LOG_MAX_CHARS + 5000))
        print("Refresh complete: 1 updated, 0 skipped, 0 failed")
        return 0

    monkeypatch.setattr(aio_android, "_run_engine", _fake)
    payload = json.loads(refresh_library_metadata())
    assert len(payload["output"]) <= aio_android._REPAIR_LOG_MAX_CHARS + 2
    assert payload["output"].startswith("…")
    assert payload["matched"] == 1


# --------------------------------------------------------------------------
# build_final_file — guards
# --------------------------------------------------------------------------


def test_build_final_requires_configuration(tmp_path, monkeypatch):
    monkeypatch.setitem(aio_android._CONFIGURED, "output_dir", "")
    assert json.loads(build_final_file(str(tmp_path)))["error"] == "not_configured"


def test_build_final_refuses_a_folder_outside_the_library(library, tmp_path):
    """The mode WRITES <prefix>.pdf into whatever folder it is handed, and that
    path crossed JSON and JNI to get here."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert json.loads(build_final_file(str(outside)))["error"] == "outside_library"


def test_build_final_refuses_a_missing_folder(library):
    assert json.loads(build_final_file(str(library / "gone")))["error"] == "not_found"


def test_build_final_reports_engine_busy(library):
    """A download may be writing archives into this very folder."""
    series = library / "Some Series"
    series.mkdir()
    release, worker = _hold_engine()
    try:
        assert json.loads(build_final_file(str(series)))["error"] == ENGINE_BUSY
    finally:
        release.set()
        worker.join(5)


# --------------------------------------------------------------------------
# build_final_file — behaviour
# --------------------------------------------------------------------------


def _chapter_pdf(path, pages=1):
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    with open(path, "wb") as handle:
        writer.write(handle)


def test_build_final_merges_the_chapter_pdfs(library):
    series = library / "Some Series"
    series.mkdir()
    for number in (1, 2, 3):
        _chapter_pdf(series / f"Some Series Ch {number}.pdf")

    payload = json.loads(build_final_file(str(series)))
    assert payload["ok"] is True
    assert payload["built"] == 1
    assert payload["files"] == ["Some Series.pdf"]

    combined = series / "Some Series.pdf"
    assert combined.is_file()
    from pypdf import PdfReader

    assert len(PdfReader(str(combined)).pages) == 3


def test_build_final_reports_zero_for_a_cbz_series(library):
    """THE HONESTY TEST. --build-final-file globs *.pdf and merges per-chapter
    PDFs only — there is no CBZ or EPUB branch. A UI must not offer this as the
    recovery for a skipped CBZ rebuild, and this pins the behaviour that makes
    that true."""
    series = library / "Some Series"
    series.mkdir()
    for number in (1, 2):
        (series / f"Some Series Ch {number:03d}.cbz").write_bytes(b"PK\x03\x04not-a-real-zip")

    payload = json.loads(build_final_file(str(series)))
    assert payload["ok"] is True
    assert payload["built"] == 0
    assert payload["files"] == []
    assert "No per-chapter PDFs found" in payload["output"]
    # Nothing was created, and nothing was destroyed.
    assert sorted(p.name for p in series.iterdir()) == [
        "Some Series Ch 001.cbz", "Some Series Ch 002.cbz",
    ]


def test_build_final_excludes_an_existing_combined_file_from_its_own_input(library):
    """Otherwise a second run doubles the page count every time."""
    series = library / "Some Series"
    series.mkdir()
    for number in (1, 2):
        _chapter_pdf(series / f"Some Series Ch {number}.pdf")

    assert json.loads(build_final_file(str(series)))["built"] == 1
    assert json.loads(build_final_file(str(series)))["built"] == 1

    from pypdf import PdfReader

    assert len(PdfReader(str(series / "Some Series.pdf")).pages) == 2


def test_build_final_releases_the_engine_lock(library):
    series = library / "Some Series"
    series.mkdir()
    build_final_file(str(series))
    assert aio_android._ENGINE_LOCK.acquire(blocking=False)
    aio_android._ENGINE_LOCK.release()


def test_build_final_argv_carries_nothing_but_the_mode_and_the_folder(library, recorded_argv):
    """_validate_build_final_cli scans sys.argv and p.error()s on ANY option
    other than -v/-d, and _run_engine sets sys.argv to exactly this list. One
    stray flag turns the whole mode into an argparse error."""
    series = library / "Some Series"
    series.mkdir()
    build_final_file(str(series))
    argv = recorded_argv[0]
    assert argv[0] == "--build-final-file"
    assert argv[1] == "--"
    assert len(argv) == 3
    assert argv[2].endswith("Some Series")


# --------------------------------------------------------------------------
# AniList enrichment — the env half
#
# The other half of the same PARITY row. `--metadata-source` defaults to "none"
# and nothing on the device ever set AIO_METADATA_SOURCE, so enrichment was off
# with no way to turn it on. build_argv's flag covers downloads (see
# tests/test_android_argv.py); this covers the entry points that fall back to
# argparse's default instead of building an argv here.
# --------------------------------------------------------------------------


@pytest.fixture
def clean_process_state(tmp_path):
    """configure() mutates the PROCESS — CWD and three env vars — so anything
    calling it has to put the process back or it poisons every later test."""
    saved_cwd = os.getcwd()
    saved_env = {
        key: os.environ.get(key)
        for key in (
            "AIO_METADATA_SOURCE", "AIO_OUTPUT_DIR", "XDG_CACHE_HOME",
            "AIO_MANGAFIRE_PROFILE_DIR", "AIO_COMIX_PROFILE_DIR", "NO_COLOR",
        )
    }
    saved_configured = dict(aio_android._CONFIGURED)
    yield tmp_path
    os.chdir(saved_cwd)
    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    aio_android._CONFIGURED.clear()
    aio_android._CONFIGURED.update(saved_configured)


def test_configure_sets_the_metadata_source_env(clean_process_state):
    root = clean_process_state
    configure(str(root / "manga"), str(root / "cache"), None, "anilist")
    assert os.environ["AIO_METADATA_SOURCE"] == "anilist"
    assert aio_android._CONFIGURED["metadata_source"] == "anilist"


def test_configure_defaults_the_metadata_source_to_none(clean_process_state):
    """A three-argument call is what the Kotlin side does today, and it must
    keep working — Chaquopy calls this positionally."""
    root = clean_process_state
    configure(str(root / "manga"), str(root / "cache"))
    assert os.environ["AIO_METADATA_SOURCE"] == "none"


@pytest.mark.parametrize("bogus", ["mal", "ANILIST_", "", "  ", None, 7])
def test_configure_normalizes_an_unrecognized_source_to_none(clean_process_state, bogus):
    """NOT an inert typo: argparse uses this env var as --metadata-source's
    DEFAULT, and an unrecognized value makes it reject its own default with
    "invalid choice" — every run fails, including the ones that never asked for
    enrichment."""
    root = clean_process_state
    configure(str(root / "manga"), str(root / "cache"), None, bogus)
    assert os.environ["AIO_METADATA_SOURCE"] == "none"


def test_configure_accepts_the_source_case_insensitively(clean_process_state):
    root = clean_process_state
    configure(str(root / "manga"), str(root / "cache"), None, " AniList ")
    assert os.environ["AIO_METADATA_SOURCE"] == "anilist"


def test_reconfiguring_can_turn_enrichment_back_off(clean_process_state):
    """The reason this is set unconditionally rather than setdefault-ed:
    Chaquopy's interpreter outlives every screen, so a setdefault would pin the
    first value for the life of the process and the UI toggle would appear to
    work while changing nothing."""
    root = clean_process_state
    configure(str(root / "manga"), str(root / "cache"), None, "anilist")
    configure(str(root / "manga"), str(root / "cache"), None, "none")
    assert os.environ["AIO_METADATA_SOURCE"] == "none"


def test_every_accepted_source_is_a_real_argparse_choice():
    """The env value has to be something --metadata-source accepts. Checked
    against aio-dl.py's own help text rather than a remembered list."""
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    help_text = subprocess.run(
        [sys.executable, str(repo / "aio-dl.py"), "--help"],
        capture_output=True, text=True, cwd=str(repo), timeout=300,
    ).stdout
    assert "--metadata-source {none,anilist}" in " ".join(help_text.split())
    assert set(aio_android._METADATA_SOURCES) == {"none", "anilist"}
