"""Webtoon support (sites/strip_match.py + its use in sites/match_session.py) on
synthetic strips.

Each chapter is one long strip (manga-like synthetic pages from test_page_ranker
stacked vertically, so art runs across any cut that lands inside a page). Stub sites
slice that strip the way real hosts do: 1,280-px slices, 3,000-px slices, tall
6,000-px slices, a softened copy, a site whose strip is a different chapter's. The
session must call the candidate a webtoon, locate the differently sliced copies,
compare them on bands, rate the softened copy lower, and leave a different-content
source unmatched with a strip reason. Manga stubs (tests/test_match_session.py) must
never take this path.
"""
import io
import threading

import numpy as np
import pytest
from PIL import Image, ImageFilter

from sites import match_session as ms
from sites import page_ranker as pr
from sites import strip_match as sm
from sites.base import BaseSiteHandler
from tests.test_match_session import _entry
from tests.test_page_ranker import _page

_W = 720
_lock = threading.Lock()
_strips = {}


def _strip(chap):
    """Chapter `chap` as one 720 x 8,000 strip."""
    with _lock:
        if chap not in _strips:
            parts = [_page(seed=chap * 1000 + i, w=_W, h=1000) for i in range(8)]
            im = Image.new("L", (_W, 8000))
            for i, p in enumerate(parts):
                im.paste(p, (0, i * 1000))
            _strips[chap] = im
        return _strips[chap]


def _png(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


class StripSite(BaseSiteHandler):
    """Chapters 1..n of one webtoon, sliced every `step` px. `content` maps the
    served chapter to the strip it shows (a site whose chapter N holds another
    chapter's content); `titles` gives each chapter an "Ep. K" title."""

    def __init__(self, name, step, variant="orig", n=12, content=None, titles=None):
        self.name, self.step, self.variant, self.n = name, step, variant, n
        self.content = content or (lambda c: c)
        self.titles = titles

    def fetch_comic_context(self, url, scraper, make_request):
        return {"url": url}

    def get_chapters(self, context, scraper, language, make_request):
        out = []
        for c in range(1, self.n + 1):
            ch = {"chap": str(c), "real": self.content(c), "url": f"{self.name}/{c}"}
            if self.titles:
                ch["title"] = self.titles(c)
            out.append(ch)
        return out

    def get_chapter_images(self, chapter, scraper, make_request):
        n = -(-8000 // self.step)
        return [f"strip://{chapter['real']}/{i}" for i in range(n)]

    def _fetch_probe_item_bytes_ex(self, item, scraper):
        chap, idx = (int(x) for x in item[len("strip://"):].split("/"))
        im = _strip(chap).crop((0, idx * self.step, _W, min(8000, (idx + 1) * self.step)))
        if self.variant == "soft":
            im = im.filter(ImageFilter.GaussianBlur(1.6))
        return _png(im), False


def _run(handlers, depth=1):
    entries = [_entry(h.name) for h in handlers]
    members = [(e, None, h, False) for e, h in zip(entries, handlers)]
    sess = ms.MatchSession([(0, depth, members)], get_scraper=lambda e, h: None,
                           make_request=None, parallelism=4, deadline_s=180)
    sess.run()
    return sess


def _k(name):
    return f"{name}:https://{name}/s"


@pytest.fixture(scope="module")
def webtoon():
    return _run([StripSite("fine", 1280), StripSite("coarse", 3000), StripSite("tall", 6000),
                 StripSite("soft", 3000, "soft"),
                 StripSite("other", 1280, content=lambda c: c + 50)])


def test_the_candidate_is_called_a_webtoon(webtoon):
    assert webtoon.cands[0].webtoon


def test_differently_sliced_copies_are_compared_on_bands(webtoon):
    r = ms.rate_sources(webtoon.verdicts, webtoon.edges)
    for name in ("fine", "coarse", "tall", "soft"):
        assert r[_k(name)].basis == "matched", name
    # same pixels sliced differently: a tie, never a winner
    assert r[_k("fine")].tie_group == r[_k("coarse")].tie_group == r[_k("tall")].tie_group
    assert r[_k("soft")].rating < r[_k("fine")].rating - 0.1
    assert any(cl.kind == "band" for cls in webtoon.cands[0].clusters.values() for cl in cls)


def test_a_different_chapter_stays_unmatched_with_a_strip_reason(webtoon):
    v = webtoon.verdicts[_k("other")]
    assert v.status == "served"
    assert v.reason == "strip_unaligned"
    assert v.unmatched_against
    assert ms.verdict_ttl(v) == ms.TTL_TRUNCATED_S


@pytest.mark.parametrize("broken", ["locate", "cut_band"])
def test_a_failing_strip_step_leaves_the_pair_unrun(monkeypatch, broken):
    """An exception in the strip path is not a verdict: the session finishes and
    neither source is cached as unmatched (it would hide them for a week)."""
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(sm, broken, boom)
    sess = _run([StripSite("fine", 1280), StripSite("coarse", 3000)])
    assert sess.cands[0].webtoon
    for name in ("fine", "coarse"):
        v = sess.verdicts[_k(name)]
        assert v.status == "served" and not v.unmatched_against, (name, v.reason)


def test_tall_slices_never_hold_full_size_arrays():
    blob = _png(_strip(1).crop((0, 0, _W, 6000)))
    c = pr.prepare(blob)
    assert c.tall and c.gray is None and c.thumb is None
    assert c.small is not None and c.small.shape[1] == pr.SMALL_W
    assert c.width == _W and c.height == 6000


def test_tall_page_comparisons_are_never_queued():
    a = pr.prepare(_png(_strip(1).crop((0, 0, _W, 6000))))
    b = pr.prepare(_png(_page(3)))
    assert not ms._registrable(a, b)
    # a trimmed 700 x 630 slice: a 3 x 2 tile grid can never keep 8 tiles
    small = pr.prepare(_png(_page(4, w=700, h=630)))
    assert not ms._registrable(small, small)
    assert ms._registrable(b, b)


def test_strip_profiles_do_not_drift_with_page_count():
    """Many short pages vs few long ones over one strip: the located offset must hold
    (per-page rounding drifted ~5 profile rows per 600 and hid real matches)."""
    class Pg:
        def __init__(self, idx, copy):
            self.idx, self.copy = idx, copy
    strip = _strip(2)
    fine = [Pg(i, pr.prepare(_png(strip.crop((0, y, _W, y + 333))))) for i, y in enumerate(range(0, 7992, 333))]
    coarse = [Pg(i, pr.prepare(_png(strip.crop((0, y, _W, min(8000, y + 2900)))))) for i, y in enumerate(range(0, 8000, 2900))]
    a = sm.build_window("a", 2, 2, fine)
    b = sm.build_window("b", 2, 2, coarse)
    loc = sm.locate(a, b)
    assert loc.accepted and loc.peak > 0.95
    assert abs(loc.off) <= 1 and loc.k == 1.0


def test_a_different_strip_is_not_located():
    class Pg:
        def __init__(self, idx, copy):
            self.idx, self.copy = idx, copy
    a = sm.build_window("a", 1, 1, [Pg(i, pr.prepare(_png(_strip(1).crop((0, y, _W, y + 2000)))))
                                     for i, y in enumerate(range(0, 8000, 2000))])
    b = sm.build_window("b", 1, 1, [Pg(i, pr.prepare(_png(_strip(9).crop((0, y, _W, y + 2000)))))
                                     for i, y in enumerate(range(0, 8000, 2000))])
    assert not sm.locate(a, b).accepted


def test_webtoon_call_rules():
    # one tall credits page among manga pages is not a webtoon; a 30% share is
    assert not sm.is_webtoon(30, 1, 0, 20)
    assert sm.is_webtoon(10, 3, 0, 0)
    # the call is made after each window and sticks: a first window of one 3-page
    # chapter holding a tall credits page must not decide it
    assert not sm.is_webtoon(3, 1, 0, 0)
    assert not sm.is_webtoon(5, 0, 2, 2)
    # one stray continuing cut is not; two making up 40% of informative cuts are
    assert not sm.is_webtoon(30, 0, 1, 20)
    assert not sm.is_webtoon(30, 0, 2, 20)
    assert sm.is_webtoon(15, 0, 2, 5)


@pytest.mark.parametrize("title,ep", [
    ("Ep. 164", 164), ("[S2] Ep. 164", 164), ("Episode 53", 53),
    ("Chapter 244: [S2] Ep. 164", 164), ("Chapter 54", None), ("Epilogue", None), (None, None),
])
def test_episode_number(title, ep):
    assert sm.episode_number(title) == ep


def test_episode_labels_pick_the_chapter_holding_the_slot():
    """'late' numbers every chapter two higher than the others (Tower of God: atsu
    243 = LINE 245) but titles carry the same episode labels: the label pairing
    fetches the right chapter at once, with no neighbour retry."""
    lab = lambda c: f"Ep. {c}"
    sess = _run([StripSite("a", 1280, titles=lab), StripSite("b", 3000, titles=lab),
                 StripSite("late", 1280, content=lambda c: c - 2, titles=lambda c: f"[S2] Ep. {c - 2}")])
    late = sess.by_key[_k("late")]
    assert all(ch == slot + 2 for ch, slot in late.assigned)
    r = ms.rate_sources(sess.verdicts, sess.edges)
    assert r[_k("late")].basis == "matched"
