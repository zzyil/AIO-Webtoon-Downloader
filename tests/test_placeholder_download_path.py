"""Downloader-side handling of placeholder chapters (aio-dl.py).

Placeholders are chapter rows a site lists but can never serve: tapas premium
episodes and MangaDex publisher link-outs (`_locked`, `_locked_kind="external"`
for the latter). These tests cover what aio-dl.py does with them:

  - the permanent-skip reason and the end-of-run replay split;
  - the --list-chapters split (a number with a readable version is not locked);
  - the no-multi-source drop after collapse-splits grouping;
  - the placeholder-only primary: switch to a readable alternative under
    --multi-source, otherwise exit before any series folder is allocated.

The main() tests drive the real main() offline with a fake handler and a
patched alternatives builder; allocate_series_output_dir is replaced with a
sentinel so a run that gets that far stops there and reports which site/URL
it would have created a folder for.

Plan: sync-temp/plans/mangadex-link-outs.md (L2). Ranker-side coverage is in
tests/test_group_selection.py; the MangaDex handler flags are in
tests/test_mangadex_handler.py.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from sites.base import BaseSiteHandler, SiteComicContext
from tests.test_cli_flag_parsing import _load_aio_dl_module


@pytest.fixture(scope="module")
def aio():
    return _load_aio_dl_module()


def _hosted(chap, group="fan"):
    return {"chap": chap, "url": f"https://x/{group}/{chap}", "group_name": group}


def _link_out(chap, host="mangaplus.shueisha.co.jp"):
    return {
        "chap": chap, "url": f"https://x/pub/{chap}", "group_name": "Publisher",
        "_locked": True, "_locked_kind": "external", "_external_host": host,
        "_external_url": f"https://{host}/viewer/{chap}",
    }


def _tapas_locked(chap):
    return {"chap": chap, "url": f"https://tapas/{chap}", "_locked": True}


# --------------------------------------------------------------------------
# Permanent skip + replay
# --------------------------------------------------------------------------

def test_external_link_is_a_permanent_skip(aio):
    assert "external_link" in aio._PERMANENT_SKIP_REASONS
    assert "locked" in aio._PERMANENT_SKIP_REASONS


def test_missed_replay_skips_permanent_reasons(aio):
    entries = [
        {"chap": "1", "reason": "external_link"},
        {"chap": "2", "reason": "empty_content"},
        {"chap": "3", "reason": "locked"},
        {"chap": "4", "reason": "exception"},
    ]
    replay, keep = aio._replayable_missed(entries)
    assert [e["chap"] for e in replay] == ["2", "4"]
    assert [e["chap"] for e in keep] == ["1", "3"]


# --------------------------------------------------------------------------
# --list-chapters split
# --------------------------------------------------------------------------

def test_list_split_mixed_number_is_listable_not_locked(aio):
    pool = [
        _hosted("5"), _link_out("5"),      # mixed: fan version + link-out
        _link_out("6"),                     # placeholder only
        _hosted("7"),
        _tapas_locked(8.0),                 # float label, :g → "8"
        _hosted("Oneshot"),                 # → "1"
        _hosted("Special"),                 # non-numeric → dropped
    ]
    by_label, locked = aio._split_listable_and_locked(pool)
    assert sorted(by_label, key=float) == ["1", "5", "7"]
    # Only the readable version of 5 is bucketed.
    assert [v.get("_locked") for v in by_label["5"]] == [None]
    assert locked == ["6", "8"]


# --------------------------------------------------------------------------
# No-multi-source drop (after grouping)
# --------------------------------------------------------------------------

def test_drop_counts_by_kind_and_keeps_readable(aio):
    chapters = [_hosted("1"), _link_out("2"), _tapas_locked("3"), _hosted("4")]
    kept, counts = aio._drop_placeholder_chapters(chapters)
    assert [c["chap"] for c in kept] == ["1", "4"]
    assert counts == {"publisher-hosted": 1, "premium/locked": 1}


def test_drop_removes_collapsed_cluster_with_a_placeholder_part(aio):
    # Collapse merged 2.1 (readable) + 2.2 (link-out) into "2". Saving 2.1
    # alone would label half a chapter as complete, so the cluster goes.
    readable_part = _hosted("2.1")
    cluster = {**readable_part, "chap": "2", "_merged_parts": [readable_part, _link_out("2.2")]}
    clean = {**_hosted("3.1"), "chap": "3", "_merged_parts": [_hosted("3.1"), _hosted("3.2")]}
    kept, counts = aio._drop_placeholder_chapters([cluster, clean])
    assert [c["chap"] for c in kept] == ["3"]
    assert counts == {"publisher-hosted": 1}


# --------------------------------------------------------------------------
# Switch helpers
# --------------------------------------------------------------------------

def test_all_placeholders_and_description(aio):
    assert not aio._primary_all_placeholders([])
    assert not aio._primary_all_placeholders([_link_out("1"), _hosted("2")])
    pool = [_link_out("1", "global.manga-up.com"), _link_out("2", "global.manga-up.com")]
    assert aio._primary_all_placeholders(pool)
    assert aio._describe_placeholder_pool(pool) == "publisher links (global.manga-up.com)"
    mixed = pool + [_tapas_locked("3")]
    assert aio._describe_placeholder_pool(mixed) == (
        "publisher links (global.manga-up.com) and premium/locked episodes"
    )


def test_pick_switch_takes_best_ranked_readable_source(aio):
    discovery = {
        "resolved_sources": [
            {"site": "deadsite", "url": "https://dead/s"},     # fetched nothing
            {"site": "mangadex", "url": "https://md/other"},   # old primary's site
            {"site": "mangafire", "url": "https://mf/s"},
            {"site": "mangakatana", "url": "https://mk/s"},
        ],
        "readable_sites": ["mangakatana", "mangafire"],
    }
    target, remaining = aio._pick_primary_switch(discovery, "mangadex")
    assert target["site"] == "mangafire"
    assert [s["site"] for s in remaining] == ["deadsite", "mangakatana"]


def test_pick_switch_none_without_readable_source(aio):
    assert aio._pick_primary_switch(None, "mangadex") == (None, [])
    discovery = {"resolved_sources": [{"site": "a", "url": "u"}], "readable_sites": []}
    assert aio._pick_primary_switch(discovery, "mangadex") == (None, [])


def test_switch_argv_replaces_url_and_one_shot_flags(aio):
    argv = [
        "--multi-source", "--multi-source-prefetched", "C:/old.json",
        "--chapters", "1-2", "https://mangadex.org/title/abc", "--format=cbz",
        "--multi_source_prefetched=C:/older.json",
    ]
    out = aio._argv_for_primary_switch(
        argv, "https://mangadex.org/title/abc", "https://mangafire.to/title/xyz",
        "C:/new.json", "mangadex",
    )
    assert out == [
        "--multi-source", "--chapters", "1-2", "--format=cbz",
        "--multi-source-prefetched", "C:/new.json",
        "--primary-switched-from", "mangadex",
        "https://mangafire.to/title/xyz",
    ]


def test_switch_argv_from_search_mode_drops_search(aio):
    argv = ["--search", "Goblin Slayer", "--auto-pick", "--multi-source"]
    out = aio._argv_for_primary_switch(argv, "https://md/t", "https://mf/t", "p.json", "mangadex")
    assert out == [
        "--multi-source", "--multi-source-prefetched", "p.json",
        "--primary-switched-from", "mangadex", "https://mf/t",
    ]


def test_primary_switched_from_is_never_persisted(aio):
    assert "primary_switched_from" in aio._RESUME_TRANSIENT_DESTS


# --------------------------------------------------------------------------
# aio_search_cli: alternatives never offer a placeholder
# --------------------------------------------------------------------------

def test_alt_lists_drop_link_outs_and_report_readable_sites(monkeypatch):
    import argparse
    import aio_search_cli

    handlers = {
        "mangadex": _FakeHandler("mangadex", [_link_out("1"), _hosted("2")]),
        "allout": _FakeHandler("allout", [_link_out("1"), _link_out("2")]),
    }
    monkeypatch.setattr(aio_search_cli, "get_handler_by_name", handlers.get)
    sources = [
        type("Src", (), {"site": s, "url": f"https://{s}/x"})() for s in handlers
    ]
    candidate = type("Cand", (), {"sources": sources})()
    recs = aio_search_cli._fetch_chapters_for_winner(
        candidate, argparse.Namespace(cookies="", language="en"),
        make_request=lambda *a, **k: None,
    )
    by_site = {r["site"]: [c["chap"] for c in r["chapters"]] for r in recs}
    # A MangaDex alternative never offers the EXTERNAL CHAPTER card.
    assert by_site == {"mangadex": ["2"], "allout": []}
    assert aio_search_cli._readable_sites(recs) == ["mangadex"]


# --------------------------------------------------------------------------
# main(): placeholder-only primary
# --------------------------------------------------------------------------

class _FakeHandler(BaseSiteHandler):
    def __init__(self, name, chapters):
        self.name = name
        self._chapters = chapters

    def configure_session(self, scraper, args):
        pass

    def fetch_comic_context(self, url, scraper, make_request):
        return SiteComicContext(
            comic={"title": "Goblin Slayer", "year": 2016}, title="Goblin Slayer",
            identifier=f"{self.name}-hid",
        )

    def get_chapters(self, context, scraper, language, make_request):
        return [dict(c) for c in self._chapters]

    def extract_additional_metadata(self, context):
        return {}


class _ReachedFolder(Exception):
    pass


@pytest.fixture
def main_env(aio, monkeypatch, tmp_path):
    """Run main() offline. Returns a dict the test fills with handlers by URL;
    records each allocate_series_output_dir call instead of making a folder."""
    env = {"handlers": {}, "allocated": [], "argv_seen": [], "alts_calls": []}

    def fake_resolve(url, site):
        env["argv_seen"].append(list(sys.argv[1:]))
        return env["handlers"][url]

    def fake_allocate(title, hid, root=None, site=None, url=None, series_dir=None):
        env["allocated"].append((site, url))
        raise _ReachedFolder(site)

    monkeypatch.setattr(aio, "resolve_site_handler", fake_resolve)
    monkeypatch.setattr(aio, "allocate_series_output_dir", fake_allocate)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AIO_COORD_ENABLED", "0")
    return env


def _run_main(aio, monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["aio-dl.py", *argv])
    return aio.main()


MD_URL = "https://mangadex.org/title/gs"
MF_URL = "https://mangafire.to/title/gs"


def test_all_link_out_without_multi_source_exits_before_folder(aio, main_env, monkeypatch):
    main_env["handlers"][MD_URL] = _FakeHandler(
        "mangadex", [_link_out("1", "global.manga-up.com"), _link_out("2.1", "global.manga-up.com")]
    )
    with pytest.raises(SystemExit) as exc:
        _run_main(aio, monkeypatch, [MD_URL])
    msg = str(exc.value.code)
    assert msg.startswith("No chapters selected")
    assert "publisher links (global.manga-up.com)" in msg
    assert "enable --multi-source" in msg
    assert main_env["allocated"] == []


def test_all_link_out_with_multi_source_switches_primary(aio, main_env, monkeypatch, tmp_path):
    import aio_search_cli

    main_env["handlers"][MD_URL] = _FakeHandler("mangadex", [_link_out("1"), _link_out("2")])
    main_env["handlers"][MF_URL] = _FakeHandler("mangafire", [_hosted("1"), _hosted("2")])
    prefetched = tmp_path / "ui-alts.json"
    prefetched.write_text(json.dumps({
        "primary": {"site": "mangadex", "url": MD_URL},
        "alternatives": [{"site": "mangafire", "url": MF_URL},
                         {"site": "mangakatana", "url": "https://mk/gs"}],
        "title": "Goblin Slayer",
    }), encoding="utf-8")
    seen_payloads = []

    def fake_prefetched(prefetched_path, primary_handler, **kw):
        with open(prefetched_path, encoding="utf-8") as f:
            payload = json.load(f)
        seen_payloads.append((primary_handler.name, payload))
        return {
            "alternatives_by_chap_num": {},
            "consensus_set": None,
            "consensus_max": None,
            "resolved_sources": payload["alternatives"],
            "readable_sites": ["mangafire", "mangakatana"],
        }

    monkeypatch.setattr(aio_search_cli, "build_alternatives_from_prefetched", fake_prefetched)
    with pytest.raises(_ReachedFolder):
        _run_main(aio, monkeypatch, [
            "--multi-source", "--multi-source-lazy",
            "--multi-source-prefetched", str(prefetched), MD_URL,
        ])
    # The folder is allocated for the readable site, never for mangadex.
    assert main_env["allocated"] == [("mangafire", MF_URL)]
    # Re-entry argv: guard flag + the new primary's URL; the old URL is gone.
    reentry = main_env["argv_seen"][-1]
    assert reentry[-3:] == ["--primary-switched-from", "mangadex", MF_URL]
    assert MD_URL not in reentry
    # The re-entry's alternatives are the remaining sources, minus the target.
    assert seen_payloads[1][0] == "mangafire"
    assert [a["site"] for a in seen_payloads[1][1]["alternatives"]] == ["mangakatana"]
    # The switch's temp payload is removed once the re-entry returns.
    sw_path = reentry[reentry.index("--multi-source-prefetched") + 1]
    assert not os.path.exists(sw_path)


def test_lazy_discovery_runs_for_the_switch(aio, main_env, monkeypatch):
    """Under --multi-source-lazy discovery waits for a chapter failure, but an
    all-placeholder primary never gets that far, so the switch runs it."""
    import aio_search_cli

    main_env["handlers"][MD_URL] = _FakeHandler("mangadex", [_link_out("1")])
    main_env["handlers"][MF_URL] = _FakeHandler("mangafire", [_hosted("1")])
    calls = []

    def fake_find(primary_url, primary_handler, **kw):
        calls.append(primary_url)
        return {
            "alternatives_by_chap_num": {}, "consensus_set": None, "consensus_max": None,
            "resolved_sources": [{"site": "mangafire", "url": MF_URL}],
            "readable_sites": ["mangafire"],
        }

    monkeypatch.setattr(aio_search_cli, "find_alternatives_for_direct_url", fake_find)
    monkeypatch.setattr(
        aio_search_cli, "build_alternatives_from_prefetched",
        lambda prefetched_path, primary_handler, **kw: {
            "alternatives_by_chap_num": {}, "consensus_set": None, "consensus_max": None,
            "resolved_sources": [], "readable_sites": [],
        },
    )
    with pytest.raises(_ReachedFolder):
        _run_main(aio, monkeypatch, ["--multi-source", "--multi-source-lazy", MD_URL])
    assert calls == [MD_URL]
    assert main_env["allocated"] == [("mangafire", MF_URL)]


def test_switch_with_no_readable_alternative_exits(aio, main_env, monkeypatch, tmp_path):
    import aio_search_cli

    main_env["handlers"][MD_URL] = _FakeHandler("mangadex", [_link_out("1")])
    prefetched = tmp_path / "ui-alts.json"
    prefetched.write_text(json.dumps({"alternatives": [{"site": "x", "url": "https://x"}]}),
                          encoding="utf-8")
    monkeypatch.setattr(
        aio_search_cli, "build_alternatives_from_prefetched",
        lambda prefetched_path, primary_handler, **kw: {
            "alternatives_by_chap_num": {}, "consensus_set": None, "consensus_max": None,
            "resolved_sources": [{"site": "x", "url": "https://x"}], "readable_sites": [],
        },
    )
    with pytest.raises(SystemExit) as exc:
        _run_main(aio, monkeypatch, ["--multi-source", "--multi-source-prefetched", str(prefetched), MD_URL])
    assert "no alternative site had readable chapters" in str(exc.value.code)
    assert main_env["allocated"] == []


def test_switched_run_never_switches_again(aio, main_env, monkeypatch):
    import aio_search_cli

    main_env["handlers"][MF_URL] = _FakeHandler("mangafire", [_tapas_locked("1")])
    main_env["handlers"][MD_URL] = _FakeHandler("mangadex", [_hosted("1")])
    # --multi-source-lazy keeps discovery deferred, and the guard must not run
    # it. Recorded rather than raised (main() swallows discovery errors), and
    # it offers a readable site, so a missing guard would visibly switch.
    searches = []
    monkeypatch.setattr(
        aio_search_cli, "find_alternatives_for_direct_url",
        lambda primary_url, primary_handler, **kw: searches.append(primary_url) or {
            "alternatives_by_chap_num": {}, "consensus_set": None, "consensus_max": None,
            "resolved_sources": [{"site": "mangadex", "url": MD_URL}],
            "readable_sites": ["mangadex"],
        },
    )
    with pytest.raises(SystemExit) as exc:
        _run_main(aio, monkeypatch, [
            "--multi-source", "--multi-source-lazy",
            "--primary-switched-from", "mangadex", MF_URL,
        ])
    assert "already switched here from mangadex" in str(exc.value.code)
    assert main_env["allocated"] == []
    assert searches == []


def test_mixed_pool_still_allocates_on_primary(aio, main_env, monkeypatch):
    main_env["handlers"][MD_URL] = _FakeHandler("mangadex", [_hosted("1"), _link_out("2")])
    with pytest.raises(_ReachedFolder):
        _run_main(aio, monkeypatch, [MD_URL])
    assert main_env["allocated"] == [("mangadex", MD_URL)]


def _library_folder(aio, root, *, with_chapters):
    """A library folder that IS the MangaDex series (identity by url)."""
    folder = root / "Goblin Slayer"
    folder.mkdir(parents=True)
    (folder / aio._SERIES_META_FILENAME).write_text(
        json.dumps({"site": "mangadex", "url": MD_URL, "hid": "mangadex-hid"}),
        encoding="utf-8",
    )
    if with_chapters:
        (folder / "Goblin Slayer Ch.001.cbz").write_bytes(b"PK")
    return folder


def test_existing_library_series_never_switches(aio, main_env, monkeypatch, tmp_path):
    """A library update (forced multi-source) of a series whose list became all
    link-outs exits instead of forking `<title> (hid=…)` (user decision
    2026-10-17), and runs no discovery to get there."""
    import aio_search_cli

    lib = tmp_path / "lib"
    _library_folder(aio, lib, with_chapters=True)
    main_env["handlers"][MD_URL] = _FakeHandler("mangadex", [_link_out("1")])
    # Recorded, not raised: main() swallows discovery errors, so a raise
    # would not fail the test if the gate were missing.
    searches = []
    monkeypatch.setattr(
        aio_search_cli, "find_alternatives_for_direct_url",
        lambda primary_url, primary_handler, **kw: searches.append(primary_url) or {
            "alternatives_by_chap_num": {}, "consensus_set": None, "consensus_max": None,
            "resolved_sources": [{"site": "mangafire", "url": MF_URL}],
            "readable_sites": ["mangafire"],
        },
    )
    with pytest.raises(SystemExit) as exc:
        _run_main(aio, monkeypatch, [
            "--multi-source", "--multi-source-lazy", "--output-dir", str(lib), MD_URL,
        ])
    msg = str(exc.value.code)
    assert msg.startswith("No chapters selected")
    assert "library folder 'Goblin Slayer' holds this series from mangadex" in msg
    assert main_env["allocated"] == []
    assert searches == []


def test_existing_series_folder_ignores_empty_and_other_series(aio, tmp_path):
    lib = tmp_path / "lib"
    _library_folder(aio, lib, with_chapters=False)
    assert aio._existing_series_folder(
        str(lib), hid="mangadex-hid", site="mangadex", url=MD_URL) is None
    (lib / "Goblin Slayer" / "Goblin Slayer Ch.001.cbz").write_bytes(b"PK")
    assert aio._existing_series_folder(
        str(lib), hid="mangadex-hid", site="mangadex", url=MD_URL) == "Goblin Slayer"
    assert aio._existing_series_folder(
        str(lib), hid="other", site="mangafire", url=MF_URL) is None
    assert aio._existing_series_folder(None, hid="x", site="y", url=None) is None
