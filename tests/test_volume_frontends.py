"""Native volumes in the non-Electron front ends: api.py and the shared
update-command builder that aio-dl.py --update-all and gui.py both use.

api.py: volume and chapter ids are separate id spaces (MangaFire), so
/api/chapter_images must look an id up in the namespace the caller names and
fetch through get_item_images. Otherwise a volume id would find a chapter with
the same id and return its pages.

library_state.volume_update_argv: the single volume-update builder.
gui.py's "Update Selected" calls it directly (that path needs a Tk root and is
not unit-tested here); aio-dl.py's children are covered in
test_volume_pipeline.py.
"""
from __future__ import annotations

import pytest

from sites.base import BaseSiteHandler, SiteComicContext
from library_state import volume_update_argv

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402


class _FakeSite(BaseSiteHandler):
    """Chapter 7 and volume 7 share the id "7", like MangaFire's two id spaces."""

    name = "apifake"

    def configure_session(self, scraper, args):
        pass

    def fetch_comic_context(self, url, scraper, make_request):
        return SiteComicContext(comic={"title": "T"}, title="T", identifier="x")

    def get_chapters(self, context, scraper, language, make_request):
        return [{"hid": "7", "chap": "7"}]

    def get_chapter_images(self, chapter, scraper, make_request):
        return [f"chapter/{chapter['hid']}"]

    def get_volumes(self, context, scraper, language, make_request):
        return [{"hid": "7", "chap": "1"}] if language == "en" else []

    def get_volume_images(self, volume, scraper, make_request):
        return [f"volume/{volume['hid']}"]


class _ChapterOnlySite(_FakeSite):
    name = "apichaponly"
    get_volumes = BaseSiteHandler.get_volumes


@pytest.fixture
def client(monkeypatch):
    holder = {"handler": _FakeSite()}
    monkeypatch.setattr(api, "_handler_for", lambda url, site=None: holder["handler"])
    monkeypatch.setattr(api, "get_scraper", lambda: None)
    return TestClient(api.app), holder


def test_volume_listing_is_normalized_and_tagged(client):
    c, _ = client
    body = c.get("/api/chapters", params={"url": "u", "type": "volume"}).json()
    assert [(v["chap"], v["_kind"]) for v in body] == [("1", "volume")]


def test_supported_site_without_volumes_is_an_empty_list(client):
    c, _ = client
    r = c.get("/api/chapters", params={"url": "u", "type": "volume", "language": "fr"})
    assert r.status_code == 200 and r.json() == []


def test_site_without_volume_support_is_501(client):
    c, holder = client
    holder["handler"] = _ChapterOnlySite()
    assert c.get("/api/chapters", params={"url": "u", "type": "volume"}).status_code == 501


def test_unknown_type_is_400(client):
    c, _ = client
    assert c.get("/api/chapters", params={"url": "u", "type": "season"}).status_code == 400


def test_images_are_looked_up_in_the_named_namespace(client):
    c, _ = client
    vol = c.get("/api/chapter_images", params={"url": "u", "chapter_id": "7", "type": "volume"})
    ch = c.get("/api/chapter_images", params={"url": "u", "chapter_id": "7"})
    assert vol.json() == {"images": ["volume/7"]}
    assert ch.json() == {"images": ["chapter/7"]}


def test_handlers_report_volume_capability():
    rows = {r["name"]: r for r in api.list_handlers()}
    assert rows["mangafire"]["volumes_capable"] is True
    assert rows["mangadex"]["volumes_capable"] is False


def _entry(**over):
    entry = {
        "params": {"site": "mangafire", "format": "pdf", "group": ["G"], "split": 50},
        "next_volume_update": "3-",
        "volume_format": "cbz",
        "volume_language": "fr",
        "language": "en",
    }
    entry.update(over)
    return entry


def test_volume_update_argv_uses_the_volume_block_not_chapter_settings():
    argv = volume_update_argv(_entry(), "ROOT")
    assert argv == [
        "--download-volumes", "--chapters", "3-", "--format", "cbz",
        "--language", "fr", "--output-dir", "ROOT", "--site", "mangafire",
    ]


def test_volume_update_argv_defaults():
    argv = volume_update_argv(
        _entry(params={}, next_volume_update=None, volume_format=None, volume_language=None), "R"
    )
    assert argv == ["--download-volumes", "--chapters", "all", "--format", "cbz",
                    "--language", "en", "--output-dir", "R"]


def test_volume_update_argv_refuses_under_komikku():
    assert volume_update_argv(_entry(params={"komikku": True}), "R") is None
