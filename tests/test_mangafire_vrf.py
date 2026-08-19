"""Offline coverage for MangaFire's `vrf` request signing (2026-08).

mangafire.to began requiring a per-request `vrf=` signature on every `/api/*`
call: `403 {"message":"Missing token."}` without one, `"Invalid token."` when
the token doesn't match the params sent. Minting it needs a browser (the cipher
ships as virtualized bytecode — see sites/mangafire_vrf.py's header), so
everything here fakes the signer. No browser, no network.

What these lock down:
  * the signed URL the handler actually builds,
  * order-sensitivity of the token cache key (the cipher covers the serialized
    query, so re-ordered params are a DIFFERENT token, not a cache hit),
  * the token-rejection backstop: re-sign exactly once, then give up,
  * that a token 403 is NOT classified as rate limiting — otherwise
    sites/hardening.py retries it 4x at 12/24/48s (~84s) for a response that
    can never change, which blows the search fan-out barrier and the chapter
    watchdog,
  * search() degrading to [] instead of raising when signing is unavailable,
  * the Cloudflare layer added 2026-08-19: the interstitial must be DETECTED
    rather than probed for modules, must never prompt from a background
    operation, and once it challenges the HTTP path the reads must move to the
    browser.

Cross-file: sites/mangafire.py:_api_get, sites/mangafire_vrf.py:sign_api_query,
sites/hardening.py:_is_api_token_rejection.
"""

from __future__ import annotations

import json

import pytest

from sites import mangafire_vrf
from sites.hardening import _is_api_token_rejection, looks_like_cloudflare_rate_limit
from sites.mangafire import MangaFireSiteHandler


# ────────────────────────────────────────────────────────────────────────
# Fakes
# ────────────────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status=200, payload=None, ctype="application/json", text=None):
        self.status_code = status
        self._payload = payload
        self.headers = {"content-type": ctype}
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class Recorder:
    """Stands in for aio-dl.py:make_request. Serves canned responses in order
    and records every URL it was asked for."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def __call__(self, url, scraper):
        self.urls.append(url)
        return self.responses.pop(0) if self.responses else FakeResponse(404, {})


@pytest.fixture
def handler():
    h = MangaFireSiteHandler()
    # Class-level suppression flag leaks across tests otherwise.
    type(h)._signing_warned = False
    return h


@pytest.fixture
def fake_signer(monkeypatch):
    """Deterministic stand-in for the browser-backed signer."""
    calls = []

    def _sign(path, pairs=()):
        calls.append((path, list(pairs)))
        query = "&".join(f"{k}={v}" for k, v in pairs)
        vrf = f"TOKEN{len(calls)}"
        query = f"{query}&vrf={vrf}" if query else f"vrf={vrf}"
        return {"vrf": vrf, "query": query}

    monkeypatch.setattr(mangafire_vrf, "sign_api_query", _sign)
    monkeypatch.setattr(mangafire_vrf, "invalidate", lambda: None)
    return calls


# ────────────────────────────────────────────────────────────────────────
# Cache key
# ────────────────────────────────────────────────────────────────────────

def test_cache_key_is_order_sensitive():
    """The cipher covers the SERIALIZED query, so a different param order is a
    different plaintext and therefore a different token. Treating the two as
    one cache entry would serve a token the server rejects."""
    a = mangafire_vrf._cache_key("/titles/x/chapters", [("page", 1), ("limit", 100)])
    b = mangafire_vrf._cache_key("/titles/x/chapters", [("limit", 100), ("page", 1)])
    assert a != b


def test_cache_key_distinguishes_path_and_values():
    base = mangafire_vrf._cache_key("/titles/aaa", [])
    assert base != mangafire_vrf._cache_key("/titles/bbb", [])
    assert mangafire_vrf._cache_key("/titles", [("page", 1)]) != mangafire_vrf._cache_key(
        "/titles", [("page", 2)]
    )


def test_cache_key_is_stable():
    pairs = [("language", "en"), ("page", 3)]
    assert mangafire_vrf._cache_key("/t", pairs) == mangafire_vrf._cache_key("/t", pairs)


# ────────────────────────────────────────────────────────────────────────
# Signed URL construction
# ────────────────────────────────────────────────────────────────────────

def test_api_get_appends_signed_query(handler, fake_signer):
    req = Recorder([FakeResponse(200, {"ok": True})])
    out = handler._api_get(
        "/titles/dkw/chapters",
        [("language", "en"), ("page", 1)],
        object(),
        req,
        label="chapters",
    )
    assert out == {"ok": True}
    assert req.urls == [
        "https://mangafire.to/api/titles/dkw/chapters?language=en&page=1&vrf=TOKEN1"
    ]


def test_api_get_paramless_path_still_carries_vrf(handler, fake_signer):
    req = Recorder([FakeResponse(200, {"data": {}})])
    handler._api_get("/chapters/99", [], object(), req, label="pages")
    assert req.urls == ["https://mangafire.to/api/chapters/99?vrf=TOKEN1"]


# ────────────────────────────────────────────────────────────────────────
# Token-rejection backstop
# ────────────────────────────────────────────────────────────────────────

def test_token_rejection_triggers_exactly_one_resign(handler, fake_signer):
    """A rejected token means the site redeployed and rotated the key under our
    cached token. Re-sign once against the fresh bundle; the retry must carry
    the NEW token, not the rejected one."""
    req = Recorder([
        FakeResponse(403, {"message": "Invalid token."}),
        FakeResponse(200, {"data": {"title": "X"}}),
    ])
    out = handler._api_get("/titles/dkw", [], object(), req, label="detail")
    assert out == {"data": {"title": "X"}}
    assert len(fake_signer) == 2, "should have re-signed once"
    assert req.urls[0].endswith("vrf=TOKEN1")
    assert req.urls[1].endswith("vrf=TOKEN2")


def test_token_rejection_gives_up_after_second_failure(handler, fake_signer):
    """Two rejections in a row is not a rotation — stop, don't loop."""
    req = Recorder([
        FakeResponse(403, {"message": "Missing token."}),
        FakeResponse(403, {"message": "Missing token."}),
    ])
    assert handler._api_get("/titles/dkw", [], object(), req, label="detail") is None
    assert len(req.urls) == 2
    assert len(fake_signer) == 2


def test_non_token_403_is_not_retried(handler, fake_signer):
    """A plain 403 (a real block) must not burn a second signing round-trip."""
    req = Recorder([FakeResponse(403, {"message": "Forbidden"})])
    assert handler._api_get("/titles/dkw", [], object(), req, label="detail") is None
    assert len(fake_signer) == 1


# ────────────────────────────────────────────────────────────────────────
# hardening: token 403 is deterministic, not congestion
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", ["Missing token.", "Invalid token."])
def test_token_403_is_not_rate_limiting(message):
    """Retrying is pure wasted wall-clock (~84s at 12/24/48s backoff) for a
    response that is deterministic."""
    resp = FakeResponse(403, {"message": message})
    assert _is_api_token_rejection(resp) is True
    assert looks_like_cloudflare_rate_limit(resp) is False


def test_ordinary_403_still_reads_as_rate_limiting():
    """The narrow token carve-out must not defeat real Cloudflare blocks."""
    resp = FakeResponse(403, {"message": "Forbidden"})
    assert _is_api_token_rejection(resp) is False
    assert looks_like_cloudflare_rate_limit(resp) is True


def test_html_403_mentioning_token_is_still_rate_limiting():
    """Only a JSON body counts. An HTML challenge page that happens to contain
    the word 'token' is a block, not a signature error."""
    resp = FakeResponse(
        403, None, ctype="text/html", text="<html>csrf token missing</html>"
    )
    assert _is_api_token_rejection(resp) is False
    assert looks_like_cloudflare_rate_limit(resp) is True


def test_404_never_reads_as_rate_limiting():
    assert looks_like_cloudflare_rate_limit(FakeResponse(404, {})) is False


# ────────────────────────────────────────────────────────────────────────
# Failure surfaces
# ────────────────────────────────────────────────────────────────────────

def test_search_degrades_to_empty_when_signing_unavailable(handler, monkeypatch):
    """Raising here would let search_orchestrator's persistent ProbeFailureCache
    blocklist mangafire.to for an hour (threshold 2, TTL 1h) — including after
    the user installs the missing browser dependency. Same deliberate override
    of base.py:search's propagate rule that sites/comix.py:search makes."""
    def _boom(path, pairs=()):
        raise mangafire_vrf.MangaFireSigningError("no browser")

    monkeypatch.setattr(mangafire_vrf, "sign_api_query", _boom)
    assert handler.search("frieren", object(), Recorder([]), limit=5) == []


def test_download_path_propagates_signing_failure(handler, monkeypatch):
    """Download paths must fail LOUDLY. Returning [] would make pages_total==0,
    and the completeness gate never fires on a zero-page chapter — the
    empty_content trap documented for comix."""
    def _boom(path, pairs=()):
        raise mangafire_vrf.MangaFireSigningError("no browser")

    monkeypatch.setattr(mangafire_vrf, "sign_api_query", _boom)
    with pytest.raises(mangafire_vrf.MangaFireSigningError):
        handler.get_chapter_images({"hid": "123"}, object(), Recorder([]))


def test_sign_api_queries_empty_input_never_starts_a_browser():
    """Zero specs must be a pure no-op — a run that touches no mangafire URL
    must never pay a Chromium launch."""
    assert mangafire_vrf.sign_api_queries([]) == []


def test_sign_api_query_raises_when_signer_returns_nothing(monkeypatch):
    monkeypatch.setattr(mangafire_vrf, "sign_api_queries", lambda specs: [None])
    with pytest.raises(mangafire_vrf.MangaFireSigningError):
        mangafire_vrf.sign_api_query("/titles/x")


# ────────────────────────────────────────────────────────────────────────
# No-browser handling
#
# CI has no Chromium. Before these landed, every _api_get retried the whole
# launch, so a 13-page chapter list paid 13 failed launches and printed
# Playwright's install banner 13 times.
# ────────────────────────────────────────────────────────────────────────

def test_env_kill_switch_blocks_launch(monkeypatch):
    """tests/conftest.py sets this for the whole suite so no unit test can
    silently boot a browser (and reach the network) via a handler call."""
    monkeypatch.setenv("AIO_MANGAFIRE_NO_SIGNER", "1")
    session = mangafire_vrf._SignerSession()
    assert session._start() is False
    assert "AIO_MANGAFIRE_NO_SIGNER" in (session._unavailable or "")


def test_unavailable_verdict_is_sticky(monkeypatch):
    """Once we know there's no usable browser, later calls must not re-attempt
    the launch. Proven by removing the env var that caused the first refusal:
    a non-sticky implementation would happily try again."""
    monkeypatch.setenv("AIO_MANGAFIRE_NO_SIGNER", "1")
    session = mangafire_vrf._SignerSession()
    assert session._start() is False

    monkeypatch.delenv("AIO_MANGAFIRE_NO_SIGNER", raising=False)
    monkeypatch.setattr(
        session,
        "_cleanup",
        lambda: (_ for _ in ()).throw(AssertionError("relaunch attempted")),
    )
    assert session._start() is False


def test_unavailable_reason_is_collapsed_to_one_line():
    """Playwright's launch error embeds a multi-line ASCII banner; this string
    ends up inside an exception message, so it must stay short and flat."""
    session = mangafire_vrf._SignerSession()
    session._mark_unavailable("launch failed:\n\n  ╔═══╗\n  ║ x ║\n" + "y" * 500)
    reason = session._unavailable or ""
    assert "\n" not in reason
    assert len(reason) <= 200
    assert reason.startswith("launch failed:")


def test_cached_tokens_still_serve_with_signer_disabled(monkeypatch, tmp_path):
    """The kill-switch is checked in _start, not in sign(), so a disk cache
    populated by an earlier run keeps working on a browser-less machine."""
    monkeypatch.setenv("AIO_MANGAFIRE_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("AIO_MANGAFIRE_NO_SIGNER", "1")
    session = mangafire_vrf._SignerSession()
    session._namespace = "ns"
    key = mangafire_vrf._cache_key("/titles/x", [])
    session._remember(key, {"vrf": "CACHED", "query": "vrf=CACHED"})

    out = session.sign([("/titles/x", [])])
    assert out == [{"vrf": "CACHED", "query": "vrf=CACHED"}]


# ────────────────────────────────────────────────────────────────────────
# Cloudflare
#
# mangafire.to went behind a Managed Challenge sitewide (including /api/*)
# around 2026-08-16. The original symptom was a lie: _bootstrap navigated to
# the homepage, landed on the interstitial, ran its module scan against it and
# reported "no vrf signer among 0 module candidates" — blaming the signer for a
# network-level block. Nothing below lets that happen again.
# ────────────────────────────────────────────────────────────────────────

CF_INTERSTITIAL = (
    "<!DOCTYPE html><html lang=\"en-US\"><head><title>Just a moment...</title>"
    "<meta name=\"robots\" content=\"noindex,nofollow\"></head><body>"
    "Enable JavaScript and cookies to continue. cf_chl_opt "
    "challenge-platform</body></html>"
)


class _FakeSession(mangafire_vrf._SignerSession):
    """A session whose browser is entirely faked out.

    Subclasses rather than mocks because the things under test are the CONTROL
    FLOW decisions (_ensure_cleared's gates, _bootstrap's ordering), and those
    have to run for real.
    """

    def __init__(self, html=CF_INTERSTITIAL):
        super().__init__()
        self.html = html
        self.evals = []
        self.gotos = []
        self.started = False

    def _start(self, headless=None, *, _ua_relaunch=False):
        self.started = True
        return True

    def _page_html(self):
        return self.html

    def _page_title(self):
        return "Just a moment..."

    def _goto(self, url):
        self.gotos.append(url)

    def _eval(self, script, arg=mangafire_vrf._EVAL_NOARG):
        self.evals.append(script)
        return None


@pytest.fixture(autouse=True)
def _no_interactive_cf(monkeypatch):
    """A test that opens a browser window is a test that hangs CI. The seam is
    the same one the shipping code reads, so this also proves the seam works."""
    monkeypatch.setenv("AIO_MANGAFIRE_NO_INTERACTIVE_CF", "1")
    monkeypatch.setattr(mangafire_vrf, "_CF_SOLVES_DONE", 0, raising=False)
    monkeypatch.setattr(mangafire_vrf, "_CF_FAILURES", 0, raising=False)
    monkeypatch.setattr(mangafire_vrf, "_CF_LAST_PROMPT_AT", 0.0, raising=False)


def test_interstitial_is_detected_as_a_challenge():
    assert _FakeSession()._challenged() is True


def test_real_page_is_not_a_challenge():
    html = "<html><body><script type='module' src='/x.js'></script></body></html>"

    class _Clean(_FakeSession):
        def _page_title(self):
            return "MangaFire"

    assert _Clean(html)._challenged() is False


def test_bootstrap_never_probes_modules_while_challenged(monkeypatch):
    """THE regression. The module scan must not run against the interstitial —
    that is what produced "no vrf signer among 0 module candidates"."""
    session = _FakeSession()
    monkeypatch.setattr(mangafire_vrf, "_CF_AUTO_CLEAR_TIMEOUT_S", 0.0)
    assert session._bootstrap() is False
    # _alive()'s liveness probe is fine and runs before the navigation; what
    # must never run is the module scan itself.
    assert mangafire_vrf._BOOTSTRAP_JS not in session.evals, (
        "ran the module scan on a Cloudflare interstitial"
    )


def test_background_operation_never_opens_a_window(monkeypatch):
    """search / --list-chapters / the library update-check must fail cleanly
    rather than demand a human. The permission is context-scoped and defaults
    to False, so this is the shipping default, not a special case."""
    from sites import crawlee_utils

    session = _FakeSession()
    monkeypatch.setattr(mangafire_vrf, "_CF_AUTO_CLEAR_TIMEOUT_S", 0.0)
    monkeypatch.setattr(crawlee_utils, "interactive_solve_allowed", lambda: False)

    def _boom():
        raise AssertionError("opened a solver from a background operation")

    monkeypatch.setattr(session, "_solve_cf_via_zendriver", _boom)
    monkeypatch.setattr(session, "_solve_cf_headed", _boom)
    assert session._ensure_cleared() is False


def test_cf_verdict_is_sticky_so_later_calls_dont_re_poll(monkeypatch):
    """A 20s auto-clear poll per API call would turn one wall into a very slow
    wall. One verdict per process."""
    from sites import crawlee_utils

    session = _FakeSession()
    monkeypatch.setattr(mangafire_vrf, "_CF_AUTO_CLEAR_TIMEOUT_S", 0.0)
    monkeypatch.setattr(crawlee_utils, "interactive_solve_allowed", lambda: False)
    assert session._ensure_cleared() is False
    assert session._cf_blocked

    calls = []
    monkeypatch.setattr(session, "_challenged", lambda: calls.append(1) or True)
    assert session._ensure_cleared() is False
    assert calls == [], "re-polled after a sticky Cloudflare verdict"


def test_cf_block_names_cloudflare_in_the_signing_error(monkeypatch):
    """The whole point of the bug report: the message must name the real cause.
    'could not sign ... (browser-backed signer unavailable)' sent the user
    looking at the signer for a network-level block."""
    from sites import crawlee_utils

    session = _FakeSession()
    monkeypatch.setattr(mangafire_vrf, "_CF_AUTO_CLEAR_TIMEOUT_S", 0.0)
    monkeypatch.setattr(crawlee_utils, "interactive_solve_allowed", lambda: False)
    session._ensure_cleared()

    monkeypatch.setattr(mangafire_vrf, "sign_api_queries", lambda specs: [None])
    with pytest.raises(mangafire_vrf.MangaFireSigningError) as exc:
        mangafire_vrf.sign_api_query("/titles/kjp9")
    assert "Cloudflare" in str(exc.value)


def test_no_interactive_env_blocks_the_prompt():
    assert mangafire_vrf._may_prompt() == "disabled"


def test_a_decline_stops_the_nagging(monkeypatch):
    monkeypatch.delenv("AIO_MANGAFIRE_NO_INTERACTIVE_CF", raising=False)
    assert mangafire_vrf._may_prompt() is None  # claims the slot
    mangafire_vrf._record_prompt_outcome(False)
    assert mangafire_vrf._may_prompt() == "already_declined"


def test_a_success_does_not_count_against_the_budget(monkeypatch):
    """One run can legitimately need more than one solve; only declines should
    spend the allowance."""
    monkeypatch.delenv("AIO_MANGAFIRE_NO_INTERACTIVE_CF", raising=False)
    mangafire_vrf._record_prompt_outcome(True)
    monkeypatch.setattr(mangafire_vrf, "_CF_LAST_PROMPT_AT", 0.0)
    assert mangafire_vrf._may_prompt() is None


# ────────────────────────────────────────────────────────────────────────
# Launch identity — the two levers, neither sufficient alone
# ────────────────────────────────────────────────────────────────────────

def test_launch_uses_both_channel_and_ua_pin():
    import inspect

    src = inspect.getsource(mangafire_vrf._SignerSession._start)
    assert "channel=_bid.BROWSER_CHANNEL" in src
    assert 'ctx_kwargs["user_agent"]' in src
    # An install that doesn't know the channel must still launch.
    assert "_launch(**ctx_kwargs)" in src, "channel launch needs a fallback"


def test_ua_reconciliation_reads_the_browser_not_the_page():
    import inspect

    from sites import browser_identity

    assert "Browser.getVersion" in inspect.getsource(
        browser_identity.probe_true_user_agent
    )
    src = inspect.getsource(mangafire_vrf._SignerSession._start)
    assert 'evaluate("navigator.userAgent")' not in src


def test_ua_relaunch_is_bounded():
    import inspect

    sig = inspect.signature(mangafire_vrf._SignerSession._start)
    assert "_ua_relaunch" in sig.parameters


# ────────────────────────────────────────────────────────────────────────
# Browser-backed API transport
# ────────────────────────────────────────────────────────────────────────

def test_api_stays_on_http_until_cloudflare_challenges(handler, fake_signer):
    """Costs one bool when Cloudflare is off — the 'only signing needs a
    browser' property has to survive this change."""
    type(handler)._api_via_browser = False
    rec = Recorder([FakeResponse(200, {"data": {"title": "X"}})])
    out = handler._api_get("/titles/kjp9", [], object(), rec, label="detail")
    assert out == {"data": {"title": "X"}}
    assert len(rec.urls) == 1
    assert type(handler)._api_via_browser is False


def test_challenged_http_switches_to_the_browser(handler, fake_signer, monkeypatch):
    type(handler)._api_via_browser = False
    fetched = []

    def _browser_fetch(url):
        fetched.append(url)
        return {"status": 200, "body": json.dumps({"data": {"title": "Saiki"}})}

    monkeypatch.setattr(mangafire_vrf, "fetch_api_json", _browser_fetch)
    rec = Recorder([FakeResponse(403, None, ctype="text/html", text=CF_INTERSTITIAL)])

    out = handler._api_get("/titles/kjp9", [], object(), rec, label="detail")
    assert out == {"data": {"title": "Saiki"}}
    assert fetched and "vrf=" in fetched[0], "browser must fetch the SIGNED url"
    assert type(handler)._api_via_browser is True
    type(handler)._api_via_browser = False


def test_browser_switch_is_sticky(handler, fake_signer, monkeypatch):
    """Re-paying a guaranteed-403 HTTP round trip on every later call is pure
    latency once the site is known to be challenging."""
    type(handler)._api_via_browser = True
    monkeypatch.setattr(
        mangafire_vrf,
        "fetch_api_json",
        lambda url: {"status": 200, "body": json.dumps({"ok": True})},
    )
    rec = Recorder([])
    assert handler._api_get("/titles/kjp9", [], object(), rec, label="detail") == {"ok": True}
    assert rec.urls == [], "went back to HTTP after switching to the browser"
    type(handler)._api_via_browser = False


def test_token_rejection_retry_still_works_on_the_browser_path(
    handler, fake_signer, monkeypatch
):
    """The response adapter exists so _is_token_rejection keeps working
    whichever transport produced the bytes — otherwise the one-shot re-sign
    silently stops firing exactly where it is most needed."""
    type(handler)._api_via_browser = True
    bodies = [
        {"status": 403, "body": json.dumps({"message": "Invalid token."})},
        {"status": 200, "body": json.dumps({"data": {"title": "OK"}})},
    ]
    monkeypatch.setattr(mangafire_vrf, "fetch_api_json", lambda url: bodies.pop(0))
    out = handler._api_get("/titles/kjp9", [], object(), Recorder([]), label="detail")
    assert out == {"data": {"title": "OK"}}
    assert len(fake_signer) == 2, "expected exactly one re-sign"
    type(handler)._api_via_browser = False


def test_browser_fetch_unavailable_returns_none_not_garbage(
    handler, fake_signer, monkeypatch
):
    type(handler)._api_via_browser = True
    monkeypatch.setattr(mangafire_vrf, "fetch_api_json", lambda url: None)
    assert handler._api_get("/titles/kjp9", [], object(), Recorder([]), label="detail") is None
    type(handler)._api_via_browser = False


def test_browser_response_adapter_is_requests_shaped():
    from sites.mangafire import _BrowserApiResponse

    r = _BrowserApiResponse(403, json.dumps({"message": "Missing token."}))
    assert r.status_code == 403
    assert MangaFireSiteHandler._is_token_rejection(r) is True
    assert r.json()["message"] == "Missing token."


def test_background_block_does_not_poison_a_later_foreground_download(monkeypatch):
    """One process holds BOTH contexts: a --multi-source run probes with the
    interactive permission explicitly off (search_orchestrator._probe_one) and
    then downloads with it on, against the same module-global _SignerSession.
    A sticky background verdict would fail every call of the download that
    follows the probe."""
    from sites import crawlee_utils

    session = _FakeSession()
    monkeypatch.setattr(mangafire_vrf, "_CF_AUTO_CLEAR_TIMEOUT_S", 0.0)

    # Probe phase: not allowed to prompt -> blocked, but only provisionally.
    monkeypatch.setattr(crawlee_utils, "interactive_solve_allowed", lambda: False)
    assert session._ensure_cleared() is False
    assert session._cf_blocked_background is True

    # Download phase: permission granted, and the challenge has since cleared.
    monkeypatch.setattr(crawlee_utils, "interactive_solve_allowed", lambda: True)
    monkeypatch.setattr(session, "_challenged", lambda: False)
    assert session._ensure_cleared() is True, "background verdict poisoned the download"
    assert session._cf_blocked is None


def test_a_real_block_stays_sticky(monkeypatch):
    """The re-open rule must NOT resurrect a verdict reached while we were
    allowed to prompt — that one is final, and re-polling it costs the
    auto-clear wait on every later call."""
    from sites import crawlee_utils

    session = _FakeSession()
    monkeypatch.setattr(mangafire_vrf, "_CF_AUTO_CLEAR_TIMEOUT_S", 0.0)
    monkeypatch.setattr(crawlee_utils, "interactive_solve_allowed", lambda: True)
    monkeypatch.setattr(crawlee_utils, "cf_solver_available", lambda **kw: False)
    monkeypatch.setattr(session, "_solve_cf_headed", lambda: False)
    monkeypatch.delenv("AIO_MANGAFIRE_NO_INTERACTIVE_CF", raising=False)

    assert session._ensure_cleared() is False
    assert session._cf_blocked_background is False

    calls = []
    monkeypatch.setattr(session, "_challenged", lambda: calls.append(1) or True)
    assert session._ensure_cleared() is False
    assert calls == [], "re-polled a final Cloudflare verdict"


# ────────────────────────────────────────────────────────────────────────
# Channel launch retry
#
# Chromium locks its user-data-dir and this profile is shared by every process
# the app spawns (searcher.js and downloader.js are separate processes). A
# collision makes Chrome self-exit with exitCode=21, which Playwright surfaces
# as TargetClosedError — nothing like "unsupported channel", but it hit the same
# except branch and permanently downgraded the run's identity. Observed live
# 2026-08-20.
# ────────────────────────────────────────────────────────────────────────

def _fake_patchright(monkeypatch, outcomes):
    """Drive _start against a fake Playwright. *outcomes* is one entry per
    launch call: an Exception to raise, or None to succeed. Returns the list
    that records the `channel` kwarg of each attempt."""
    attempts = []

    class _Page:
        def is_closed(self):
            return False

    class _Context:
        def __init__(self):
            self.pages = [_Page()]

        def new_cdp_session(self, page):
            raise RuntimeError("no cdp in the fake")

        def close(self):
            pass

    class _Chromium:
        def launch_persistent_context(self, profile, **kw):
            attempts.append(kw.get("channel"))
            outcome = outcomes[len(attempts) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return _Context()

    class _PW:
        chromium = _Chromium()

        def stop(self):
            pass

    import patchright.sync_api as psa

    monkeypatch.setattr(psa, "sync_playwright", lambda: type("F", (), {"start": lambda s: _PW()})())
    monkeypatch.setattr(mangafire_vrf, "_CHANNEL_RETRY_DELAY_S", 0.0)
    return attempts


def test_transient_launch_failure_retries_and_keeps_the_channel(monkeypatch, tmp_path):
    """A profile collision must cost a retry, not the run's identity.

    The live failure: Chrome self-exits with exitCode=21 when another AIO
    process holds the shared user-data-dir, Playwright reports TargetClosedError,
    and the first cut of this code downgraded to channel-less headless for the
    WHOLE process — reintroducing the HeadlessChrome client-hint leak the channel
    exists to remove.
    """
    monkeypatch.setenv("AIO_MANGAFIRE_PROFILE_DIR", str(tmp_path))
    monkeypatch.delenv("AIO_MANGAFIRE_NO_SIGNER", raising=False)
    collision = RuntimeError(
        "BrowserType.launch_persistent_context: Target page, context or browser "
        "has been closed <process did exit: exitCode=21>"
    )
    attempts = _fake_patchright(monkeypatch, [collision, None])

    session = mangafire_vrf._SignerSession()
    assert session._start() is True
    assert attempts == ["chromium", "chromium"], (
        "the retry dropped the channel instead of retrying it"
    )


def test_unsupported_channel_downgrades_immediately(monkeypatch, tmp_path):
    """The opposite case must NOT burn retries: a build that cannot do the
    channel will never start doing it."""
    monkeypatch.setenv("AIO_MANGAFIRE_PROFILE_DIR", str(tmp_path))
    monkeypatch.delenv("AIO_MANGAFIRE_NO_SIGNER", raising=False)
    attempts = _fake_patchright(
        monkeypatch, [RuntimeError('Unsupported channel "chromium"'), None]
    )

    session = mangafire_vrf._SignerSession()
    assert session._start() is True
    assert attempts == ["chromium", None], "expected one attempt then a downgrade"


def test_persistent_failure_eventually_downgrades(monkeypatch, tmp_path):
    """The fallback must still exist — a profile held for the whole run has to
    degrade to a working browser rather than failing the download."""
    monkeypatch.setenv("AIO_MANGAFIRE_PROFILE_DIR", str(tmp_path))
    monkeypatch.delenv("AIO_MANGAFIRE_NO_SIGNER", raising=False)
    boom = lambda: RuntimeError("Target page, context or browser has been closed")
    attempts = _fake_patchright(
        monkeypatch,
        [boom() for _ in range(mangafire_vrf._CHANNEL_LAUNCH_ATTEMPTS)] + [None],
    )

    session = mangafire_vrf._SignerSession()
    assert session._start() is True
    assert attempts[-1] is None, "never downgraded"
    assert attempts.count("chromium") == mangafire_vrf._CHANNEL_LAUNCH_ATTEMPTS


def test_target_closed_is_not_read_as_unsupported_channel():
    """The classifier is the whole fix: a transient collision must not look like
    an incompatible build."""
    transient = Exception(
        "TargetClosedError: BrowserType.launch_persistent_context: Target page, "
        "context or browser has been closed ... <process did exit: exitCode=21>"
    )
    assert mangafire_vrf._channel_unsupported(transient) is False


def test_genuine_channel_incompatibility_is_recognised():
    for msg in (
        "Unsupported channel \"chromium\"",
        "Chromium distribution 'chromium' is not supported",
        "browserType.launchPersistentContext: Executable doesn't exist at ...",
        "launch_persistent_context() got an unexpected keyword argument 'channel'",
    ):
        assert mangafire_vrf._channel_unsupported(Exception(msg)) is True, msg


def test_channel_launch_retries_before_giving_up():
    import inspect

    src = inspect.getsource(mangafire_vrf._SignerSession._start)
    assert "_CHANNEL_LAUNCH_ATTEMPTS" in src
    assert "_channel_unsupported" in src
    assert mangafire_vrf._CHANNEL_LAUNCH_ATTEMPTS >= 2
