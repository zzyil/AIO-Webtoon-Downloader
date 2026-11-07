"""Coverage for embedded-metadata editing — aio_android's wrapper AND the
shared metadata_editor module underneath it.

WHY BOTH LAYERS ARE HERE: the wrapper's job is to stop a bad path or a bad
moment from reaching a function that REWRITES the user's archive in place, and
the module's job is to write something the reader can parse. Neither is
observable from the other, and both fail quietly — an unsupported extension
falls straight through metadata_editor's router and reads in the UI as "Save did
nothing".

THE LIST BUG THIS FILE PINS: the setters used to call `value.strip()` directly,
so any list raised AttributeError. LibraryTab.jsx's editor splits Writers /
Pencillers / Genres on commas and sends them as JSON ARRAYS — so every desktop
metadata save that touched those three fields failed outright. `_as_text` is the
fix and these are the regression tests; see the module header for why the
collapse happens in one place rather than in each format writer.

Cross-file: metadata_editor.py, metadata_cli.py (the desktop's subprocess
route), UI-source/src/components/LibraryTab.jsx:MetadataEditorPanel (the caller
that sends the arrays), android/.../ui/screens/SeriesDetailScreen.kt.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import xml.etree.ElementTree as ET
import zipfile

import pytest

import aio_android
from aio_android import ENGINE_BUSY, read_book_metadata, write_book_metadata
from metadata_editor import _as_text, read_metadata, update_metadata

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006"
    "0005a3a3a30000000049454e44ae426082"
)


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A configured library root with one two-chapter series. Yields
    (root, [chapter paths as str])."""
    root = tmp_path / "manga"
    folder = root / "Some Series"
    folder.mkdir(parents=True)
    monkeypatch.setitem(aio_android._CONFIGURED, "output_dir", str(root))
    monkeypatch.setitem(aio_android._CONFIGURED, "work_dir", str(tmp_path / "work"))

    paths = []
    for number in (1, 2):
        path = folder / f"Ch.{number:03d}.cbz"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("0001.jpg", PNG_1PX)
        paths.append(str(path))
    return root, paths


def comic_info(path):
    """The raw ComicInfo.xml element tree, for assertions the reader-facing
    round trip cannot make (element PRESENCE vs an empty string)."""
    with zipfile.ZipFile(path) as archive:
        return ET.fromstring(archive.read("ComicInfo.xml"))


# --------------------------------------------------------------------------
# metadata_editor._as_text — the shared normalizer
# --------------------------------------------------------------------------


def test_as_text_collapses_the_shapes_callers_actually_send():
    assert _as_text(None) == ""
    assert _as_text("") == ""
    assert _as_text("Solo") == "Solo"
    assert _as_text(["A", "B"]) == "A, B"
    assert _as_text(("A",)) == "A"
    # Blank members are dropped, not joined into ", , " — the desktop's
    # split(",").filter(Boolean) can still hand over a trailing empty.
    assert _as_text(["A", "", "  ", "B"]) == "A, B"
    # Non-strings are stringified rather than rejected: a year arriving as an
    # int should land as "2020", not raise.
    assert _as_text(2020) == "2020"


def test_a_list_value_no_longer_raises(library):
    """The exact payload UI-source/src/components/LibraryTab.jsx sends. Before
    _as_text this raised AttributeError and the whole save failed."""
    _, paths = library
    update_metadata(paths[0], {"title": "T", "writers": ["A", "B"], "genres": ["x", "y"]})
    assert read_metadata(paths[0])["writers"] == "A, B"
    assert read_metadata(paths[0])["genres"] == "x, y"


def test_a_blank_value_removes_the_element_rather_than_emptying_it(library):
    """Readers treat a present-but-empty tag as a real (empty) value, which is
    not the same as "this series has no publisher"."""
    _, paths = library
    update_metadata(paths[0], {"publisher": "Shueisha"})
    assert comic_info(paths[0]).find("Publisher") is not None

    update_metadata(paths[0], {"publisher": ""})
    assert comic_info(paths[0]).find("Publisher") is None


def test_updating_preserves_the_pages(library):
    """update_metadata rebuilds the archive member by member. Losing a page
    while renaming an author would be the worst possible trade."""
    _, paths = library
    update_metadata(paths[0], {"title": "T"})
    with zipfile.ZipFile(paths[0]) as archive:
        assert "0001.jpg" in archive.namelist()
        assert archive.read("0001.jpg") == PNG_1PX


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits; Windows chmod only moves the read-only flag")
def test_updating_preserves_the_files_permissions(library):
    """An in-place edit must not rewrite the file's permissions.

    update_metadata rebuilds into a `tempfile.mkstemp()` file (mode 0600 by
    design) and moves it over the original; across filesystems shutil.move
    degrades to copy2, which stamps the SOURCE's mode onto the destination.
    Observed on device: `-rw-rw----` became `-rw-------`.

    Matters most where the library is a folder other apps read — the shared
    MANAGE_EXTERNAL_STORAGE path, or a multi-user/NAS library on desktop. NOT a
    rescue on Android's app-scoped storage, where aio-dl.py's own downloads
    already land as 0600.

    POSIX-only: Windows chmod moves the read-only bit and nothing else, so the
    assertion would be vacuous there.
    """
    _, paths = library
    target = paths[0]
    os.chmod(target, 0o664)
    before = stat.S_IMODE(os.stat(target).st_mode)

    update_metadata(target, {"title": "T"})

    assert stat.S_IMODE(os.stat(target).st_mode) == before
    # And the group really can still read it — the property that actually
    # matters, rather than the number matching.
    assert stat.S_IMODE(os.stat(target).st_mode) & stat.S_IRGRP


def test_title_also_writes_series(library):
    """Komikku and Mihon group by <Series>, not <Title>. Writing only the latter
    renames the chapter and leaves the series grouped under the old name."""
    _, paths = library
    update_metadata(paths[0], {"title": "Renamed"})
    root = comic_info(paths[0])
    assert root.findtext("Title") == "Renamed"
    assert root.findtext("Series") == "Renamed"


# --------------------------------------------------------------------------
# read_book_metadata
# --------------------------------------------------------------------------


def test_read_returns_every_field_even_when_absent(library):
    """A fixed key set, so the editor can bind a fixed form. Missing fields come
    back as empty strings rather than absent keys."""
    _, paths = library
    payload = json.loads(read_book_metadata(paths[0]))
    assert payload["ok"] is True
    assert set(payload["metadata"]) == {
        "title", "writers", "pencillers", "genres", "publisher", "synopsis",
    }
    assert all(value == "" for value in payload["metadata"].values())


def test_read_round_trips_a_write(library):
    _, paths = library
    write_book_metadata(json.dumps([paths[0]]), json.dumps({"title": "T", "writers": ["A", "B"]}))
    metadata = json.loads(read_book_metadata(paths[0]))["metadata"]
    assert metadata["title"] == "T"
    assert metadata["writers"] == "A, B"


def test_read_rejects_an_unsupported_extension(library):
    """metadata_editor's router silently returns {} for anything else, which
    would present as an editor that shows nothing and saves nothing."""
    root, _ = library
    cover = root / "Some Series" / "cover.jpg"
    cover.write_bytes(PNG_1PX)
    assert json.loads(read_book_metadata(str(cover)))["error"] == "unsupported_format"


def test_read_rejects_a_path_outside_the_library(library, tmp_path):
    outside = tmp_path / "elsewhere.cbz"
    with zipfile.ZipFile(outside, "w") as archive:
        archive.writestr("0001.jpg", PNG_1PX)
    assert json.loads(read_book_metadata(str(outside)))["error"] == "outside_library"


def test_read_is_allowed_during_a_download(library):
    """Reading opens the archive read-only. Refusing to DISPLAY metadata while
    an unrelated series downloads would be caution with no hazard behind it."""
    _, paths = library
    with aio_android._ENGINE_LOCK:
        assert json.loads(read_book_metadata(paths[0]))["ok"] is True


# --------------------------------------------------------------------------
# write_book_metadata
# --------------------------------------------------------------------------


def test_write_applies_to_every_path_in_one_call(library):
    """"Apply to all chapters" is ONE call holding the lock once. Per-file calls
    would let a download start midway and leave the series half-edited."""
    _, paths = library
    payload = json.loads(write_book_metadata(json.dumps(paths), json.dumps({"title": "T"})))
    assert payload["written"] == 2
    assert payload["failed"] == []
    assert all(json.loads(read_book_metadata(p))["metadata"]["title"] == "T" for p in paths)


def test_only_known_fields_are_forwarded(library):
    """A key the caller never showed must not reach the XML writer — as an
    unexpected element, or worse, as a present-and-empty one that WIPES a field
    the editor was not editing."""
    _, paths = library
    update_metadata(paths[0], {"publisher": "Kept"})
    write_book_metadata(
        json.dumps([paths[0]]),
        json.dumps({"title": "T", "bogus": "nope", "Genre": "wrong case"}),
    )
    root = comic_info(paths[0])
    assert root.find("bogus") is None
    assert root.findtext("Publisher") == "Kept"


def test_one_bad_path_does_not_abort_the_rest(library, tmp_path):
    """With 300 chapter archives, stopping at the first failure leaves the user
    worse off than skipping it."""
    _, paths = library
    outside = str(tmp_path / "nope.cbz")
    payload = json.loads(
        write_book_metadata(json.dumps([paths[0], outside, paths[1]]), json.dumps({"title": "T"}))
    )
    assert payload["written"] == 2
    assert [f["error"] for f in payload["failed"]] == ["outside_library"]


def test_write_refuses_while_the_engine_is_held(library):
    """A download is writing archives INTO the library. Rewriting one underneath
    it is a corrupted file, so this reports the same ENGINE_BUSY the update
    checks use rather than racing."""
    _, paths = library
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
        payload = json.loads(write_book_metadata(json.dumps(paths), json.dumps({"title": "T"})))
        assert payload["error"] == ENGINE_BUSY
    finally:
        release.set()
        worker.join(5)

    # And the refusal really was a refusal — nothing was written.
    assert json.loads(read_book_metadata(paths[0]))["metadata"]["title"] == ""


def test_write_rejects_degenerate_input(library):
    _, paths = library
    assert json.loads(write_book_metadata("[]", json.dumps({"title": "T"})))["error"] == "no_paths"
    assert json.loads(write_book_metadata("not json", "{}"))["error"] == "bad_paths"
    assert json.loads(
        write_book_metadata(json.dumps(paths), json.dumps({"nothing": 1}))
    )["error"] == "no_fields"


def test_write_without_configuration_refuses(library, monkeypatch):
    """No configured root means no containment guard, and the guard is the only
    thing standing between a JNI-delivered string and an in-place rewrite."""
    _, paths = library
    monkeypatch.setitem(aio_android._CONFIGURED, "output_dir", "")
    payload = json.loads(write_book_metadata(json.dumps([paths[0]]), json.dumps({"title": "T"})))
    assert [f["error"] for f in payload["failed"]] == ["not_configured"]
    assert payload["written"] == 0


# --------------------------------------------------------------------------
# write_book_metadata's cover argument
#
# The third argument used to be a hardcoded None, so the metadata editor could
# never change a cover. The interesting decision is the CONTAINMENT SET: the
# book must be in the library, but the cover cannot be — a picked image arrives
# as a content:// URI Python cannot open(), so the caller copies it into the app
# cache first. A library-only guard would reject every cover the feature can
# produce. See aio_android._cover_path_error.
# --------------------------------------------------------------------------


@pytest.fixture
def cover(tmp_path, monkeypatch):
    """A configured cache dir holding one pickable image. Yields its path."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setitem(aio_android._CONFIGURED, "cache_dir", str(cache))
    path = cache / "picked_cover.png"
    path.write_bytes(PNG_1PX)
    return str(path)


def test_a_cover_from_the_app_cache_is_embedded(library, cover):
    """The whole point: the cache is NOT the library, and it still has to work."""
    _, paths = library
    payload = json.loads(
        write_book_metadata(json.dumps([paths[0]]), json.dumps({"title": "T"}), cover)
    )
    assert payload["ok"] is True
    assert payload["written"] == 1
    assert payload["cover"] is True
    with zipfile.ZipFile(paths[0]) as archive:
        embedded = [n for n in archive.namelist() if n.startswith("0000_cover")]
        assert embedded == ["0000_cover.png"]
        assert archive.read(embedded[0]) == PNG_1PX
    # And the pages survived the repackage.
    with zipfile.ZipFile(paths[0]) as archive:
        assert "0001.jpg" in archive.namelist()


def test_omitting_the_cover_leaves_the_existing_one_alone(library, cover):
    """A text-only edit must not strip a cover a previous save embedded."""
    _, paths = library
    write_book_metadata(json.dumps([paths[0]]), json.dumps({"title": "T"}), cover)
    write_book_metadata(json.dumps([paths[0]]), json.dumps({"title": "Renamed"}))
    with zipfile.ZipFile(paths[0]) as archive:
        assert "0000_cover.png" in archive.namelist()
    assert json.loads(read_book_metadata(paths[0]))["metadata"]["title"] == "Renamed"


def test_the_default_third_argument_keeps_the_old_two_argument_call_working():
    """Kotlin calls this positionally. A required third argument would break
    every existing call site the moment this shipped."""
    import inspect

    params = inspect.signature(write_book_metadata).parameters
    assert list(params) == ["paths_json", "data_json", "cover_path"]
    assert params["cover_path"].default == ""


def test_a_cover_only_edit_is_allowed(library, cover):
    """The user changed the picture and nothing else. Before the cover argument
    existed this path returned no_fields, which would have made the picker a
    silent no-op whenever the text fields happened to be untouched."""
    _, paths = library
    payload = json.loads(write_book_metadata(json.dumps([paths[0]]), "{}", cover))
    assert payload["ok"] is True
    assert payload["written"] == 1
    with zipfile.ZipFile(paths[0]) as archive:
        assert "0000_cover.png" in archive.namelist()


def test_no_fields_and_no_cover_is_still_refused(library):
    _, paths = library
    assert json.loads(write_book_metadata(json.dumps(paths), "{}"))["error"] == "no_fields"


def test_a_cover_outside_the_apps_own_directories_is_refused(library, cover, tmp_path):
    """The bytes get copied INTO the user's archives, so an arbitrary
    JNI-delivered path is not something to trust."""
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(PNG_1PX)
    _, paths = library
    payload = json.loads(
        write_book_metadata(json.dumps([paths[0]]), json.dumps({"title": "T"}), str(outside))
    )
    assert payload["error"] == "cover_outside_app_dirs"


def test_a_non_image_cover_is_refused(library, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setitem(aio_android._CONFIGURED, "cache_dir", str(cache))
    bogus = cache / "notes.txt"
    bogus.write_text("nope")
    _, paths = library
    payload = json.loads(
        write_book_metadata(json.dumps([paths[0]]), json.dumps({"title": "T"}), str(bogus))
    )
    assert payload["error"] == "unsupported_cover_format"


def test_a_missing_cover_is_refused(library, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setitem(aio_android._CONFIGURED, "cache_dir", str(cache))
    _, paths = library
    payload = json.loads(
        write_book_metadata(
            json.dumps([paths[0]]), json.dumps({"title": "T"}), str(cache / "gone.png")
        )
    )
    assert payload["error"] == "cover_not_found"


def test_a_bad_cover_fails_the_whole_call_rather_than_being_dropped(library, tmp_path):
    """The opposite of how a bad BOOK path is treated, deliberately. Skipping
    one unwritable archive out of 300 still leaves the user better off; silently
    ignoring the cover they just picked and reporting ok:true does not."""
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(PNG_1PX)
    _, paths = library
    payload = json.loads(
        write_book_metadata(json.dumps(paths), json.dumps({"title": "T"}), str(outside))
    )
    assert "ok" not in payload
    assert payload["error"] == "cover_outside_app_dirs"
    # Nothing was written — not even the metadata half.
    assert json.loads(read_book_metadata(paths[0]))["metadata"]["title"] == ""


def test_a_cover_inside_the_library_is_also_accepted(library):
    """`cover.jpg` next to the archives is the one aio-dl.py itself writes, so
    "re-embed the series cover" must not be refused by the guard."""
    root, paths = library
    existing = root / "Some Series" / "cover.jpg"
    existing.write_bytes(PNG_1PX)
    payload = json.loads(
        write_book_metadata(json.dumps([paths[0]]), json.dumps({"title": "T"}), str(existing))
    )
    assert payload["ok"] is True
    with zipfile.ZipFile(paths[0]) as archive:
        assert "0000_cover.jpg" in archive.namelist()
