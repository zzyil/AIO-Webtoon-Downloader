"""Multi-source rescue fills a failed chapter only with content-proven pages,
stored under the PRIMARY's chapter identity.

WHAT BROKE. `_process_chapter_strict` looked alternatives up by chapter NUMBER and
processed the ALT's chapter dict, so the rescue was saved under the alt's number,
title and url. Sites number differently and the gap drifts: on a real unOrdinary
run (LINE primary) 21 of 23 rescued chapters were a different chapter, 2-5 ahead,
and the real chapters were never fetched (plan sync-temp/plans/multisource-offset.md).

These tests drive the real main() offline: a fake primary and a fake alt site
whose numbering runs 2 behind the story (alt "3" holds the primary's chapter 5,
alt "5" holds chapter 7, the observed shape). Pages are synthetic strip art,
sliced differently per site, served by a patched `dl_image`; nothing touches the
network.

Cross-file:
  - aio-dl.py — `_rescue_search` (own-pages proof), `_place` (placement by the
    neighbours' copies, with primary probes; chains for a locked run at either
    end of the list), `_process_chapter_impl(store_as=,
    rescue=)`, `_capture_partial_pages`, `_fingerprint_chapter`, <AioRescue> in
    `build_per_chapter_comic_info_xml`, the locked/external abort in
    `_process_chapter_strict`.
  - sites/rescue_pairing.py — locate / confirm / order_lists (unit tests:
    tests/test_rescue_pairing.py).
  - sites/chapter_fingerprint.py — the proof itself (unit tests:
    tests/test_chapter_fingerprint.py).
"""

from __future__ import annotations

import io
import json
import os
import sys
import zipfile

import numpy as np
import pytest
from PIL import Image, ImageFilter

from sites.base import BaseSiteHandler, SiteComicContext
from sites import chapter_fingerprint as cf
from tests.test_cli_flag_parsing import _load_aio_dl_module

PRIMARY_URL = "https://primary.test/series/x"
ALT_URL = "https://alt.test/series/x"
WIDTH = 400
STRIP_H = 20000          # 50 content widths per chapter
P_SLICE, A_SLICE = 2000, 1000   # each site cuts the strip differently
ALT_LAG = 2              # alt chapter c holds story chapter c + ALT_LAG


@pytest.fixture(scope="module")
def aio():
    return _load_aio_dl_module()


def _story_strip(story: int) -> Image.Image:
    """Deterministic, structured art for one story chapter: blurred noise plus
    blocks, so bands are informative and every chapter differs."""
    rng = np.random.default_rng(1000 + story)
    small = rng.integers(0, 256, (STRIP_H // 40, WIDTH // 40), dtype=np.uint8)
    im = Image.fromarray(small).resize((WIDTH, STRIP_H), Image.BICUBIC)
    return im.filter(ImageFilter.GaussianBlur(3)).convert("RGB")


_STRIPS: dict = {}


def _pages(story: int, slice_h: int, fmt: str) -> list:
    if story not in _STRIPS:
        _STRIPS[story] = _story_strip(story)
    strip = _STRIPS[story]
    out = []
    for y in range(0, STRIP_H, slice_h):
        buf = io.BytesIO()
        strip.crop((0, y, WIDTH, min(STRIP_H, y + slice_h))).save(buf, fmt, quality=90)
        out.append(buf.getvalue())
    return out


class _Site(BaseSiteHandler):
    """A fake site: chapter label c serves story `story_of(c)` sliced at
    `slice_h`; `missing` holds (label, page) pairs that never download;
    `locked` labels are placeholders (tapas-premium shape); `title_of(label)`
    is the entry's title (episode labels live there)."""

    SUPPORTS_FAST_DOWNLOAD = False

    def __init__(self, name, host, labels, story_of, slice_h, fmt, missing=(), locked=(),
                 title_of=None):
        self.name = name
        self.host = host
        self.labels = labels
        self.story_of = story_of
        self.slice_h = slice_h
        self.fmt = fmt
        self.missing = set(missing)
        self.locked = set(locked)
        self.title_of = title_of or (lambda c: f"{name} title {c}")
        self.image_calls = []

    def configure_session(self, scraper, args):
        pass

    def fetch_comic_context(self, url, scraper, make_request):
        return SiteComicContext(comic={"title": "Test Series"}, title="Test Series",
                                identifier=f"{self.name}-hid")

    def get_chapters(self, context, scraper, language, make_request):
        out = []
        for c in self.labels:
            ch = {"chap": c, "url": f"https://{self.host}/c/{c}", "title": self.title_of(c),
                  "group_name": self.name}
            if c in self.locked:
                ch["_locked"] = True
            out.append(ch)
        return out

    def get_chapter_images(self, chapter, scraper, make_request):
        from sites.base import IncompleteChapterError
        self.image_calls.append(chapter["chap"])
        if chapter.get("_locked"):
            raise IncompleteChapterError(reason="locked", host=self.host, pages_ok=0, pages_total=0)
        story = self.story_of(chapter["chap"])
        n = len(_pages(story, self.slice_h, self.fmt))
        return [f"https://{self.host}/img/{chapter['chap']}/{i}" for i in range(1, n + 1)]

    def get_group_name(self, chapter):
        return self.name

    def extract_additional_metadata(self, context):
        return {}

    def page_bytes(self, label, i):
        if (label, i) in self.missing:
            return None
        return _pages(self.story_of(label), self.slice_h, self.fmt)[i - 1]


@pytest.fixture
def env(aio, monkeypatch, tmp_path):
    sites = {}
    env_alts = [ALT_URL]          # alt URLs, in catalog order; tests may add more
    out_root = tmp_path / "manga"

    def fake_resolve(url, site):
        return sites[url]

    def fake_allocate(title, hid, root=None, site=None, url=None, series_dir=None):
        d = out_root / title
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def fake_dl_image(url, folder, name, scraper, cleanup=True):
        host = url.split("/")[2]
        site = next(s for s in sites.values() if s.host == host)
        label, i = url.split("/")[-2], int(url.split("/")[-1])
        blob = site.page_bytes(label, i)
        if blob is None:
            return None
        ext = ".png" if site.fmt == "PNG" else ".jpg"
        path = os.path.join(folder, os.path.splitext(name)[0] + ext)
        with open(path, "wb") as fh:
            fh.write(blob)
        return path

    import aio_search_cli

    def fake_find(primary_url, primary_handler, **kw):
        # Same two shapes the real builders return: the per-number dict (only
        # what a label aligner would pair) and the full per-site catalog.
        by_num, catalog, resolved = {}, [], []
        for url in env_alts:
            alt = sites[url]
            ctx = alt.fetch_comic_context(url, None, None)
            chs = [c for c in alt.get_chapters(ctx, None, "en", None) if not c.get("_locked")]
            scraper = object()
            for ch in chs:
                by_num.setdefault(float(ch["chap"]), []).append({
                    "site": alt.name, "url": url, "chapter": ch,
                    "scraper": scraper, "handler": alt, "context": ctx,
                })
            catalog.append({"site": alt.name, "url": url, "chapters": chs,
                            "handler": alt, "scraper": scraper, "context": ctx})
            resolved.append({"site": alt.name, "url": url})
        return {"alternatives_by_chap_num": by_num, "consensus_set": None,
                "consensus_max": None, "resolved_sources": resolved,
                "readable_sites": [r["site"] for r in resolved], "catalog": catalog}

    monkeypatch.setattr(aio, "resolve_site_handler", fake_resolve)
    monkeypatch.setattr(aio, "allocate_series_output_dir", fake_allocate)
    monkeypatch.setattr(aio, "dl_image", fake_dl_image)
    monkeypatch.setattr(aio, "_NEAR_MISS_RETRY_BACKOFF", 0.0)
    monkeypatch.setattr(aio_search_cli, "find_alternatives_for_direct_url", fake_find)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AIO_COORD_ENABLED", "0")
    return {"sites": sites, "alts": env_alts, "out": out_root / "Test Series"}


def _run(aio, monkeypatch, extra=(), combined=False):
    """Komikku (per-chapter CBZs) by default; `combined` builds one CBZ."""
    mode = [] if combined else ["--komikku"]
    argv = ["aio-dl.py", "--format", "cbz", *mode, "--multi-source",
            "--metadata-source", "none", "--inline-chapter-retries", "0",
            "--prefetch-image-workers", "0", "--image-workers", "4", *extra, PRIMARY_URL]
    monkeypatch.setattr(sys, "argv", argv)
    try:
        aio.main()
    except SystemExit:
        pass


def _cbz_for(out_dir, chap: int):
    hits = [f for f in os.listdir(out_dir) if f.endswith(".cbz") and f"Ch.{chap:03d}" in f]
    assert len(hits) <= 1, hits
    return os.path.join(out_dir, hits[0]) if hits else None


def _pages_of(cbz):
    with zipfile.ZipFile(cbz) as z:
        names = sorted(n for n in z.namelist() if not n.endswith(".xml") and not n.startswith("_aio/"))
        return [z.read(n) for n in names], z.read("ComicInfo.xml").decode("utf-8")


def _sites(env, primary_missing=(), primary_locked=()):
    labels = [str(c) for c in range(1, 9)]
    env["sites"][PRIMARY_URL] = _Site("primaryfake", "primary.test", labels, lambda c: int(c),
                                      P_SLICE, "PNG", missing=primary_missing, locked=primary_locked)
    env["sites"][ALT_URL] = _Site("altfake", "alt.test", labels, lambda c: int(c) + ALT_LAG,
                                  A_SLICE, "JPEG")


def test_failed_chapter_is_filled_with_the_proven_alt_chapter_under_its_own_number(aio, env, monkeypatch):
    # Primary chapter 5 permanently loses page 8 of 10: a near-miss, then a rescue.
    _sites(env, primary_missing=[("5", 8)])
    _run(aio, monkeypatch)
    alt = env["sites"][ALT_URL]
    cbz = _cbz_for(env["out"], 5)
    assert cbz, os.listdir(env["out"])
    pages, ci = _pages_of(cbz)
    # The content is the alt's chapter 3 (= story 5), byte for byte, not its "5".
    assert pages == _pages(5, A_SLICE, "JPEG")
    # Stored as the primary's chapter: its number and title, with provenance.
    assert "<Number>5</Number>" in ci
    assert "primaryfake title 5" in ci
    assert '<AioRescue site="altfake" chap="3"' in ci
    assert 'tier="own-pages"' in ci
    # The number-paired candidate was looked at and rejected; nothing was
    # stored under the alt's numbers.
    assert "5" in alt.image_calls and "3" in alt.image_calls
    for c in range(1, 9):
        assert _cbz_for(env["out"], c), f"chapter {c} missing"
    # Fingerprints: every chapter, the rescue with its provenance.
    fpdir = os.path.join(env["out"], ".aio_fp", "primaryfake")
    assert sorted(f for f in os.listdir(fpdir) if f.endswith(".npz")) == sorted(f"{c}.npz" for c in range(1, 9))
    # The proven pair is remembered for the next run (by URL, own-pages tier).
    pairs = json.load(open(os.path.join(fpdir, "pairings.json"), encoding="utf-8"))
    rec = pairs["lists"]["altfake"]["https://primary.test/c/5"]
    assert rec["alt_url"] == "https://alt.test/c/3" and rec["tier"] == "own-pages"
    meta = cf.load_pages(os.path.join(fpdir, "5.npz")).meta
    assert meta["rescue"]["chap"] == "3" and meta["source_site"] == "altfake"
    assert cf.load_pages(os.path.join(fpdir, "4.npz")).meta.get("rescue") is None


def _story_pages_in_order(cbz):
    """Every page image of a combined CBZ, in archive order."""
    with zipfile.ZipFile(cbz) as z:
        names = sorted(n for n in z.namelist()
                       if not n.endswith(".xml") and not n.startswith("_aio/"))
        return [z.read(n) for n in names]


def _combined_cbz(out_dir):
    hits = [f for f in os.listdir(out_dir) if f.endswith(".cbz")]
    return os.path.join(out_dir, hits[0]) if len(hits) == 1 else None


def _pairs_file(env):
    return json.load(open(os.path.join(env["out"], ".aio_fp", "primaryfake", "pairings.json"),
                          encoding="utf-8"))["lists"]


def test_chapter_with_no_delivered_pages_is_placed_by_its_neighbours(aio, env, monkeypatch):
    # Every page of chapter 5 fails: no own pages to prove a candidate with. It
    # is placed at once, between chapter 4 (on disk) and chapter 6, which the
    # run has not reached yet and is probed on the primary.
    _sites(env, primary_missing=[("5", i) for i in range(1, 11)])
    primary = env["sites"][PRIMARY_URL]
    _run(aio, monkeypatch)
    cbz = _cbz_for(env["out"], 5)
    assert cbz, os.listdir(env["out"])
    pages, ci = _pages_of(cbz)
    assert pages == _pages(5, A_SLICE, "JPEG")          # alt "3" = story 5
    assert "<Number>5</Number>" in ci
    assert '<AioRescue site="altfake" chap="3"' in ci and 'tier="placement"' in ci
    for c in range(1, 9):
        assert _cbz_for(env["out"], c), f"chapter {c} missing"
    # Chapter 6's page list was asked twice: the probe, then its own download.
    assert primary.image_calls.count("6") == 2
    fpdir = os.path.join(env["out"], ".aio_fp", "primaryfake")
    assert os.path.exists(os.path.join(fpdir, "6.probe.npz"))
    # Neighbours located by content are anchor grade; the fill only predicts.
    pairs = _pairs_file(env)["altfake"]
    assert pairs["https://primary.test/c/4"]["tier"] == "probe"
    assert pairs["https://primary.test/c/6"]["tier"] == "probe"
    assert pairs["https://primary.test/c/5"]["tier"] == "placement"
    assert pairs["https://primary.test/c/5"]["list_len"] == 8


def test_locked_chapter_lands_in_its_place_in_the_combined_book(aio, env, monkeypatch):
    _sites(env, primary_locked=["5"])
    _run(aio, monkeypatch, combined=True)
    cbz = _combined_cbz(env["out"])
    assert cbz, os.listdir(env["out"])
    expect = []
    for c in range(1, 9):
        expect += _pages(5, A_SLICE, "JPEG") if c == 5 else _pages(c, P_SLICE, "PNG")
    assert _story_pages_in_order(cbz) == expect


def test_a_locked_newest_tail_is_chained_from_the_last_readable_chapter(aio, env, monkeypatch):
    # 7 and 8 are locked and nothing comes after them: no far neighbour to
    # check the count against. Each is chained one-for-one from the chapter
    # before it (plan D4): 7 from 6, then 8 from the fill of 7.
    _sites(env, primary_locked=["7", "8"])
    _run(aio, monkeypatch)
    for c, alt_label in ((7, 5), (8, 6)):
        pages, ci = _pages_of(_cbz_for(env["out"], c))
        assert pages == _pages(c, A_SLICE, "JPEG")
        assert f'<AioRescue site="altfake" chap="{alt_label}"' in ci and 'tier="chain"' in ci
    # A chain fill predicts only, like a placement.
    pairs = _pairs_file(env)["altfake"]
    assert pairs["https://primary.test/c/7"]["tier"] == "placement"
    assert pairs["https://primary.test/c/6"]["tier"] == "probe"


def test_a_chain_stops_where_the_numbers_do_not_step_by_one(aio, env, monkeypatch, capsys):
    # The alt lacks the copy of our 7: after its copy of 6 ("4") comes "6",
    # which holds our 8. Counting on would fill 7 with 8's content; the guard
    # sees the alt's numbers jump and the run stops. Not asserted:
    # .aio_series.json still claims chapters 1-6, which live only in the temp
    # folder; every aborted combined run does that (memory
    # downloader-shipped-defects.md #5, ledgered by the user 2026-10-23).
    _sites(env, primary_locked=["7", "8"])
    env["sites"][ALT_URL] = _Site("altfake", "alt.test", ["1", "2", "3", "4", "6", "7", "8"],
                                  lambda c: int(c) + ALT_LAG, A_SLICE, "JPEG")
    _run(aio, monkeypatch, combined=True)
    out = capsys.readouterr().out
    assert _combined_cbz(env["out"]) is None
    assert "the chain from Ch 6 stops, not one-for-one" in out
    assert "(chained)" not in out                  # nothing downloaded in full


def test_a_newest_chapter_the_primary_fails_to_serve_is_not_chained(aio, env, monkeypatch, capsys):
    # Not locked: its pages fail (an outage looks like this). The primary may
    # serve it next run, so position is not trusted here and the run stops.
    _sites(env, primary_missing=[("8", i) for i in range(1, 11)])
    _run(aio, monkeypatch)
    out = capsys.readouterr().out
    assert _cbz_for(env["out"], 8) is None
    assert "no readable chapter within 3 entries after it" in out
    assert "chaining" not in out


def test_a_locked_head_block_is_chained_back_from_the_first_readable_chapter(aio, env, monkeypatch):
    # 1 and 2 are locked (MangaDex link-outs shape). 1 has nothing before it:
    # it is chained back from 3, whose copy is found by probing 3, 4 and 5 on
    # the primary. 2 then has the fill of 1 on its left and is placed.
    _sites(env, primary_locked=["1", "2"])
    env["sites"][ALT_URL] = _Site("altfake", "alt.test", [str(c) for c in range(2, 10)],
                                  lambda c: int(c) - 1, A_SLICE, "JPEG")
    _run(aio, monkeypatch)
    for c, tier in ((1, "chain"), (2, "placement")):
        pages, ci = _pages_of(_cbz_for(env["out"], c))
        assert pages == _pages(c, A_SLICE, "JPEG")
        assert f'<AioRescue site="altfake" chap="{c + 1}"' in ci and f'tier="{tier}"' in ci
    for c in range(3, 9):
        assert _cbz_for(env["out"], c), f"chapter {c} missing"


def test_a_locked_chapter_after_a_blank_one_is_not_chained(aio, env, monkeypatch, capsys):
    # Chapter 1 is readable but carries no picture content (a text card), so
    # the locked 2 has no neighbour to locate before it. It is not the head of
    # the list, so no chain: the primary has a chapter there, and counting
    # from 3 alone would skip the check the left side gives. The run stops.
    _STRIPS[0] = Image.new("RGB", (WIDTH, STRIP_H), "white")
    _sites(env, primary_locked=["2"])
    env["sites"][PRIMARY_URL].story_of = lambda c: 0 if c == "1" else int(c)
    env["sites"][ALT_URL].story_of = lambda c: int(c)
    _run(aio, monkeypatch)
    out = capsys.readouterr().out
    assert _cbz_for(env["out"], 1) and _cbz_for(env["out"], 2) is None
    assert "no readable chapter within 3 entries before it" in out
    assert "chaining" not in out


def _second_alt(env, story_of, slice_h=1500, fmt="PNG", labels=None):
    url = "https://alt2.test/series/x"
    env["sites"][url] = _Site("alt2fake", "alt2.test", labels or [str(c) for c in range(1, 9)],
                              story_of, slice_h, fmt)
    env["alts"].append(url)
    return env["sites"][url]


def test_a_list_that_cannot_place_it_hands_over_to_the_next_list(aio, env, monkeypatch, capsys):
    # One list at a time, no second-site witness (user decision 2026-10-25):
    # the first list lacks the chapter (its count between 4 and 6 is off), the
    # second places it.
    _sites(env, primary_locked=["5"])
    env["sites"][ALT_URL] = _Site("altfake", "alt.test", ["1", "2", "4", "5", "6"],
                                  lambda c: int(c) + ALT_LAG, A_SLICE, "JPEG")
    alt2 = _second_alt(env, lambda c: int(c) + ALT_LAG)
    _run(aio, monkeypatch)
    pages, ci = _pages_of(_cbz_for(env["out"], 5))
    assert pages == _pages(5, 1500, "PNG")               # alt2's "3" = story 5
    assert '<AioRescue site="alt2fake" chap="3"' in ci
    assert "altfake: Ch 6 is not 2 entries after Ch 4" in capsys.readouterr().out


def test_a_slot_holding_a_chapter_we_hold_is_vetoed(aio, env, monkeypatch, capsys):
    # The count fits, but the alt entry in the slot holds our chapter 3 again
    # (a re-upload): the +-15 veto refuses it and the run stops.
    _sites(env, primary_locked=["5"])
    env["sites"][ALT_URL] = _Site("altfake", "alt.test", [str(c) for c in range(1, 9)],
                                  lambda c: 3 if c == "3" else int(c) + ALT_LAG, A_SLICE, "JPEG")
    _run(aio, monkeypatch)
    out = capsys.readouterr().out
    assert _cbz_for(env["out"], 5) is None
    assert "holds Ch 3's content" in out
    assert _cbz_for(env["out"], 6) is None                # the run stopped at 5


def test_episode_labels_locate_both_neighbours_for_free(aio, env, monkeypatch):
    # Both sides title their entries "Episode N": the neighbours' copies are
    # found by label, so the only alt pages fetched are the fill's own.
    _sites(env, primary_locked=["5"])
    primary = env["sites"][PRIMARY_URL]
    primary.title_of = lambda c: f"Episode {c}"
    env["sites"][ALT_URL].title_of = lambda c: f"Chapter {c}: Episode {int(c) + ALT_LAG}"
    _run(aio, monkeypatch)
    pages, _ci = _pages_of(_cbz_for(env["out"], 5))
    assert pages == _pages(5, A_SLICE, "JPEG")
    assert env["sites"][ALT_URL].image_calls == ["3"]


def test_a_label_that_disagrees_with_the_slot_vetoes_it(aio, env, monkeypatch, capsys):
    _sites(env, primary_locked=["5"])
    alt = env["sites"][ALT_URL]
    # The slot's entry says it is episode 9; ours is episode 5.
    alt.title_of = lambda c: "Episode 9" if c == "3" else f"alt title {c}"
    env["sites"][PRIMARY_URL].title_of = lambda c: f"Episode {c}"
    _run(aio, monkeypatch)
    assert _cbz_for(env["out"], 5) is None
    assert "labelled episode 9, not 5" in capsys.readouterr().out


def test_a_locked_block_fills_left_to_right_and_fills_never_anchor(aio, env, monkeypatch):
    # 5 and 6 are locked: 5 is placed between 4 and 7, then 6 between the fill
    # of 5 and 7. The pair a probe finds for the filled 5 stays placement grade:
    # its content came from the alt by position, so it proves nothing new.
    _sites(env, primary_locked=["5", "6"])
    _run(aio, monkeypatch)
    for c, alt_label in ((5, 3), (6, 4)):
        pages, ci = _pages_of(_cbz_for(env["out"], c))
        assert pages == _pages(c, A_SLICE, "JPEG")
        assert f'<AioRescue site="altfake" chap="{alt_label}"' in ci
    pairs = _pairs_file(env)["altfake"]
    assert pairs["https://primary.test/c/5"]["tier"] == "placement"
    assert pairs["https://primary.test/c/6"]["tier"] == "placement"
    assert pairs["https://primary.test/c/7"]["tier"] == "probe"


def test_a_chapter_outside_the_selection_is_probed_as_the_left_neighbour(aio, env, monkeypatch):
    # A run of chapters 5-8 on a library without fingerprints for 1-4 (an
    # update, or one made before fingerprints existed): the left neighbour is
    # probed on the primary instead of re-downloaded (plan D6).
    _sites(env, primary_locked=["5"])
    primary = env["sites"][PRIMARY_URL]
    _run(aio, monkeypatch, extra=("--chapters", "5-8"))
    pages, _ci = _pages_of(_cbz_for(env["out"], 5))
    assert pages == _pages(5, A_SLICE, "JPEG")
    fpdir = os.path.join(env["out"], ".aio_fp", "primaryfake")
    assert os.path.exists(os.path.join(fpdir, "4.probe.npz"))
    assert not os.path.exists(os.path.join(fpdir, "4.npz"))
    assert _cbz_for(env["out"], 4) is None
    assert primary.image_calls.count("4") == 1


def test_an_extra_alt_entry_inside_the_bracket_leaves_the_chapter_unfilled(aio, env, monkeypatch, capsys):
    # The alt lists a recap between its copies of chapters 4 and 6: two entries
    # where the primary has one, and position cannot say which is chapter 5.
    _sites(env, primary_locked=["5"])
    labels = ["1", "2", "2.5", "3", "4", "5", "6", "7", "8"]
    env["sites"][ALT_URL] = _Site("altfake", "alt.test", labels,
                                  lambda c: 50 if c == "2.5" else int(c) + ALT_LAG,
                                  A_SLICE, "JPEG")
    _run(aio, monkeypatch)
    out = capsys.readouterr().out
    assert _cbz_for(env["out"], 5) is None
    assert "altfake: Ch 6 is not 2 entries after Ch 4" in out
    assert "(placed)" not in out                  # no candidate downloaded in full


def test_three_unservable_chapters_in_a_row_stop_the_run(aio, env, monkeypatch, capsys):
    # A host outage, not three holes to fill one by one: 4 and 5 are placed,
    # the third in a row stops the run before any other site is asked.
    _sites(env, primary_missing=[(str(c), i) for c in (4, 5, 6) for i in range(1, 11)])
    _run(aio, monkeypatch)
    assert _cbz_for(env["out"], 4) and _cbz_for(env["out"], 5)
    for c in (6, 7):
        assert _cbz_for(env["out"], c) is None
    assert "chapter 3 in a row that primaryfake could not serve" in capsys.readouterr().out


def test_aio_rescue_survives_a_comicinfo_rewrite(aio, tmp_path):
    xml = aio.build_per_chapter_comic_info_xml(
        series_title="S", chapter_title="T", chapter_num="5", volume=None,
        scanlator="g", web_url="https://p/5", uploaded_epoch=None, comic_info={},
        publishers=[], lang="en", page_count=3,
        rescue={"site": "alt", "chap": "3", "url": 'https://a/3?x="y"', "tier": "own-pages"},
    )
    assert '<AioRescue site="alt" chap="3" url="https://a/3?x=&quot;y&quot;" tier="own-pages" />' in xml
    folder = tmp_path / "s"
    folder.mkdir()
    with zipfile.ZipFile(folder / "c.cbz", "w") as z:
        z.writestr("0000.jpg", b"x")
        z.writestr("ComicInfo.xml", xml)
    aio._rewrite_cbz_comicinfo(str(folder), {}, "S", "en")
    with zipfile.ZipFile(folder / "c.cbz") as z:
        assert '<AioRescue site="alt" chap="3"' in z.read("ComicInfo.xml").decode("utf-8")


def test_a_chapter_with_no_images_is_recorded_missed_and_the_run_continues(aio, env, monkeypatch):
    # A handler that returns an EMPTY image list (no exception) reaches the main
    # loop's empty-content backstop, which used to read an undefined name and
    # crash the run there (grep _decode_dropped in aio-dl.py).
    _sites(env)
    primary = env["sites"][PRIMARY_URL]
    real = primary.get_chapter_images
    primary.get_chapter_images = lambda ch, s, mr: [] if ch["chap"] == "5" else real(ch, s, mr)
    _run(aio, monkeypatch)
    assert _cbz_for(env["out"], 5) is None
    for c in (4, 6, 7, 8):
        assert _cbz_for(env["out"], c), f"chapter {c} missing"


# ── --search --multi-source --auto-pick (plan P4) ────────────────────────────
# The search hands its fetched chapter lists to main(), which builds the
# alternatives and the catalog against the primary it resolved
# (aio_search_cli.alternatives_from_search_state). Before, the search built a
# per-number dict holding only id() tokens for the live objects, which nothing
# turned back into objects, and no catalog: the run rescued nothing.

class _Handler:
    def __init__(self, name):
        self.name = name


def _record(site, url, chapters):
    return {"site": site, "url": url, "chapters": chapters, "scraper": object(),
            "handler": _Handler(site), "context": object()}


def _source(site, url):
    from sites.search_orchestrator import SourceEntry
    return SourceEntry(site=site, url=url, title="Test Series", cover=None,
                       title_match=1.0, seed_quality=0.9)


def test_search_lists_become_alternatives_for_the_primary_main_resolved():
    # The search ranked A first but B had the most chapters, so B is the
    # download primary: A becomes an alternative, B never is one.
    import argparse
    import aio_search_cli
    chs = lambda n: [{"chap": str(c), "url": f"u{c}"} for c in range(1, n + 1)]
    state = {"source_records": [_record("a", "https://a/x", chs(5)),
                                _record("b", "https://b/x", chs(8)),
                                _record("c", "https://c/x", chs(6))],
             "sources": [_source("a", "https://a/x"), _source("b", "https://b/x"),
                         _source("c", "https://c/x")]}
    res = aio_search_cli.alternatives_from_search_state(
        state, primary_handler=_Handler("b"), primary_chapters=chs(8),
        args=argparse.Namespace())
    assert [r["site"] for r in res["catalog"]] == ["a", "c"]
    assert [s["site"] for s in res["resolved_sources"]] == ["a", "c"]
    assert res["readable_sites"] == ["a", "c"]
    assert {a["site"] for alts in res["alternatives_by_chap_num"].values() for a in alts} == {"a", "c"}
    # Live objects, not tokens: the rescue swaps these in.
    assert res["catalog"][0]["handler"] is state["source_records"][0]["handler"]


def test_an_auto_picked_download_rescues_from_the_searched_lists(aio, env, monkeypatch):
    import aio_search_cli
    from sites.search_orchestrator import SeriesCandidate
    _sites(env, primary_missing=[("5", 8)])
    # The alt list is shorter, so the search keeps the primary as its anchor.
    alt = env["sites"][ALT_URL] = _Site("altfake", "alt.test", [str(c) for c in range(1, 7)],
                                        lambda c: int(c) + ALT_LAG, A_SLICE, "JPEG")
    primary = env["sites"][PRIMARY_URL]

    def records(winner, *a, **kw):
        out = []
        for s in winner.sources:
            site = env["sites"][s.url]
            ctx = site.fetch_comic_context(s.url, None, None)
            out.append({"site": site.name, "url": s.url, "scraper": object(), "handler": site,
                        "context": ctx, "chapters": site.get_chapters(ctx, None, "en", None)})
        return out

    def no_search(*a, **kw):
        raise AssertionError("the search's lists must be reused, not searched again")

    persisted = []
    monkeypatch.setattr(aio_search_cli, "search_all", lambda *a, **kw: [SeriesCandidate(
        "Test Series", None, [_source(primary.name, PRIMARY_URL), _source(alt.name, ALT_URL)])])
    monkeypatch.setattr(aio_search_cli, "_fetch_chapters_for_winner", records)
    monkeypatch.setattr(aio_search_cli, "find_alternatives_for_direct_url", no_search)
    monkeypatch.setattr(aio, "_persist_multi_source_cache", lambda p, c: persisted.append(c))
    monkeypatch.setattr(sys, "argv", [
        "aio-dl.py", "--search", "Test Series", "--auto-pick", "--format", "cbz", "--komikku",
        "--multi-source", "--metadata-source", "none", "--inline-chapter-retries", "0",
        "--prefetch-image-workers", "0", "--image-workers", "4"])
    try:
        aio.main()
    except SystemExit:
        pass
    pages, ci = _pages_of(_cbz_for(env["out"], 5))
    assert pages == _pages(5, A_SLICE, "JPEG")          # alt "3" = story 5
    assert "<Number>5</Number>" in ci and '<AioRescue site="altfake" chap="3"' in ci
    for c in range(1, 9):
        assert _cbz_for(env["out"], c), f"chapter {c} missing"
    # A resume of this run is a direct-URL run: it reuses the found sources.
    assert [s["site"] for s in persisted[0]["alternatives"]] == ["altfake"]


def test_the_resume_cache_is_saved_when_only_the_catalog_has_alternatives(aio, env, monkeypatch):
    # Sites that number differently pair nothing by label; the rescue reads the
    # catalog, and a resume must not search again for it.
    import aio_search_cli
    _sites(env)
    found = aio_search_cli.find_alternatives_for_direct_url   # the env's fake

    def catalog_only(*a, **kw):
        res = found(*a, **kw)
        return {**res, "alternatives_by_chap_num": {}}

    persisted = []
    monkeypatch.setattr(aio_search_cli, "find_alternatives_for_direct_url", catalog_only)
    monkeypatch.setattr(aio, "_persist_multi_source_cache", lambda p, c: persisted.append(c))
    _run(aio, monkeypatch)
    assert [s["site"] for s in persisted[0]["alternatives"]] == ["altfake"]
