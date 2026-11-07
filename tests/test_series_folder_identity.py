"""Series folders are chosen by IDENTITY, not by the site's title string.

WHY THIS FILE EXISTS: allocate_series_output_dir used to build
``root/<sanitized title>`` and only THEN consult ``.series_hid``. When a site
renamed a series the title-derived folder did not exist, so a second folder was
created carrying an identical marker — the library showed two entries for one
series and the update check under-reported on both halves (the 59-chapter folder
said "5 new" forever because the delta kept landing in its 5-chapter fork, while
the fork said "59 new"). Two real cases, both forked in one update sweep:
MangaDex retitled "Isekai de Slow Life o (Ganbou)" to "...wo (Ganbou)", and
atsumaru retitled "Konosuba:" to "Konosuba!".

The regressions that matter here are as much about what must NOT change: the
"(hid=…)" collision sibling, the empty-orphan reclaim from PR #48, and the asura
rotating-hash tolerance all predate this and are load-bearing.

Cross-file: UI-source/electron/library.js:seriesIdentityKey and
library_state.py:series_identity_key are the twins that group forks already on
disk. grep seriesIdentityKey.
"""

from __future__ import annotations

import importlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

aio = importlib.import_module("aio-dl")


# ── fixtures ────────────────────────────────────────────────────────────────


def make_series(
    root,
    name,
    *,
    hid="H",
    site="mangadex",
    url="https://mangadex.org/title/H",
    chapters=0,
    write_meta=True,
    marker=None,
):
    """A series folder as a real download leaves it."""
    folder = os.path.join(str(root), name)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, ".series_hid"), "w", encoding="utf-8") as f:
        f.write(marker if marker is not None else str(hid))
    for n in range(1, chapters + 1):
        with open(os.path.join(folder, f"Ch.{n:03d}.cbz"), "wb") as f:
            f.write(b"payload")
    if write_meta:
        with open(os.path.join(folder, ".aio_series.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "url": url,
                    "hid": hid,
                    "site": site,
                    "title": name,
                    "chapters_downloaded": [str(i) for i in range(1, chapters + 1)],
                },
                f,
            )
    return folder


# ── the bug ─────────────────────────────────────────────────────────────────


def test_site_rename_reuses_the_existing_folder(tmp_path):
    """The exact MangaDex case: same url, same hid, drifted title."""
    original = make_series(
        tmp_path,
        "Isekai de Slow Life o (Ganbou)",
        hid="544240b7-557d-4f86-bcc0-28ad5fa99795",
        site="mangadex",
        url="https://mangadex.org/title/544240b7-557d-4f86-bcc0-28ad5fa99795",
        chapters=59,
    )
    got = aio.allocate_series_output_dir(
        "Isekai de Slow Life wo (Ganbou)",
        "544240b7-557d-4f86-bcc0-28ad5fa99795",
        root=str(tmp_path),
        site="mangadex",
        url="https://mangadex.org/title/544240b7-557d-4f86-bcc0-28ad5fa99795",
    )
    assert got == original
    # The folder is deliberately NOT renamed — folderPath is the key the UI,
    # resume params and readers all hold.
    assert os.path.basename(got) == "Isekai de Slow Life o (Ganbou)"
    assert len([d for d in os.listdir(tmp_path) if os.path.isdir(tmp_path / d)]) == 1


def test_punctuation_only_rename_reuses_the_folder(tmp_path):
    """The atsumaru case: "Konosuba:" -> "Konosuba!" (the colon is stripped by
    folder sanitization, the bang is not, so the derived name changes)."""
    original = make_series(
        tmp_path,
        "Konosuba God's Blessing on This Wonderful World! (hid=gvHMj)",
        hid="gvHMj",
        site="atsumaru",
        url="https://atsu.moe/manga/gvHMj",
        chapters=133,
    )
    got = aio.allocate_series_output_dir(
        "Konosuba! God's Blessing on This Wonderful World!",
        "gvHMj",
        root=str(tmp_path),
        site="atsumaru",
        url="https://atsu.moe/manga/gvHMj",
    )
    assert got == original


def test_hid_change_with_stable_url_reuses_the_folder(tmp_path):
    """A site that reissues ids but keeps the URL (the relaunch shape)."""
    original = make_series(
        tmp_path, "Some Series", hid="OLD", site="mangafire",
        url="https://mangafire.to/title/abc", chapters=10,
    )
    got = aio.allocate_series_output_dir(
        "Some Series", "NEW", root=str(tmp_path),
        site="mangafire", url="https://mangafire.to/title/abc",
    )
    assert got == original


def test_url_matching_ignores_www_and_trailing_slash(tmp_path):
    original = make_series(
        tmp_path, "A", hid="OLD", site="s", url="https://www.example.test/title/9/", chapters=1
    )
    got = aio.allocate_series_output_dir(
        "A Renamed", "NEW", root=str(tmp_path), site="s", url="https://example.test/title/9"
    )
    assert got == original


# ── the new guard ───────────────────────────────────────────────────────────


def test_same_hid_on_a_different_site_is_not_the_same_series(tmp_path):
    """Markers hold a BARE hid, so two sites minting short base-36 ids could
    collide on one. Pre-fix nothing checked the site."""
    other = make_series(
        tmp_path, "Konosuba", hid="gvHMj", site="atsumaru",
        url="https://atsu.moe/manga/gvHMj", chapters=133,
    )
    got = aio.allocate_series_output_dir(
        "Something Else", "gvHMj", root=str(tmp_path),
        site="comix", url="https://comix.to/title/gvHMj-x",
    )
    assert got != other
    assert os.path.basename(got) == "Something Else"


def test_no_site_argument_keeps_the_old_hid_only_behaviour(tmp_path):
    """A caller with no site to offer must not be made stricter than it was."""
    original = make_series(tmp_path, "Legacy", hid="LH", site="mangadex",
                           url="https://x/1", chapters=3)
    got = aio.allocate_series_output_dir("Legacy Renamed", "LH", root=str(tmp_path))
    assert got == original


def test_folder_without_metadata_still_matches_on_its_marker(tmp_path):
    """A run that crashed before its first chapter leaves only .series_hid."""
    original = make_series(tmp_path, "Half Written", hid="H9", write_meta=False, chapters=2)
    got = aio.allocate_series_output_dir(
        "Half Written Renamed", "H9", root=str(tmp_path), site="s", url="https://s/9"
    )
    assert got == original


# ── behaviour that must NOT regress ─────────────────────────────────────────


def test_different_series_with_the_same_title_still_gets_a_hid_sibling(tmp_path):
    first = make_series(tmp_path, "Twin Star", hid="AAA", site="mangadex",
                        url="https://mangadex.org/title/AAA", chapters=5)
    got = aio.allocate_series_output_dir(
        "Twin Star", "BBB", root=str(tmp_path),
        site="mangadex", url="https://mangadex.org/title/BBB",
    )
    assert got != first
    assert os.path.basename(got) == "Twin Star (hid=BBB)"


def test_empty_orphan_folder_is_reclaimed(tmp_path):
    """PR #48: a crash after allocation but before the first chapter leaves an
    empty folder with a stale marker; the next attempt must reuse it."""
    orphan = os.path.join(str(tmp_path), "Orphan")
    os.makedirs(orphan)
    with open(os.path.join(orphan, ".series_hid"), "w", encoding="utf-8") as f:
        f.write("STALE")
    got = aio.allocate_series_output_dir(
        "Orphan", "FRESH", root=str(tmp_path), site="s", url="https://s/o"
    )
    assert got == orphan


def test_asura_rotating_hash_suffix_still_matches(tmp_path):
    original = make_series(
        tmp_path, "SSS Class", hid="sss-class-suicide-hunter-46f09241",
        site="asura", url="https://asurascans.com/series/sss-class", chapters=20,
    )
    got = aio.allocate_series_output_dir(
        "SSS Class", "sss-class-suicide-hunter", root=str(tmp_path), site="asura"
    )
    assert got == original
    # Converged onto the canonical hid so next run is an exact match.
    with open(os.path.join(got, ".series_hid"), encoding="utf-8") as f:
        assert f.read().strip() == "sss-class-suicide-hunter"


def test_brand_new_series_creates_the_title_folder(tmp_path):
    got = aio.allocate_series_output_dir(
        "Fresh Title", "NEW", root=str(tmp_path), site="s", url="https://s/n"
    )
    assert os.path.basename(got) == "Fresh Title"
    assert os.path.isdir(got)


# ── already-forked libraries converge on the richest folder ─────────────────


def test_when_two_folders_match_the_richest_wins(tmp_path):
    big = make_series(tmp_path, "Rich", hid="H1", site="s", url="https://s/x", chapters=59)
    make_series(tmp_path, "Rich Renamed", hid="H1", site="s", url="https://s/x", chapters=5)
    got = aio.allocate_series_output_dir(
        "Rich Renamed", "H1", root=str(tmp_path), site="s", url="https://s/x"
    )
    assert got == big, "new chapters must land beside the bulk of the series"


def test_a_matching_folder_beats_the_title_folder_when_it_holds_more(tmp_path):
    """The husk carries the CURRENT title, so the title branch would have
    picked it. Identity + richness must override that."""
    big = make_series(tmp_path, "Old Name", hid="H1", site="s", url="https://s/x", chapters=133)
    husk = make_series(tmp_path, "New Name", hid="H1", site="s", url="https://s/x", chapters=1)
    got = aio.allocate_series_output_dir(
        "New Name", "H1", root=str(tmp_path), site="s", url="https://s/x"
    )
    assert got == big
    assert got != husk


def test_duplicate_folders_are_reported(tmp_path, capsys):
    make_series(tmp_path, "Rich", hid="H1", site="s", url="https://s/x", chapters=59)
    make_series(tmp_path, "Rich Renamed", hid="H1", site="s", url="https://s/x", chapters=5)
    aio.allocate_series_output_dir(
        "Rich Renamed", "H1", root=str(tmp_path), site="s", url="https://s/x"
    )
    err = capsys.readouterr().err
    assert "2 folders hold this same series" in err
    assert "Rich Renamed" in err


# ── --series-dir ────────────────────────────────────────────────────────────


def test_series_dir_is_honoured_when_it_holds_this_series(tmp_path):
    big = make_series(tmp_path, "Old Name", hid="H1", site="s", url="https://s/x", chapters=59)
    make_series(tmp_path, "New Name", hid="H1", site="s", url="https://s/x", chapters=5)
    got = aio.allocate_series_output_dir(
        "New Name", "H1", root=str(tmp_path), site="s", url="https://s/x", series_dir=big
    )
    assert got == big


def test_series_dir_can_point_at_the_smaller_fork(tmp_path):
    """The UI is authoritative about WHICH folder a queued delta targets, so
    the flag has to be able to override the richness rule."""
    make_series(tmp_path, "Old Name", hid="H1", site="s", url="https://s/x", chapters=59)
    husk = make_series(tmp_path, "New Name", hid="H1", site="s", url="https://s/x", chapters=5)
    got = aio.allocate_series_output_dir(
        "New Name", "H1", root=str(tmp_path), site="s", url="https://s/x", series_dir=husk
    )
    assert got == husk


def test_stale_series_dir_falls_back_instead_of_creating_it(tmp_path, capsys):
    original = make_series(tmp_path, "A", hid="H", site="s", url="https://s/1", chapters=3)
    missing = os.path.join(str(tmp_path), "Merged Away")
    got = aio.allocate_series_output_dir(
        "A", "H", root=str(tmp_path), site="s", url="https://s/1", series_dir=missing
    )
    assert got == original
    assert not os.path.exists(missing), "a stale path must not be created"
    assert "Ignoring --series-dir" in capsys.readouterr().err


def test_series_dir_outside_the_library_is_refused(tmp_path, capsys):
    root = tmp_path / "lib"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    original = make_series(root, "A", hid="H", site="s", url="https://s/1", chapters=3)
    got = aio.allocate_series_output_dir(
        "A", "H", root=str(root), site="s", url="https://s/1", series_dir=str(elsewhere)
    )
    assert got == original
    assert "not inside the library root" in capsys.readouterr().err


def test_series_dir_holding_a_different_series_is_refused(tmp_path, capsys):
    original = make_series(tmp_path, "A", hid="H", site="s", url="https://s/1", chapters=3)
    other = make_series(tmp_path, "B", hid="OTHER", site="s", url="https://s/2", chapters=9)
    got = aio.allocate_series_output_dir(
        "A", "H", root=str(tmp_path), site="s", url="https://s/1", series_dir=other
    )
    assert got == original
    assert "different series" in capsys.readouterr().err


def test_series_dir_accepts_an_unclaimed_empty_folder(tmp_path):
    """Makes the flag usable by hand: "put this series here"."""
    empty = tmp_path / "Chosen By Hand"
    empty.mkdir()
    got = aio.allocate_series_output_dir(
        "A", "H", root=str(tmp_path), site="s", url="https://s/1", series_dir=str(empty)
    )
    assert got == str(empty)
    with open(os.path.join(got, ".series_hid"), encoding="utf-8") as f:
        assert f.read().strip() == "H"


# ── the end-of-run duplicate report ─────────────────────────────────────────


def test_duplicate_report_finds_a_same_series_fork(tmp_path, capsys):
    mine = make_series(tmp_path, "A", hid="H", site="s", url="https://s/1", chapters=59)
    make_series(tmp_path, "A Renamed", hid="H", site="s", url="https://s/1", chapters=5)
    aio._warn_duplicate_series_folders(
        str(tmp_path), mine, hid="H", site="s", url="https://s/1", anilist_id=None
    )
    assert "Duplicate library folders" in capsys.readouterr().err


def test_duplicate_report_finds_a_cross_source_anilist_match(tmp_path, capsys):
    mine = make_series(tmp_path, "No More Love", hid="a.1", site="mangakatana",
                       url="https://mangakatana.com/manga/a.1", chapters=160)
    folder = make_series(tmp_path, "Rom-Com", hid="kVGsS", site="atsumaru",
                         url="https://atsu.moe/manga/kVGsS", chapters=154)
    with open(os.path.join(folder, ".aio_series.json"), encoding="utf-8") as f:
        meta = json.load(f)
    meta["anilist_id"] = 146858
    with open(os.path.join(folder, ".aio_series.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)

    aio._warn_duplicate_series_folders(
        str(tmp_path), mine, hid="a.1", site="mangakatana",
        url="https://mangakatana.com/manga/a.1", anilist_id=146858,
    )
    err = capsys.readouterr().err
    assert "Possible duplicate" in err
    assert "Rom-Com" in err


def test_duplicate_report_is_silent_for_a_lone_series(tmp_path, capsys):
    mine = make_series(tmp_path, "Only One", hid="Q", site="s", url="https://s/q", chapters=4)
    make_series(tmp_path, "Unrelated", hid="Z", site="s", url="https://s/z", chapters=4)
    aio._warn_duplicate_series_folders(
        str(tmp_path), mine, hid="Q", site="s", url="https://s/q", anilist_id=7
    )
    assert capsys.readouterr().err == ""


# ── the grouping twins agree ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "meta,expected",
    [
        ({"site": "mangadex", "hid": "H", "url": "https://x/1"}, "hid:mangadex:H"),
        ({"url": "https://WWW.Example.test/title/5/"}, "url:https://example.test/title/5"),
        ({"site": "mangadex"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_series_identity_key(meta, expected):
    from library_state import series_identity_key

    assert series_identity_key(meta) == expected


def test_group_entries_by_series_picks_the_richest_primary(tmp_path):
    from library_state import group_entries_by_series, scan_library

    make_series(tmp_path, "A", hid="H", site="s", url="https://s/1", chapters=59)
    make_series(tmp_path, "A Renamed", hid="H", site="s", url="https://s/1", chapters=5)
    make_series(tmp_path, "B", hid="Z", site="s", url="https://s/2", chapters=3)

    groups = group_entries_by_series(scan_library(str(tmp_path)))
    assert len(groups) == 2
    forked = [g for g in groups if len(g["members"]) > 1]
    assert len(forked) == 1
    assert forked[0]["primary"]["name"] == "A"


def test_entries_without_identity_never_share_a_group(tmp_path):
    from library_state import group_entries_by_series

    entries = [{"name": "a", "folder": "a", "series_meta": {}, "files": 1},
               {"name": "b", "folder": "b", "series_meta": {}, "files": 1}]
    groups = group_entries_by_series(entries)
    assert len(groups) == 2
