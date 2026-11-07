"""sites/match_session.py end to end on stub sites serving synthetic pages.

The stub sites share one "scan" (synthetic manga pages from test_page_ranker's
generator) and differ the way real hosts do: a byte-identical mirror, a softened
re-encode, a colored edition, a CDN that serves nothing, a site whose list
numbers chapters one higher. The session must agree on target chapters, match
the same pages, tie the mirror exactly, rate the softened copy lower, call the
colored copy an alternate edition, mark the dead CDN unreachable, and order the
sources with search_orchestrator.source_rank_key.
"""
import io
import threading

import pytest
from PIL import Image, ImageFilter

from sites.base import BaseSiteHandler
from sites import match_session as ms
from sites.search_orchestrator import SourceEntry, sort_sources, _apply_ratings
from tests.test_page_ranker import _page

_PAGES = 12
_cache_lock = threading.Lock()
_page_cache = {}


def _scan_page(chap, idx):
    key = (chap, idx)
    with _cache_lock:
        if key not in _page_cache:
            _page_cache[key] = _page(seed=chap * 100 + idx, w=640, h=920)
        return _page_cache[key]


def _png(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


class StubSite(BaseSiteHandler):
    """chapters 1..n; `variant` decides what each page looks like."""

    def __init__(self, name, variant="orig", n=20, offset=0, fail=False):
        self.name = name
        self.variant = variant
        self.n = n
        self.offset = offset   # list number = real chapter + offset
        self.fail = fail

    def fetch_comic_context(self, url, scraper, make_request):
        return {"url": url}

    def get_chapters(self, context, scraper, language, make_request):
        return [{"chap": str(c + self.offset), "real": c, "url": f"{self.name}/{c}"} for c in range(1, self.n + 1)]

    def get_chapter_images(self, chapter, scraper, make_request):
        return [f"stub://{chapter['real']}/{i}" for i in range(_PAGES)]

    def _fetch_probe_item_bytes_ex(self, item, scraper):
        if self.fail:
            return None, False
        chap, idx = (int(x) for x in item[len("stub://"):].split("/"))
        im = _scan_page(chap, idx)
        if self.variant == "soft":
            im = im.filter(ImageFilter.GaussianBlur(1.6))
        elif self.variant == "color":
            rgb = im.convert("RGB")
            im = Image.merge("RGB", (rgb.getchannel(0), rgb.getchannel(1).point(lambda v: v * 0.6), rgb.getchannel(2)))
        return _png(im), False


def _entry(site, title=1.0, seed=0.8):
    return SourceEntry(site=site, url=f"https://{site}/s", title="T", cover=None,
                       title_match=title, seed_quality=seed)


def _run(handlers, depth=1):
    entries = [_entry(h.name) for h in handlers]
    members = [(e, None, h, False) for e, h in zip(entries, handlers)]
    sess = ms.MatchSession([(0, depth, members)], get_scraper=lambda e, h: None,
                           make_request=None, parallelism=4, deadline_s=120)
    sess.run()
    return sess, entries


@pytest.fixture(scope="module")
def five():
    hs = [StubSite("orig"), StubSite("mirror"), StubSite("soft", "soft"),
          StubSite("color", "color"), StubSite("dead", fail=True)]
    return _run(hs)


def test_verdicts(five):
    sess, _ = five
    v = sess.verdicts
    assert v["orig:https://orig/s"].status == "served"
    assert v["dead:https://dead/s"].status == "unreachable"
    assert v["dead:https://dead/s"].reason == "pages_failed"
    assert all(v[k].succeeded > 0 for k in v if not k.startswith("dead"))


def test_mirror_ties_softened_copy_rates_lower(five):
    sess, entries = five
    r = ms.rate_sources(sess.verdicts, sess.edges)
    o, m, s = r["orig:https://orig/s"], r["mirror:https://mirror/s"], r["soft:https://soft/s"]
    assert o.basis == m.basis == s.basis == "matched"
    assert o.q == pytest.approx(m.q, abs=1e-9)
    assert o.tie_group == m.tie_group == 0
    assert s.rating < o.rating - 0.1
    assert o.rating == pytest.approx(0.95, abs=0.01)


def test_colored_copy_is_an_alternate_edition(five):
    sess, _ = five
    assert sess.verdicts["color:https://color/s"].edition == "alternate"
    assert sess.verdicts["orig:https://orig/s"].edition == "original"


def test_rank_order(five):
    sess, entries = five
    _apply_ratings(entries, sess.verdicts, sess.edges)
    sort_sources(entries)
    order = [e.site for e in entries]
    # the tied pair first (coverage/seed equal -> site name), then the softer
    # copy, then the alternate edition, the dead CDN last
    assert order[:2] == ["mirror", "orig"]
    assert order[2:] == ["soft", "color", "dead"]
    assert entries[-1].img_quality_score == 0.0


def test_offset_numbering_is_found_by_the_neighbour_retry():
    # "late" numbers every chapter one higher: its chapter N is the others' N-1
    sess, _ = _run([StubSite("a"), StubSite("b"), StubSite("late", offset=1)])
    r = ms.rate_sources(sess.verdicts, sess.edges)
    assert r["late:https://late/s"].basis == "matched"
    assert r["late:https://late/s"].tie_group == r["a:https://a/s"].tie_group


def test_choose_targets_union_trim_and_spares():
    t, sp = ms.choose_targets([set(range(1, 51)), set(range(1, 40)), set(range(5, 51))], 3)
    assert t == [10, 22, 34]
    assert set(t).isdisjoint(sp) and 4 in sp and 47 in sp and 1 not in sp
    # a single list uses its own numbers
    assert ms.choose_targets([{1, 2, 3}], 3)[0] == [1, 2, 3]


def test_cache_roundtrip(tmp_path, monkeypatch, five):
    from sites.search_orchestrator import ImageQualityCache
    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path))
    sess, _ = five
    c = ImageQualityCache()
    c.put_records(list(sess.verdicts.values()), sess.edges)
    again = ImageQualityCache()
    recs = {}
    for k in sess.verdicts:
        site, url = k.split(":", 1)
        recs[k] = again.get_record(site, url)
    assert all(recs.values())
    assert recs["dead:https://dead/s"][0].status == "unreachable"   # D7: kept
    from sites.search_orchestrator import _edges_among
    r1 = ms.rate_sources(sess.verdicts, sess.edges)
    r2 = ms.rate_sources({k: v[0] for k, v in recs.items()}, _edges_among(recs))
    for k in r1:
        assert r1[k].basis == r2[k].basis
        if r1[k].q is not None:
            assert r1[k].q == pytest.approx(r2[k].q, abs=1e-9)
    # a pending verdict never replaces a fresh served one
    v = ms.SourceVerdict(key="orig:https://orig/s", status="pending")
    again.put_records([v], [])
    assert ImageQualityCache().get_record("orig", "https://orig/s")[0].status == "served"


# ---------------------------------------------------------------- failure paths


class _BrokenChapterSite(StubSite):
    """Every page of the chosen chapters fails; others serve normally."""

    def __init__(self, name, bad):
        super().__init__(name)
        self.bad = set(bad)

    def _fetch_probe_item_bytes_ex(self, item, scraper):
        chap = int(item[len("stub://"):].split("/")[0])
        if chap in self.bad:
            return None, False
        return super()._fetch_probe_item_bytes_ex(item, scraper)


def test_one_broken_chapter_does_not_make_a_site_unreachable():
    # depth 1: the only target fails; the session retries one spare chapter
    # (a peer fetches it too) before calling the site unreachable
    a, b = StubSite("a"), StubSite("b")
    t = ms.choose_targets([set(range(1, 21))] * 3, 1)[0]
    broken = _BrokenChapterSite("broken", bad=t)
    sess, _ = _run([a, b, broken])
    v = sess.verdicts["broken:https://broken/s"]
    assert v.status == "served", (v.status, v.reason, v.chapters)
    assert ms.rate_sources(sess.verdicts, sess.edges)["broken:https://broken/s"].basis == "matched"


def test_list_failures_are_cached_briefly():
    lf = ms.SourceVerdict(key="k", status="unreachable", reason="list_failed")
    pf = ms.SourceVerdict(key="k", status="unreachable", reason="pages_failed")
    assert ms.verdict_ttl(lf) == ms.TTL_PENDING_S
    assert ms.verdict_ttl(pf) == ms.TTL_UNREACHABLE_S


def test_a_comparator_crash_never_ends_the_session(monkeypatch):
    """An unexpected exception inside a comparison costs that edge, not the
    search, and the sources are NOT recorded as unmatched (that would stop
    them being re-probed for weeks)."""
    from sites import page_ranker as pr

    def boom(*a, **k):
        raise RuntimeError("decoder bug")
    monkeypatch.setattr(pr, "measure", boom)
    sess, _ = _run([StubSite("a"), StubSite("b", "soft")])   # different copies: compared
    assert sess.edges == []
    for v in sess.verdicts.values():
        assert v.status == "served" and v.unmatched_against == []


class _SlowSite(StubSite):
    def __init__(self, name, delay, budget=None, list_delay=0.0):
        super().__init__(name)
        self.delay = delay
        self.list_delay = list_delay
        if budget is not None:
            self.PROBE_SOURCE_BUDGET_S = budget

    def get_chapters(self, *a, **k):
        import time
        time.sleep(self.list_delay)
        return super().get_chapters(*a, **k)

    def get_chapter_images(self, *a, **k):
        import time
        time.sleep(self.delay)
        return super().get_chapter_images(*a, **k)


def test_per_source_budget_skips_a_slow_site_without_a_verdict_about_it():
    slow = _SlowSite("slow", delay=0.6, budget=0.3)
    sess, _ = _run([StubSite("a"), StubSite("b"), slow], depth=3)
    v = sess.verdicts["slow:https://slow/s"]
    assert v.truncated and v.unmatched_against == []
    assert v.status in ("served", "pending")       # never unreachable
    assert sess.verdicts["a:https://a/s"].succeeded >= 10


def test_a_list_arriving_after_the_freeze_is_still_compared(monkeypatch):
    """The freeze can come before a slow list; the early source's chapter
    completes with its pages unmatched. Their pixels must survive until every
    list is in, or the late source's pages match by thumbnail but can never
    be compared."""
    monkeypatch.setattr(ms, "FREEZE_WAIT_S", 0.05)
    late = _SlowSite("late", delay=0.0, list_delay=1.5)
    sess, _ = _run([StubSite("a"), late])
    assert any({e.a, e.b} == {"a:https://a/s", "late:https://late/s"} for e in sess.edges)


class _HungSite(StubSite):
    """Every chapter-image call blocks until released (a hung CDN fetch)."""

    def __init__(self, name, budget):
        super().__init__(name)
        self.PROBE_SOURCE_BUDGET_S = budget
        self.release = threading.Event()

    def get_chapter_images(self, *a, **k):
        self.release.wait(30)
        return super().get_chapter_images(*a, **k)


def test_a_hung_source_is_given_up_at_its_budget_not_the_phase_deadline():
    """Running tasks past the source budget stop being waited for (the old
    probe's join gate): the session ends with the others' results, long
    before its 120 s deadline, and the hung source is `pending`, never
    unreachable or unmatched."""
    import time
    hung = _HungSite("hung", budget=0.5)
    t0 = time.monotonic()
    try:
        sess, _ = _run([StubSite("a"), StubSite("b"), hung], depth=3)
    finally:
        hung.release.set()
    assert time.monotonic() - t0 < 30
    v = sess.verdicts["hung:https://hung/s"]
    assert v.status == "pending" and v.truncated and v.unmatched_against == []
    assert any({e.a, e.b} == {"a:https://a/s", "b:https://b/s"} for e in sess.edges)
    assert sess._outstanding == 0
    # the abandoned fetch time still counts toward the slow-site signal
    assert sess.by_key["hung:https://hung/s"].busy_s >= 0.3


def test_one_failed_chapter_and_a_cut_retry_is_pending_not_unreachable():
    """unreachable (a day's sink) needs every window to have RUN and failed;
    a first chapter whose pages fail plus a spare-chapter retry abandoned at
    the budget is slowness, not a dead CDN."""

    class _FailThenHang(StubSite):
        def __init__(self):
            super().__init__("flaky", fail=True)
            self.PROBE_SOURCE_BUDGET_S = 0.5
            self.release = threading.Event()
            self.calls = 0

        def get_chapter_images(self, *a, **k):
            self.calls += 1
            if self.calls > 1:
                self.release.wait(30)
            return super().get_chapter_images(*a, **k)

    flaky = _FailThenHang()
    try:
        sess, _ = _run([StubSite("a"), StubSite("b"), flaky], depth=1)
    finally:
        flaky.release.set()
    assert flaky.calls >= 2   # the first chapter, then a retry (spare or +-1 neighbour)
    v = sess.verdicts["flaky:https://flaky/s"]
    assert v.status == "pending" and v.reason == "pages_cut", (v.status, v.reason)


def test_a_window_with_any_timeout_is_no_dead_cdn_verdict():
    """One page failing while the rest time out is a throttled or slow CDN:
    pending, never the day-long unreachable sink."""

    class _Throttled(StubSite):
        def _fetch_probe_item_bytes_ex(self, item, scraper):
            idx = int(item.rsplit("/", 1)[1])
            return (None, idx % 2 == 0)   # half fail, half time out

    sess, _ = _run([StubSite("a"), StubSite("b"), _Throttled("thr")], depth=3)
    v = sess.verdicts["thr:https://thr/s"]
    assert v.status == "pending" and v.reason == "pages_timeout", (v.status, v.reason)


def test_queue_wait_does_not_spend_a_sources_budget():
    """The budget clock runs only while one of the source's own tasks runs.
    With one worker, the fast site's windows wait behind the slow one's for
    longer than the fast site's budget, yet every one of them still runs.
    Margins: the fast site's own decode/match work is ~1 s of budget clock on
    a dev machine (0.6 s used to flake), and it queues behind ~4 s of the
    slow site's tasks, so 2.5 s sits well clear of both."""
    slow = _SlowSite("slow", delay=1.2)
    fast = _SlowSite("fast", delay=0.0, budget=2.5)
    entries = [_entry(h.name) for h in (slow, fast)]
    sess = ms.MatchSession([(0, 3, [(e, None, h, False) for e, h in zip(entries, (slow, fast))])],
                           get_scraper=lambda e, h: None, make_request=None,
                           parallelism=1, deadline_s=120)
    sess.run()
    v = sess.verdicts["fast:https://fast/s"]
    assert not v.truncated and v.succeeded >= 3 * 5 - 1, (v.truncated, v.succeeded)


def test_two_dead_chapters_are_unreachable():
    """The dead-CDN rule still fires: every window ran, served nothing, had
    failures and no timeout, on the target and its retry chapter."""
    sess, _ = _run([StubSite("a"), StubSite("b"), StubSite("dead2", fail=True)], depth=1)
    v = sess.verdicts["dead2:https://dead2/s"]
    assert v.status == "unreachable" and v.reason == "pages_failed"


def test_daemon_map_keeps_order_context_and_default():
    import contextvars
    from sites.base import daemon_map
    var = contextvars.ContextVar("v", default="empty")
    var.set("caller")

    def fn(x):
        if x == 3:
            raise ValueError
        return (x, var.get(), threading.current_thread().daemon)

    out = daemon_map(fn, range(6), 3, "t", default="dflt")
    assert out[3] == "dflt"
    assert [o[0] for i, o in enumerate(out) if i != 3] == [0, 1, 2, 4, 5]
    assert all(o[1] == "caller" and o[2] for i, o in enumerate(out) if i != 3)


def test_an_abandoned_hung_page_fetch_does_not_hold_process_exit():
    """The session walks away from a hung window (its source's budget); the
    page fetches under it must be daemon threads, or interpreter exit joins
    them (concurrent.futures does, even after shutdown(wait=False)) and the
    search hangs after printing its result."""
    import os, subprocess, sys, time
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        "import threading, time\n"
        "from sites.base import daemon_map\n"
        "threading.Thread(target=lambda: daemon_map(lambda x: time.sleep(60), range(3), 3, 'p'),"
        " daemon=True).start()\n"
        "time.sleep(0.3)\n"
    )
    t0 = time.monotonic()
    subprocess.run([sys.executable, "-c", code], check=True, timeout=50, cwd=repo)
    assert time.monotonic() - t0 < 20


def test_each_sources_first_window_runs_before_any_second():
    """With one worker, the window tasks interleave by source: a slow site's
    queued windows cannot hold the pool while another site waits."""
    calls = []

    class Rec(StubSite):
        def get_chapter_images(self, chapter, *a, **k):
            calls.append(self.name)
            return super().get_chapter_images(chapter, *a, **k)

    hs = [Rec("a"), Rec("b"), Rec("c")]
    entries = [_entry(h.name) for h in hs]
    sess = ms.MatchSession([(0, 3, [(e, None, h, False) for e, h in zip(entries, hs)])],
                           get_scraper=lambda e, h: None, make_request=None,
                           parallelism=1, deadline_s=120)
    sess.run()
    assert sorted(calls[:3]) == ["a", "b", "c"]
    assert sorted(calls[3:6]) == ["a", "b", "c"]


# ---------------------------------------------------------------- orchestrator


def test_a_small_reprobe_keeps_the_cached_edition(tmp_path, monkeypatch):
    """A re-probe with fewer than 3 copy classes cannot vote on editions; the
    cached `alternate` must survive it (else a colored copy requalifies as a
    fallback for B&W downloads)."""
    from unittest.mock import MagicMock, patch
    import sites.search_orchestrator as so
    from sites.base import SearchHit
    from sites.search_orchestrator import ImageQualityCache, search_all

    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path))
    hs = {n: StubSite(n, v) for n, v in (("orig", "orig"), ("color", "color"))}
    for h in hs.values():
        h.domains = (f"{h.name}.example",)
        h.search = (lambda name: lambda *a, **k: [SearchHit(
            site=name, title="Series", url=f"https://{name}/s", cover=None, alt_titles=[],
            year=None, language=None, chapter_count_hint=20, raw_score=1.0)])(h.name)
    cache = ImageQualityCache()
    ko, kc = "orig:https://orig/s", "color:https://color/s"
    cache.put_records(
        [ms.SourceVerdict(key=ko, status="served", depth=3, edition="original", succeeded=9),
         ms.SourceVerdict(key=kc, status="served", depth=1, edition="alternate", succeeded=3)],
        [ms.PairEdge(ko, kc, 0.1, 3, {"sharp": 0.1, "res": 0.0, "blk": 0.0}, __import__("time").time())])
    monkeypatch.setattr(so, "_load_quality_seed", lambda: {n: 0.9 for n in hs})
    with patch("sites.iter_search_capable_handlers", lambda: iter(hs.values())), \
            patch("sites.get_handler_by_name", lambda n: hs.get(n)):
        cands = search_all("Series", MagicMock(return_value=None), MagicMock(), img_quality_cache=cache)
    color = next(s for s in cands[0].sources if s.site == "color")
    assert color.edition == "alternate"
    assert cache.get_record("color", "https://color/s")[0].edition == "alternate"
    assert cache.get_record("color", "https://color/s")[0].depth == ms.DEPTH_TOP   # it was re-probed


def test_same_file_peers_and_no_same_scan_across_editions(five):
    """The byte-identical mirror is "same file" as the original; the colored copy
    shares its gray line art (tone-matched PSNR ~69 dB on the stub) but is another
    edition, so it is never "same scan"."""
    sess, _ = five
    r = ms.rate_sources(sess.verdicts, sess.edges)
    o, m, c = r["orig:https://orig/s"], r["mirror:https://mirror/s"], r["color:https://color/s"]
    assert o.same_file == ["mirror:https://mirror/s"] and m.same_file == ["orig:https://orig/s"]
    # The softened copy is NOT asserted either way: its per-page tone PSNR against the
    # original spans ~13.4-19 dB around SAME_SCAN_DB (16), so which 4 pages the
    # threaded session happens to compare decides it (it flipped on Linux CI).
    ck = "color:https://color/s"
    assert ck not in o.same_scan and ck not in m.same_scan
    assert c.same_scan == [] and c.same_file == []
    md = ms.quality_metadata(sess.verdicts["orig:https://orig/s"], o)
    assert md["same_file"] == ["mirror"] and "color" not in md["same_scan"]


def _sv(key):
    return ms.SourceVerdict(key=key, status="served", edition="original", attempted=5, succeeded=5)


def _pe(a, b, psnr, same=False):
    return ms.PairEdge(a, b, 0.1, 3, {"sharp": 0.1, "res": 0.0, "blk": 0.0}, 0.0, same, psnr)


def test_same_scan_is_the_psnr_family_minus_same_file():
    """a~b re-encoded (30 dB), b~c re-encoded (25 dB) -> a, b, c one scan (transitive);
    d differs (12 dB, under SAME_SCAN_DB); e is b's identical mirror -> same file,
    and in a's family as same scan."""
    v = {k: _sv(k) for k in ("a:1", "b:1", "c:1", "d:1", "e:1")}
    edges = [_pe("a:1", "b:1", 30.0), _pe("b:1", "c:1", 25.0), _pe("a:1", "d:1", 12.0),
             _pe("b:1", "e:1", None, same=True)]
    r = ms.rate_sources(v, edges)
    assert r["a:1"].same_scan == ["b:1", "c:1", "e:1"] and r["a:1"].same_file == []
    assert r["b:1"].same_file == ["e:1"] and r["b:1"].same_scan == ["a:1", "c:1"]
    assert r["d:1"].same_scan == [] and r["d:1"].same_file == []


def test_median_edge_keeps_psnr_positive_and_out_of_the_score():
    """_psnr rides in the per-page parts but is no score term: median_edge takes
    its own median, and the score is the sharp+res+blk sum only."""
    pages = [{"sharp": 0.1, "res": 0.0, "blk": 0.0, "_psnr": 20.0},
             {"sharp": 0.3, "res": 0.0, "blk": 0.0, "_psnr": 30.0},
             {"sharp": 0.2, "res": 0.0, "blk": 0.0, "_psnr": 40.0}]
    e = ms.median_edge("a:1", "b:1", pages, 0.0)
    assert e.psnr == 30.0 and e.score == pytest.approx(0.2) and set(e.parts) == {"sharp", "res", "blk"}
    assert e.flipped().psnr == 30.0


def test_same_file_never_crosses_editions():
    """Live Chainsaw Man (2026-10-21): mangakatana mixes colored and B&W chapters,
    served byte-identical pages to weebcentral on the compared chapters, and was
    still voted the alternate edition. The note must not call it the same file."""
    v = {k: _sv(k) for k in ("wc:1", "mp:1", "mk:1")}
    v["mk:1"].edition = "alternate"
    edges = [_pe("wc:1", "mp:1", None, same=True), _pe("wc:1", "mk:1", None, same=True)]
    r = ms.rate_sources(v, edges)
    assert r["wc:1"].same_file == ["mp:1"] and r["mp:1"].same_file == ["wc:1"]
    assert r["mk:1"].same_file == [] and r["mk:1"].same_scan == []
