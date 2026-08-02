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
  * search() degrading to [] instead of raising when signing is unavailable.

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
