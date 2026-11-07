"""BaseSiteHandler volume API: listing normalization and image routing.

The guarantee here is that a volume can never reach ``get_chapter_images``.
Volume and chapter ids are separate id spaces, so on a site where they overlap
that call would return a different chapter's pages and nothing would notice.
Every engine call goes through ``get_item_images``. The source guard below keeps
it that way.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import item_kinds
import sites
from sites.base import BaseSiteHandler, SiteComicContext

REPO = Path(__file__).resolve().parents[1]


class _VolumeSite(BaseSiteHandler):
    name = "volfake"

    def __init__(self, volumes):
        self._volumes = volumes
        self.calls = []

    def get_volumes(self, context, scraper, language, make_request):
        return self._volumes

    def get_volume_images(self, volume, scraper, make_request):
        self.calls.append(("volume", volume["hid"]))
        return [f"v/{volume['hid']}/1"]

    def get_chapter_images(self, chapter, scraper, make_request):
        self.calls.append(("chapter", chapter.get("hid")))
        return [f"c/{chapter.get('hid')}/1"]


class _ChapterOnlySite(BaseSiteHandler):
    name = "chapfake"


def _ctx():
    return SiteComicContext(comic={}, title="T", identifier="x")


def test_list_volumes_tags_normalizes_dedupes_and_sorts(capsys):
    site = _VolumeSite([
        {"hid": "30", "chap": 3, "title": "Three"},
        {"hid": "10", "chap": "1.0", "title": "One"},
        {"hid": "11", "chap": "1", "title": "One (newer)"},
        {"hid": "99", "chap": "TBD", "title": "Unnumbered"},
        {"hid": "98", "chap": None, "title": "None"},
    ])
    out = site.list_volumes(_ctx(), None, "en", None)
    assert [v["chap"] for v in out] == ["1", "3"]
    assert [v["hid"] for v in out] == ["11", "30"]  # last duplicate wins
    assert all(v["_kind"] == item_kinds.VOLUME for v in out)
    assert all(v["vol"] == v["chap"] for v in out)
    assert "Unnumbered" in capsys.readouterr().err


def test_kind_tag_literal_matches_item_kinds():
    # sites/ does not import the root item_kinds module, so the literal is
    # duplicated. This is the tie between the two copies.
    assert BaseSiteHandler._ITEM_KIND_VOLUME == item_kinds.VOLUME


def test_get_item_images_routes_by_kind():
    site = _VolumeSite([{"hid": "7", "chap": "1"}])
    vol = site.list_volumes(_ctx(), None, "en", None)[0]
    site.get_item_images(vol, None, None)
    site.get_item_images({"hid": "7", "chap": "1"}, None, None)
    assert site.calls == [("volume", "7"), ("chapter", "7")]


def test_volume_on_a_site_without_volume_pages_raises_clearly():
    site = _ChapterOnlySite()
    with pytest.raises(NotImplementedError):
        site.get_item_images({"hid": "1", "_kind": "volume"}, None, None)


def test_supports_volumes_is_derived_from_the_override():
    assert _VolumeSite.supports_volumes()
    assert not _ChapterOnlySite.supports_volumes()
    assert not BaseSiteHandler.supports_volumes()


def test_handler_capabilities_rows():
    rows = sites.handler_capabilities()
    assert len(rows) == len(sites._REGISTERED_HANDLERS)
    for row in rows:
        assert set(row) >= {"name", "domains", "search", "volumes"}
        assert isinstance(row["volumes"], bool)


def test_engine_never_calls_get_chapter_images_directly():
    """Source guard: every page fetch in the engine goes through
    get_item_images. A direct call would let a volume reach the chapter
    endpoint."""
    src = (REPO / "aio-dl.py").read_text(encoding="utf-8")
    code_lines = [l for l in src.splitlines() if not l.lstrip().startswith("#")]
    hits = [l.strip() for l in code_lines if re.search(r"\.get_chapter_images\(", l)]
    assert hits == []
