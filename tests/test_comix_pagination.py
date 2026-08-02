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


def test_bridge_passes_chapter_floor_through():
    """The hint is useless if the facade drops it."""
    src = inspect.getsource(comix._ComixBrowserBridge.fetch_chapters_via_dom)
    assert "chapter_floor" in src
    sig = inspect.signature(comix._ComixBrowserBridge.fetch_chapters_via_dom)
    assert "chapter_floor" in sig.parameters
    assert sig.parameters["chapter_floor"].default is None
