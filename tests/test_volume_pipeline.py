"""--download-volumes end to end: a chapter run and a volume run into ONE folder.

Drives the real main() offline with a fake site whose chapter and volume
numbers overlap (chapters 1-2, volumes 1-2) and whose pages are inline
binary_image blobs, so no network is touched. The point is that every storage
key is derived from the run's item kind (item_kinds.py), so volume 1 never
lands on chapter 1:

  - distinct files ("T Ch 1.cbz" vs "T Vol.01.cbz") and tmp dirs (tmp_ vs tmpvol_);
  - the volume run leaves the combined archive, download_params.json and the
    .aio_fp fingerprints exactly as the chapter run left them;
  - the ledger gets a `volumes` block and the chapter namespace is untouched;
  - the volume CBZ carries <Volume> and no <Number>;
  - volume-mode coercions, the Komikku override, and the two "no volumes"
    outcomes;
  - the --list-chapters protocol for both kinds;
  - --update-all's per-kind children and the library scan behind them;
  - the stretchable chapter watchdog.

MangaFire's own volume parsing is in tests/test_mangafire_volumes.py. Plan:
sync-temp/plans/volume-downloads.md (P1).
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import time
import zipfile

import pytest
from PIL import Image

import item_kinds
import library_state
import series_ledger
from sites.base import BaseSiteHandler, SiteComicContext
from tests.test_cli_flag_parsing import _load_aio_dl_module

URL = "https://volfake.test/title/abc"
TITLE = "Test Saga"


@pytest.fixture(scope="module")
def aio():
    return _load_aio_dl_module()


def _png(color):
    buf = io.BytesIO()
    Image.new("RGB", (40, 60), color).save(buf, "PNG")
    return buf.getvalue()


def _pages(kind_tag, n, count):
    # A distinct colour per item, so a test can tell whose pages landed where.
    shade = (n * 40) % 256
    color = (shade, 0, 200) if kind_tag == "V" else (0, shade, 50)
    return [
        {"type": "binary_image", "data": _png(color), "extension": ".png"}
        for _ in range(count)
    ]


class _VolSite(BaseSiteHandler):
    name = "volfake"

    def __init__(self, volumes=None, chapters=None):
        self._volumes = volumes if volumes is not None else [
            {"hid": "v1", "chap": "1", "title": "Dawn"},
            {"hid": "v2", "chap": "2", "title": "Dusk"},
        ]
        self._chapters = chapters if chapters is not None else [
            {"hid": "c1", "chap": "1", "title": "One"},
            {"hid": "c2", "chap": "2", "title": "Two"},
        ]
        self.calls = []

    def configure_session(self, scraper, args):
        pass

    def fetch_comic_context(self, url, scraper, make_request):
        return SiteComicContext(comic={"title": TITLE}, title=TITLE, identifier="abc")

    def extract_additional_metadata(self, context):
        return {}

    def get_chapters(self, context, scraper, language, make_request):
        return [dict(c) for c in self._chapters]

    def get_chapter_images(self, chapter, scraper, make_request):
        self.calls.append(("chapter", chapter["hid"]))
        return _pages("C", int(float(chapter["chap"])), 2)

    def has_volumes(self, context):
        return bool(self._volumes)

    def get_volumes(self, context, scraper, language, make_request):
        return [dict(v) for v in self._volumes]

    def get_volume_images(self, volume, scraper, make_request):
        self.calls.append(("volume", volume["hid"]))
        return _pages("V", int(float(volume["chap"])), 3)


class _ChapterOnlySite(_VolSite):
    name = "chaponly"
    get_volumes = BaseSiteHandler.get_volumes


@pytest.fixture
def env(aio, monkeypatch, tmp_path):
    state = {"site": _VolSite()}
    monkeypatch.setattr(aio, "resolve_site_handler", lambda url, site: state["site"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AIO_COORD_ENABLED", "0")
    state["root"] = tmp_path / "manga"
    state["tmp"] = tmp_path / "tmp"
    state["tmp"].mkdir()
    return state


def _run(aio, monkeypatch, env, *argv, capsys=None):
    monkeypatch.setattr(sys, "argv", [
        "aio-dl.py", URL, "--output-dir", str(env["root"]), "--temp-dir", str(env["tmp"]),
        "--image-prefetch-depth", "0", *argv,
    ])
    return aio.main()


def _series(env):
    return env["root"] / TITLE


def _snapshot(folder):
    out = {}
    for dirpath, _dirs, files in os.walk(folder):
        for name in files:
            p = os.path.join(dirpath, name)
            if name == series_ledger.SERIES_META_FILE:
                continue
            with open(p, "rb") as f:
                out[os.path.relpath(p, folder)] = f.read()
    return out


def _comic_info(path):
    with zipfile.ZipFile(path) as z:
        return z.read("ComicInfo.xml").decode() if "ComicInfo.xml" in z.namelist() else ""


def _page_colors(path):
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist() if n[0].isdigit())
        return {Image.open(io.BytesIO(z.read(n))).convert("RGB").getpixel((5, 5)) for n in names}, len(names)


# --------------------------------------------------------------------------
# The two kinds in one folder
# --------------------------------------------------------------------------

def test_chapter_then_volume_run_share_a_folder_and_nothing_else(aio, env, monkeypatch):
    _run(aio, monkeypatch, env, "--chapters", "1-2", "--format", "cbz", "--keep-chapters", "--save-params")
    folder = _series(env)
    before = _snapshot(folder)
    ledger_before = series_ledger.load_series_meta(str(folder))
    assert {"Test Saga Ch 1.cbz", "Test Saga Ch 2.cbz", "Test Saga.cbz", "download_params.json"} <= set(before)

    env["site"].calls.clear()
    _run(aio, monkeypatch, env, "--download-volumes", "--chapters", "1", "--format", "cbz",
         "--save-params", "--group", "SomeScan", "--multi-source")
    after = _snapshot(folder)

    # Only the volume file is new; every chapter-run artifact is byte-identical
    # (combined archive, per-chapter files, download_params.json, .aio_fp/).
    assert set(after) - set(before) == {"Test Saga Vol.01.cbz"}
    for name, data in before.items():
        assert after[name] == data, name
    # The volume went to the volume endpoint only.
    assert env["site"].calls == [("volume", "v1")]

    colors, count = _page_colors(folder / "Test Saga Vol.01.cbz")
    assert count == 3 and all(c[2] == 200 for c in colors)  # volume pages, not chapter 1's
    ci = _comic_info(folder / "Test Saga Vol.01.cbz")
    assert "<Volume>1</Volume>" in ci and "<Number>" not in ci
    assert "<Title>Dawn</Title>" in ci

    ledger = series_ledger.load_series_meta(str(folder))
    for key, value in ledger_before.items():
        if key != "volumes":
            assert ledger[key] == value, key
    assert ledger["volumes"]["downloaded"] == ["1"]
    assert ledger["volumes"]["format"] == "cbz"
    assert ledger["volumes"]["total_available_at_download"] == 2
    assert "download_volumes" not in ledger


def test_a_later_chapter_run_keeps_the_volume_block(aio, env, monkeypatch):
    _run(aio, monkeypatch, env, "--download-volumes", "--chapters", "2", "--format", "cbz")
    _run(aio, monkeypatch, env, "--chapters", "1", "--format", "cbz", "--keep-chapters")
    ledger = series_ledger.load_series_meta(str(_series(env)))
    assert ledger["volumes"]["downloaded"] == ["2"]
    assert ledger["chapters_downloaded"] == ["1"]


def test_tmp_dirs_are_per_kind(aio, env, monkeypatch):
    _run(aio, monkeypatch, env, "--chapters", "1", "--format", "cbz", "--keep-chapters", "--no-cleanup")
    _run(aio, monkeypatch, env, "--download-volumes", "--chapters", "1", "--format", "cbz", "--no-cleanup")
    names = sorted(os.listdir(env["tmp"]))
    assert names == ["tmp_abc", "tmpvol_abc"]
    meta = json.loads((env["tmp"] / "tmpvol_abc" / "run_meta.json").read_text())
    assert meta["kind"] == "volume"
    assert json.loads((env["tmp"] / "tmpvol_abc" / "run_params.json").read_text())["params"]["download_volumes"] is True


def test_keep_images_uses_volume_folders(aio, env, monkeypatch):
    _run(aio, monkeypatch, env, "--download-volumes", "--chapters", "2", "--format", "cbz", "--keep-images")
    assert os.listdir(_series(env) / "images") == ["Volume_2"]


# --------------------------------------------------------------------------
# Coercions and refusals
# --------------------------------------------------------------------------

def test_volume_mode_coercions_are_announced(aio, env, monkeypatch, capsys):
    _run(aio, monkeypatch, env, "--download-volumes", "--chapters", "1", "--format", "cbz",
         "--collapse-splits", "--multi-source", "--group", "X", "--mtl", "exclude")
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if l.startswith("[Volumes] Forcing"))
    for bit in ("--keep-chapters", "--no-final-file", "off --collapse-splits",
                "off --multi-source", "off --group", "--mtl avoid"):
        assert bit in line
    assert "Selected 1 volumes." in out
    assert re.search(r"^Volume 1 \(", out, re.M)
    assert not os.path.exists(_series(env) / "Test Saga.cbz")


def test_split_is_rejected(aio, env, monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _run(aio, monkeypatch, env, "--download-volumes", "--split", "10ch")
    assert exc.value.code == 2


def test_komikku_forces_volume_mode_off(aio, env, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _run(aio, monkeypatch, env, "--komikku", "--download-volumes", "--list-chapters")
    out = capsys.readouterr().out
    assert "off --download-volumes" in out
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["chapters"] == ["1", "2"]
    assert "volumes" not in payload


def test_site_without_volume_support(aio, env, monkeypatch):
    env["site"] = _ChapterOnlySite()
    with pytest.raises(SystemExit) as exc:
        _run(aio, monkeypatch, env, "--download-volumes")
    assert "(volumes_unsupported)" in str(exc.value.code)
    assert not os.path.exists(env["root"])


def test_series_without_volumes(aio, env, monkeypatch):
    env["site"] = _VolSite(volumes=[])
    with pytest.raises(SystemExit) as exc:
        _run(aio, monkeypatch, env, "--download-volumes")
    assert "(no_volumes)" in str(exc.value.code)


def test_nothing_new_reads_as_up_to_date(aio, env, monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _run(aio, monkeypatch, env, "--download-volumes", "--chapters", "9-")
    assert str(exc.value.code) == "No volumes selected."


# --------------------------------------------------------------------------
# --list-chapters protocol
# --------------------------------------------------------------------------

def _listing(aio, env, monkeypatch, capsys, *argv):
    with pytest.raises(SystemExit) as exc:
        _run(aio, monkeypatch, env, "--list-chapters", *argv)
    assert exc.value.code == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_list_chapters_alone_is_unchanged(aio, env, monkeypatch, capsys):
    d = _listing(aio, env, monkeypatch, capsys)
    assert d["chapters"] == ["1", "2"]
    assert "volumes" not in d and "volumes_supported" not in d


def test_list_chapters_with_volumes_lists_both(aio, env, monkeypatch, capsys):
    d = _listing(aio, env, monkeypatch, capsys, "--with-volumes")
    assert d["chapters"] == ["1", "2"]
    assert d["volumes"] == ["1", "2"] and d["volumes_total"] == 2
    assert d["volumes_supported"] is True and d["has_volumes"] is True


def test_list_chapters_with_volumes_on_a_chapter_only_site(aio, env, monkeypatch, capsys):
    env["site"] = _ChapterOnlySite()
    d = _listing(aio, env, monkeypatch, capsys, "--with-volumes")
    assert d["volumes_supported"] is False and d["volumes"] == []


def test_volume_listing_failure_keeps_the_chapter_listing(aio, env, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("volume endpoint down")
    monkeypatch.setattr(env["site"], "get_volumes", boom)
    d = _listing(aio, env, monkeypatch, capsys, "--with-volumes")
    assert d["chapters"] == ["1", "2"] and d["volumes"] is None


def test_list_chapters_download_volumes_puts_volumes_under_their_own_key(aio, env, monkeypatch, capsys):
    d = _listing(aio, env, monkeypatch, capsys, "--download-volumes")
    assert d["chapters"] == [] and d["total"] == 0
    assert d["volumes"] == ["1", "2"] and d["volume_titles"] == {"1": "Dawn", "2": "Dusk"}
    assert d["kind"] == "volume"


def test_list_volumes_of_a_series_without_any_is_an_empty_listing(aio, env, monkeypatch, capsys):
    env["site"] = _VolSite(volumes=[])
    d = _listing(aio, env, monkeypatch, capsys, "--download-volumes")
    assert d["volumes"] == [] and d["has_volumes"] is False


def test_site_capabilities(aio, env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["aio-dl.py", "--site-capabilities"])
    aio.main()
    rows = json.loads(capsys.readouterr().out)
    assert {r["name"] for r in rows if r["volumes"]} == {"mangafire"}


# --------------------------------------------------------------------------
# --update-all and the library scan
# --------------------------------------------------------------------------

def _entry(tmp_path, meta, files=()):
    folder = tmp_path / "lib" / TITLE
    folder.mkdir(parents=True)
    (folder / series_ledger.SERIES_META_FILE).write_text(json.dumps(meta), encoding="utf-8")
    for name in files:
        (folder / name).write_bytes(b"x")
    return next(e for e in library_state.scan_library(str(tmp_path / "lib")) if e["name"] == TITLE)


def _flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_scan_keeps_volume_files_out_of_the_chapter_count(tmp_path):
    e = _entry(
        tmp_path,
        {"url": URL, "chapters_downloaded": ["1", "2"],
         "volumes": {"downloaded": ["1"], "format": "pdf", "language": "fr"}},
        files=["Test Saga Ch 1.cbz", "Test Saga Ch 2.cbz", "Test Saga Vol.01.pdf", "Test Saga Vol.03.pdf"],
    )
    assert e["chapters"] == 2 and e["next_update"] == "3-"
    assert e["volumes"] == 2 and e["next_volume_update"] == "4-"
    assert e["tracked_kinds"] == ["chapter", "volume"]
    assert (e["volume_format"], e["volume_language"]) == ("pdf", "fr")


def test_a_title_ending_in_vol_does_not_make_its_archive_a_volume(tmp_path):
    # Series "Saga Vol.3": its combined archive is "Saga Vol.3.cbz". Without
    # the ledger-title base it would read as volume 3 and the update check
    # would start a volume job for a chapter series.
    e = _entry(tmp_path, {"url": URL, "title": "Saga: Vol.3", "chapters_downloaded": ["1"]},
               files=["Saga Vol.3.cbz", "Saga Vol.3 Ch 1.cbz"])
    assert e["tracked_kinds"] == ["chapter"] and e["volumes"] == 0
    e2 = _entry(tmp_path / "b", {"url": URL, "title": "Saga: Vol.3", "chapters_downloaded": ["1"]},
                files=["Saga Vol.3.cbz", "Saga Vol.3 Vol.01.cbz"])
    assert e2["tracked_kinds"] == ["chapter", "volume"] and e2["volumes"] == 1


def test_scan_self_heals_a_dropped_volume_block(tmp_path):
    e = _entry(tmp_path, {"url": URL, "chapters_downloaded": []}, files=["Test Saga Vol.02.cbz"])
    assert e["tracked_kinds"] == ["volume"]
    assert e["volume_format"] == "cbz"


def test_update_children_per_kind(aio, tmp_path):
    e = _entry(
        tmp_path,
        {"url": URL, "format": "pdf", "chapters_downloaded": ["1"],
         "volumes": {"downloaded": ["1", "2"], "format": "cbz", "language": "en"}},
    )
    kids = aio._build_update_children(e, "ROOT")
    assert [k[0] for k in kids] == [TITLE, f"{TITLE} (volumes)"]
    chap, vol = kids[0][1], kids[1][1]
    assert "--download-volumes" not in chap and _flag(chap, "--format") == "pdf"
    assert "--download-volumes" in vol
    assert _flag(vol, "--chapters") == "3-" and _flag(vol, "--format") == "cbz"
    assert "--save-params" not in vol


def test_volume_only_series_gets_no_chapter_child(aio, tmp_path):
    e = _entry(tmp_path, {"url": URL, "volumes": {"downloaded": ["1"]}})
    kids = aio._build_update_children(e, "ROOT")
    assert [k[0] for k in kids] == [f"{TITLE} (volumes)"]


@pytest.mark.parametrize("saved,flag", [(True, False), (False, True)])
def test_no_volume_child_under_komikku(aio, tmp_path, saved, flag, capsys):
    e = _entry(tmp_path, {"url": URL, "komikku": saved, "chapters_downloaded": ["1"],
                          "volumes": {"downloaded": ["1"]}})
    kids = aio._build_update_children(e, "ROOT", komikku=flag)
    assert [k[0] for k in kids] == [TITLE]
    assert "volume updates skipped: Komikku mode" in capsys.readouterr().out


def test_legacy_series_still_gets_exactly_the_old_chapter_command(aio, tmp_path):
    e = _entry(tmp_path, {"url": URL, "format": "epub", "chapters_downloaded": ["1", "2"]})
    kids = aio._build_update_children(e, "ROOT")
    assert len(kids) == 1
    cmd = kids[0][1]
    # The replay's own default (--scaling 100 unless --no-processing) included.
    assert cmd[2:] == [URL, "--chapters", "3-", "--format", "epub", "--language", "en",
                       "--output-dir", "ROOT", "--save-params", "--keep-chapters",
                       "--scaling", "100"]


def test_download_volumes_is_no_longer_replayed_onto_chapter_updates(aio):
    cmd = []
    aio._append_saved_update_options(cmd, {"download_volumes": True})
    assert "--download-volumes" not in cmd


# --------------------------------------------------------------------------
# Watchdog
# --------------------------------------------------------------------------

def test_deadline_stretches_with_page_count(aio):
    fired = threading.Event()
    d = aio._ChapterDeadline(0.3, fired.set)
    try:
        assert d.extend_for_pages(30) == 0.3          # short item: no change
        assert d.extend_for_pages(600) == pytest.approx(3.0)
        time.sleep(0.6)
        assert not fired.is_set()                     # the old 0.3 s timer is gone
    finally:
        d.cancel()


def test_deadline_never_shrinks_or_revives(aio):
    fired = threading.Event()
    d = aio._ChapterDeadline(0.05, fired.set)
    assert fired.wait(2)
    assert d.extend_for_pages(1000) == 0.05           # already fired: stays fired
    d.cancel()
    d2 = aio._ChapterDeadline(10, lambda: None)
    d2.extend_for_pages(600)
    assert d2.extend_for_pages(120) == pytest.approx(100)
    d2.cancel()
