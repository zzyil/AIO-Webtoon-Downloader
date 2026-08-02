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
_DATA_URL = "data:image/webp;base64," + base64.b64encode(b"RIFF____WEBP").decode()

# Shapes a scripted page can present. "canvas+img" is the race candidate the
# capture-shapes summary exists to count: a canvas AND a fully decoded <img>.
IMG = "img"
CANVAS = "canvas"
CANVAS_AND_IMG = "canvas+img"


class _ScriptedReader:
    """Patchright page stand-in that reports a scripted reader DOM.

    ``ready`` maps page number -> (poll round it becomes ready, shape). Pages
    left out never become ready, which is how the "never rendered" cases are
    built. Dispatch is on distinctive substrings of the JS the session sends,
    so the tests stay coupled to behaviour rather than to exact source text.
    """

    def __init__(self, page_count, ready, url="https://comix.to/1234-chapter-1"):
        self.page_count = page_count
        self.ready = dict(ready)
        self.poll_rounds = 0
        self.scrolls = []
        self.reloads = 0
        self.canvas_reads = []
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
                shape = None
        base = {
            "n": n, "loading": False, "hasCanvas": False, "cw": 0, "ch": 0,
            "src": "", "complete": False, "nw": 0,
        }
        if shape in (CANVAS, CANVAS_AND_IMG):
            base.update(hasCanvas=True, cw=800, ch=1200)
        if shape in (IMG, CANVAS_AND_IMG):
            base.update(
                src=f"https://cdn.comix.to/p/{n:04d}.webp", complete=True, nw=800,
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
            self.scrolls.append(arg)
            return None
        if "toDataURL" in js:
            self.canvas_reads.append(arg)
            return _DATA_URL
        if "reader.webtoon.v3" in js:
            return "all"
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


def test_the_lowest_unresolved_page_is_nudged_into_view():
    page = _ScriptedReader(4, {1: (0, IMG), 2: (0, IMG), 3: (2, IMG), 4: (2, IMG)})
    _session(page).fetch_chapter_images_via_dom("https://comix.to/1-chapter-1")

    assert page.scrolls, "a straggler round must scroll something into view"
    assert page.scrolls[0] == 3


def test_unresolved_pages_trigger_exactly_one_reload_retry():
    """Bounded to one: a page that survives a fresh render plus a full
    convergence pass won't appear on a third try inside the same budget."""
    page = _ScriptedReader(3, {1: (0, IMG), 2: (0, IMG)})
    cap = _session(page).fetch_chapter_images_via_dom("https://comix.to/1-chapter-1")

    assert page.reloads == 1
    assert len(cap.urls) == 2
    assert list(cap.unresolved) == [3]


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


def test_the_download_path_still_reloads_for_the_same_shortfall():
    """Same scripted DOM as the probe case above; only the cap differs. Pins
    that the exemption is keyed on the probe, not on the shortfall."""
    page = _ScriptedReader(4, {1: (0, IMG), 2: (0, IMG)})
    _session(page).fetch_chapter_images_via_dom("https://comix.to/1-chapter-1")

    assert page.reloads == 1


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
