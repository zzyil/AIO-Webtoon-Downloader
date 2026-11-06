"""Coverage for aio_android's library API — the Python half of M5.

WHY THIS IS WORTH TESTING OFFLINE: every function here is reached from a tap on
a device, three layers of JSON and JNI away from anything a stack trace would
point at, and two of them touch the user's actual files. `delete_series` is
destructive; `library_cover` writes; `check_series_updates` decides whether a
series claims "+N new" forever. On a device those cost a rebuild-install cycle
apiece and a real library to reproduce against. Here they are assertions over a
tmp_path.

The engine-lock tests are the load-bearing ones. Desktop runs one download per
OS process, so nothing there ever needed serialization; Android shares ONE
interpreter between the download service and the library's update checks, and
aio-dl.py's main() clears _RUN_CANCEL on entry. An unserialized check started
while a download is being cancelled would UN-CANCEL it.

Cross-file: android/app/src/main/java/com/aio/downloader/core/LibraryRepository.kt
(the caller), library_state.py (the scan these build on),
UI-source/electron/main.js:_checkSeriesUpdates (the behaviour being ported).
"""

from __future__ import annotations

import json
import os
import threading
import zipfile

import pytest

import aio_android
from aio_android import (
    ENGINE_BUSY,
    chapters_to_range,
    check_series_updates,
    delete_series,
    library_cover,
    series_files,
)


# --------------------------------------------------------------------------
# Fixtures — a library on disk, shaped the way aio-dl.py leaves one
# --------------------------------------------------------------------------

# A 1x1 PNG. Real bytes rather than b"stub" so anything that ever tries to
# decode one of these gets a decodable image.
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006"
    "0005a3a3a30000000049454e44ae426082"
)


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A configured library root. Yields (root, cache) as Paths.

    Patches _CONFIGURED directly rather than calling configure(), which would
    os.chdir() the whole test session into tmp_path and leave it there.
    """
    root = tmp_path / "manga"
    cache = tmp_path / "cache"
    root.mkdir()
    cache.mkdir()
    monkeypatch.setitem(aio_android._CONFIGURED, "output_dir", str(root))
    monkeypatch.setitem(aio_android._CONFIGURED, "cache_dir", str(cache))
    return root, cache


def make_series(root, name, *, meta=None, cbz_members=None, cover_file=None):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    if meta is not None:
        (folder / ".aio_series.json").write_text(json.dumps(meta), encoding="utf-8")
    if cover_file:
        (folder / cover_file).write_bytes(PNG_1PX)
    if cbz_members is not None:
        with zipfile.ZipFile(folder / f"{name} Ch 1.cbz", "w") as archive:
            for member in cbz_members:
                archive.writestr(member, PNG_1PX)
    return folder


# --------------------------------------------------------------------------
# Engine serialization
# --------------------------------------------------------------------------


def test_list_chapters_reports_busy_instead_of_waiting():
    """A check started while another THREAD holds the engine must fail fast.

    Blocking would be worse than it looks: the caller is a tap on "check for
    updates", and the holder is a download that can run for forty minutes.
    """
    holder_has_lock = threading.Event()
    release = threading.Event()

    def hold():
        with aio_android._ENGINE_LOCK:
            holder_has_lock.set()
            release.wait(5)

    worker = threading.Thread(target=hold, daemon=True)
    worker.start()
    try:
        assert holder_has_lock.wait(5)
        payload = json.loads(aio_android.list_chapters("https://example.test/x"))
        assert payload["error"] == ENGINE_BUSY
    finally:
        release.set()
        worker.join(5)


def test_engine_lock_is_reentrant_on_one_thread():
    """check_series_updates calls list_chapters, which re-acquires the lock.

    A plain Lock would deadlock the update check against itself — silently, as a
    UI that spins forever. This is the reason the lock is an RLock, so pin it.
    """
    assert isinstance(aio_android._ENGINE_LOCK, type(threading.RLock()))
    with aio_android._ENGINE_LOCK:
        assert aio_android._ENGINE_LOCK.acquire(blocking=False) is True
        aio_android._ENGINE_LOCK.release()


def test_run_download_holds_the_lock_while_running(monkeypatch):
    """The download path WAITS for the engine rather than reporting busy.

    Inverse of the check path, and the asymmetry is the whole design: a download
    must never fail because a 3-second chapter listing was in flight.
    """
    seen = []

    def fake_engine(argv, sink=None):
        # An RLock cannot be probed for "held by another thread" from the
        # holding thread, so assert from a second one.
        result = {}

        def probe():
            result["free"] = aio_android._ENGINE_LOCK.acquire(blocking=False)
            if result["free"]:
                aio_android._ENGINE_LOCK.release()

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(5)
        seen.append(result.get("free"))
        return 0

    monkeypatch.setattr(aio_android, "_run_engine", fake_engine)
    assert aio_android.run_download(["--help"]) == 0
    assert seen == [False], "run_download did not hold _ENGINE_LOCK"


# --------------------------------------------------------------------------
# chapters_to_range
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "labels,expected",
    [
        ([], "all"),
        (["7"], "7"),
        (["51", "52", "53"], "51-53"),
        # Out of order in, sorted out — the caller hands us a set difference.
        (["12", "10", "11"], "10-12"),
        (["1", "2", "3", "7", "9", "10", "11"], "1-3,7,9-11"),
        # THE divergence from chaptersToRangeString: it would emit "5-6" here
        # (its join threshold is 1.001), and `is_chapter_wanted` reads that as a
        # range, so an already-downloaded 5.5 would be re-fetched.
        (["5", "5.5", "6"], "5,5.5,6"),
        (["oneshot"], "oneshot"),
    ],
)
def test_chapters_to_range(labels, expected):
    assert chapters_to_range(labels) == expected


def test_chapters_to_range_ignores_blanks():
    assert chapters_to_range(["", "  ", "4", None]) == "4"


# --------------------------------------------------------------------------
# library_cover
# --------------------------------------------------------------------------


def test_cover_prefers_the_on_disk_file_and_does_not_copy(library):
    """--komikku writes cover.jpg at the series root. Use it in place."""
    root, cache = library
    folder = make_series(root, "Komikku Series", cover_file="cover.jpg")
    resolved = library_cover(str(folder))
    assert resolved == str(folder / "cover.jpg")
    assert not (cache / "covers").exists(), "no cache write for an on-disk cover"


def test_cover_extracts_from_cbz_into_the_cache_not_the_library(library):
    """The series folder is what Komikku/Mihon read — it stays byte-identical.

    library_state.find_cover_path CAN extract, but only with write_cache=True,
    which drops a `.cover.*` into the series folder. That is the behaviour this
    function exists to avoid.
    """
    root, cache = library
    folder = make_series(root, "Zip Series", cbz_members=["0001.png", "0002.png"])
    before = sorted(p.name for p in folder.iterdir())

    resolved = library_cover(str(folder))

    assert resolved, "expected a cover extracted from the CBZ"
    assert os.path.isfile(resolved)
    assert os.path.getsize(resolved) == len(PNG_1PX)
    assert str(cache) in resolved, "cover must land in the app cache"
    assert sorted(p.name for p in folder.iterdir()) == before, "library folder mutated"


def test_cover_picks_the_member_library_state_would(library):
    """Ordering comes from library_state._cover_sort_key, not from us.

    A member literally named "cover" outranks page 1 even though page 1 sorts
    first alphabetically. If this ever diverges, a series is represented by a
    different image on the phone than on the desktop.
    """
    root, _cache = library
    folder = root / "Ordered"
    folder.mkdir()
    with zipfile.ZipFile(folder / "Ordered Ch 1.cbz", "w") as archive:
        archive.writestr("0001.png", PNG_1PX)
        archive.writestr("cover.jpg", PNG_1PX + b"\x00")

    resolved = library_cover(str(folder))
    assert resolved.endswith(".jpg")


def test_cover_cache_is_keyed_on_the_source_file(library):
    """A re-download must not keep showing the old cover.

    Same path in, DIFFERENT filename out once the archive changes — which is
    what makes the result safe to hand to an image loader that caches by path.
    """
    root, _cache = library
    folder = make_series(root, "Rewritten", cbz_members=["0001.png"])
    first = library_cover(str(folder))
    assert library_cover(str(folder)) == first, "stable across calls"

    book = folder / "Rewritten Ch 1.cbz"
    with zipfile.ZipFile(book, "w") as archive:
        archive.writestr("0001.png", PNG_1PX + b"\x00\x00")
    os.utime(book, (0, 0))

    assert library_cover(str(folder)) != first


def test_cover_is_empty_for_pdf_only_and_for_junk(library):
    """PDF resolves to "" ON PURPOSE — PdfRenderer handles it Kotlin-side."""
    root, _cache = library
    pdf = make_series(root, "Pdf Series")
    (pdf / "Pdf Series Ch 1-10.pdf").write_bytes(b"%PDF-1.4\n")
    assert library_cover(str(pdf)) == ""

    corrupt = root / "Corrupt"
    corrupt.mkdir()
    (corrupt / "Corrupt Ch 1.cbz").write_bytes(b"not a zip at all")
    assert library_cover(str(corrupt)) == ""

    assert library_cover(str(root / "does-not-exist")) == ""
    assert library_cover("") == ""


# --------------------------------------------------------------------------
# series_files
# --------------------------------------------------------------------------


def test_series_files_lists_archives_and_raw_chapter_dirs(library):
    root, _cache = library
    folder = make_series(root, "Mixed", cbz_members=["0001.png"])
    (folder / "Mixed Ch 2.cbz").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    raw = folder / "Chapter_3"
    raw.mkdir()
    (raw / "0001.webp").write_bytes(PNG_1PX)
    (raw / "0002.webp").write_bytes(PNG_1PX)
    (raw / "notes.txt").write_text("ignored", encoding="utf-8")

    payload = json.loads(series_files(str(folder)))

    assert {f["name"] for f in payload["files"]} == {"Mixed Ch 1.cbz", "Mixed Ch 2.cbz"}
    assert all(f["ext"] == "cbz" for f in payload["files"])
    assert all(f["size"] > 0 for f in payload["files"])

    assert len(payload["chapter_dirs"]) == 1
    chapter = payload["chapter_dirs"][0]
    assert chapter["name"] == "Chapter_3"
    # Only images counted; notes.txt is not a page.
    assert chapter["images"] == 2
    assert chapter["size"] == 2 * len(PNG_1PX)


def test_series_files_finds_nested_image_dirs(library):
    """`--keep-images` alongside a final file nests pages under images/."""
    root, _cache = library
    folder = root / "Nested"
    (folder / "images" / "Chapter_1").mkdir(parents=True)
    (folder / "images" / "Chapter_1" / "0001.png").write_bytes(PNG_1PX)

    payload = json.loads(series_files(str(folder)))
    assert [c["name"] for c in payload["chapter_dirs"]] == ["Chapter_1"]


def test_series_files_on_a_missing_folder_is_empty_not_an_error(library):
    payload = json.loads(series_files(str(library[0] / "nope")))
    assert payload == {"files": [], "chapter_dirs": []}


# --------------------------------------------------------------------------
# check_series_updates — the diff, with the engine stubbed out
# --------------------------------------------------------------------------


@pytest.fixture
def fake_listing(monkeypatch):
    """Stub `list_chapters` and record the argv extras it was handed."""
    captured = {}

    def install(payload):
        def fake(url, extra_args=None):
            captured["url"] = url
            captured["extra"] = list(extra_args or [])
            return json.dumps(payload)

        monkeypatch.setattr(aio_android, "list_chapters", fake)
        return captured

    return install


def test_update_check_diffs_against_recorded_chapters(library, fake_listing):
    root, _cache = library
    folder = make_series(
        root,
        "Tekyuu",
        meta={
            "url": "https://example.test/title/1",
            "site": "dynasty",
            "language": "en",
            "chapters_downloaded": ["1", "2", "3"],
        },
    )
    captured = fake_listing(
        {"chapters": ["1", "2", "3", "4", "5"], "status": "Releasing", "title": "Tekyuu"}
    )

    result = json.loads(check_series_updates(str(folder)))

    assert result["ok"] is True
    assert result["newChapters"] == ["4", "5"]
    assert result["range"] == "4-5"
    assert result["total"] == 5
    assert result["downloaded"] == 3
    assert result["status"] == "Releasing"
    assert captured["url"] == "https://example.test/title/1"
    # `en` is aio-dl.py's own default, so it stays off the command line.
    assert captured["extra"] == ["--site", "dynasty"]


def test_update_check_subtracts_skipped_fragments(library, fake_listing):
    """The one thing that makes a series stick at "+N new" forever.

    Fragment labels the download path merged away are re-listed by a
    consensus-free `--list-chapters`; without this subtraction every check
    reports them, and every "download missing" refetches duplicates.
    """
    root, _cache = library
    folder = make_series(
        root,
        "Fragmented",
        meta={
            "url": "https://example.test/f",
            "chapters_downloaded": ["51", "52"],
            "chapters_skipped_fragments": ["52.1", "52.2"],
        },
    )
    fake_listing({"chapters": ["51", "52", "52.1", "52.2", "53"]})

    result = json.loads(check_series_updates(str(folder)))
    assert result["newChapters"] == ["53"]
    assert result["total"] == 3


def test_update_check_splits_off_user_crossed_out_chapters(library, fake_listing):
    """Chapters the desktop user crossed out are reported, not offered.

    Deliberately NOT the same treatment as chapters_skipped_fragments: those
    are machine-derived duplicates and vanish from the count entirely, while
    these are real chapters a person chose to skip and must stay visible so the
    choice can be undone. So they leave newChapters (nothing offers to download
    them) but ride ignoredChapters, and `total` still counts them as existing.
    """
    root, _cache = library
    folder = make_series(
        root,
        "CrossedOut",
        meta={
            "url": "https://example.test/x",
            "chapters_downloaded": ["1"],
            "chapters_ignored": ["2", "4"],
        },
    )
    fake_listing({"chapters": ["1", "2", "3", "4"]})

    result = json.loads(check_series_updates(str(folder)))
    assert result["newChapters"] == ["3"]
    assert result["ignoredChapters"] == ["2", "4"]
    assert result["range"] == "3"
    # Crossed-out chapters exist on the site, so they still count toward it.
    assert result["total"] == 4


def test_update_check_drops_stale_cross_outs(library, fake_listing):
    """A cross-out for a chapter that is gone or already downloaded is not shown.

    The on-disk list is intersected with what is genuinely missing, so it can
    never accumulate labels the user has no way to act on. The file itself is
    left alone — aio-dl.py prunes it on the next download of the series.
    """
    root, _cache = library
    folder = make_series(
        root,
        "Stale",
        meta={
            "url": "https://example.test/s",
            "chapters_downloaded": ["1", "2"],
            # 2 is already on device; 99 no longer exists on the site.
            "chapters_ignored": ["2", "99"],
        },
    )
    fake_listing({"chapters": ["1", "2", "3"]})

    result = json.loads(check_series_updates(str(folder)))
    assert result["newChapters"] == ["3"]
    assert result["ignoredChapters"] == []


def test_update_check_without_cross_outs_reports_an_empty_list(library, fake_listing):
    """Absent means empty — every series predating the feature has no such key."""
    root, _cache = library
    folder = make_series(
        root, "Plain", meta={"url": "https://example.test/p", "chapters_downloaded": ["1"]}
    )
    fake_listing({"chapters": ["1", "2"]})

    result = json.loads(check_series_updates(str(folder)))
    assert result["newChapters"] == ["2"]
    assert result["ignoredChapters"] == []


def test_update_check_unions_the_disk_scan(library, fake_listing):
    """The divergence from the desktop, and why it is the safe direction.

    A series downloaded before `chapters_downloaded` existed has files on disk
    and nothing recorded. The desktop's default mode reports EVERY chapter as
    new; the union reports only what is genuinely missing.
    """
    root, _cache = library
    folder = make_series(root, "Legacy", meta={"url": "https://example.test/l"})
    for n in (1, 2):
        (folder / f"Legacy Ch {n}.cbz").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    fake_listing({"chapters": ["1", "2", "3"]})

    result = json.loads(check_series_updates(str(folder)))
    assert result["newChapters"] == ["3"]


def test_update_check_forwards_language_and_collapse(library, fake_listing):
    root, _cache = library
    folder = make_series(
        root,
        "Japanese",
        meta={"url": "https://example.test/j", "language": "ja", "site": "mangadex"},
    )
    captured = fake_listing({"chapters": []})

    check_series_updates(str(folder), collapse_splits=True)
    assert captured["extra"] == [
        "--language", "ja",
        "--site", "mangadex",
        "--collapse-splits",
    ]


# Volumes (native volume releases; series_ledger keeps them under "volumes").
# The device has no volume UI, but the library is synced from the desktop, so
# a volume series must not read as "every chapter is new".


@pytest.fixture
def fake_listings(monkeypatch):
    """Stub `list_chapters` with a function of the argv extras; records every call."""
    calls = []

    def install(respond):
        def fake(url, extra_args=None):
            extra = list(extra_args or [])
            calls.append(extra)
            return json.dumps(respond(extra))

        monkeypatch.setattr(aio_android, "list_chapters", fake)
        return calls

    return install


ONE_PIECE = "https://mangafire.to/title/dkw-one-piece"


def test_update_check_volume_only_series_offers_no_chapters(library, fake_listings):
    root, _cache = library
    folder = make_series(root, "VolOnly", meta={
        "url": ONE_PIECE, "site": "mangafire", "title": "VolOnly",
        "volumes": {"schema": 1, "downloaded": ["1", "2"], "ignored": ["4"]},
    })
    calls = fake_listings(lambda extra: {"volumes": ["1", "2", "3", "4", "5"], "chapters": ["1", "2"]})

    result = json.loads(check_series_updates(str(folder), collapse_splits=True))
    assert calls == [["--download-volumes", "--site", "mangafire"]]
    assert result["trackedKinds"] == ["volume"]
    assert result["newChapters"] == [] and result["total"] == 0
    assert result["newVolumes"] == ["3", "5"]
    assert result["ignoredVolumes"] == ["4"]
    assert result["volumesTotal"] == 5 and result["volumesDownloaded"] == 2


def test_update_check_chapter_series_lists_no_volumes(library, fake_listings):
    root, _cache = library
    folder = make_series(root, "ChOnly", meta={"url": ONE_PIECE, "chapters_downloaded": ["1"]})
    calls = fake_listings(lambda extra: {"chapters": ["1", "2"]})

    result = json.loads(check_series_updates(str(folder)))
    assert calls == [[]]
    assert result["trackedKinds"] == ["chapter"]
    assert "newVolumes" not in result


def test_update_check_both_kinds_one_listing(library, fake_listings):
    root, _cache = library
    folder = make_series(root, "Both", meta={
        "url": ONE_PIECE, "title": "Both", "chapters_downloaded": ["1"],
        "volumes": {"schema": 1, "downloaded": ["1"]},
    })
    calls = fake_listings(lambda extra: {"chapters": ["1", "2"], "volumes": ["1", "2"]})

    result = json.loads(check_series_updates(str(folder)))
    assert calls == [["--with-volumes"]]
    assert result["trackedKinds"] == ["chapter", "volume"]
    assert result["newChapters"] == ["2"] and result["newVolumes"] == ["2"]


def test_update_check_volume_listing_failure_keeps_the_chapter_half(library, fake_listings):
    root, _cache = library
    folder = make_series(root, "VolFail", meta={
        "url": ONE_PIECE, "chapters_downloaded": ["1"], "volumes": {"downloaded": ["1"]},
    })
    fake_listings(lambda extra: {"chapters": ["1", "2"], "volumes": None})

    result = json.loads(check_series_updates(str(folder)))
    assert result["ok"] is True and result["newChapters"] == ["2"]
    assert result["newVolumes"] == [] and result["volumesError"]


def test_update_check_volume_language_gets_its_own_listing(library, fake_listings):
    root, _cache = library
    folder = make_series(root, "TwoLangs", meta={
        "url": ONE_PIECE, "language": "en", "chapters_downloaded": ["1"],
        "volumes": {"downloaded": ["1"], "language": "fr"},
    })
    calls = fake_listings(
        lambda extra: {"volumes": ["1", "2"]} if "--download-volumes" in extra else {"chapters": ["1"]}
    )

    result = json.loads(check_series_updates(str(folder)))
    assert calls == [[], ["--download-volumes", "--language", "fr"]]
    assert result["newChapters"] == [] and result["newVolumes"] == ["2"]


def test_update_check_volume_files_without_a_block_still_track_volumes(library, fake_listings):
    """An older build rewrote the ledger and dropped the volumes block; the
    files are the record (series_ledger.tracked_kinds self-heal)."""
    root, _cache = library
    folder = make_series(root, "Healed", meta={"url": ONE_PIECE, "title": "Healed"})
    (folder / "Healed Vol.01.cbz").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    calls = fake_listings(lambda extra: {"volumes": ["1", "2"]})

    result = json.loads(check_series_updates(str(folder)))
    assert calls == [["--download-volumes"]]
    assert result["newChapters"] == [] and result["newVolumes"] == ["2"]


def test_update_check_unreadable_peer_does_not_turn_chapters_on(library, fake_listings):
    root, _cache = library
    folder = make_series(root, "VolPrimary", meta={
        "url": ONE_PIECE, "volumes": {"downloaded": ["1"]},
    })
    peer = root / "VolPeer"
    peer.mkdir()
    (peer / ".aio_series.json").write_text("{broken", encoding="utf-8")
    calls = fake_listings(lambda extra: {"volumes": ["1"]})

    result = json.loads(check_series_updates(str(folder), peer_folders=[str(peer)]))
    assert calls == [["--download-volumes"]]
    assert result["trackedKinds"] == ["volume"]


def test_update_check_error_vocabulary(library, fake_listing):
    """Same error names as _checkSeriesUpdates, so both UIs can say the same things."""
    root, _cache = library

    missing = root / "NoMeta"
    missing.mkdir()
    assert json.loads(check_series_updates(str(missing)))["error"] == "no_metadata"

    broken = root / "Broken"
    broken.mkdir()
    (broken / ".aio_series.json").write_text("{not json", encoding="utf-8")
    assert json.loads(check_series_updates(str(broken)))["error"] == "invalid_metadata"

    # A JSON scalar parses but is not a metadata object.
    scalar = root / "Scalar"
    scalar.mkdir()
    (scalar / ".aio_series.json").write_text("42", encoding="utf-8")
    assert json.loads(check_series_updates(str(scalar)))["error"] == "invalid_metadata"

    urlless = make_series(root, "NoUrl", meta={"title": "x"})
    assert json.loads(check_series_updates(str(urlless)))["error"] == "no_url"

    folder = make_series(root, "Busy", meta={"url": "https://example.test/b"})
    fake_listing({"error": ENGINE_BUSY})
    assert json.loads(check_series_updates(str(folder)))["error"] == ENGINE_BUSY


# --------------------------------------------------------------------------
# delete_series — the only destructive entry point in the module
# --------------------------------------------------------------------------


def test_delete_removes_the_folder(library):
    root, _cache = library
    folder = make_series(root, "Doomed", cbz_members=["0001.png"])
    assert json.loads(delete_series(str(folder))) == {"ok": True}
    assert not folder.exists()
    assert root.exists()


def test_delete_refuses_the_library_root(library):
    """"Delete this series" must never be able to mean "delete the library"."""
    root, _cache = library
    make_series(root, "Bystander", cbz_members=["0001.png"])
    assert json.loads(delete_series(str(root)))["error"] == "refused_root"
    assert (root / "Bystander").exists()


def test_delete_refuses_paths_outside_the_library(library, tmp_path):
    root, _cache = library
    outside = tmp_path / "not-the-library"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")

    assert json.loads(delete_series(str(outside)))["error"] == "outside_library"
    # The classic traversal spelling, which realpath collapses before the check.
    escaped = str(root / ".." / "not-the-library")
    assert json.loads(delete_series(escaped))["error"] == "outside_library"
    assert (outside / "keep.txt").exists()


def test_delete_reports_a_missing_folder(library):
    root, _cache = library
    assert json.loads(delete_series(str(root / "ghost")))["error"] == "not_found"


def test_delete_without_configure_refuses(monkeypatch, tmp_path):
    """No configured root means no containment guard, so refuse outright."""
    monkeypatch.setattr(aio_android, "_CONFIGURED", {})
    victim = tmp_path / "victim"
    victim.mkdir()
    assert json.loads(delete_series(str(victim)))["error"] == "not_configured"
    assert victim.exists()


# --------------------------------------------------------------------------
# scan_library — the grid's payload shape
# --------------------------------------------------------------------------


def test_scan_library_is_json_and_survives_decimals(library):
    """Chapter numbers are Decimals inside library_state; to_jsonable flattens
    them. Without that pass json.dumps raises and the whole grid stays empty."""
    root, _cache = library
    folder = make_series(root, "Halves", meta={"url": "https://example.test/h"})
    (folder / "Halves Ch 1.cbz").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    (folder / "Halves Ch 1~5.cbz").write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    entries = json.loads(aio_android.scan_library(str(root)))
    assert len(entries) == 1
    entry = entries[0]
    assert entry["name"] == "Halves"
    assert entry["url"] == "https://example.test/h"
    assert sorted(entry["chapter_numbers"], key=float) == ["1", "1.5"]
    assert entry["latest_chapter"] == "1.5"
    assert entry["next_update"] == "1.5-"
