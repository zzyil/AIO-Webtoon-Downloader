"""Offline coverage for comix's chapter-page image capture.

Two regressions are pinned here, both from the same live failure (Spark in Your
Eyes ch.104, 2026-08-02: "67/68 pages captured ... 1 pages failed to render").

  1. A page that renders LATE used to be lost. The old loop walked pages in
     order and gave each a private 10s window; once it moved past page 62 it
     never looked again, even though the scrape ran on for six more pages.
     `_converge` re-polls every unresolved page each round instead.
  2. A short capture used to be INVISIBLE. aio-dl.py's zero-tolerance gate reads
     pages_total off len() of what the handler returns, so 67 URLs looked like
     67/67 complete and the CBZ was saved a page short (pages get renumbered on
     the way in, so there wasn't even a visible gap). get_chapter_images now
     raises IncompleteChapterError, the same signal sites/mangadex.py uses.

Everything runs against a scripted stand-in for the Patchright page — no
browser, no network. Cross-file: sites/comix.py (_ComixBrowserSession
.fetch_chapter_images_via_dom, ComixSiteHandler.get_chapter_images),
sites/base.py (IncompleteChapterError).
"""

import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sites import comix  # noqa: E402
from sites.base import IncompleteChapterError  # noqa: E402


# A byte string is all the canvas path needs — it only b64-decodes and caches.
# Padded past comix._MIN_CANVAS_BYTES: a real painted page encodes to tens of KB,
# and the harvester now rejects anything tiny as an unpainted (blank) canvas
# rather than archiving a dead page. A 12-byte stub would trip that floor.
_CANVAS_BYTES = b"RIFF____WEBP" + b"\x00" * 4096
_DATA_URL = "data:image/webp;base64," + base64.b64encode(_CANVAS_BYTES).decode()

# Shapes a scripted page can present. "canvas+img" is the race candidate the
# capture-shapes summary exists to count: a canvas AND a fully decoded <img>.
# "img-nosrc" is the INTERMEDIATE state a page passes through while it resolves —
# <img> mounted, src not set yet — proved live on 2026-08-03 by a snapshot that
# caught page 100 in exactly it. Not capturable, but it IS progress.
IMG = "img"
CANVAS = "canvas"
CANVAS_AND_IMG = "canvas+img"
IMG_NO_SRC = "img-nosrc"
LOADING = "loading"


class _ScriptedReader:
    """Patchright page stand-in that reports a scripted reader DOM.

    ``ready`` maps page number -> (poll round it becomes ready, shape). Pages
    left out never become ready, which is how the "never rendered" cases are
    built. Dispatch is on distinctive substrings of the JS the session sends,
    so the tests stay coupled to behaviour rather than to exact source text.
    """

    def __init__(
        self,
        page_count,
        ready,
        url="https://comix.to/1234-chapter-1",
        pending_shape=None,
        canvas_unmounted=(),
        canvas_needs_scroll=(),
    ):
        self.page_count = page_count
        self.ready = dict(ready)
        # page -> shape shown BEFORE its due round (default: nothing rendered).
        # Lets a test script the is-loading / img-without-src rungs a real page
        # climbs through, which is what the stall clock has to see as progress.
        self.pending_shape = dict(pending_shape or {})
        # Pages whose <canvas> ALWAYS reports itself unmounted when read.
        self.canvas_unmounted = set(canvas_unmounted)
        # Pages whose <canvas> is unmounted until the page is scrolled to —
        # the real reader's behaviour, and what silently lost 6 of 10 scrambled
        # pages live before the harvest learned to scroll back.
        self.canvas_needs_scroll = set(canvas_needs_scroll)
        self.poll_rounds = 0
        self.scrolls = []
        self.scroll_batches = []
        self.reloads = 0
        self.canvas_reads = []
        self.preload_writes = []
        self._url = url

    @property
    def url(self):
        return self._url

    def goto(self, url, **_kw):
        self._url = url

    def reload(self, **_kw):
        self.reloads += 1

    def wait_for_timeout(self, _ms):
        return None

    def _state(self, n):
        entry = self.ready.get(n)
        shape = None
        if entry is not None:
            due, shape = entry
            if self.poll_rounds < due:
                # Before its due round a page may still be scripted to show an
                # intermediate shape, so "not ready yet" is not automatically
                # "nothing at all" — see `pending_shape`.
                shape = self.pending_shape.get(n)
        base = {
            "n": n, "loading": False, "hasCanvas": False, "cw": 0, "ch": 0,
            "hasImg": False, "src": "", "complete": False, "nw": 0,
        }
        if shape == LOADING:
            base.update(loading=True)
        if shape == IMG_NO_SRC:
            base.update(hasImg=True)
        if shape in (CANVAS, CANVAS_AND_IMG):
            base.update(hasCanvas=True, cw=800, ch=1200)
        if shape in (IMG, CANVAS_AND_IMG):
            base.update(
                hasImg=True,
                src=f"https://cdn.comix.to/p/{n:04d}.webp",
                complete=True,
                nw=800,
            )
        return base

    def evaluate(self, js, arg=None):
        if "querySelectorAll('.rpage-page').length" in js:
            return self.page_count
        if "nums.map" in js:
            states = [self._state(int(n)) for n in (arg or [])]
            self.poll_rounds += 1
            return states
        if "scrollIntoView" in js:
            # ONE page per call — an IntersectionObserver only delivers
            # observations once per frame, so a multi-page scroll would visit
            # just its last page. `scroll_batches` records the call shape so a
            # test can pin that nothing regresses to batching.
            nums = [arg] if isinstance(arg, int) else list(arg or [])
            self.scrolls.extend(nums)
            self.scroll_batches.append(nums)
            return True
        if "toDataURL" in js:
            # The reader returns {data, why} now, not a bare data URL — a null
            # used to collapse "canvas unmounted", "canvas 0x0" and a tainted
            # SecurityError into one indistinguishable failure.
            self.canvas_reads.append(arg)
            if arg in self.canvas_unmounted:
                return {"data": None, "why": "canvas unmounted"}
            # Models the real reader: a canvas is torn down once it is far from
            # the viewport and only comes back after you scroll to it.
            if arg in self.canvas_needs_scroll and arg not in self.scrolls:
                return {"data": None, "why": "canvas unmounted"}
            return {"data": _DATA_URL, "why": ""}
        if "reader.webtoon.v3" in js:
            self.preload_writes.append(arg)
            return arg
        return {}


def _session(page):
    """A _ComixBrowserSession wired to a scripted page, with the browser
    lifecycle and WAF guard stubbed out (both are covered elsewhere)."""
    sess = comix._ComixBrowserSession.__new__(comix._ComixBrowserSession)
    sess._page = page
    sess._scrambled_urls = set()
    sess._start = lambda *a, **k: True
    sess._sync_cf_cookies = lambda *a, **k: None
    sess._enforce_no_waf = lambda *a, **k: None
    sess._apply_reader_preload_pref = lambda *a, **k: True
    return sess


@pytest.fixture(autouse=True)
def _fast_stall(monkeypatch):
    """The stall bound is wall-clock. Shrink it so the "never renders" cases
    don't sit for the production 20s."""
    monkeypatch.setattr(comix, "_COMIX_CAPTURE_STALL_S", 0.05)
    monkeypatch.setattr(comix, "_COMIX_PAGE_POLL_INTERVAL_S", 0.0)


# --------------------------------------------------------------- convergence

def test_a_fully_loaded_chapter_converges_in_a_single_poll():
    """The round-trip win: with preload=all the first batched poll harvests the
    whole chapter. The old loop paid a scroll + a poll PER PAGE (>=136 evaluates
    for 68 pages); this must be one."""
    page = _ScriptedReader(5, {n: (0, IMG) for n in range(1, 6)})
    cap = _session(page).fetch_chapter_images_via_dom("https://comix.to/1-chapter-1")

    assert len(cap.urls) == 5
    assert cap.expected_pages == 5
    assert not cap.unresolved
    assert page.poll_rounds == 1
    assert page.scrolls == []   # nothing to nudge
    assert page.reloads == 0


def test_a_late_page_is_recovered_rather_than_dropped():
    """THE page-62 regression. Page 4 only renders on the fourth round; the old
    per-page window would have abandoned it while the walk continued. Progress
    on other pages keeps the stall clock alive, so it still lands — and without
    needing the reload retry."""
    ready = {n: (0, IMG) for n in (1, 2, 3, 5)}
    ready[4] = (3, IMG)
    page = _ScriptedReader(5, ready)
    cap = _session(page).fetch_chapter_images_via_dom("https://comix.to/1-chapter-1")

    assert len(cap.urls) == 5
    assert not cap.unresolved
    assert page.reloads == 0
    assert page.poll_rounds >= 4


def test_pages_are_returned_in_page_order_not_resolution_order():
    """Convergence resolves pages in whatever order the browser finishes them;
    the CBZ needs them in page order. The old append-in-order was only correct
    because the walk was sequential."""
    ready = {1: (3, IMG), 2: (1, IMG), 3: (0, IMG), 4: (2, IMG)}
    page = _ScriptedReader(4, ready)
    cap = _session(page).fetch_chapter_images_via_dom("https://comix.to/1-chapter-1")

    assert cap.urls == [
        "https://cdn.comix.to/p/0001.webp",
        "https://cdn.comix.to/p/0002.webp",
        "https://cdn.comix.to/p/0003.webp",
        "https://cdn.comix.to/p/0004.webp",
    ]


def test_every_pending_page_is_swept_not_just_the_lowest():
    """THE every-10th-page regression (2026-08-03).

    comix defers each chunk-boundary page until the viewport reaches it, so a
    page that is never scrolled to can never load. The old loop scrolled ONLY
    `pending[0]`, which meant that once the bulk resolved and pending was
    [10, 20, ... 110] it re-scrolled to page 10 forever while 20..110 were never
    visited even once — and those are exactly the pages that then failed. Every
    pending page must be reached.
    """
    # 1-2 resolve immediately; 20/40/60 never do, standing in for boundaries.
    ready = {1: (0, IMG), 2: (0, IMG)}
    page = _ScriptedReader(60, ready)
    _session(page).fetch_chapter_images_via_dom("https://comix.to/1-chapter-1")

    for boundary in (20, 40, 60):
        assert boundary in page.scrolls, (
            f"page {boundary} was never scrolled into view — this is the "
            f"pending[0]-only bug that stranded every 10th page"
        )


def test_the_sweep_scrolls_one_page_per_round():
    """An IntersectionObserver delivers observations once per FRAME, after the JS
    turn ends — so scrolling several positions inside one evaluate produces a
    single observation at the last position. Batching the sweep would look like
    it visited every pending page while actually visiting one per batch, which is
    the original bug wearing a disguise. One scroll per round, each getting its
    own frame via the poll interval."""
    page = _ScriptedReader(40, {1: (0, IMG)})
    _session(page).fetch_chapter_images_via_dom("https://comix.to/1-chapter-1")

    assert page.scroll_batches, "the sweep never ran"
    assert all(len(b) == 1 for b in page.scroll_batches), (
        "a multi-page scroll batch only ever visits its last page"
    )


def test_a_stalled_page_is_re_approached_from_further_up():
    """A3. An IntersectionObserver fires on a TRANSITION, so re-scrolling a page
    already at the scroll position does nothing — which is why the old repeated
    nudge never recovered anything. The re-nudge must back off far enough to push
    the target out of the viewport, then return to it."""
    page = _ScriptedReader(20, {n: (0, IMG) for n in range(1, 20)})
    _session(page).fetch_chapter_images_via_dom("https://comix.to/1-chapter-1")

    # Page 20 is the only straggler. The backoff scroll must precede the return.
    backoff = 20 - comix._COMIX_RENUDGE_BACKOFF_PAGES
    assert backoff in page.scrolls, "no back-off scroll — no intersection edge"
    assert page.scrolls.index(backoff) < len(page.scrolls) - 1


def test_intermediate_progress_keeps_the_stall_clock_alive():
    """A6. `<img>` mounted but src not yet set is the state a RESOLVING page
    passes through (proved live: page 100 was caught in exactly it). If only
    terminal states counted as progress, a chapter whose boundaries were all
    mid-transition would look idle and trip the 20s stall break while the reader
    was still working."""
    # Page 3 sits in the intermediate state for several rounds, then lands. The
    # stall bound is 0.05s here, so it can only survive if that counts as
    # progress.
    ready = {1: (0, IMG), 2: (0, IMG), 3: (6, IMG)}
    page = _ScriptedReader(3, ready, pending_shape={3: IMG_NO_SRC})
    cap = _session(page).fetch_chapter_images_via_dom("https://comix.to/1-chapter-1")

    assert len(cap.urls) == 3, "a page that was visibly progressing was dropped"
    assert not cap.unresolved


def test_the_recovery_ladder_is_bounded_and_ends():
    """Each rung changes something MATERIAL about how the reader is driven
    (re-nudge, reload, lazy-mode) — a plain repeat would re-fail identically,
    which is exactly what the live log showed. But it must terminate: at most two
    reloads, then report short rather than looping."""
    page = _ScriptedReader(3, {1: (0, IMG), 2: (0, IMG)})
    cap = _session(page).fetch_chapter_images_via_dom("https://comix.to/1-chapter-1")

    assert page.reloads <= 2, "the ladder must not loop"
    assert len(cap.urls) == 2
    assert list(cap.unresolved) == [3]


def test_the_lazy_mode_rung_restores_eager_preload_afterwards():
    """preload is persisted per-origin in localStorage, so leaving the last rung's
    `some` in place would silently make EVERY later chapter take the slow path."""
    page = _ScriptedReader(3, {1: (0, IMG), 2: (0, IMG)})
    sess = comix._ComixBrowserSession.__new__(comix._ComixBrowserSession)
    sess._page = page
    sess._scrambled_urls = set()
    sess._start = lambda *a, **k: True
    sess._sync_cf_cookies = lambda *a, **k: None
    sess._enforce_no_waf = lambda *a, **k: None
    sess.fetch_chapter_images_via_dom("https://comix.to/1-chapter-1")

    assert "some" in page.preload_writes, "the lazy-mode rung never ran"
    assert page.preload_writes[-1] == "all", (
        "preload was left on 'some' — every later chapter would crawl"
    )


def test_the_unresolved_report_carries_the_last_observed_dom_state():
    """A bare page number can't tell "never rendered" from "src was set but
    hadn't decoded" — which is the distinction you need to fix a capture miss
    without a repro."""
    page = _ScriptedReader(2, {1: (0, IMG)})
    cap = _session(page).fetch_chapter_images_via_dom("https://comix.to/1-chapter-1")

    state = cap.unresolved[2]
    assert state["n"] == 2
    assert state["src"] == ""
    assert state["hasCanvas"] is False


# ------------------------------------------------------------- canvas + shapes

def test_canvas_pages_are_captured_under_a_synthetic_key():
    page = _ScriptedReader(2, {1: (0, CANVAS), 2: (0, IMG)})
    cap = _session(page).fetch_chapter_images_via_dom("https://comix.to/1234-chapter-1")

    assert cap.urls[0] == "comix-page://1234/0001.webp"
    assert cap.urls[1] == "https://cdn.comix.to/p/0002.webp"
    assert cap.canvas_pages == 1
    assert cap.img_pages == 1
    assert page.canvas_reads == [1]


def test_canvas_pages_that_also_had_a_decoded_img_are_counted():
    """The measurement that decides whether the canvas branch still earns its
    keep. canvas-before-img is UNCHANGED here — a page presenting both still
    resolves as canvas — but now we count how often that happened."""
    page = _ScriptedReader(3, {1: (0, CANVAS_AND_IMG), 2: (0, CANVAS), 3: (0, IMG)})
    cap = _session(page).fetch_chapter_images_via_dom("https://comix.to/1-chapter-1")

    assert cap.canvas_pages == 2
    assert cap.canvas_with_img == 1
    assert cap.urls[0].startswith("comix-page://"), "canvas still wins the race"


def test_a_canvas_is_read_inline_the_round_it_is_detected():
    """THE speed fix. comix still tile-scrambles roughly every 10th page
    (confirmed live: scramble-headered responses == canvas page count) and the
    reader UNMOUNTS a canvas once the sweep moves past it. Harvesting as a batch
    AFTER convergence therefore found ~half of them already gone and pushed each
    through all three recovery rungs — 12s of capture became 31s, and 81s on a
    bad chapter. A canvas is mounted and painted at the instant the poll reports
    it (`!loading`, non-zero dimensions), so it must be read THERE: no scroll, no
    second round trip, nothing that lets it be torn down first."""
    page = _ScriptedReader(3, {1: (0, IMG), 2: (0, CANVAS), 3: (0, IMG)})
    cap = _session(page).fetch_chapter_images_via_dom("https://comix.to/1234-chapter-1")

    assert cap.urls[1] == "comix-page://1234/0002.webp"
    assert page.scrolls == [], (
        "a healthy canvas page must cost no scroll at all — the inline read is "
        "the whole speed win"
    )
    assert not cap.unresolved


def test_an_unmounted_canvas_still_falls_back_to_the_scroll_recovery():
    """The inline read is an optimisation, not a replacement. When it misses —
    the canvas really was torn down before we got there — the batch harvest must
    still scroll back to the page and recover it, which is what turns a dropped
    page into a captured one."""
    page = _ScriptedReader(
        3, {1: (0, IMG), 2: (0, CANVAS), 3: (0, IMG)},
        canvas_needs_scroll={2},
    )
    cap = _session(page).fetch_chapter_images_via_dom("https://comix.to/1234-chapter-1")

    assert 2 in page.scrolls, "the recovery never scrolled back to the canvas"
    assert cap.urls[1] == "comix-page://1234/0002.webp"
    assert not cap.unresolved


def test_an_unmounted_canvas_is_reported_not_silently_dropped(capsys):
    """The read returned null and the page vanished with no log line at all —
    six pages disappeared per chapter with nothing to diagnose from. A failure
    must name its reason."""
    page = _ScriptedReader(
        2, {1: (0, IMG), 2: (0, CANVAS)}, canvas_unmounted={2},
    )
    cap = _session(page).fetch_chapter_images_via_dom("https://comix.to/1234-chapter-1")

    assert list(cap.unresolved) == [2]
    err = capsys.readouterr().err
    assert "canvas unmounted" in err, "the failure reason must reach the log"


def test_an_unpainted_canvas_is_not_archived_as_a_blank_page(monkeypatch):
    """A canvas that exists but hasn't been painted yet encodes to a few hundred
    bytes of uniform transparency, and toDataURL SUCCEEDS on it. Accepting that
    writes a dead page into the CBZ while reporting success — the worst outcome
    available. Below the floor it must count as unresolved instead."""
    monkeypatch.setattr(comix, "_MIN_CANVAS_BYTES", 10_000)  # our stub is ~4KB
    page = _ScriptedReader(2, {1: (0, IMG), 2: (0, CANVAS)})
    cap = _session(page).fetch_chapter_images_via_dom("https://comix.to/1234-chapter-1")

    assert list(cap.unresolved) == [2]
    assert all(not u.startswith("comix-page://") for u in cap.urls)


def test_scramble_headers_are_recorded_off_the_response_listener():
    """The authoritative scramble signal is the RESPONSE header, not DOM shape.
    Recorded only — nothing branches on it yet."""
    sess = _session(_ScriptedReader(1, {}))
    sess._note_scrambled_url("https://cdn.comix.to/si/a.webp")
    sess._note_scrambled_url("https://cdn.comix.to/si/a.webp")
    assert sess._scrambled_urls == {"https://cdn.comix.to/si/a.webp"}


def test_scramble_memory_is_bounded():
    """The session is one browser for every chapter of every run — exactly the
    shape that leaks."""
    sess = _session(_ScriptedReader(1, {}))
    for i in range(comix._ComixBrowserSession._SCRAMBLED_URL_MEMORY + 50):
        sess._note_scrambled_url(f"https://cdn.comix.to/si/{i}.webp")
    assert (
        len(sess._scrambled_urls)
        == comix._ComixBrowserSession._SCRAMBLED_URL_MEMORY
    )


# ------------------------------------------------- probe cap (must stay exempt)

def test_the_probe_cap_shrinks_expected_pages_to_match():
    """A deliberately capped probe render must never look like a truncated
    chapter, or every comix search would raise."""
    page = _ScriptedReader(70, {n: (0, IMG) for n in range(1, 71)})
    cap = _session(page).fetch_chapter_images_via_dom(
        "https://comix.to/1-chapter-1", max_capture_pages=8,
    )

    assert len(cap.urls) == 8
    assert cap.expected_pages == 8, "capped render is complete BY DEFINITION"


def test_the_probe_never_pays_for_the_reload_retry():
    """A probe samples representative pages and already tolerates a partial
    capture, so completeness buys it nothing — and a reload plus a second
    convergence would spend its whole 60s budget chasing pages it was never
    going to score."""
    page = _ScriptedReader(70, {1: (0, IMG), 2: (0, IMG)})
    cap = _session(page).fetch_chapter_images_via_dom(
        "https://comix.to/1-chapter-1", max_capture_pages=4,
    )

    assert page.reloads == 0
    assert len(cap.urls) == 2


def test_the_download_path_still_runs_the_ladder_for_the_same_shortfall():
    """Same scripted DOM as the probe case above; only the cap differs. Pins
    that the exemption is keyed on the probe, not on the shortfall."""
    page = _ScriptedReader(4, {1: (0, IMG), 2: (0, IMG)})
    _session(page).fetch_chapter_images_via_dom("https://comix.to/1-chapter-1")

    assert page.reloads >= 1


# ------------------------------------------------- stalled-page classification

def _capture(urls, expected, unresolved):
    return comix.ComixChapterCapture(
        urls=list(urls), expected_pages=expected, unresolved=unresolved,
    )


def _stalled_state(n):
    """The chunk-boundary signature: container present, reader marked it
    loading, no <img> element ever mounted, no canvas."""
    return {
        "n": n, "loading": True, "hasCanvas": False, "cw": 0, "ch": 0,
        "hasImg": False, "src": "", "complete": False, "nw": 0,
    }


def test_deferred_pages_are_classified_as_a_permanent_skip(monkeypatch):
    """THE run-killer. comix defers every 10th page until the viewport reaches
    it; when the handler's ladder still can't get one, re-rendering the identical
    chapter cannot either. Live 2026-08-02: 99/107, retry, 99/107, retry, 99/107,
    then the whole 256-chapter run aborted on chapter 1. The reason must route to
    _PERMANENT_SKIP_REASONS so it skips-and-continues instead."""
    cap = _capture(
        [f"u{i}" for i in range(9)], 10, {10: _stalled_state(10)},
    )
    handler = _handler_with_capture(monkeypatch, cap)
    with pytest.raises(IncompleteChapterError) as exc:
        handler.get_chapter_images({"url": "https://comix.to/1-chapter-1"}, None, None)
    assert exc.value.reason == "comix_pages_stalled"


def test_an_ordinary_render_miss_stays_retryable(monkeypatch):
    """The flip side: a page that got as far as an <img> with a src, or whose
    container is missing entirely, is a genuine transient failure and must KEEP
    its inline retries. Mis-classifying it as permanent would silently give up on
    recoverable chapters."""
    state = _stalled_state(10)
    state.update(loading=False, hasImg=True, src="https://cdn/x.webp")
    cap = _capture([f"u{i}" for i in range(9)], 10, {10: state})
    handler = _handler_with_capture(monkeypatch, cap)
    with pytest.raises(IncompleteChapterError) as exc:
        handler.get_chapter_images({"url": "https://comix.to/1-chapter-1"}, None, None)
    assert exc.value.reason == "comix_dom_render_incomplete"


def test_one_ordinary_miss_downgrades_the_whole_classification(monkeypatch):
    """Requires EVERY miss to fit the deferral pattern. A mixed set means
    something else is also wrong, and the retryable read is the safer one."""
    cap = _capture(
        [f"u{i}" for i in range(8)], 10,
        {10: _stalled_state(10), 9: {"n": 9, "missing": True}},
    )
    handler = _handler_with_capture(monkeypatch, cap)
    with pytest.raises(IncompleteChapterError) as exc:
        handler.get_chapter_images({"url": "https://comix.to/1-chapter-1"}, None, None)
    assert exc.value.reason == "comix_dom_render_incomplete"


def test_the_gapped_opt_in_keeps_the_chapter_and_records_the_gap(monkeypatch):
    """--comix-allow-gapped-chapters trades completeness for availability, but
    the gap must never be SILENT: pages are renumbered on the way into the CBZ,
    so a missing page 10 is otherwise indistinguishable from a shorter chapter.
    The numbers ride the chapter dict into ComicInfo's <AioMissingPages>."""
    monkeypatch.setenv(comix._COMIX_ALLOW_GAPPED_ENV, "1")
    cap = _capture(
        [f"u{i}" for i in range(9)], 10, {10: _stalled_state(10)},
    )
    handler = _handler_with_capture(monkeypatch, cap)
    ch = {"url": "https://comix.to/1-chapter-1"}
    images = handler.get_chapter_images(ch, None, None)

    assert len(images) == 9, "the partial chapter is kept"
    assert ch["_missing_pages"] == [10], "the gap must be recorded"


def test_a_stale_gap_record_is_cleared_when_not_opted_in(monkeypatch):
    """The same chapter dict is reused across retries. A record left by an
    earlier gapped attempt must not leak into a later strict one."""
    cap = _capture([f"u{i}" for i in range(9)], 10, {10: _stalled_state(10)})
    handler = _handler_with_capture(monkeypatch, cap)
    ch = {"url": "https://comix.to/1-chapter-1", "_missing_pages": [10]}
    with pytest.raises(IncompleteChapterError):
        handler.get_chapter_images(ch, None, None)
    assert "_missing_pages" not in ch


# --------------------------------------------- handler contract (the raise)

def _handler_with_capture(monkeypatch, capture):
    handler = comix.ComixSiteHandler()
    monkeypatch.setattr(
        comix._COMIX_BROWSER_BRIDGE,
        "fetch_chapter_images_via_dom",
        lambda *a, **k: capture,
    )
    return handler


def test_get_chapter_images_raises_when_the_render_comes_up_short(monkeypatch):
    """The whole point. 67 of 68 used to be returned as a plain list, which
    aio-dl.py's gate reads as 67/67 complete."""
    handler = _handler_with_capture(
        monkeypatch,
        comix.ComixChapterCapture(
            urls=[f"https://cdn/{n}.webp" for n in range(67)], expected_pages=68,
        ),
    )
    with pytest.raises(IncompleteChapterError) as exc:
        handler.get_chapter_images(
            {"url": "https://comix.to/1-chapter-104"}, None, None,
        )
    assert exc.value.pages_ok == 67
    assert exc.value.pages_total == 68
    assert exc.value.reason == "comix_dom_render_incomplete"


def test_a_short_capture_is_never_memoized(monkeypatch):
    """The memo TTL (600s) outlives the caller's inline-retry backoff (30s then
    60s), so caching a short list would hand the retry the same truncated result
    from memory without ever re-rendering."""
    url = "https://comix.to/1-chapter-104"
    handler = _handler_with_capture(
        monkeypatch,
        comix.ComixChapterCapture(urls=["https://cdn/1.webp"], expected_pages=3),
    )
    with pytest.raises(IncompleteChapterError):
        handler.get_chapter_images({"url": url}, None, None)

    assert handler._get_cached_chapter_images(url) is None


def test_a_complete_capture_is_returned_and_memoized(monkeypatch):
    url = "https://comix.to/1-chapter-9"
    urls = ["https://cdn/1.webp", "https://cdn/2.webp"]
    handler = _handler_with_capture(
        monkeypatch, comix.ComixChapterCapture(urls=urls, expected_pages=2),
    )
    assert handler.get_chapter_images({"url": url}, None, None) == urls
    assert handler._get_cached_chapter_images(url) == urls


def test_a_render_that_never_learned_a_page_count_stays_an_empty_miss(monkeypatch):
    """expected_pages=0 means the reader never mounted / nav failed. "We were
    told nothing" is not "we were told 68", so this keeps the pre-existing
    empty_content behaviour instead of aborting the run."""
    handler = _handler_with_capture(
        monkeypatch, comix.ComixChapterCapture(urls=[], expected_pages=0),
    )
    assert handler.get_chapter_images(
        {"url": "https://comix.to/1-chapter-1"}, None, None,
    ) == []
