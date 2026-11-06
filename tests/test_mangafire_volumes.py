"""MangaFire native volumes: listing and page fetch, with the API mocked.

Pins the parser against the shapes seen live on 2026-11-06 (header of
sites/mangafire.py): /titles/{hid}/volumes returns every language and ignores
`language`, so the filter is ours; `hasVolumes: false` must cost no request;
volume pages come from /volumes/{id}, never /chapters/{id} (separate id
spaces). The server itself is only checked by the live run, not here.
"""
from __future__ import annotations

import pytest

from sites.base import SiteComicContext
from sites.mangafire import MangaFireSiteHandler

VOLUMES_PAYLOAD = {
    "items": [
        {"id": 901, "number": 2, "name": "Buggy the Clown", "language": "en", "chapterCount": 9},
        {"id": 900, "number": 1, "name": "Romance Dawn", "language": "en", "chapterCount": 8},
        {"id": 950, "number": 1, "name": "Romance Dawn (FR)", "language": "fr", "chapterCount": 8},
        {"id": 902, "number": 3.5, "name": "", "language": "EN", "chapterCount": 1},
        {"id": 903, "number": None, "name": "broken", "language": "en"},
    ]
}


@pytest.fixture
def mf(monkeypatch):
    h = MangaFireSiteHandler()
    calls = []

    def fake_api_get(path, pairs, scraper, make_request, *, label):
        calls.append(path)
        if path.endswith("/volumes"):
            return VOLUMES_PAYLOAD
        if path.startswith("/volumes/"):
            return {"data": {"pages": [{"url": "https://cdn/v/1.jpg"}, {"url": ""}, {"nourl": 1},
                                       {"url": "https://cdn/v/2.jpg"}]}}
        raise AssertionError(f"unexpected API path {path}")

    monkeypatch.setattr(h, "_api_get", fake_api_get)
    h.calls = calls
    return h


def _ctx(has_volumes=True):
    return SiteComicContext(
        comic={"slug": "one-piece", "_has_volumes": has_volumes}, title="One Piece", identifier="dkw"
    )


def test_volumes_are_filtered_to_the_language_and_sorted(mf):
    vols = mf.get_volumes(_ctx(), None, "en", None)
    assert [v["chap"] for v in vols] == ["1", "2", "3.5"]
    assert [v["hid"] for v in vols] == ["900", "901", "902"]
    assert vols[0]["title"] == "Romance Dawn"
    assert vols[2]["title"] == "Volume 3.5"
    assert vols[0]["url"] == "https://mangafire.to/title/dkw-one-piece/volume/900"
    assert mf.calls == ["/titles/dkw/volumes"]


def test_other_language(mf):
    assert [v["hid"] for v in mf.get_volumes(_ctx(), None, "fr", None)] == ["950"]


def test_has_volumes_false_costs_no_request(mf):
    assert mf.get_volumes(_ctx(has_volumes=False), None, "en", None) == []
    assert mf.calls == []
    assert mf.has_volumes(_ctx(has_volumes=False)) is False


def test_unknown_has_volumes_still_asks(mf):
    assert mf.get_volumes(_ctx(has_volumes=None), None, "en", None)
    assert mf.calls == ["/titles/dkw/volumes"]


def test_volume_pages_come_from_the_volume_endpoint(mf):
    vol = mf.list_volumes(_ctx(), None, "en", None)[0]
    assert mf.get_item_images(vol, None, None) == ["https://cdn/v/1.jpg", "https://cdn/v/2.jpg"]
    assert mf.calls[-1] == "/volumes/900"


def test_mangafire_reports_volume_support():
    assert MangaFireSiteHandler.supports_volumes()


def test_volume_reader_url_resolves_to_the_series_hid():
    h = MangaFireSiteHandler()
    assert h._extract_hid("https://mangafire.to/title/dkw-one-piece/volume/900") == "dkw"
