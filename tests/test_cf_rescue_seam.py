"""Coverage for the Cloudflare rescue seam — the gate four handlers ask before
deciding they cannot get past an interstitial.

WHY THIS IS WORTH TESTING OFFLINE: the defect these tests pin was invisible in
every way a defect can be. `if ZENDRIVER_AVAILABLE and is_cf_challenge(...)`
short-circuits on a platform without zendriver, so the detector was never even
called; the whole block sat inside `except Exception: pass`, so nothing was
logged; and the user-visible symptom was "No chapters selected" — a message
about their chapter filter. It survived a whole milestone, across 244 of 303
handlers, and no test could have caught it because no test asserted on the
GATE. These do.

The two axes that matter, and both are asserted for every handler here:
  * an embedder backend installed + ZENDRIVER_AVAILABLE False  → must divert
    (the Android case, and the one that was broken)
  * no embedder backend + ZENDRIVER_AVAILABLE True             → must take the
    existing desktop path, untouched (the no-regression proof)

Cross-file: sites/crawlee_utils.py (cf_solver_available / rescue_cf_html /
warn_cf_*), sites/madara.py:_fetch_html, sites/manhwaread.py,
sites/mangathemesia.py:_fetch_html_guarded, sites/weebcentral.py:_fetch_html.
tests/test_android_browser_backend.py is the model for the fake backend.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from sites import browser_backend as bb
from sites import crawlee_utils
from sites.madara import MadaraSiteHandler
from sites.mangathemesia import MangaThemesiaSiteHandler
from sites.manhwaread import ManhwaReadHandler
from sites.weebcentral import WeebCentralSiteHandler


CHALLENGE_HTML = (
    "<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
    "<body>Checking your browser before accessing. Enable JavaScript and "
    "cookies to continue. cf_chl_opt</body></html>"
)

# Absolute image src on purpose: manhwaread returns the src verbatim while
# mangathemesia urljoins it, and an absolute URL makes both assertions read the
# same without either handler's normalizer being under test here.
SOLVED_HTML = (
    "<html><body><h1 class='entry-title'>Solved Series</h1>"
    "<div id='chapterlist'><li><a href='/ch/1'>"
    "<span class='chapternum'>Chapter 1</span></a></li></div>"
    "<div class='reading-content'><img src='https://cdn.example.org/p/1.jpg'></div>"
    "<div id='chaptersList'></div></body></html>"
)


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class FakeScraper:
    """Just enough of a cloudscraper session for sync_cf_cookies to write to."""

    def __init__(self) -> None:
        self.headers: Dict[str, str] = {}
        self.cookie_writes: List[tuple] = []

        class _Jar:
            def __init__(self, owner):
                self._owner = owner

            def set(self, name, value, **kw):
                self._owner.cookie_writes.append((name, value))

        self.cookies = _Jar(self)


class RecordingMakeRequest:
    """Stands in for aio-dl.py:make_request, which returns the response rather
    than raising on a content-bearing 4xx — that behaviour is precisely why a
    CF interstitial reaches the parser instead of the retry loop."""

    def __init__(self, *responses: FakeResponse) -> None:
        self._responses = list(responses)
        self.urls: List[str] = []

    def __call__(self, url: str, scraper) -> FakeResponse:
        self.urls.append(url)
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


class FakeChallengeBackend:
    """A BrowserBackend that CAN solve challenges — the Android WebView's role.

    Only the members sites/crawlee_utils.py:get_cf_session touches are real;
    everything else satisfies the Protocol so isinstance checks pass.
    """

    def __init__(self, profile: str = "cf") -> None:
        self.profile = profile
        self.solved: List[str] = []
        self.solve_error: Optional[Exception] = None

    # -- the parts get_cf_session uses ------------------------------------
    @property
    def supports_challenge_solving(self) -> bool:
        return True

    def solve_challenge(
        self, url: str, *, timeout_s: float = 45.0, interactive: bool = True
    ) -> Dict[str, Any]:
        self.solved.append(url)
        if self.solve_error is not None:
            raise self.solve_error
        return {
            "cookies": [
                {
                    "name": "cf_clearance",
                    "value": "solved",
                    "domain": "example.org",
                    "path": "/",
                }
            ],
            "user_agent": "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36",
        }

    # -- Protocol filler ---------------------------------------------------
    def goto(self, url, *, wait_until="domcontentloaded", timeout_ms=45_000):
        return None

    def evaluate(self, script, arg=bb.NOARG):
        return None

    def content(self) -> str:
        return SOLVED_HTML

    def wait_for_selector(self, selector, *, timeout_ms=10_000) -> bool:
        return True

    def user_agent(self) -> str:
        return "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36"

    def cookies(self, url):
        return []

    def close(self) -> None:
        return None

    @property
    def unavailable_reason(self):
        return None


class NonSolvingBackend(FakeChallengeBackend):
    """PatchrightBackend's shape: navigates, cannot solve. Must NOT satisfy the
    gate — burning a user interaction on a backend that cannot finish is the
    whole reason supports_challenge_solving exists."""

    @property
    def supports_challenge_solving(self) -> bool:
        return False


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_registry():
    """The backend registry and the CF cookie cache are BOTH process-wide. A
    leaked factory hands every later test a browser that isn't one; a leaked
    cookie-cache entry makes get_cf_session skip the solve entirely and the
    next test's assertion on `solved` silently fails."""
    yield
    bb.set_backend_factory(None)
    bb.reset()
    with crawlee_utils._cf_cookie_lock:
        crawlee_utils._cf_cookie_cache.clear()
    crawlee_utils._cf_warn_seen.clear()


@pytest.fixture
def android(monkeypatch):
    """Android: a challenge-capable backend installed, zendriver absent."""
    backend = FakeChallengeBackend()
    bb.set_backend_factory(lambda profile: backend)
    monkeypatch.setattr(crawlee_utils, "ZENDRIVER_AVAILABLE", False)
    return backend


@pytest.fixture
def desktop(monkeypatch):
    """Desktop: no embedder backend, zendriver present."""
    bb.set_backend_factory(None)
    monkeypatch.setattr(crawlee_utils, "ZENDRIVER_AVAILABLE", True)


@pytest.fixture
def solver_calls(monkeypatch):
    """Intercept the ONE function every handler's rescue funnels through, so a
    test can assert "a browser was reached" without launching one."""
    calls: List[str] = []

    def _fake(url, *, base_url=None, scraper=None, timeout=20.0):
        calls.append(url)
        if scraper is not None:
            scraper.headers["User-Agent"] = "solved-ua"
            scraper.cookies.set("cf_clearance", "solved")
        return SOLVED_HTML

    monkeypatch.setattr(crawlee_utils, "rescue_cf_html", _fake)
    return calls


# --------------------------------------------------------------------------
# The gate itself
# --------------------------------------------------------------------------


def test_gate_true_for_embedder_backend_without_zendriver(android):
    assert crawlee_utils.cf_solver_available() is True
    assert crawlee_utils.cf_solver_available(embedder_only=True) is True


def test_gate_true_for_zendriver_without_backend(desktop):
    assert crawlee_utils.cf_solver_available() is True
    # ...but the narrow question is False: weebcentral must still prefer impit
    # over a solver that opens a visible Chrome window.
    assert crawlee_utils.cf_solver_available(embedder_only=True) is False


def test_gate_false_when_nothing_can_solve(monkeypatch):
    bb.set_backend_factory(None)
    monkeypatch.setattr(crawlee_utils, "ZENDRIVER_AVAILABLE", False)
    assert crawlee_utils.cf_solver_available() is False


def test_gate_rejects_a_backend_that_cannot_solve(monkeypatch):
    bb.set_backend_factory(lambda profile: NonSolvingBackend(profile))
    monkeypatch.setattr(crawlee_utils, "ZENDRIVER_AVAILABLE", False)
    assert crawlee_utils.cf_solver_available() is False
    assert crawlee_utils.cf_solver_available(embedder_only=True) is False


def test_gate_survives_a_backend_that_raises(monkeypatch):
    class Exploding:
        @property
        def supports_challenge_solving(self):
            raise RuntimeError("bridge died")

    bb.set_backend_factory(lambda profile: Exploding())
    monkeypatch.setattr(crawlee_utils, "ZENDRIVER_AVAILABLE", False)
    assert crawlee_utils.cf_solver_available() is False


def test_detector_fires_on_the_shipped_challenge_body():
    # If this ever goes False the gate fix is moot — everything downstream
    # depends on is_cf_challenge recognising the interstitial.
    assert crawlee_utils.is_cf_challenge(403, CHALLENGE_HTML) is True
    assert crawlee_utils.is_cf_challenge(200, SOLVED_HTML) is False


# --------------------------------------------------------------------------
# rescue_cf_html — the diversion, end to end through get_cf_session
# --------------------------------------------------------------------------


def test_get_cf_session_routes_to_the_embedder_backend(android):
    """The rung underneath every handler fix: with zendriver absent,
    get_cf_session must reach backend.solve_challenge and build a session that
    carries what came back. This already worked — it is asserted here because
    the handler fixes are worthless if it ever stops.

    Permission granted explicitly: this asserts ROUTING, and the default is now
    deny, so without the scope it would exercise the gate instead."""
    with crawlee_utils.interactive_solving(True):
        session = crawlee_utils.get_cf_session("https://example.org/manga/x")

    assert android.solved == ["https://example.org/manga/x"]
    assert session.headers["User-Agent"].startswith("Mozilla/5.0 (Linux; Android")
    assert session.cookies.get("cf_clearance") == "solved"


def test_rescue_cf_html_syncs_the_scraper_after_fetching(android, monkeypatch):
    """rescue_cf_html = fetch + sync. The sync half is not bookkeeping: without
    it only the rescued page ever succeeds, because every later image and
    chapter URL still goes through `scraper`."""
    monkeypatch.setattr(
        crawlee_utils, "fetch_html_with_cf_cookies", lambda url, **kw: SOLVED_HTML
    )
    with crawlee_utils._cf_cookie_lock:
        crawlee_utils._cf_cookie_cache["example.org"] = {
            "cookies": [{"name": "cf_clearance", "value": "v", "domain": "example.org"}],
            "user_agent": "solved-ua",
            "ts": crawlee_utils._time.time(),
        }

    scraper = FakeScraper()
    got = crawlee_utils.rescue_cf_html(
        "https://example.org/manga/x", base_url="https://example.org", scraper=scraper
    )

    assert got == SOLVED_HTML
    assert scraper.headers["User-Agent"] == "solved-ua"
    assert scraper.cookie_writes == [("cf_clearance", "v")]


def test_warn_dedupes_per_host_and_kind(capsys):
    crawlee_utils.warn_cf_no_solver("https://a.example/1")
    crawlee_utils.warn_cf_no_solver("https://a.example/2")
    crawlee_utils.warn_cf_no_solver("https://b.example/1")
    err = capsys.readouterr().err
    assert err.count("a.example") == 1
    assert err.count("b.example") == 1


def test_warn_goes_to_stderr_not_stdout(capsys):
    # aio_search_cli writes its JSON to stdout; a line there corrupts the whole
    # --search-json payload. Same rule sites/hardening.py follows.
    crawlee_utils.warn_cf_rescue("https://c.example/1", "boom")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "c.example" in captured.err


# --------------------------------------------------------------------------
# madara — the chokepoint for 244 handlers
# --------------------------------------------------------------------------


def _madara() -> MadaraSiteHandler:
    return MadaraSiteHandler("testmadara", "https://example.org")


def test_madara_diverts_on_android(android, solver_calls):
    handler = _madara()
    scraper = FakeScraper()
    mk = RecordingMakeRequest(FakeResponse(403, CHALLENGE_HTML))

    html = handler._fetch_html("https://example.org/manga/x", scraper, mk)

    assert solver_calls == ["https://example.org/manga/x"]
    assert html == SOLVED_HTML
    # And the clearance reached the session the handler's own later calls use.
    assert scraper.headers["User-Agent"] == "solved-ua"


def test_madara_desktop_path_is_unchanged(desktop, solver_calls):
    """The no-regression proof: no embedder factory, zendriver present — the
    rescue must still fire, through exactly the same call."""
    handler = _madara()
    mk = RecordingMakeRequest(FakeResponse(403, CHALLENGE_HTML))

    html = handler._fetch_html("https://example.org/manga/x", FakeScraper(), mk)

    assert solver_calls == ["https://example.org/manga/x"]
    assert html == SOLVED_HTML


def test_madara_leaves_a_clean_200_alone(android, solver_calls):
    handler = _madara()
    mk = RecordingMakeRequest(FakeResponse(200, SOLVED_HTML))

    html = handler._fetch_html("https://example.org/manga/x", FakeScraper(), mk)

    assert solver_calls == []
    assert html == SOLVED_HTML


def test_madara_without_any_solver_keeps_the_body_and_says_why(monkeypatch, capsys):
    bb.set_backend_factory(None)
    monkeypatch.setattr(crawlee_utils, "ZENDRIVER_AVAILABLE", False)
    handler = _madara()
    mk = RecordingMakeRequest(FakeResponse(403, CHALLENGE_HTML))

    html = handler._fetch_html("https://example.org/manga/x", FakeScraper(), mk)

    # Same bytes as before the fix — the fetch is never killed by a missing
    # solver — but the run now says Cloudflare instead of dying later on
    # "No chapters selected."
    assert html == CHALLENGE_HTML
    err = capsys.readouterr().err
    assert "Cloudflare" in err and "example.org" in err


def test_madara_survives_a_failing_rescue(android, monkeypatch, capsys):
    def _boom(url, **kw):
        raise RuntimeError("solve timed out")

    monkeypatch.setattr(crawlee_utils, "rescue_cf_html", _boom)
    handler = _madara()
    mk = RecordingMakeRequest(FakeResponse(403, CHALLENGE_HTML))

    html = handler._fetch_html("https://example.org/manga/x", FakeScraper(), mk)

    assert html == CHALLENGE_HTML  # a broken rescue must not kill the fetch
    assert "solve timed out" in capsys.readouterr().err


def test_madara_fetch_html_is_still_the_single_chokepoint():
    """0 overrides is what makes _fetch_html worth fixing once. If a subclass
    ever grows its own, that subclass's sites silently leave the seam."""
    from sites import _REGISTERED_HANDLERS

    offenders = sorted(
        {
            type(h).__qualname__
            for h in _REGISTERED_HANDLERS
            if isinstance(h, MadaraSiteHandler)
            and type(h)._fetch_html is not MadaraSiteHandler._fetch_html
        }
    )
    assert offenders == []


# --------------------------------------------------------------------------
# manhwaread — the image path
# --------------------------------------------------------------------------


def test_manhwaread_diverts_on_android(android, solver_calls):
    handler = ManhwaReadHandler()
    mk = RecordingMakeRequest(FakeResponse(403, CHALLENGE_HTML))

    images = handler.get_chapter_images(
        {"url": "https://manhwaread.com/ch/1"}, FakeScraper(), mk
    )

    assert solver_calls == ["https://manhwaread.com/ch/1"]
    assert images == ["https://cdn.example.org/p/1.jpg"]


def test_manhwaread_desktop_path_is_unchanged(desktop, solver_calls):
    handler = ManhwaReadHandler()
    mk = RecordingMakeRequest(FakeResponse(403, CHALLENGE_HTML))

    handler.get_chapter_images({"url": "https://manhwaread.com/ch/1"}, FakeScraper(), mk)

    assert solver_calls == ["https://manhwaread.com/ch/1"]


def test_manhwaread_leaves_a_healthy_page_alone(android, solver_calls):
    # Padded past the handler's own len<2000 "blocked body" heuristic.
    healthy = SOLVED_HTML + "<!--" + ("x" * 2500) + "-->"
    handler = ManhwaReadHandler()
    mk = RecordingMakeRequest(FakeResponse(200, healthy))

    handler.get_chapter_images({"url": "https://manhwaread.com/ch/1"}, FakeScraper(), mk)

    assert solver_calls == []


# --------------------------------------------------------------------------
# mangathemesia — 28 handlers, 27 of which had no CF handling at all
# --------------------------------------------------------------------------


def _mt(**kw) -> MangaThemesiaSiteHandler:
    return MangaThemesiaSiteHandler(
        name="testmt",
        display_name="Test MT",
        base_url="https://example.org",
        domains=("example.org",),
        **kw,
    )


def test_mangathemesia_context_diverts_on_android(android, solver_calls):
    handler = _mt()
    mk = RecordingMakeRequest(FakeResponse(403, CHALLENGE_HTML))

    ctx = handler.fetch_comic_context("https://example.org/manga/x", FakeScraper(), mk)

    assert solver_calls == ["https://example.org/manga/x"]
    # Before the fix the interstitial was parsed and this was "Unknown".
    assert ctx.title == "Solved Series"


def test_mangathemesia_context_desktop_path_is_unchanged(desktop, solver_calls):
    handler = _mt()
    mk = RecordingMakeRequest(FakeResponse(403, CHALLENGE_HTML))

    ctx = handler.fetch_comic_context("https://example.org/manga/x", FakeScraper(), mk)

    assert solver_calls == ["https://example.org/manga/x"]
    assert ctx.title == "Solved Series"


def test_mangathemesia_chapters_divert(android, solver_calls):
    handler = _mt()
    mk = RecordingMakeRequest(FakeResponse(403, CHALLENGE_HTML))
    ctx = type("C", (), {"comic": {"url": "https://example.org/manga/x"}})()

    chapters = handler.get_chapters(ctx, FakeScraper(), "en", mk)

    assert solver_calls == ["https://example.org/manga/x"]
    assert [c["chap"] for c in chapters] == [1.0]


def test_mangathemesia_images_divert(android, solver_calls):
    handler = _mt()
    mk = RecordingMakeRequest(FakeResponse(503, CHALLENGE_HTML))

    images = handler.get_chapter_images(
        {"url": "https://example.org/ch/1"}, FakeScraper(), mk
    )

    assert solver_calls == ["https://example.org/ch/1"]
    assert images == ["https://cdn.example.org/p/1.jpg"]


def test_mangathemesia_search_uses_the_guarded_fetch_like_everything_else(
    android, solver_calls
):
    """search() is no longer the odd one out.

    It used to skip the guard on purpose, to keep a background fan-out from
    opening a browser. That is now the permission's job, so the fetch can be
    uniform — and a challenged search gets the headless rescue tiers instead of
    parsing an interstitial into zero hits. (solver_calls stubs rescue_cf_html
    wholesale, so this asserts the WIRING; the permission itself is asserted by
    the property tests below, which drive the real one.)"""
    handler = _mt()
    mk = RecordingMakeRequest(FakeResponse(403, CHALLENGE_HTML))

    handler.search("frieren", FakeScraper(), mk)
    assert solver_calls == ["https://example.org/?s=frieren"]


# --------------------------------------------------------------------------
# The PROPERTY: background work never reaches a human.
#
# These replace two tests that asserted an OMISSION — "search() does not call
# the guarded helper" — which is a fact about one method, not the guarantee
# anyone cared about. They passed while the probe path (fetch_comic_context /
# get_chapters / get_chapter_images, grep sites/base.py:_probe_chapter_aggregate)
# reached an interactive solve from inside a background search.
#
# Every test here drives the REAL rescue_cf_html/get_cf_session and asserts on
# the backend's own solve_challenge call log, because that is the exact moment a
# window opens on desktop or a ChallengeActivity starts on a phone.
# --------------------------------------------------------------------------


def test_the_probe_path_never_opens_a_browser(android, monkeypatch):
    """THE regression test for the hole. Differential on purpose: the negative
    half alone would also pass if the probe never reached a fetch at all, so the
    positive half pins that this exact call DOES reach the solver when allowed."""
    from sites.base import SearchHit

    # Isolate the browser tier: impit would rescue first and the solver would
    # never be consulted either way, which would make both halves vacuous.
    monkeypatch.setattr(crawlee_utils, "IMPIT_AVAILABLE", False)
    handler = _mt()
    hit = SearchHit(
        title="Frieren", url="https://example.org/manga/frieren", site="mangathemesia"
    )

    def _probe():
        return handler._probe_chapter_aggregate(
            hit,
            FakeScraper(),
            RecordingMakeRequest(FakeResponse(403, CHALLENGE_HTML)),
            fetch_memo=None,
        )

    # Default context = a background search. No window, no Activity, no wait.
    _probe()
    assert android.solved == [], (
        "the image-quality probe reached an interactive CF solve; a cross-site "
        "search would open a browser for a series nobody picked"
    )

    # Same call, foreground download. Now it is allowed to interrupt.
    # Solved against the site's BASE url, not the page url: rescue_cf_html
    # passes base_url through to get_cf_session, whose cookie cache is
    # per-DOMAIN — one solve clears the whole host.
    with crawlee_utils.interactive_solving(True):
        _probe()
    assert android.solved == ["https://example.org"]


def test_permission_defaults_to_denied():
    """The inverted default is the whole safety argument: a call site nobody
    remembered to mark loses a rescue tier instead of ambushing someone."""
    assert crawlee_utils.interactive_solve_allowed() is False


def test_permission_restores_on_exit_even_when_the_body_raises():
    with crawlee_utils.interactive_solving(True):
        assert crawlee_utils.interactive_solve_allowed() is True
        with pytest.raises(ValueError):
            with crawlee_utils.interactive_solving(False):
                assert crawlee_utils.interactive_solve_allowed() is False
                raise ValueError("boom")
        assert crawlee_utils.interactive_solve_allowed() is True
    assert crawlee_utils.interactive_solve_allowed() is False


def test_a_fresh_thread_does_not_inherit_permission():
    """The trap this design is built around. A new thread starts with an empty
    context and reads the DEFAULT — so permission can never be granted around a
    pool submission and reach the workers, and (the direction that protects
    people) a worker cannot inherit a download's permission. It is also why
    search_orchestrator._probe_one sets False INSIDE the worker rather than
    around the thread start."""
    import threading

    seen: List[bool] = []
    with crawlee_utils.interactive_solving(True):
        assert crawlee_utils.interactive_solve_allowed() is True
        t = threading.Thread(
            target=lambda: seen.append(crawlee_utils.interactive_solve_allowed())
        )
        t.start()
        t.join()
    assert seen == [False]


def test_the_orchestrator_sets_the_permission_inside_the_worker():
    """Structural: _probe_one must enter the scope in its own body. Wrapping the
    thread start instead would be a no-op per the test above, and the failure
    would be invisible — the probe would simply solve challenges again."""
    import inspect
    from sites import search_orchestrator

    src = inspect.getsource(search_orchestrator)
    assert "def _probe_one(src: SourceEntry) -> None:" in src
    body = src.split("def _probe_one(src: SourceEntry) -> None:", 1)[1]
    head = body.split("def _probe_one_guarded", 1)[0]
    assert "interactive_solving(False)" in head


def test_impit_tier_is_never_gated(monkeypatch):
    """The gate must not be a capability regression: a background search on
    desktop still rescues itself headlessly, exactly as it did before."""
    bb.set_backend_factory(None)
    monkeypatch.setattr(crawlee_utils, "ZENDRIVER_AVAILABLE", True)
    monkeypatch.setattr(crawlee_utils, "IMPIT_AVAILABLE", True)
    monkeypatch.setattr(
        crawlee_utils, "fetch_html_impit", lambda url, **kw: "<html>impit ok</html>"
    )

    assert crawlee_utils.interactive_solve_allowed() is False
    assert (
        crawlee_utils.rescue_cf_html("https://example.org/x")
        == "<html>impit ok</html>"
    )


def test_impit_returning_a_challenge_is_not_mistaken_for_a_rescue(android, monkeypatch):
    """A challenged body from impit must fall through to the next tier, not be
    returned as the page — that IS the defect this whole seam exists to stop."""
    monkeypatch.setattr(crawlee_utils, "IMPIT_AVAILABLE", True)
    monkeypatch.setattr(crawlee_utils, "fetch_html_impit", lambda url, **kw: CHALLENGE_HTML)

    with pytest.raises(crawlee_utils.InteractiveSolveBlocked):
        crawlee_utils.rescue_cf_html("https://example.org/x")


def test_cached_clearance_serves_background_work_without_permission(android, monkeypatch):
    """The gate sits at the cache MISS, so a host some earlier download already
    solved keeps serving background searches for free. Gating the whole function
    would have thrown that away for no benefit — a cache hit interrupts nobody."""
    monkeypatch.setattr(crawlee_utils, "IMPIT_AVAILABLE", False)
    with crawlee_utils.interactive_solving(True):
        crawlee_utils.get_cf_session("https://example.org/a")
    assert android.solved == ["https://example.org/a"]

    # Background now: same domain, no permission, no second solve.
    session = crawlee_utils.get_cf_session("https://example.org/b")
    assert android.solved == ["https://example.org/a"]
    assert session.cookies.get("cf_clearance") == "solved"


def test_a_blocked_solve_says_so_on_stderr_and_names_the_fix(android, monkeypatch, capsys):
    monkeypatch.setattr(crawlee_utils, "IMPIT_AVAILABLE", False)
    with pytest.raises(crawlee_utils.InteractiveSolveBlocked):
        crawlee_utils.get_cf_session("https://example.org/x")

    err = capsys.readouterr().err
    assert "example.org" in err
    assert "background" in err
    assert "re-run this series as a download" in err


def test_mangathemesia_use_zendriver_contract_is_intact():
    """Repo CLAUDE.md pins this: a constructor kwarg, default False, three use
    sites. sites/__init__.py registers kingofshojo with it."""
    assert _mt(use_zendriver=True).use_zendriver is True
    assert _mt().use_zendriver is False
    import inspect

    src = inspect.getsource(MangaThemesiaSiteHandler)
    assert src.count("if self.use_zendriver:") == 3


# --------------------------------------------------------------------------
# weebcentral — the ladder
# --------------------------------------------------------------------------


def test_weebcentral_diverts_on_a_200_interstitial(android, solver_calls):
    """The bug this file shipped with: the turn-away test was `status_code in
    (403, 429, 503)` and nothing else, so CF's JS-redirect variant — served with
    **HTTP 200** — was returned verbatim and parsed as the series page. That is
    the same defect shape the rewrite was meant to remove, surviving inside the
    rewritten function. is_cf_challenge is what catches it."""
    handler = WeebCentralSiteHandler()
    mk = RecordingMakeRequest(FakeResponse(200, CHALLENGE_HTML))

    html = handler._fetch_html("https://weebcentral.com/x", FakeScraper(), mk, "test")

    assert solver_calls == ["https://weebcentral.com/x"]
    assert html == SOLVED_HTML


def test_weebcentral_delegates_tier_order_instead_of_reimplementing_it(
    android, monkeypatch
):
    """The ladder used to live here: embedder → impit → solver, hand-ordered so
    impit stayed ahead of a window-opening solver on desktop. That policy now
    lives once, in rescue_cf_html. A second copy is precisely how madara and
    manhwaread drifted, so assert weebcentral OWNS none of it."""
    impit_calls: List[str] = []
    monkeypatch.setattr(crawlee_utils, "IMPIT_AVAILABLE", True)
    monkeypatch.setattr(
        crawlee_utils,
        "fetch_html_impit",
        lambda url, **kw: impit_calls.append(url) or "<html>impit</html>",
    )

    handler = WeebCentralSiteHandler()
    mk = RecordingMakeRequest(FakeResponse(403, CHALLENGE_HTML))
    html = handler._fetch_html("https://weebcentral.com/x", FakeScraper(), mk, "test")

    # Reached impit through the shared tier list, not a local rung.
    assert impit_calls == ["https://weebcentral.com/x"]
    assert html == "<html>impit</html>"
    assert android.solved == []

    # Structural, over the AST rather than the text: the docstring and comments
    # in _fetch_html legitimately NAME the retired ladder to explain where the
    # policy went, so a substring check matches the explanation and fails.
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(WeebCentralSiteHandler._fetch_html)))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    kwargs = {
        kw.arg for n in ast.walk(tree) if isinstance(n, ast.Call) for kw in n.keywords
    }
    assert "IMPIT_AVAILABLE" not in names
    assert "fetch_html_impit" not in names
    assert "embedder_only" not in kwargs


def test_weebcentral_raises_with_its_own_prefix_when_the_rescue_fails(monkeypatch):
    bb.set_backend_factory(None)
    monkeypatch.setattr(crawlee_utils, "ZENDRIVER_AVAILABLE", False)
    monkeypatch.setattr(crawlee_utils, "IMPIT_AVAILABLE", False)
    # Stubbed so the no-solver path cannot reach the network from a unit test.
    monkeypatch.setattr(
        crawlee_utils,
        "fetch_html_with_cf_cookies",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no solver")),
    )

    handler = WeebCentralSiteHandler()
    mk = RecordingMakeRequest(FakeResponse(403, CHALLENGE_HTML))
    with pytest.raises(RuntimeError) as exc:
        handler._fetch_html("https://weebcentral.com/x", FakeScraper(), mk, "chapter list")

    # The prefix callers have always seen, and the real cause instead of the
    # misleading "impit not available".
    assert "WeebCentral chapter list fetch failed" in str(exc.value)
    assert "403" in str(exc.value)


def test_weebcentral_happy_path_never_touches_a_rescue(android, solver_calls):
    handler = WeebCentralSiteHandler()
    mk = RecordingMakeRequest(FakeResponse(200, "<html>ok</html>"))

    assert (
        handler._fetch_html("https://weebcentral.com/x", FakeScraper(), mk, "test")
        == "<html>ok</html>"
    )
    assert solver_calls == []


def test_weebcentral_series_page_uses_the_ladder_too(android, solver_calls):
    """fetch_comic_context was the third bare fetch in this file. A
    content-bearing 403 used to be parsed as the series page, degrading the
    title to the URL slug."""
    handler = WeebCentralSiteHandler()
    mk = RecordingMakeRequest(FakeResponse(403, CHALLENGE_HTML))

    handler.fetch_comic_context(
        "https://weebcentral.com/series/ABC/x", FakeScraper(), mk
    )

    assert solver_calls == ["https://weebcentral.com/series/ABC/x"]


def test_weebcentral_search_gets_the_headless_tiers_but_not_a_browser(
    android, monkeypatch
):
    """Replaces a test that asserted weebcentral's search skipped the rescue.
    The guarantee that matters is not "no rescue" but "no HUMAN" — so a
    background search should still be rescued headlessly and must still never
    reach solve_challenge."""
    monkeypatch.setattr(crawlee_utils, "IMPIT_AVAILABLE", True)
    impit_calls: List[str] = []
    monkeypatch.setattr(
        crawlee_utils,
        "fetch_html_impit",
        lambda url, **kw: impit_calls.append(url) or "<html>impit</html>",
    )

    handler = WeebCentralSiteHandler()
    mk = RecordingMakeRequest(FakeResponse(403, CHALLENGE_HTML))
    handler.search("frieren", FakeScraper(), mk)

    assert impit_calls, "a challenged search should still get the headless rescue"
    assert android.solved == []


def test_weebcentral_calls_the_rescue_exactly_once(android, monkeypatch):
    """One rung now. The old three-rung ladder had to actively avoid queueing
    the identical solver call twice (embedder_only and the plain gate are both
    True on Android); collapsing it made that impossible by construction."""
    monkeypatch.setattr(crawlee_utils, "IMPIT_AVAILABLE", False)
    calls: List[str] = []

    def _boom(url, **kw):
        calls.append(url)
        raise RuntimeError("solve failed")

    monkeypatch.setattr(crawlee_utils, "rescue_cf_html", _boom)
    handler = WeebCentralSiteHandler()
    mk = RecordingMakeRequest(FakeResponse(403, CHALLENGE_HTML))
    with pytest.raises(RuntimeError):
        handler._fetch_html("https://weebcentral.com/x", FakeScraper(), mk, "test")

    assert calls == ["https://weebcentral.com/x"]


# ────────────────────────────────────────────────────────────────────────
# The DOM-shaped detector, and the make_request chokepoint
#
# Added 2026-08-19 with the mangafire Cloudflare fix. is_cf_challenge judges an
# HTTP response; looks_like_cf_interstitial judges a RENDERED DOM, where there
# is no status code and the hydrated markup can blow past the 15 KB gate the
# 200-branch relies on.
# ────────────────────────────────────────────────────────────────────────

def test_interstitial_detected_without_a_status_code():
    assert crawlee_utils.looks_like_cf_interstitial(CHALLENGE_HTML) is True


def test_interstitial_detected_when_hydrated_past_the_15kb_gate():
    """A live Turnstile interstitial renders well past the length gate that
    is_cf_challenge's 200-branch uses, which is exactly why that function
    cannot be reused for a page.content()."""
    hydrated = CHALLENGE_HTML + ("<div>x</div>" * 4000)
    assert len(hydrated) > 15_000
    assert crawlee_utils.is_cf_challenge(200, hydrated) is False
    assert crawlee_utils.looks_like_cf_interstitial(hydrated) is True


def test_title_alone_is_enough():
    assert crawlee_utils.looks_like_cf_interstitial("<html></html>", "Just a moment...") is True


def test_ordinary_page_is_not_an_interstitial():
    """"challenge" appears in real comic synopses — one loose word must not
    read as Cloudflare."""
    assert crawlee_utils.looks_like_cf_interstitial(
        "<html><body><h1>A Challenge Appears</h1></body></html>"
    ) is False
    assert crawlee_utils.looks_like_cf_interstitial("") is False


def test_is_cf_challenge_contract_is_unchanged():
    """The new shared primitive must not have moved the old thresholds."""
    assert crawlee_utils.is_cf_challenge(403, CHALLENGE_HTML) is True
    assert crawlee_utils.is_cf_challenge(200, CHALLENGE_HTML) is True
    assert crawlee_utils.is_cf_challenge(200, "<html>fine</html>") is False
    assert crawlee_utils.is_cf_challenge(404, CHALLENGE_HTML) is False


def test_make_request_warns_but_still_returns_the_challenged_response(monkeypatch):
    """THE regression guard for the 244-handler rescue path.

    madara._fetch_html, mangathemesia._fetch_html_guarded and kappabeast's
    _read_frontend_text all call make_request and THEN inspect the body to
    decide whether to rescue. If make_request ever starts RAISING on a detected
    challenge, every one of those rescues silently stops firing — the failure
    mode would be indistinguishable from the bug this seam was built to fix.
    """
    import importlib
    import sys

    sys.path.insert(0, ".")
    aio = importlib.import_module("aio-dl")

    class _Resp:
        status_code = 403
        text = CHALLENGE_HTML
        headers: Dict[str, str] = {"content-type": "text/html"}

        def raise_for_status(self):
            raise AssertionError("must not raise on a challenge with a body")

    class _Scraper:
        def get(self, url, timeout=None):
            return _Resp()

    warned: List[str] = []
    monkeypatch.setattr(
        crawlee_utils,
        "warn_cf_rescue",
        lambda url, reason, kind="failed": warned.append(kind),
    )

    out = aio.make_request("https://example.invalid/series/x", _Scraper())
    assert out is not None, "make_request must still return the response"
    assert out.text == CHALLENGE_HTML, "the body a rescue would inspect must survive"
    assert warned == ["detected"], "the challenge must be reported once"
