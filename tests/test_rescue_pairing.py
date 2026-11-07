"""Placement logic of the multi-source rescue, without the downloader.

Covers sites/rescue_pairing.py (alt lists, pairs + their cache, prediction,
the placement search: locate / confirm / order_lists, the length band),
sites/book_buffer.py (chapter order in the combined book and its split parts),
and the placement checks in sites/chapter_fingerprint.py (`middle_pages`,
`probe_names`, `holds_other`, `FingerprintWriter.drain`). The end-to-end runs
through main() are in tests/test_multisource_rescue.py. Plan:
sync-temp/plans/multisource-offset.md rev 3, D3.
"""

from __future__ import annotations

import io
import json
import os

import numpy as np
import pytest
from PIL import Image, ImageFilter

from sites import chapter_fingerprint as cf
from sites import rescue_pairing as rp
from sites.book_buffer import BookBuffer


# ---------------------------------------------------------------- alt lists

def _rec(site, chapters, **kw):
    return {"site": site, "url": f"https://{site}/s", "chapters": chapters,
            "handler": object(), "scraper": object(), "context": object(), **kw}


def _chs(labels, titles=None, group=None):
    out = []
    for i, c in enumerate(labels):
        d = {"chap": c, "url": f"u/{c}/{i}", "title": (titles or {}).get(c, f"Chapter {c}")}
        if group:
            d["group"] = group[i] if isinstance(group, list) else group
        out.append(d)
    return out


def test_newest_first_list_is_put_in_story_order_and_text_entries_stay_put():
    cat = rp.build_catalog([_rec("a", _chs(["5", "4", "extra", "3", "2", "1"]))])
    (alt,) = cat
    assert alt.ordered
    # "extra" was listed between 4 and 3 newest-first, i.e. after 3 in story order.
    assert [e.chap for e in alt.entries] == ["1", "2", "3", "extra", "4", "5"]


def test_one_number_under_two_groups_splits_the_site_per_group():
    chs = _chs(["1", "1", "2", "2", "3"], group=["G", "W", "G", "W", "G"])
    cat = rp.build_catalog([_rec("atsu", chs)], group_of=lambda r, c: c["group"])
    assert sorted(a.key for a in cat) == ["atsu:G", "atsu:W"]
    g = next(a for a in cat if a.key == "atsu:G")
    assert [e.chap for e in g.entries] == ["1", "2", "3"]


def test_degenerate_numbers_order_by_title_or_mark_the_list_unordered():
    # mangapill: every "Group 2 Chapter N" parses as 2; the title's last number orders it.
    chs = [{"chap": "2", "url": f"u{i}", "title": f"Group 2 Chapter {n}"}
           for i, n in enumerate([3, 2, 1])]
    (alt,) = rp.build_catalog([_rec("mp", chs)])
    assert alt.ordered and [e.chapter["title"][-1] for e in alt.entries] == ["1", "2", "3"]
    chs = [{"chap": "x", "url": f"u{i}", "title": "Special"} for i in range(4)]
    (alt,) = rp.build_catalog([_rec("mp", chs)])
    assert not alt.ordered


def test_browser_sites_are_probed_last_and_duplicate_sites_kept_apart():
    cat = rp.build_catalog([_rec("comix", _chs(["1"])), _rec("a", _chs(["1"])),
                            _rec("a", _chs(["1", "2"]))], browser_sites={"comix"})
    assert [a.key for a in cat] == ["a", "a#2", "comix"]


# ---------------------------------------------------------------- pairs + prediction

def _primary(n, titles=None):
    return [{"chap": str(i + 1), "url": f"p/{i + 1}", "title": (titles or {}).get(i, "")}
            for i in range(n)]


def test_prediction_follows_the_nearest_proven_pair_through_drift():
    # Story order: the alt lists 3 extra entries before primary 10 and 2 more
    # before primary 30 (offset 0 -> +3 -> +5, the unOrdinary shape).
    alt = rp.build_catalog([_rec("a", _chs([str(i) for i in range(1, 60)]))])[0]
    prim = _primary(40)
    pairs = [rp.Pair(5, 5, "own-pages"), rp.Pair(12, 15, "probe"), rp.Pair(32, 37, "probe")]
    assert rp.predict(alt, 6, prim, pairs) == 6        # near the +0 proof
    assert rp.predict(alt, 14, prim, pairs) == 17      # near the +3 proof
    assert rp.predict(alt, 35, prim, pairs) == 40      # near the +5 proof


def test_prediction_without_pairs_uses_labels_then_numbers():
    alt = rp.build_catalog([_rec("mk", _chs(["1", "2", "3", "4"], titles={
        "3": "Chapter 3: Episode 1", "4": "Chapter 4: Episode 2"}))])[0]
    prim = _primary(3, titles={1: "Episode 2"})
    assert rp.predict(alt, 1, prim, []) == 3           # label "Episode 2" -> alt "4"
    assert rp.predict(alt, 0, prim, []) == 0           # number 1 -> alt "1"


def test_prediction_reports_a_list_that_does_not_reach_the_number():
    # A MangaDex group that only scanned 262-264: the nearest number to our 72
    # is 190 away, which the rescue search treats as "this list lacks it".
    late = rp.build_catalog([_rec("md:late", _chs(["262", "263", "264"]))])[0]
    full = rp.build_catalog([_rec("md:full", _chs([str(i) for i in range(1, 90)]))])[0]
    prim = _primary(80)
    assert rp.predict_ex(late, 71, prim, []) == (0, "number", 190.0)
    assert rp.predict_ex(full, 71, prim, [])[1:] == ("number", 0.0)
    assert rp.predict_ex(late, 71, prim, [rp.Pair(70, 1, "own-pages")])[1:] == ("pair", None)


def test_pair_cache_round_trip_keeps_strong_tiers_and_drops_stale_urls(tmp_path):
    path = str(tmp_path / ".aio_fp" / "p" / "pairings.json")
    st = rp.PairStore(path)
    assert st.put("a", "p/1", "1", "u/1/0", "1", "probe", 2)
    assert not st.put("a", "p/1", "1", "u/9/9", "9", "placement", 2)   # weaker: kept
    assert st.put("a", "p/1", "1", "u/1/0", "1", "own-pages", 2)       # stronger: replaces
    st.put("a", "p/2", "2", "gone", "x", "own-pages", 2)               # alt url no longer listed
    again = rp.PairStore(path)
    assert json.load(open(path))["lists"]["a"]["p/1"]["tier"] == "own-pages"
    alt = rp.build_catalog([_rec("a", _chs(["1", "2"]))])[0]
    got = rp.resolve_pairs(alt, again, {"p/1": 0, "p/2": 1})
    assert [(p.primary_idx, p.alt_pos, p.tier) for p in got] == [(0, 0, "own-pages")]


def test_a_content_pair_from_a_changed_list_only_predicts(tmp_path):
    # A site that re-indexes can keep a URL and move its content, so a pair made
    # when the list had another length no longer anchors; and a weaker pair made
    # on today's list may replace it.
    st = rp.PairStore(str(tmp_path / "pairings.json"))
    st.put("a", "p/1", "1", "u/1/0", "1", "own-pages", 5)
    alt = rp.build_catalog([_rec("a", _chs(["1", "2"]))])[0]          # 2 entries now
    (p,) = rp.resolve_pairs(alt, st, {"p/1": 0})
    assert p.tier == "placement"
    assert st.put("a", "p/1", "1", "u/1/0", "1", "probe", 2)


def test_tiers_of_the_p2_build_read_as_placement(tmp_path):
    st = rp.PairStore(str(tmp_path / "pairings.json"))
    st.put("a", "p/1", "1", "u/1/0", "1", "head", 2)
    st.put("a", "p/2", "2", "u/2/1", "2", "elimination", 2)
    alt = rp.build_catalog([_rec("a", _chs(["1", "2"]))])[0]
    assert {p.tier for p in rp.resolve_pairs(alt, st, {"p/1": 0, "p/2": 1})} == {"placement"}


# ---------------------------------------------------------------- placement search

def _ident(truth):
    """identify() over a table {alt position: our index | None | NOIMG}: the
    entry names its index when it lies within the window, as the probe would."""
    calls = []

    def identify(q, centre):
        calls.append(q)
        m = truth.get(q)
        if isinstance(m, int) and abs(m - centre) > rp.IDENTIFY_WINDOW:
            return None
        return m
    return identify, calls


def _alt(n, titles=None):
    return rp.build_catalog([_rec("a", _chs([str(i) for i in range(1, n + 1)], titles=titles))])[0]


def test_a_probe_that_names_another_chapter_moves_the_search_by_the_difference():
    # The alt lists 3 extra entries up front: its position q holds our q - 3.
    alt, prim = _alt(40), _primary(30)
    identify, calls = _ident({q: q - 3 for q in range(3, 40)})
    loc = rp.locate(alt, 10, prim, [], identify)
    # Predicted by number at position 10 (our 7 there), then straight to 13.
    assert (loc.pos, loc.tier, loc.probes) == (13, "probe", 2)
    assert calls == [10, 13]
    assert [(p.primary_idx, p.alt_pos) for p in loc.found] == [(7, 10)]


def test_entries_without_images_cost_no_probe_budget():
    alt, prim = _alt(40), _primary(30)
    identify, calls = _ident({10: rp.NOIMG, 9: None, 11: None})
    loc = rp.locate(alt, 10, prim, [], identify, budget=2)
    assert loc.pos is None and loc.probes == 2 and calls == [10, 9, 11]


def test_free_anchors_are_content_pairs_and_unique_labels_never_placements():
    titles = {"12": "Chapter 12: Episode 10"}
    alt, prim = _alt(40, titles), _primary(30, titles={9: "Episode 10"})
    identify, calls = _ident({})
    assert rp.locate(alt, 9, prim, [], identify).tier == "label"
    assert rp.locate(alt, 5, prim, [rp.Pair(5, 8, "probe")], identify).pos == 8
    # A placement pair only predicts: the search still probes.
    identify, calls = _ident({8: 5})
    loc = rp.locate(alt, 5, prim, [rp.Pair(5, 8, "placement")], identify)
    assert (loc.pos, loc.tier) == (8, "probe") and calls == [8]


def test_confirm_checks_the_far_neighbour_where_the_count_puts_it():
    alt, prim = _alt(40), _primary(30)
    identify, _ = _ident({12: 12, 14: rp.NOIMG, 15: 13})
    assert rp.confirm(alt, 12, 12, prim, [], identify, lambda: True)[0]
    # A free anchor decides without a probe, and one that disagrees fails.
    assert rp.confirm(alt, 12, 12, prim, [rp.Pair(12, 12, "probe")], identify, lambda: True)[0]
    ok, why, _ = rp.confirm(alt, 12, 13, prim, [rp.Pair(12, 12, "probe")], identify, lambda: True)
    assert not ok and "entry 12" in why
    # An image-less entry hands the question to the next one, against our k + 1.
    ok, _why, found = rp.confirm(alt, 12, 14, prim, [], identify, lambda: True)
    assert ok and [(p.primary_idx, p.alt_pos) for p in found] == [(13, 15)]
    assert not rp.confirm(alt, 12, 14, prim, [], identify, lambda: False)[0]
    assert not rp.confirm(alt, 12, 40, prim, [], identify, lambda: True)[0]


def test_a_chain_needs_both_sides_to_step_by_exactly_one():
    alt, prim = _alt(10), _primary(10)
    # Our 5 -> 8 against the alt's positions 5 -> 8 (chapters "6" -> "9"), and backwards.
    assert rp.lockstep(alt, prim, 5, 5, 8)[0]
    assert rp.lockstep(alt, prim, 5, 5, 2)[0]
    assert rp.lockstep(alt, prim, 5, 5, 5)[0]                  # nothing to step
    # The alt skips a number: its "7" is missing, so position 6 holds "8".
    gap = rp.build_catalog([_rec("a", _chs(["1", "2", "3", "4", "5", "6", "8", "9"]))])[0]
    ok, why = rp.lockstep(gap, prim, 4, 4, 7)
    assert not ok and 'chapter 6 -> 8 there' in why
    # Our list skips a number (an episode the alt has and we do not): every
    # later fill would shift by one (M1 smoke test).
    ours = [{"chap": c, "url": f"p/{c}"} for c in ("1", "2", "3", "5", "6")]
    ok, why = rp.lockstep(alt, ours, 1, 1, 4)
    assert not ok and "Ch 3 -> Ch 5 here" in why
    # A repeat, a number-less entry, and the end of the list stop it too.
    rep = rp.build_catalog([_rec("a", _chs(["1", "2", "2", "3"]))])[0]
    assert not rp.lockstep(rep, prim, 0, 0, 3)[0]
    assert not rp.lockstep(alt, [{"chap": "Special"}] + prim[1:], 1, 1, 0)[0]
    assert rp.lockstep(alt, prim, 8, 8, 9)[0]
    ok, why = rp.lockstep(alt, prim + prim, 8, 8, 11)
    assert not ok and "the list ends" in why


def test_a_chain_fill_reads_as_placement():
    assert rp.tier_of("chain") == "placement"


def test_lists_are_tried_http_first_then_by_content_pairs_then_labels():
    recs = [_rec("plain", _chs(["1", "2"])),
            _rec("labelled", _chs(["1", "2"], titles={"1": "Episode 1"})),
            _rec("paired", _chs(["1", "2"])),
            _rec("comix", _chs(["1", "2"])),
            _rec("odd", [{"chap": "x", "url": f"o{i}", "title": "Special"} for i in range(3)])]
    cat = rp.build_catalog(recs, browser_sites={"comix"})
    pairs = {"paired": [rp.Pair(0, 0, "probe")], "comix": [rp.Pair(0, 0, "probe")]}
    got = rp.order_lists(cat, lambda a: pairs.get(a.key, []))
    assert [a.key for a in got] == ["paired", "labelled", "plain", "comix"]


def test_length_band():
    nb = [(50.0, 10), (52.0, 10), (48.0, 10)]
    assert rp.length_check(50.0, 10, nb)[0] is True
    assert rp.length_check(28.0, 10, nb)[0] is False     # the back half of a chapter
    assert rp.length_check(70.0, 0, nb)[0] is True       # no page count: loose band
    assert rp.length_check(130.0, 0, nb)[0] is False
    assert rp.length_check(50.0, 10, [])[0] is None


# ---------------------------------------------------------------- book buffer

def _fill(b, k, size=10, pages=0):
    b.add(k, {"chap": k}, [f"{k}.a", f"{k}.b"], size, f"g{k}", pages)


def test_unsplit_book_keeps_list_order_whatever_order_chapters_arrive_in():
    b = BookBuffer(preamble=["cover"], preamble_size=1)
    _fill(b, "0")
    b.hole("1", {"chap": "1"})
    _fill(b, "2")
    assert b.cut_parts() == []
    b.fill("1", {"chap": "1"}, ["1.a"], 5, "g1")
    (part,) = b.finish()
    assert part.content == ["cover", "0.a", "0.b", "1.a", "2.a", "2.b"]
    assert [c["chap"] for c in part.chapters] == ["0", "1", "2"]
    assert part.size == 26 and part.groups == {"g0", "g1", "g2"}


def test_a_hole_holds_back_split_parts_until_it_is_filled():
    b = BookBuffer(split_count=2)
    _fill(b, "0")
    b.hole("1", {"chap": "1"})
    _fill(b, "2")
    _fill(b, "3")
    assert b.cut_parts() == []                          # part 1 would need chapter 1
    b.fill("1", {"chap": "1"}, ["1.a"], 5)
    (p1,) = b.cut_parts()
    assert [c["chap"] for c in p1.chapters] == ["0", "1"]
    p2, = b.finish()
    assert [c["chap"] for c in p2.chapters] == ["2", "3"]


def test_split_by_size_matches_the_flat_list_rule_and_dropped_slots_are_skipped():
    b = BookBuffer(split_size=25, preamble=["cover"], preamble_size=1)
    _fill(b, "0")                                        # 1 + 10
    b.drop_new("1", {"chap": "1"})
    _fill(b, "2")                                        # 21
    _fill(b, "3")                                        # 31 > 25: closes before 3
    parts = b.cut_parts()
    assert [[c["chap"] for c in p.chapters] for p in parts] == [["0", "2"]]
    assert parts[0].content[0] == "cover"
    last = b.finish()
    assert [[c["chap"] for c in p.chapters] for p in last] == [["3"]]
    assert last[0].content[0] != "cover"


def test_cover_alone_never_closes_a_part():
    b = BookBuffer(split_size=5, preamble=["cover"], preamble_size=4)
    _fill(b, "0", size=10)
    assert b.cut_parts() == []
    (p,) = b.finish()
    assert p.content[0] == "cover" and len(p.chapters) == 1


def test_epub_markers_count_pages_from_each_parts_start():
    b = BookBuffer(split_count=2)
    _fill(b, "0", pages=3)
    _fill(b, "1", pages=0)                               # no EPUB page: no marker
    _fill(b, "2", pages=4)
    _fill(b, "3", pages=2)
    (p1,) = b.cut_parts()
    assert [(m["ch"]["chap"], m["page_index"]) for m in p1.markers] == [("0", 0)]
    (p2,) = b.finish()
    assert [(m["ch"]["chap"], m["page_index"]) for m in p2.markers] == [("2", 0), ("3", 4)]


def test_unfilled_holes_are_dropped_at_finish_and_cut_parts_are_final():
    b = BookBuffer(split_count=1)
    _fill(b, "0")
    _fill(b, "1")
    assert len(b.cut_parts()) == 1
    with pytest.raises(ValueError):
        b.fill("0", {}, [], 0)                           # not a hole
    with pytest.raises(KeyError):
        b.hole("1", {})                                  # one slot per chapter
    b.hole("2", {"chap": "2"})
    parts = b.finish()
    assert [[c["chap"] for c in p.chapters] for p in parts] == [["1"]]


# ---------------------------------------------------------------- fingerprint checks

W, H = 400, 20000


def _strip(seed):
    rng = np.random.default_rng(seed)
    small = rng.integers(0, 256, (H // 40, W // 40), dtype=np.uint8)
    im = Image.fromarray(small).resize((W, H), Image.BICUBIC)
    return im.filter(ImageFilter.GaussianBlur(3))


def _cp(img, slice_h, upto=None):
    cp = cf.ChapterPages(pages_total=(img.height + slice_h - 1) // slice_h)
    for i, y in enumerate(range(0, img.height, slice_h), 1):
        if upto and i > upto:
            break
        buf = io.BytesIO()
        img.crop((0, y, W, min(img.height, y + slice_h))).save(buf, "JPEG", quality=90)
        cp.pages[i] = cf.page_small(buf.getvalue())
    return cp


@pytest.fixture(scope="module")
def strips():
    return {s: _strip(s) for s in (1, 2, 3)}


def test_holds_other(strips):
    a, b = _cp(strips[1], 2000).fingerprint(), _cp(strips[2], 2000).fingerprint()
    same_other_scan = _cp(strips[1].resize((W, int(H * 1.03))), 1500).fingerprint()
    assert cf.holds_other(a, same_other_scan) and not cf.holds_other(a, b)
    half = _cp(strips[1].crop((0, H // 2, W, H)), 2000).fingerprint()
    assert cf.holds_other(half, a)                       # a slice of chapter 1


def test_middle_pages_widen_around_the_same_middle():
    assert cf.middle_pages(10, 4) == [4, 5, 6, 7]
    assert cf.middle_pages(10, 6) == [3, 4, 5, 6, 7, 8]
    assert cf.middle_pages(3, 4) == [1, 2, 3]


def test_a_probe_names_its_own_chapter_from_a_full_copy_or_a_probe(strips):
    def probe(img, slice_h):
        cp = _cp(img, slice_h)
        idx = cf.middle_pages(cp.pages_total, cf.PROBE_PAGES)
        return cp.fingerprint(idx)

    theirs = probe(strips[1], 1000)                      # an alt entry, sliced its way
    full_1, full_2 = _cp(strips[1], 2000).fingerprint(), _cp(strips[2], 2000).fingerprint()
    assert cf.probe_names(theirs, full_1, False)
    assert not cf.probe_names(theirs, full_2, False)
    # Our side only a probe too (a chapter the run has not reached): the two
    # middles overlap, so one lies inside the other.
    ours = probe(strips[1], 2000)
    assert cf.probe_names(theirs, ours, True)
    assert not cf.probe_names(theirs, probe(strips[2], 2000), True)


def test_fingerprint_writer_drain_waits_and_keeps_accepting(tmp_path, strips):
    buf = io.BytesIO()
    strips[3].crop((0, 0, W, 2000)).save(buf, "JPEG")
    w = cf.FingerprintWriter()
    for k in range(3):
        w.submit(str(tmp_path / f"{k}.npz"), [(1, buf.getvalue())], 1, {})
    w.drain()
    assert sorted(os.listdir(tmp_path)) == ["0.npz", "1.npz", "2.npz"]
    w.submit(str(tmp_path / "3.npz"), [(1, buf.getvalue())], 1, {})
    w.close()
    assert os.path.exists(tmp_path / "3.npz")
