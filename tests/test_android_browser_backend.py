"""Coverage for aio_android._WebViewBackend — the Python half of the Android
browser bridge.

WHY THIS IS WORTH TESTING OFFLINE: the bridge is a JNI boundary, and every bug
it can have is an encoding bug — an argument that should have been absent
arriving as `null`, a cookie header parsed into the wrong shape, a JSON payload
double-encoded. On a device those surface as "MangaFire returns no pages", three
layers away from the cause and only after a full rebuild-install-run cycle. Here
they surface as an assertion.

The fake bridge below stands in for browser/WebViewBridge.kt. Its method names
and positional signatures ARE the contract — if a name changes on one side and
not the other, Chaquopy raises at runtime with no compile-time warning, so these
tests are also the only thing pinning them.

Cross-file: android/app/src/main/java/com/aio/downloader/browser/WebViewBridge.kt
(the real implementation), sites/browser_backend.py (the Protocol being adapted).
"""

from __future__ import annotations

import json

import pytest

import aio_android
from aio_android import _WebViewBackend, _parse_cookie_header
from sites import browser_backend as bb


class FakeProfileBridge:
    """Records calls the way the Kotlin ProfileBridge would receive them."""

    def __init__(self, profile: str = "default") -> None:
        self.profile = profile
        self.calls: list[tuple] = []
        self.eval_result = '{"ok": true}'
        self.content_result = "<html></html>"
        self.selector_result = True
        self.ua_result = "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36"
        self.cookie_result = "cf_clearance=abc; session=xyz"
        self.solve_result = '{"cookies": "cf_clearance=abc", "userAgent": "UA/1.0"}'
        self.raises: set[str] = set()

    def _maybe_raise(self, name: str) -> None:
        if name in self.raises:
            raise RuntimeError(f"{name} blew up")

    def goto(self, url, timeout_ms):
        self.calls.append(("goto", url, timeout_ms))
        self._maybe_raise("goto")

    def evaluate(self, script, arg_json):
        self.calls.append(("evaluate", script, arg_json))
        self._maybe_raise("evaluate")
        return self.eval_result

    def content(self):
        self.calls.append(("content",))
        self._maybe_raise("content")
        return self.content_result

    def waitForSelector(self, selector, timeout_ms):  # noqa: N802 — Kotlin name
        self.calls.append(("waitForSelector", selector, timeout_ms))
        self._maybe_raise("waitForSelector")
        return self.selector_result

    def userAgent(self):  # noqa: N802 — Kotlin name
        self.calls.append(("userAgent",))
        self._maybe_raise("userAgent")
        return self.ua_result

    def cookies(self, url):
        self.calls.append(("cookies", url))
        self._maybe_raise("cookies")
        return self.cookie_result

    def solveChallenge(self, url, timeout_ms):  # noqa: N802 — Kotlin name
        self.calls.append(("solveChallenge", url, timeout_ms))
        self._maybe_raise("solveChallenge")
        return self.solve_result


@pytest.fixture
def bridge():
    return FakeProfileBridge()


@pytest.fixture
def backend(bridge):
    return _WebViewBackend(bridge, "mangafire")


# --------------------------------------------------------------------------
# evaluate: the NOARG contract
#
# sites/browser_backend.py's header is explicit that "" means "no argument" and
# that `null` is a legal argument value. Conflating them invokes a zero-arg
# script as fn(null), which is harmless for the two scripts in the tree today
# and wrong for the next one.
# --------------------------------------------------------------------------

def test_evaluate_without_arg_sends_empty_arg_json(backend, bridge):
    backend.evaluate("() => 1")
    assert bridge.calls[-1] == ("evaluate", "() => 1", "")


def test_evaluate_with_explicit_noarg_sentinel_sends_empty_arg_json(backend, bridge):
    # Callers holding the real browser_backend.NOARG must work too — that is the
    # sentinel PatchrightBackend's callers use.
    backend.evaluate("() => 1", bb.NOARG)
    assert bridge.calls[-1] == ("evaluate", "() => 1", "")


def test_evaluate_with_explicit_none_sends_null(backend, bridge):
    backend.evaluate("(a) => a", None)
    assert bridge.calls[-1] == ("evaluate", "(a) => a", "null")


def test_evaluate_serializes_structured_arg(backend, bridge):
    specs = [["/titles", [["keyword", "frieren"]]]]
    backend.evaluate("async (s) => s", specs)
    assert json.loads(bridge.calls[-1][2]) == specs


# --------------------------------------------------------------------------
# evaluate: decoding what comes back
# --------------------------------------------------------------------------

def test_evaluate_parses_json_result(backend, bridge):
    bridge.eval_result = '{"moduleUrl": "https://x/polyfill-a1b2.js", "exportName": "z"}'
    assert backend.evaluate("() => 1") == {
        "moduleUrl": "https://x/polyfill-a1b2.js",
        "exportName": "z",
    }


def test_evaluate_empty_result_is_none(backend, bridge):
    bridge.eval_result = ""
    assert backend.evaluate("() => 1") is None


def test_evaluate_json_null_is_none(backend, bridge):
    bridge.eval_result = "null"
    assert backend.evaluate("() => 1") is None


def test_evaluate_non_json_result_comes_back_as_the_raw_string(backend, bridge):
    # Deliberate: the vrf bootstrap checks isinstance(dict) and reports its own
    # error, which is more useful than a JSONDecodeError from in here.
    bridge.eval_result = "not json at all"
    assert backend.evaluate("() => 1") == "not json at all"


# --------------------------------------------------------------------------
# navigation / content
# --------------------------------------------------------------------------

def test_goto_drops_wait_until_and_passes_int_timeout(backend, bridge):
    backend.goto("https://mangafire.to/", wait_until="networkidle", timeout_ms=45000)
    # wait_until has no WebView analogue; the timeout must arrive as an int
    # because the Kotlin signature takes a primitive Int.
    assert bridge.calls[-1] == ("goto", "https://mangafire.to/", 45000)
    assert isinstance(bridge.calls[-1][2], int)


def test_content_is_returned_verbatim_not_json_decoded(backend, bridge):
    # The Kotlin side already decodes the JSON string evaluateJavascript hands
    # back. Decoding again here would mangle any HTML containing a quote.
    bridge.content_result = '<html><body data-x="1"></body></html>'
    assert backend.content() == '<html><body data-x="1"></body></html>'


def test_content_none_becomes_empty_string(backend, bridge):
    bridge.content_result = None
    assert backend.content() == ""


def test_wait_for_selector_returns_bool(backend, bridge):
    assert backend.wait_for_selector("#reader", timeout_ms=1000) is True
    assert bridge.calls[-1] == ("waitForSelector", "#reader", 1000)


def test_wait_for_selector_swallows_bridge_failures(backend, bridge):
    # Contract: "Never raises on timeout — callers treat a missing selector as
    # 'maybe it never renders' and continue."
    bridge.raises.add("waitForSelector")
    assert backend.wait_for_selector("#reader") is False


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------

def test_user_agent_passes_through(backend, bridge):
    assert backend.user_agent().startswith("Mozilla/5.0")


def test_user_agent_degrades_to_empty_string(backend, bridge):
    bridge.raises.add("userAgent")
    assert backend.user_agent() == ""


def test_cookies_parses_the_flat_header(backend, bridge):
    got = backend.cookies("https://mangafire.to/title/x")
    assert got == [
        {"name": "cf_clearance", "value": "abc", "domain": "mangafire.to", "path": "/"},
        {"name": "session", "value": "xyz", "domain": "mangafire.to", "path": "/"},
    ]


def test_cookies_degrade_to_empty_list(backend, bridge):
    bridge.raises.add("cookies")
    assert backend.cookies("https://mangafire.to/") == []


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", []),
        ("   ", []),
        # No '=' at all is not a cookie.
        ("garbage", []),
        # A value may legitimately contain '=' (base64 padding is the common
        # case), so only the FIRST '=' separates.
        ("t=eyJhbGci==", [("t", "eyJhbGci==")]),
        # CookieManager emits "; " separators, but be liberal.
        ("a=1;b=2", [("a", "1"), ("b", "2")]),
        ("  a = 1 ;  b = 2 ", [("a", "1"), ("b", "2")]),
        # An empty value is real — it is how a server deletes a cookie.
        ("a=", [("a", "")]),
    ],
)
def test_parse_cookie_header_edge_cases(raw, expected):
    got = _parse_cookie_header(raw, "https://example.org/x")
    assert [(c["name"], c["value"]) for c in got] == expected


def test_parse_cookie_header_synthesizes_domain_from_the_url():
    # CookieManager gives no domain metadata; the URL's host IS the scope the
    # browser applied, which is all get_cf_session needs.
    got = _parse_cookie_header("a=1", "https://sub.example.org:8443/deep/path?q=1")
    assert got[0]["domain"] == "sub.example.org:8443"
    assert got[0]["path"] == "/"


# --------------------------------------------------------------------------
# challenge solving
# --------------------------------------------------------------------------

def test_supports_challenge_solving(backend):
    # True is the whole point of the Android backend: the challenge goes to a
    # human instead of to a headless browser. PatchrightBackend returns False.
    assert backend.supports_challenge_solving is True


def test_solve_challenge_converts_seconds_to_millis(backend, bridge):
    backend.solve_challenge("https://x.org/", timeout_s=45.0)
    assert bridge.calls[-1] == ("solveChallenge", "https://x.org/", 45000)


def test_solve_challenge_parses_a_flat_cookie_string(backend, bridge):
    got = backend.solve_challenge("https://x.org/")
    assert got["user_agent"] == "UA/1.0"
    assert got["cookies"] == [
        {"name": "cf_clearance", "value": "abc", "domain": "x.org", "path": "/"},
    ]


def test_solve_challenge_accepts_a_prebuilt_cookie_list(backend, bridge):
    listed = [{"name": "a", "value": "1", "domain": "x.org", "path": "/"}]
    bridge.solve_result = json.dumps({"cookies": listed, "user_agent": "UA/2.0"})
    got = backend.solve_challenge("https://x.org/")
    assert got["cookies"] == listed
    # Both spellings are accepted; Kotlin sends camelCase.
    assert got["user_agent"] == "UA/2.0"


def test_solve_challenge_survives_a_malformed_payload(backend, bridge):
    bridge.solve_result = "<html>not json</html>"
    got = backend.solve_challenge("https://x.org/")
    assert got == {"cookies": [], "user_agent": ""}


# --------------------------------------------------------------------------
# factory wiring
# --------------------------------------------------------------------------

class FakeBridgeFactory:
    """Stands in for the Kotlin WebViewBridge object."""

    def __init__(self) -> None:
        self.requested: list[str] = []
        self.made: dict[str, FakeProfileBridge] = {}

    def forProfile(self, profile):  # noqa: N802 — Kotlin name
        self.requested.append(profile)
        return self.made.setdefault(profile, FakeProfileBridge(profile))


@pytest.fixture
def clean_registry():
    yield
    # The registry is process-wide; leaving a fake installed would hand every
    # later test a browser that isn't one.
    bb.set_backend_factory(None)


def test_set_browser_bridge_gives_each_profile_its_own_session(clean_registry):
    factory = FakeBridgeFactory()
    aio_android.set_browser_bridge(factory)

    mangafire = bb.custom_backend("mangafire")
    fetch = bb.custom_backend("fetch")

    assert factory.requested == ["mangafire", "fetch"]
    # Distinct sessions is the point: MangaFire bootstraps window.__aioMfSign
    # into a live page and an unrelated navigation on the same WebView would
    # wipe it.
    assert mangafire is not fetch


def test_backends_are_memoized_per_profile(clean_registry):
    factory = FakeBridgeFactory()
    aio_android.set_browser_bridge(factory)

    first = bb.custom_backend("mangafire")
    second = bb.custom_backend("mangafire")

    assert first is second
    assert factory.requested == ["mangafire"]


def test_set_browser_bridge_none_restores_the_desktop_default(clean_registry):
    aio_android.set_browser_bridge(FakeBridgeFactory())
    assert bb.custom_backend("fetch") is not None

    aio_android.set_browser_bridge(None)
    # None, not a PatchrightBackend: custom_backend reports only the
    # EMBEDDER-installed backend, which is what keeps the three desktop
    # consumers on their own hard-won Patchright paths.
    assert bb.custom_backend("fetch") is None


def test_installed_backend_satisfies_the_protocol(clean_registry):
    aio_android.set_browser_bridge(FakeBridgeFactory())
    backend = bb.custom_backend("cf")
    assert isinstance(backend, bb.BrowserBackend)
    assert backend.unavailable_reason is None
