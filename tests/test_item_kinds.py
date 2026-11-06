"""item_kinds: chapter and volume names must never be readable as each other.

Every chapter parser in the project (library_state.CHAPTER_FILE_RE and
RAW_IMAGE_DIR_RE, plus the Komikku and legacy forms the Electron library
reads) must reject every name the volume kind produces. The volume parser must
reject every chapter name, including Komikku's "Vol.01 Ch.005", where Vol is
only the chapter's volume membership. If either direction leaks, a volume
download is counted as a chapter (or the reverse) by the update check.
"""
from __future__ import annotations

import re

import pytest

import item_kinds as ik
import library_state as ls

VOLUME_LABELS = ["1", "2", "9", "10", "99", "117", "0", "2.5"]
CHAPTER_LABELS = ["1", "5", "5.5", "10", "117", "1000"]

# JS twin of library.js KOMIKKU_CH_RE, for the cross-parser assertions.
KOMIKKU_CH_RE = re.compile(r"^(?:Vol\.\S+\s+)?Ch\.(\d+(?:\.\d+)?[a-z]?)", re.IGNORECASE)


def _strip_ext(name):
    return re.sub(r"\.[^.]+$", "", name)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("tmp_abc", ("abc", ik.CHAPTER)),
        ("tmpvol_abc", ("abc", ik.VOLUME)),
        # A chapter hid that starts with "vol_" stays a chapter dir.
        ("tmp_vol_x", ("vol_x", ik.CHAPTER)),
        ("tmpvol_vol_x", ("vol_x", ik.VOLUME)),
        ("tmp_", None),
        ("tmpvol_", None),
        ("other", None),
    ],
)
def test_tmp_dir_name_round_trip(name, expected):
    assert ik.parse_tmp_dir_name(name) == expected
    if expected:
        hid, kind = expected
        assert ik.space_of(kind).tmp_dir_name(hid) == name


def test_volume_tmp_prefix_is_invisible_to_old_chapter_scanners():
    # downloader.js / aio_android.py scan with startsWith("tmp_"). A volume dir
    # must be skipped by them, not parsed as chapter hid "vol_<hid>".
    assert not ik.VOLUMES.tmp_dir_name("abc").startswith("tmp_")


@pytest.mark.parametrize(
    "label,expected",
    [("1", "Title Vol.01.cbz"), ("10", "Title Vol.10.cbz"), ("117", "Title Vol.117.cbz"),
     ("0", "Title Vol.00.cbz"), ("2.5", "Title Vol.02~5.cbz"), ("007", "Title Vol.07.cbz")],
)
def test_volume_filenames(label, expected):
    assert ik.VOLUMES.item_filename("Title", label, "cbz") == expected


@pytest.mark.parametrize(
    "label,expected",
    [("1", "Title Ch 1.pdf"), ("5.5", "Title Ch 5~5.pdf"), ("117", "Title Ch 117.pdf")],
)
def test_chapter_filenames_match_the_engine_form(label, expected):
    # The engine's own ch_filename: join_name(base, f"Ch {format_chap_for_filename(n)}").
    assert ik.CHAPTERS.item_filename("Title", label, "pdf") == expected


@pytest.mark.parametrize("label", VOLUME_LABELS)
@pytest.mark.parametrize("fmt", ["cbz", "pdf", "epub"])
def test_no_chapter_parser_reads_a_volume_name(label, fmt):
    name = ik.VOLUMES.item_filename("Some Series", label, fmt)
    stem = _strip_ext(name)
    assert ls.CHAPTER_FILE_RE.search(stem) is None, name
    assert KOMIKKU_CH_RE.match(stem) is None, name
    assert " Ch " not in stem, name
    assert ls.RAW_IMAGE_DIR_RE.match(ik.VOLUMES.raw_image_dir_name(label)) is None


@pytest.mark.parametrize("label", VOLUME_LABELS)
def test_volume_parser_round_trips_its_own_names(label):
    name = ik.VOLUMES.item_filename("Some Series", label, "cbz")
    assert ik.parse_volume_number_from_name(name) == f"{float(label):g}"
    assert ik.parse_volume_number_from_name(name, base="Some Series") == f"{float(label):g}"


@pytest.mark.parametrize(
    "name",
    [
        "Vol.01 Ch.005.cbz",          # Komikku chapter carrying volume membership
        "Vol.01 Ch.005.5 - Title.cbz",
        "Ch.005.cbz",
        "Title.cbz",                  # combined archive
        "Title Ch 1-50.pdf",          # combined range
        "Title Ch 5~5.pdf",
        "Volume 3.cbz",               # not our form
        "Title Vol.3 Extra.cbz",      # Vol not at the end of the stem
    ],
)
def test_volume_parser_rejects_chapter_and_archive_names(name):
    assert ik.parse_volume_number_from_name(name) is None


@pytest.mark.parametrize("label", CHAPTER_LABELS)
def test_volume_parser_rejects_every_chapter_name(label):
    assert ik.parse_volume_number_from_name(ik.CHAPTERS.item_filename("Title", label, "cbz")) is None


def test_base_guard_rejects_a_combined_archive_whose_title_ends_in_vol():
    # Series "Saga Vol.3": its combined archive "Saga Vol.3.cbz" looks like a
    # volume file. With the base name known, only "<base> Vol.N" qualifies.
    assert ik.parse_volume_number_from_name("Saga Vol.3.cbz", base="Saga Vol.3") is None
    assert ik.parse_volume_number_from_name("Saga Vol.3 Vol.01.cbz", base="Saga Vol.3") == "1"


def test_volume_raw_image_dirs():
    assert ik.RAW_VOLUME_DIR_RE.match(ik.VOLUMES.raw_image_dir_name("3"))
    assert not ik.RAW_VOLUME_DIR_RE.match(ik.CHAPTERS.raw_image_dir_name("3"))


def test_feature_flags_keep_volumes_out_of_chapter_only_stages():
    v = ik.VOLUMES
    assert not (v.combined_archive or v.multi_source or v.collapse_splits or v.fingerprints)
    c = ik.CHAPTERS
    assert c.combined_archive and c.multi_source and c.collapse_splits and c.fingerprints


def test_space_selection():
    assert ik.space_for(True) is ik.VOLUMES
    assert ik.space_for(False) is ik.CHAPTERS
    with pytest.raises(ValueError):
        ik.space_of("issue")
