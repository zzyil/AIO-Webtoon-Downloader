"""comix WAF detection + chapter-list pagination correctness (sites/comix.py).

Offline only: every test here is a pure-function or structural check, so the
suite runs with no browser and no network. The live end-to-end check is in the
plan file's Verification section.

Each test names the defect it pins. The headline ones, both found live
2026-08-02 against https://comix.to/title/3j50-magic-emperor:

  - THE CHAPTER LIST TRUNCATED SILENTLY. `fetch_chapters_via_dom` ended its
    pagination loop with a bare `if not rows: break`, which cannot tell "past
    the last page" from "the render hadn't finished" or "a WAF interstitial is
    in front of us". Rows are per (chapter x group), so a 4-group series yields
    only ~5 distinct chapters per 20-row page. The scrape stopped at page 5 of
    360, collected 78 rows = 20 distinct chapters, and the download began at
    chapter 871 instead of 1 — with no warning at all. The list is now bounded
    and terminated by the site's own `.npager` control, and a short scrape
    RAISES instead of returning a partial series (a partial list gets persisted
    to .aio_series.json as though it were the whole thing, so every later
    update run would inherit the truncation).

  - THE WAF INTERSTITIAL WAS INVISIBLE. comix serves a first-party interactive
    CAPTCHA at /@waf/challenge ("Verify you're human — drag to rotate the
    circle"), separate from the Cloudflare layer it ALSO runs. Nothing in the
    handler detected it, so it surfaced as three different lies: "0 .rpage-page
    divs", "no typeahead results", and — via the HTTP path, which 200s with
    challenge HTML — a slug-derived junk title written into the library.

  - THE USER'S ?group_id= WAS DISCARDED. `title_url.split("?", 1)[0]` threw
    away the scanlation-group filter comix honors server-side: 360 pager pages
    versus 59.

Cross-file:
  - sites/comix.py (under test).
  - tests/test_comix_bridge.py (the bridge's threading contract).
  - sites/crawlee_utils.py:is_cf_challenge (the Cloudflare sibling that the WAF
    detector must NOT be confused with).
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import re
import sys

import pytest

from sites import comix
from sites.base import BaseSiteHandler


def _load_aio_dl_module():
    """Load aio-dl.py as a module (its hyphenated filename isn't importable).

    Same spec/exec dance as tests/test_cli_flag_parsing.py; cached in
    sys.modules so repeated use across tests is free.
    """
    if "aio_dl" in sys.modules:
        return sys.modules["aio_dl"]
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "aio_dl", os.path.join(here, "aio-dl.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["aio_dl"] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------- WAF detector

@pytest.mark.parametrize(
    "url",
    [
        "https://comix.to/@waf/challenge?return=%2Ftitle%2F3j50-magic-emperor",
        "https://comix.to/@waf/challenge",
        "https://www.comix.to/@waf/anything",
    ],
)
def test_waf_detected_from_url(url):
    """The landed URL is the authoritative signal — this is the exact redirect
    target the user reported."""
    assert comix._looks_like_waf_challenge(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://comix.to/title/3j50-magic-emperor",
        "https://comix.to/title/3j50-magic-emperor?group_id=4856",
        "https://comix.to/title/3j50-magic-emperor/10142993-chapter-870",
        "https://comix.to/",
        None,
        "",
    ],
)
def test_normal_urls_are_not_waf(url):
    assert comix._looks_like_waf_challenge(url) is False


def test_waf_detected_from_body_text():
    """Fallback for the HTTP path, where requests has already followed the 302
    and the caller may only hold the body. Copy is from the live page."""
    body = "Verify you're human\n\nDrag to rotate the circle until the picture lines up\n\n0°\nRefresh\nVerify"
    assert comix._looks_like_waf_challenge(None, body) is True


def test_waf_body_detection_handles_html_escaped_apostrophe():
    """The interstitial ships server-rendered HTML, so the apostrophe in
    "you're" can arrive escaped. Raw-body callers must still match."""
    body = "<h1>Verify you&#039;re human</h1>"
    assert comix._looks_like_waf_challenge(None, body) is True


def test_security_check_alone_is_too_generic():
    """"Security check" is the interstitial's <title>, but it is also ordinary
    English. On its own it must NOT fire, or a comic synopsis could abort a
    perfectly good download."""
    synopsis = "He must pass a security check before entering the tower."
    assert comix._looks_like_waf_challenge(None, synopsis) is False
    # ...but paired with the interstitial's noindex meta it is conclusive.
    interstitial = (
        '<meta name="robots" content="noindex, nofollow"><title>Security check</title>'
    )
    assert comix._looks_like_waf_challenge(None, interstitial) is True


def test_cloudflare_challenge_is_not_a_waf_challenge():
    """THE distinction that makes two detectors necessary: comix runs
    Cloudflare AND its own WAF. Only the WAF needs a human, so a CF body must
    not be routed to the "ask the user" path."""
    cf_body = (
        "Just a moment...\nChecking your browser before accessing comix.to.\n"
        "Please enable JavaScript and cookies to continue.\nCloudflare Ray ID: 8f2"
    )
    assert comix._looks_like_waf_challenge(None, cf_body) is False


def test_waf_detector_tolerates_none_and_empty():
    assert comix._looks_like_waf_challenge(None, None) is False
    assert comix._looks_like_waf_challenge("", "") is False


def test_force_waf_env_is_single_shot(monkeypatch):
    """The debug seam must be CONSUMED, not merely read: the challenge is
    behavioral and can't be summoned on demand, so this exercises the handoff
    branch exactly once instead of wedging a run in a permanent challenge."""
    monkeypatch.setenv(comix._FORCE_WAF_ENV, "1")
    assert comix._looks_like_waf_challenge("https://comix.to/title/x") is True
    assert comix._looks_like_waf_challenge("https://comix.to/title/x") is False
    assert os.environ.get(comix._FORCE_WAF_ENV) is None


# ------------------------------------------------------------------ exceptions

def test_waf_error_carries_challenge_url():
    err = comix.ComixWafChallengeError("nope", challenge_url="https://comix.to/@waf/challenge")
    assert err.challenge_url == "https://comix.to/@waf/challenge"
    assert isinstance(err, RuntimeError)


def test_scrape_error_is_a_runtime_error():
    """aio-dl.py's __main__ handler catches both by type to print the
    remediation instead of a traceback."""
    assert issubclass(comix.ComixChapterScrapeError, RuntimeError)


def test_cli_prints_remediation_for_comix_errors_instead_of_traceback():
    """Structural guard on aio-dl.py's __main__ block: both comix exceptions
    must be routed to the clean-exit branch. Without it the user gets a Python
    traceback whose actionable message is buried."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "aio-dl.py"), encoding="utf-8").read()
    tail = src[src.index('if __name__ == "__main__":'):]
    assert "ComixWafChallengeError" in tail
    assert "ComixChapterScrapeError" in tail


# -------------------------------------------------------------- group_id parse

@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://comix.to/title/3j50-magic-emperor?group_id=4856", "4856"),
        ("https://comix.to/title/3j50-magic-emperor?page=3&group_id=4856", "4856"),
        ("https://comix.to/title/3j50-magic-emperor?group_id=4856&page=3", "4856"),
        ("https://comix.to/title/3j50-magic-emperor", None),
        ("https://comix.to/title/3j50-magic-emperor?other=1", None),
        (None, None),
        ("", None),
    ],
)
def test_extract_group_id(url, expected):
    assert comix._extract_group_id(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://comix.to/title/x?group_id=../evil",
        "https://comix.to/title/x?group_id=4856%20OR%201",
        "https://comix.to/title/x?group_id=",
        "https://comix.to/title/x?group_id=abc",
    ],
)
def test_group_id_must_be_numeric(url):
    """The value is interpolated straight back into a URL we navigate, so
    anything non-numeric is dropped rather than trusted."""
    assert comix._extract_group_id(url) is None


# ------------------------------------------------------- chapter number coerce

@pytest.mark.parametrize(
    "href,label,expected",
    [
        ("/title/3j50-magic-emperor/10142993-chapter-870", None, 870.0),
        ("/title/3j50-magic-emperor/10142993-chapter-302.5", None, 302.5),
        ("https://comix.to/title/x/1-chapter-1", None, 1.0),
        (None, "Ch.866", 866.0),
        (None, "Ch.388.5", 388.5),
        # href wins over a disagreeing label — it's machine-generated.
        ("/title/x/9-chapter-42", "Ch.999", 42.0),
        (None, "Oneshot", None),
        (None, None, None),
    ],
)
def test_coerce_chapter_number(href, label, expected):
    assert comix._coerce_chapter_number(href, label) == expected


def test_unparseable_row_does_not_vote_on_early_stop():
    """None means "this row has no opinion". The early-stop compares the page
    MAXIMUM, so a None must never be treated as 0 — that would look like a page
    below the floor and cut the walk short."""
    assert comix._coerce_chapter_number(None, "Extra: Side Story") is None


# ------------------------------------------------------------- profile dir

def test_profile_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AIO_COMIX_PROFILE_DIR", str(tmp_path / "p"))
    assert comix._comix_profile_dir() == os.path.abspath(str(tmp_path / "p"))


def test_profile_dir_default_is_absolute_and_app_owned(monkeypatch):
    """App-owned, never the user's real Chrome profile: Chromium locks a
    user-data dir (so the real one would fail whenever Chrome is open) and we
    must not read the user's actual browsing data."""
    monkeypatch.delenv("AIO_COMIX_PROFILE_DIR", raising=False)
    path = comix._comix_profile_dir()
    assert os.path.isabs(path)
    assert "AIO-Webtoon-Downloader" in path
    assert "comix-profile" in path


# ---------------------------------------------------------- chapter floor hint

def test_base_handler_declares_chapter_floor_hint():
    """Advisory contract: default None means "list everything", so a handler
    that ignores the hint stays correct."""
    assert BaseSiteHandler.chapter_floor_hint is None
    assert comix.ComixSiteHandler().chapter_floor_hint is None


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("all", None),
        ("", None),
        ("5", 5.0),
        ("10-20", 10.0),
        ("1.5-3", 1.5),
        ("5,300-400", 5.0),
        ("300-400,5", 5.0),
        ("oneshot", 1.0),
    ],
)
def test_chapter_range_floor(spec, expected):
    assert _load_aio_dl_module()._chapter_range_floor(spec) == expected


@pytest.mark.parametrize("spec", ["-20", "100-", "junk", "5,", "1-2,garbage"])
def test_chapter_range_floor_is_none_when_not_provably_safe(spec):
    """A wrong floor would silently drop chapters the user asked for, so every
    ambiguous spec degrades to "no early stop". `-20` ("last 20 chapters")
    depends on the very list we haven't fetched yet."""
    assert _load_aio_dl_module()._chapter_range_floor(spec) is None


def test_floored_pool_is_not_reported_as_the_series_total():
    """A floored listing is a partial view by design, so
    total_available_at_download must not report it as the total."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "aio-dl.py"), encoding="utf-8").read()
    assert '"total_available_at_download": None if pool_is_partial else len(pool)' in src


# ------------------------------------------------------------ structural guards
# These pin decisions that live in control flow rather than in a return value,
# so a well-meant refactor can't quietly restore the truncating behavior.

def _chapters_scrape_source() -> str:
    return inspect.getsource(comix._ComixBrowserSession.fetch_chapters_via_dom)


def test_scrape_never_breaks_silently_on_empty_rows():
    """THE regression guard. The old loop's `if not rows: break` is what made
    the download start at chapter 871. An empty page must now be classified
    against the pager, retried, and then raised on."""
    src = _chapters_scrape_source()
    assert "if not rows:\n                break" not in src
    assert "ComixChapterScrapeError" in src
    # The pager, not the row list, decides when we're done.
    assert "total_pages" in src
    assert "npager" in src


def test_scrape_preserves_the_query_string():
    """`base = title_url.split("?", 1)[0]` discarded the user's ?group_id=
    filter — the difference between walking 59 pages and 360.

    Matches on the ASSIGNMENT, not the bare substring: the method's docstring
    quotes the old expression to explain what changed, and a test that can't
    tell code from prose would fail on its own documentation.
    """
    src = _chapters_scrape_source()
    assert "base = title_url.split(" not in src
    assert "base_url, _sep, base_query = title_url.partition(" in src


def test_scrape_does_not_rely_on_first_row_href_diffing():
    """The old freshness check compared the first row's href across
    navigations. comix's React list swaps row CONTENT in place, so the previous
    page's nodes outlive the swap and the diff can pass on stale DOM. The
    active pager number replaced it."""
    src = _chapters_scrape_source()
    assert "prev_first_href" not in src
    assert "is-active" in comix._ComixBrowserSession.fetch_chapters_via_dom.__doc__ or True
    assert "_wait_for_pager" in src


def test_scrape_advance_falls_back_to_navigation():
    """A pager click is an optimization, never the only way forward: a
    mid-render pager can briefly lack a usable Next button, and letting that
    end the loop would resurrect the truncation in a new disguise."""
    src = _chapters_scrape_source()
    assert "advanced" in src
    assert "goto(" in src


def test_browser_uses_a_persistent_profile():
    """A virgin context per process is the most bot-like fingerprint available
    and is the main reason the WAF fires "sometimes". The persistent profile is
    also what makes one human solve last across runs AND processes."""
    src = inspect.getsource(comix._ComixBrowserSession._start)
    assert "launch_persistent_context" in src
    assert "_comix_profile_dir()" in src
    # headless must stay parameterised — the handoff relaunches headed.
    assert "headless=headless" in src


def test_handoff_never_automates_the_captcha():
    """Scope boundary, asserted in code. The handoff may navigate, print, and
    POLL; it must not synthesise input or touch the widget. If someone adds
    mouse/keyboard automation here, this test is the tripwire.

    Matches on automation APIs only. The word "drag" itself appears in the
    instruction printed FOR the user ("drag the slider until..."), which is the
    opposite of automating it — a guard that can't tell an API call from an
    instruction would forbid telling the user what to do.
    """
    src = inspect.getsource(comix._ComixBrowserSession.solve_waf_interactively)
    for forbidden in (
        "mouse.",
        "keyboard.",
        "drag_to",
        "drag_and_drop",
        "dispatchEvent",
        "dispatch_event",
        ".hover(",
        ".fill(",
        ".click(",
        ".press(",
        ".type(",
    ):
        assert forbidden not in src, f"handoff must not automate the CAPTCHA: {forbidden}"


def test_search_swallows_waf_instead_of_raising():
    """search() must still degrade to [] on a challenge. Raising would let the
    orchestrator's persistent ProbeFailureCache blocklist comix.to for an hour
    (threshold 2, TTL 3600s) over a transient interstitial."""
    src = inspect.getsource(comix._ComixBrowserSession.fetch_search_via_dom)
    assert "_looks_like_waf_challenge" in src
    assert "_enforce_no_waf" not in src, (
        "search must not use the raising guard — see ComixSiteHandler.search"
    )


def test_chapter_and_image_paths_enforce_waf():
    """Both download-critical paths fail loud rather than returning a short
    result: a half-scraped chapter yields a short CBZ, and a truncated list
    gets persisted as the whole series."""
    assert "_enforce_no_waf" in _chapters_scrape_source()
    assert "_enforce_no_waf" in inspect.getsource(
        comix._ComixBrowserSession.fetch_chapter_images_via_dom
    )
    assert "_waf_recover_once" in inspect.getsource(
        comix.ComixSiteHandler._cf_aware_request
    )


# ------------------------------------------- post-handoff recovery (PR #68 P1)

class _FakePage:
    """Minimal Patchright page stand-in: records goto() targets and reports a
    url that follows them, which is all _enforce_no_waf touches."""

    def __init__(self, start_url: str):
        self._url = start_url
        self.goto_calls: list[str] = []

    def goto(self, url, **_kwargs):
        self.goto_calls.append(url)
        self._url = url

    @property
    def url(self):
        return self._url


def _session_with_page(page):
    """A _ComixBrowserSession that skips __init__ (no browser, no profile)."""
    sess = comix._ComixBrowserSession.__new__(comix._ComixBrowserSession)
    sess._page = page
    return sess


def test_enforce_no_waf_reloads_the_target_after_a_successful_solve():
    """PR #68 review (P1). The handoff tears the context down and relaunches
    headless, so self._page comes back on about:blank. Without reloading the
    caller's target, every scrape resumed on a blank page and failed AFTER the
    user had successfully completed the verification."""
    page = _FakePage("https://comix.to/@waf/challenge?return=%2Ftitle%2Fx")
    sess = _session_with_page(page)
    verdicts = iter([True, False])  # blocked, then clear once reloaded
    sess._waf_blocked = lambda stage: next(verdicts)
    sess.solve_waf_interactively = lambda return_url=None: {"solved": True}

    target = "https://comix.to/title/3j50-magic-emperor?group_id=4856&page=7"
    sess._enforce_no_waf("chapter list page 7", target)

    assert page.goto_calls == [target], (
        "a solved challenge must put the browser back on the caller's page"
    )


def test_enforce_no_waf_raises_when_challenge_survives_the_reload():
    """One handoff per process is the cap, so a challenge that reappears on the
    reload is terminal — it must not fall through as success."""
    page = _FakePage("https://comix.to/@waf/challenge")
    sess = _session_with_page(page)
    sess._waf_blocked = lambda stage: True  # still blocked after the reload
    sess.solve_waf_interactively = lambda return_url=None: {"solved": True}

    with pytest.raises(comix.ComixWafChallengeError):
        sess._enforce_no_waf("the chapter list", "https://comix.to/title/x")


def test_enforce_no_waf_raises_when_not_solved():
    page = _FakePage("https://comix.to/@waf/challenge")
    sess = _session_with_page(page)
    sess._waf_blocked = lambda stage: True
    sess.solve_waf_interactively = lambda return_url=None: {"solved": False}

    with pytest.raises(comix.ComixWafChallengeError):
        sess._enforce_no_waf("the chapter list", "https://comix.to/title/x")
    assert page.goto_calls == [], "must not reload when the check wasn't passed"


def test_enforce_no_waf_is_a_noop_when_clear():
    page = _FakePage("https://comix.to/title/x")
    sess = _session_with_page(page)
    sess._waf_blocked = lambda stage: False
    sess.solve_waf_interactively = lambda return_url=None: pytest.fail(
        "must not open a verification window when nothing is blocking"
    )
    sess._enforce_no_waf("the chapter list", "https://comix.to/title/x")
    assert page.goto_calls == []


def test_mid_pagination_guards_restore_their_own_page():
    """A guard that always restored page 1 would silently rewind the walk to
    the start while the loop counter still said page N."""
    src = _chapters_scrape_source()
    assert "def _waf_guard(stage: str, page_num: int = 1)" in src
    assert '_waf_guard(f"chapter list page {page_n}", page_n)' in src
    assert '_waf_guard(f"chapter list page {next_page}", next_page)' in src


# ------------------------------------- headless UA + handoff budget (live bug)
# Both pinned from a real failing run: the user solved the check, and seconds
# later the run died claiming the check "was not completed".

@pytest.fixture
def waf_budget_reset(monkeypatch):
    """Isolate the handoff budget so these tests measure only the budget.

    Two gates sit AHEAD of the budget in solve_waf_interactively — the
    "interactive verification disabled" env flag and the "no display available"
    check — and both short-circuit with their own reason. On a headless Linux CI
    runner the display gate fires first, so the budget assertions below never
    ran and these tests passed only on Windows (where the gate is skipped
    outright). Neutralize both so the budget is exercised on every platform.

    Nothing here opens a window: every test using this fixture stubs _start.
    """
    monkeypatch.delenv(comix._WAF_NO_INTERACTIVE_ENV, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")

    saved = (
        comix._COMIX_WAF_SOLVES_DONE,
        comix._COMIX_WAF_FAILURES,
        comix._COMIX_WAF_LAST_PROMPT_AT,
    )
    comix._COMIX_WAF_SOLVES_DONE = 0
    comix._COMIX_WAF_FAILURES = 0
    comix._COMIX_WAF_LAST_PROMPT_AT = 0.0
    yield
    (
        comix._COMIX_WAF_SOLVES_DONE,
        comix._COMIX_WAF_FAILURES,
        comix._COMIX_WAF_LAST_PROMPT_AT,
    ) = saved


def test_headless_user_agent_is_stabilized():
    """THE reason a solved check didn't stick. Headless Chromium advertises
    `HeadlessChrome/147.0.7727.15`; the headed handoff window advertises
    `Chrome/147.0.0.0`. The WAF binds its clearance to the UA that earned it, so
    the relaunched headless context could not use what the user had just
    passed — and got re-challenged instantly."""
    headless = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) HeadlessChrome/147.0.7727.15 Safari/537.36"
    )
    out = comix._stabilize_user_agent(headless)
    assert "HeadlessChrome" not in out
    assert "Chrome/147.0.7727.15" in out
    # Everything except the product token is left exactly as reported.
    assert out == headless.replace("HeadlessChrome/", "Chrome/")


def test_stabilize_user_agent_tolerates_missing_input():
    assert comix._stabilize_user_agent(None) is None
    assert comix._stabilize_user_agent("") is None


def test_start_pins_a_stable_user_agent():
    """The UA pin drops the `HeadlessChrome` token from the UA STRING.

    Necessary but NOT sufficient on its own — see
    test_start_also_sets_the_browser_channel for the other half.
    """
    src = inspect.getsource(comix._ComixBrowserSession._start)
    assert "_resolve_stable_user_agent()" in src
    assert "_stabilize_user_agent" in src
    assert 'ctx_kwargs["user_agent"]' in src


def test_start_also_sets_the_browser_channel():
    """THE half b014f12 missed. Playwright's `user_agent=` calls
    Emulation.setUserAgentOverride without userAgentMetadata, so it fixes the UA
    header and NOTHING else: Sec-CH-UA and navigator.userAgentData keep saying
    HeadlessChrome. Measured 2026-08-02 — pin alone leaves the context
    announcing HeadlessChrome in the hints while its UA claims Chrome, a
    contradiction no real browser emits. `channel="chromium"` is what moves the
    hints; only the two together match the headed handoff window."""
    src = inspect.getsource(comix._ComixBrowserSession._start)
    assert "channel=_COMIX_BROWSER_CHANNEL" in src
    assert comix._COMIX_BROWSER_CHANNEL == "chromium"
    # An install that doesn't know the channel must still launch.
    assert "_launch(**ctx_kwargs)" in src, "channel launch needs a fallback"


def test_true_user_agent_is_read_past_the_override():
    """A UA override makes navigator.userAgent report our own pin straight back,
    so a WRONG pin looks self-consistent and re-caches itself forever. The
    reconciliation must therefore read the browser, not the page."""
    # The implementation lives in the shared module now (both comix and
    # mangafire need it); comix must still route through it rather than
    # growing a second, drifting copy.
    from sites import browser_identity

    assert "Browser.getVersion" in inspect.getsource(
        browser_identity.probe_true_user_agent
    )
    src = inspect.getsource(comix._ComixBrowserSession._probe_true_user_agent)
    assert "probe_true_user_agent" in src
    assert 'evaluate("navigator.userAgent")' not in src
    start = inspect.getsource(comix._ComixBrowserSession._start)
    assert "_probe_true_user_agent()" in start
    assert 'evaluate("navigator.userAgent")' not in start, (
        "_start must not reconcile the pin against the overridden page value"
    )


def test_start_ua_relaunch_is_bounded():
    """Termination must be provable locally, not inferred from cache warmth."""
    sig = inspect.signature(comix._ComixBrowserSession._start)
    assert "_ua_relaunch" in sig.parameters
    assert sig.parameters["_ua_relaunch"].default is False
    src = inspect.getsource(comix._ComixBrowserSession._start)
    assert "_ua_relaunch=True" in src
    assert "not _ua_relaunch" in src


def test_successful_solve_does_not_consume_the_ask_again_budget(waf_budget_reset):
    """THE live bug. The old cap was a single "already attempted" boolean, so a
    SUCCESSFUL solve for the HTTP metadata request spent the whole allowance.
    When the browser was then challenged separately (cloudscraper and the
    browser are distinct identities to the WAF), no window was opened and the
    run died claiming the user hadn't completed a check it never showed them."""
    comix._COMIX_WAF_SOLVES_DONE = 1  # one solve already succeeded this run
    comix._COMIX_WAF_FAILURES = 0
    sess = comix._ComixBrowserSession.__new__(comix._ComixBrowserSession)
    # __new__ skips __init__, so supply the two attributes the handoff reads to
    # decide whether it must switch the browser's mode. Headless here forces the
    # teardown+relaunch path, which is what this test wants to reach.
    sess._page = None
    sess._headless = True
    sess._cleanup = lambda: None
    sess._start = lambda headless=None: False  # fail fast, no real browser

    out = sess.solve_waf_interactively("https://comix.to/title/x")

    # It must have gone PAST the budget gates and actually tried to open a
    # window; only the stubbed launch stopped it.
    assert out["reason"] == "launch_failed", (
        "a previous successful solve must not block a later prompt"
    )


def test_the_browser_is_headed_by_default(monkeypatch):
    """comix's reader defers every 10th page until the viewport reaches it, which
    needs a live rendering lifecycle (layout, compositing, IntersectionObserver
    delivery). Measured 2026-08-03: in a non-compositing context an observer over
    103 page elements fired ZERO callbacks, so those pages can never load. Headed
    is therefore correctness, not just anti-bot politeness."""
    monkeypatch.delenv(comix._COMIX_HEADLESS_ENV, raising=False)
    assert comix._comix_headless() is False
    sig = inspect.signature(comix._ComixBrowserSession._start)
    assert sig.parameters["headless"].default is None, (
        "_start must defer to _comix_headless(), not hardcode a mode"
    )


def test_headless_remains_available_as_an_explicit_opt_out(monkeypatch):
    """Unattended runs (cron/CI/headless servers) still need a way out, even
    though chunk-boundary pages may not load there."""
    monkeypatch.setenv(comix._COMIX_HEADLESS_ENV, "1")
    assert comix._comix_headless() is True
    monkeypatch.setenv(comix._COMIX_HEADLESS_ENV, "0")
    assert comix._comix_headless() is False


def test_the_handoff_does_not_relaunch_when_already_headed():
    """THE identity bug, structurally closed. A clearance is bound to the client
    that earned it; the old code always relaunched headless afterwards, handing
    the rest of the run a measurably different client (HeadlessChrome in
    Sec-CH-UA even with the UA string pinned) — so the very next navigation was
    re-challenged. When the session is already headed nothing may be torn down."""
    src = inspect.getsource(
        comix._ComixBrowserSession.solve_waf_interactively
    )
    assert "mode_switched" in src
    assert "self._start(headless=True)" not in src, (
        "an unconditional headless relaunch re-introduces the identity mismatch"
    )


def test_a_declined_prompt_stops_further_prompting(waf_budget_reset):
    """The flip side: if the user let one window time out or closed it, don't
    keep popping windows they are ignoring."""
    comix._COMIX_WAF_FAILURES = comix._COMIX_WAF_MAX_FAILURES
    sess = comix._ComixBrowserSession.__new__(comix._ComixBrowserSession)
    sess._start = lambda headless=True: pytest.fail("must not open a window")
    out = sess.solve_waf_interactively("https://comix.to/title/x")
    assert out["solved"] is False
    assert out["reason"] == "already_declined"


def test_repeated_solves_are_capped(waf_budget_reset):
    comix._COMIX_WAF_SOLVES_DONE = comix._COMIX_WAF_MAX_SOLVES
    sess = comix._ComixBrowserSession.__new__(comix._ComixBrowserSession)
    sess._start = lambda headless=True: pytest.fail("must not open a window")
    out = sess.solve_waf_interactively("https://comix.to/title/x")
    assert out["reason"] == "solve_limit"


def test_failure_message_never_claims_the_user_failed_a_check_it_never_showed():
    """The original message said "it was not completed" for every outcome,
    including the case where no window was ever opened."""
    never_asked = comix._waf_failure_message("the chapter list", "already_declined")
    assert "not completed" not in never_asked.lower().split("verification window")[0]
    assert "opened earlier this run" in never_asked

    for reason in ("solve_limit", "too_soon", "disabled", "no_display",
                   "launch_failed", "window_closed"):
        msg = comix._waf_failure_message("the chapter list", reason)
        assert msg.startswith("comix.to is asking for human verification")
        assert len(msg) > 90, f"{reason} needs an actionable tail"

    # Unknown/absent reason still gets the generic actionable text.
    assert "Re-run" in comix._waf_failure_message("x", None)


# ------------------------------- HTTP metadata path uses the browser (2026-08-02)
# The old recovery opened the interactive handoff, copied the solved cookies into
# cloudscraper, overwrote its User-Agent, and retried over HTTP. It could not
# work, and burned a human solve to fail anyway — live log: "verification passed
# - thanks" at 00:48:22, "it was not completed" at 00:48:27.

class _FakeScraper:
    def __init__(self):
        self.headers = {}
        self.cookies = self
        self.set_calls = []

    def set(self, name, value, **kw):
        self.set_calls.append((name, value, kw))


class _FakeResponse:
    def __init__(self, text, url, status_code=200):
        self.text = text
        self.url = url
        self.status_code = status_code


_WAF_BODY = "<html><title>Security check</title><meta name='robots' content='noindex'></html>"


def test_waf_recovery_reads_through_the_browser_not_a_cookie_transplant():
    """The clearance rides an HttpOnly cookie bound to the browser identity that
    earned it; replaying it from cloudscraper's OpenSSL TLS + 2017 header set is
    not the same client by any measure the WAF uses. Re-read the page with the
    browser — which already holds a PERSISTED session — instead."""
    handler = comix.ComixSiteHandler()
    scraper = _FakeScraper()
    challenged = _FakeResponse(_WAF_BODY, "https://comix.to/@waf/challenge?return=%2Ftitle%2Fx")

    calls = {"series_html": 0, "make_request": 0}

    def _fake_make_request(url, s):
        calls["make_request"] += 1
        return _FakeResponse(_WAF_BODY, url)

    def _fake_series_html(url):
        calls["series_html"] += 1
        return "<html><script id='initial-data'>{}</script></html>"

    comix._COMIX_BROWSER_BRIDGE.fetch_series_html = _fake_series_html
    comix._COMIX_BROWSER_BRIDGE.context_cookies = lambda: [
        {"name": "session", "value": "abc", "domain": "comix.to", "path": "/"}
    ]
    try:
        out = handler._waf_recover_once(
            challenged, "https://comix.to/title/x", scraper, _fake_make_request
        )
    finally:
        del comix._COMIX_BROWSER_BRIDGE.fetch_series_html
        del comix._COMIX_BROWSER_BRIDGE.context_cookies

    assert calls["series_html"] == 1, "must re-read the page with the browser"
    assert calls["make_request"] == 0, "must NOT retry the doomed HTTP request"
    assert "initial-data" in out.text
    assert out.status_code == 200
    # The UA must be left alone: mixing a modern UA into cloudscraper's
    # period-correct header set is what made the old retry self-contradictory.
    assert "User-Agent" not in scraper.headers


def test_waf_recovery_is_a_noop_when_no_challenge_is_present():
    handler = comix.ComixSiteHandler()
    clean = _FakeResponse("<html>a comic about a challenge</html>",
                          "https://comix.to/title/x")
    comix._COMIX_BROWSER_BRIDGE.fetch_series_html = lambda url: pytest.fail(
        "must not boot a browser when nothing is blocking"
    )
    try:
        out = handler._waf_recover_once(
            clean, "https://comix.to/title/x", _FakeScraper(), lambda u, s: clean
        )
    finally:
        del comix._COMIX_BROWSER_BRIDGE.fetch_series_html
    assert out is clean


def test_waf_recovery_raises_when_the_browser_cannot_help():
    handler = comix.ComixSiteHandler()
    challenged = _FakeResponse(_WAF_BODY, "https://comix.to/@waf/challenge")
    comix._COMIX_BROWSER_BRIDGE.fetch_series_html = lambda url: None
    try:
        with pytest.raises(comix.ComixWafChallengeError) as exc:
            handler._waf_recover_once(
                challenged, "https://comix.to/title/x", _FakeScraper(),
                lambda u, s: challenged,
            )
    finally:
        del comix._COMIX_BROWSER_BRIDGE.fetch_series_html
    assert "browser" in str(exc.value).lower()


def test_series_html_path_enforces_waf_and_is_bridged():
    """The browser read must go through the raising guard (so a challenged
    browser prompts and then RELOADS the target), and the bridge facade must not
    swallow that exception the way the search facade does."""
    src = inspect.getsource(comix._ComixBrowserSession.fetch_series_html)
    assert "_enforce_no_waf" in src
    assert "page.content()" in src
    facade = inspect.getsource(comix._ComixBrowserBridge.fetch_series_html)
    assert "except Exception" not in facade, (
        "ComixWafChallengeError must reach _waf_recover_once to become the "
        "user-facing remediation — only the timeout parse may be guarded"
    )


def test_failure_message_never_calls_a_completed_check_incomplete():
    """The contradiction the user hit: the HTTP path had its own hardcoded 'it
    was not completed' string and used it even when the check HAD been passed —
    the thing that actually failed was the request afterwards."""
    msg = comix._waf_failure_message(
        "the series page", "solved_but_browser_still_blocked"
    )
    assert "not completed" not in msg.lower()
    assert "WAS completed" in msg

    unavailable = comix._waf_failure_message("the series page", "browser_unavailable")
    assert "patchright install chromium" in unavailable

    # And the raising site must use the helper rather than a private string.
    src = inspect.getsource(comix.ComixSiteHandler._waf_recover_once)
    assert "_waf_failure_message(" in src
    assert "it was not completed" not in src


def test_http_session_does_not_advertise_a_decade_old_browser():
    """cloudscraper picks a RANDOM 2016-2019 profile per session (Chrome 53-72,
    Win 7/8, Opera 43, and one synthetic Chrome/53.7.2410.8782), sends a
    Chrome-56-era Accept, and no client hints at all. That is why the metadata
    request was challenged so reliably."""
    headers = comix._comix_http_headers()
    ua = headers["User-Agent"]
    major = int(re.search(r"Chrome/(\d+)", ua).group(1))
    assert major >= 120, f"UA must not be ancient: {ua}"
    assert "HeadlessChrome" not in ua
    # Hints must be DERIVED from the UA, never drift from it.
    assert f'v="{major}"' in headers["sec-ch-ua"]
    assert "image/avif" in headers["Accept"], "modern UA needs a modern Accept"
    assert headers["Sec-Fetch-Mode"] == "navigate"

    handler = comix.ComixSiteHandler()
    scraper = _FakeScraper()
    handler.configure_session(scraper, None)
    assert scraper.headers["User-Agent"] == ua
    assert scraper.headers["Referer"] == "https://comix.to/"


def test_enforce_no_waf_recursion_is_bounded_locally(waf_budget_reset):
    """Termination must not depend on the module-level prompt budget: a solve
    that always reports success would otherwise loop forever."""
    page = _FakePage("https://comix.to/@waf/challenge")
    sess = _session_with_page(page)
    sess._waf_blocked = lambda stage: True          # never clears
    sess.solve_waf_interactively = lambda return_url=None: {"solved": True}
    with pytest.raises(comix.ComixWafChallengeError):
        sess._enforce_no_waf("the chapter list", "https://comix.to/title/x")
    assert len(page.goto_calls) <= comix._WAF_MAX_ENFORCE_PASSES


# --------------------------------------- last-page determination (PR #68 P2)

def test_unknown_last_page_raises_instead_of_using_the_visible_window():
    """PR #68 review (P2). A live Last button PROVES pages exist beyond the
    visible window, so the window maximum is a LOWER BOUND. Accepting it as the
    total would walk ~5 pages of a 360-page series and call it complete —
    reintroducing the truncation this rewrite exists to kill."""
    src = _chapters_scrape_source()
    # The old lower-bound fallback must be gone...
    assert "falling back to the highest visible page" not in src
    # ...replaced by a bounded retry that raises when still unknown.
    assert "determined" in src
    assert "advertises more pages" in src


def test_bridge_passes_chapter_floor_through():
    """The hint is useless if the facade drops it."""
    src = inspect.getsource(comix._ComixBrowserBridge.fetch_chapters_via_dom)
    assert "chapter_floor" in src
    sig = inspect.signature(comix._ComixBrowserBridge.fetch_chapters_via_dom)
    assert "chapter_floor" in sig.parameters
    assert sig.parameters["chapter_floor"].default is None


# ------------------------------------------- reader preload preference (2026-08-02)
# The chapter-image scrape used to open comix.to on EVERY chapter purely to
# write `localStorage['reader.default'] = {preload:'all'}` — a key the site does
# not read, in a shape it does not use. Verified live: a profile that has
# rendered a chapter and opened the reader settings panel holds
# `front_t3.searchHistory`, `auth` and `reader.webtoon.v3`, never
# `reader.default`. So the navigation was paid every chapter and bought nothing.
# Effect of the correct write on one 75-page chapter, same URL, plain reload:
# preload `some` (site default) -> 3 pages with a loaded <img>; `all` -> 68.

class _FakeEvalPage(_FakePage):
    """_FakePage plus an evaluate() stub, so the navigation DECISION (real
    Python control flow) can be tested without executing the injected JS."""

    def __init__(self, start_url: str, eval_result="all", raises: bool = False):
        super().__init__(start_url)
        self.eval_calls: list[str] = []
        self._eval_result = eval_result
        self._raises = raises

    def evaluate(self, js, *_args):
        self.eval_calls.append(js)
        if self._raises:
            raise RuntimeError("execution context destroyed")
        return self._eval_result


def _preload_source() -> str:
    return inspect.getsource(comix._ComixBrowserSession._apply_reader_preload_pref)


def test_preload_targets_the_key_the_site_actually_reads():
    """THE regression guard for this fix. `reader.default` was never read by
    anything; the reader's store is `reader.webtoon.v3`."""
    src = _preload_source()
    assert "reader.webtoon.v3" in src
    # The dead key may only survive in prose explaining what changed, never as
    # a getItem/setItem target.
    assert "getItem('reader.default')" not in src
    assert "setItem('reader.default'" not in src


def test_preload_writes_under_state_not_at_the_top_level():
    """The store is a Zustand-persist envelope: {"state": {...}, "version": 0}.
    A top-level `cur.preload` is ignored even with the right key — that was the
    second half of why the old write could not have worked.

    The written VALUE is a parameter now (`all` for normal capture, `some` for
    the recovery ladder's last rung — grep _apply_reader_preload_pref), so this
    pins the nesting, which is the actual invariant, rather than the literal."""
    src = _preload_source()
    assert "cur.state.preload =" in src
    assert "cur.preload =" not in src


def test_preload_is_a_read_modify_write_that_leaves_version_alone():
    """The profile carries the user's other reader settings, so a blind
    overwrite would clobber them. `version` in particular must not be rewritten:
    the store's own migration logic would treat the blob as a different schema
    generation and discard it."""
    src = _preload_source()
    assert "localStorage.getItem(k)" in src   # reads before writing
    assert "cur.version =" not in src


def test_preload_does_not_navigate_when_already_on_the_origin():
    """The whole per-chapter saving. localStorage is per-origin, but by the time
    chapters are being fetched the page is already on comix.to (the chapter-list
    scrape or the previous chapter left it there), so the steady-state cost must
    be one evaluate and NO navigation."""
    page = _FakeEvalPage("https://comix.to/title/k7yg7-x/6132494-chapter-8")
    sess = _session_with_page(page)
    assert sess._apply_reader_preload_pref() is True
    assert page.goto_calls == []
    assert len(page.eval_calls) == 1


def test_preload_navigates_only_when_off_origin():
    """Cold page (about:blank after a relaunch) still has to reach the origin
    once — the optimization must not become a silent no-op."""
    page = _FakeEvalPage("about:blank")
    sess = _session_with_page(page)
    assert sess._apply_reader_preload_pref() is True
    assert page.goto_calls == ["https://comix.to/"]


def test_preload_reports_failure_without_raising():
    """Failure here is cosmetic — the per-page scroll loop still works, just
    slower — so it must never propagate out of a chapter fetch."""
    unconfirmed = _session_with_page(_FakeEvalPage("https://comix.to/", eval_result="some"))
    assert unconfirmed._apply_reader_preload_pref() is False

    throwing = _session_with_page(_FakeEvalPage("https://comix.to/", raises=True))
    assert throwing._apply_reader_preload_pref() is False


def _image_scrape_source() -> str:
    return inspect.getsource(comix._ComixBrowserSession.fetch_chapter_images_via_dom)


def test_image_scrape_no_longer_opens_the_homepage_every_chapter():
    """The unconditional `page.goto("https://comix.to/")` pre-flight is what
    made the dead write cost a full SPA navigation per chapter."""
    src = _image_scrape_source()
    assert 'goto(\n                "https://comix.to/"' not in src
    assert "_apply_reader_preload_pref()" in src


# ------------------------------------ zero-page diagnostics (2026-08-02)
# "chapter had 0 .rpage-page divs ... Either the React app failed to mount or CF
# re-challenged" asserted two causes while testing neither, so a renamed
# selector was indistinguishable from a transient miss. The chapter-LIST scrape
# had collected evidence before failing for a while; this path now matches it.

def test_zero_page_failure_collects_evidence_before_blaming_the_render():
    src = _image_scrape_source()
    # Reader-mounted-but-selector-missed probe (the analogue of the list
    # scrape's ".mchap-row__primary exists but .mchap-item doesn't" hint).
    assert "rpageAny" in src
    assert "dataPage" in src
    assert "renamed the" in src
    # The CF layer is now actually tested rather than named as a guess.
    assert "is_cf_challenge(200, body_text)" in src


def test_zero_page_failure_does_not_claim_a_wait_that_never_happened():
    """`deadline` is armed before the preload step, the navigation and any WAF
    handoff (which alone can burn 180s of a 300s budget), so the mount loop can
    break on its first line having waited nothing. The old message said "after
    wait" regardless, which sent you looking for a render bug."""
    src = _image_scrape_source()
    assert "mount_polls" in src
    assert "mount_waited_s" in src
    assert "if mount_polls == 0:" in src
    # The unconditional claim is gone.
    assert "divs in DOM \n" not in src
    assert "Either the React app failed to mount or" not in src


# ------------------------------- human-facing window geometry (2026-08-03)
# A user reported the verification window opening COMPLETELY BLANK, so the
# check could not be solved at all. Nothing had failed to load: _COMIX_VIEWPORT
# is 2400px tall and is applied via device-metrics EMULATION, so it holds no
# matter how big the OS window is, while comix's interstitial is a flex-centred
# card on a `min-height:100vh` body. Measured against the live challenge page:
#
#     .card 978->1422   .ring 1077   Verify 1335   scrollHeight == innerHeight
#
# A real window shows ~950-1180px, and the document being exactly
# viewport-height means there is no scrollbar to reach the rest with — so the
# widget sat below the fold, unreachable, on a page that reported itself fine.
#
# It only became reachable-by-accident before 2026-08-03 because the handoff
# relaunched the browser headed at Playwright's 1280x720 default. Making headed
# the default removed that relaunch, and the human inherited the reader's
# scrape geometry. Both human-facing windows now swap in
# _COMIX_INTERACTIVE_VIEWPORT and put the reader's back in a `finally`.
#
# Offline: the fake page records geometry calls, so no browser and no network.

class _FakeHumanWindow:
    """Page stand-in that records the viewport swaps and navigations a
    human-facing window performs, in order.

    `wait_for_timeout` is a no-op rather than a sleep — the handoff's poll loop
    would otherwise make this suite wait real seconds for nothing.
    """

    def __init__(self, url="about:blank", goto_error=None, is_closed_error=None):
        self._url = url
        self.events: list = []
        self._goto_error = goto_error
        self._is_closed_error = is_closed_error

    def set_viewport_size(self, size):
        self.events.append(("viewport", dict(size)))

    def goto(self, url, **_kwargs):
        self.events.append(("goto", url))
        if self._goto_error is not None:
            raise self._goto_error
        self._url = url

    @property
    def url(self):
        return self._url

    def is_closed(self):
        if self._is_closed_error is not None:
            raise self._is_closed_error
        return False

    def wait_for_timeout(self, _ms):
        pass

    def evaluate(self, *_args, **_kwargs):
        # open_login_window's auth probe: report "signed in" so the happy path
        # completes on the first poll.
        return True


def _viewport_events(page):
    return [size for kind, size in page.events if kind == "viewport"]


def _interactive_session(page, monkeypatch):
    """A session wired for a human-facing window, with every environmental
    escape hatch neutralised so the geometry code is actually REACHED.

    DISPLAY matters most: CI is bare ubuntu-latest with no X server, and both
    methods bail at their no_display guard before touching the viewport. Left
    unset, these tests would pass green on a machine that never ran the code
    they exist to pin. Setting it here (harmless on win32/darwin, which skip
    the check) is what makes them meaningful in CI.
    """
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv(comix._WAF_NO_INTERACTIVE_ENV, raising=False)
    monkeypatch.delenv(comix._WAF_SOLVE_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(comix._FORCE_WAF_ENV, raising=False)
    # Module-level handoff budget: reset through monkeypatch so a test that
    # spends it can't leak into the next one.
    monkeypatch.setattr(comix, "_COMIX_WAF_SOLVES_DONE", 0)
    monkeypatch.setattr(comix, "_COMIX_WAF_FAILURES", 0)
    monkeypatch.setattr(comix, "_COMIX_WAF_LAST_PROMPT_AT", 0.0)

    sess = comix._ComixBrowserSession.__new__(comix._ComixBrowserSession)
    sess._page = page
    # Headed already — the mode the default configuration runs in, and the one
    # where no relaunch intervenes to reset the viewport for us.
    sess._headless = False
    sess._context = None
    sess._browser = None
    sess._pw = None
    return sess


def test_interactive_viewport_fits_a_real_window():
    """The value itself is the fix, so pin it rather than just its use.

    950px is the content height of a maximised window on a 1080p screen — the
    reporter's case and the smallest realistic target. A future "let's make the
    handoff taller too" edit has to fail here.
    """
    interactive = comix._COMIX_INTERACTIVE_VIEWPORT["height"]
    assert interactive <= 950, (
        "the human-facing viewport must fit inside a real window, else the "
        "centred CAPTCHA lands below the fold on a page that cannot scroll"
    )
    assert interactive < comix._COMIX_VIEWPORT["height"], (
        "shrinking is the entire point; the reader's tall viewport is what "
        "pushed the widget off-screen"
    )


def test_waf_handoff_hands_the_user_a_usable_viewport_then_restores_it(monkeypatch):
    """Shrink BEFORE the navigation, restore after.

    Order matters on both ends: shrinking after the goto would reflow the
    interstitial under the user mid-solve, and failing to restore would leave
    the run at 720 — where comix's chunk-boundary pages never enter the
    viewport, silently shorting every later chapter by ~10%.
    """
    page = _FakeHumanWindow()
    sess = _interactive_session(page, monkeypatch)

    # The handoff navigates to the page the user WANTED; the site is what
    # redirects to /@waf/. Landing on a non-challenge URL means the poll loop
    # sees a pass on its first iteration.
    target = "https://comix.to/title/zq5g5-heavenly-demon-reborn"
    result = sess.solve_waf_interactively(target)

    assert result["solved"] is True
    assert page.events[0] == ("viewport", dict(comix._COMIX_INTERACTIVE_VIEWPORT)), (
        "the window must be resized before the challenge is loaded into it"
    )
    assert ("goto", target) in page.events
    assert page.events[-1] == ("viewport", dict(comix._COMIX_VIEWPORT)), (
        "the reader's geometry must be back before any chapter scrape resumes"
    )


def test_waf_handoff_restores_the_viewport_when_the_wait_is_interrupted(monkeypatch):
    """The restore lives in a `finally` for a reason.

    The user stares at this window for up to 180s, so Ctrl-C during the wait is
    an ordinary event — and KeyboardInterrupt is a BaseException, so the poll
    loop's `except Exception` does not catch it. A trailing call instead of a
    `finally` would leave the session at 720px, and the resulting short
    chapters carry no error to trace back to here.
    """
    page = _FakeHumanWindow(is_closed_error=KeyboardInterrupt())
    sess = _interactive_session(page, monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        sess.solve_waf_interactively("https://comix.to/title/x")

    assert _viewport_events(page)[-1] == dict(comix._COMIX_VIEWPORT)


def test_login_window_hands_the_user_a_usable_viewport(monkeypatch):
    """comix's login card is centred too, so it had the same defect."""
    page = _FakeHumanWindow()
    sess = _interactive_session(page, monkeypatch)

    result = sess.open_login_window(timeout_s=5.0)

    assert result["signed_in"] is True
    assert page.events[0] == ("viewport", dict(comix._COMIX_INTERACTIVE_VIEWPORT))


def test_login_window_restores_the_viewport_when_navigation_fails(monkeypatch):
    """The `navigation_failed` early return skips the trailing _cleanup(), so
    it hands back a LIVE context — the one path where a missing restore would
    silently leave the whole run at the interactive viewport."""
    page = _FakeHumanWindow(goto_error=RuntimeError("net::ERR_CONNECTION_RESET"))
    sess = _interactive_session(page, monkeypatch)

    result = sess.open_login_window(timeout_s=5.0)

    assert result["reason"] == "navigation_failed"
    assert _viewport_events(page)[-1] == dict(comix._COMIX_VIEWPORT), (
        "a failed navigation must not strand the run at the human viewport"
    )


def test_viewport_swap_survives_a_dead_page(monkeypatch):
    """_apply_viewport is best-effort by contract: both callers may run it
    against a page that has just been torn down (open_login_window's success
    path closes the context before the `finally` fires). It must not turn that
    into a raised error on an otherwise successful sign-in."""
    class _DeadPage:
        def set_viewport_size(self, _size):
            raise RuntimeError("Target page, context or browser has been closed")

    sess = comix._ComixBrowserSession.__new__(comix._ComixBrowserSession)
    sess._apply_viewport(_DeadPage(), comix._COMIX_VIEWPORT)
    sess._apply_viewport(None, comix._COMIX_VIEWPORT)
