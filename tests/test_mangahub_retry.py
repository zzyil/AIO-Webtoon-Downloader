"""mangahub's GraphQL POST: transient-failure retry and access-token minting.

Two live failure modes (2026-11-06), both handled in sites/mangahub.py:
- api.mghcdn.com answers in short bursts of HTTP 525. Before the retry every
  burst failed the search, the series load and each chapter's page list, and
  two failed searches got the host blocked from search for an hour.
- the API rate-limits each access token to a handful of calls ("API rate limit
  excessed!"). The handler used to send one hardcoded token, long spent, so
  every page list failed; it now mints tokens from the site's `mhub_access`
  cookie and re-mints when one is spent.
Cross-file: sites/mangahub.py (grep _RETRY_STATUSES, _mint_token).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import requests

import sites.mangahub as mangahub_mod
from sites.mangahub import MangaHubSiteHandler

RATE_LIMITED = {"errors": [{"message": "API rate limit excessed! Go to mangahub.io to continue reading!"}],
                "data": {"chapter": None}}


class _Resp:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code = status
        self._body = body if body is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error")

    def json(self):
        return self._body


class _Jar(list):
    """The slice of RequestsCookieJar the handler uses: iteration + clear()."""

    def clear(self, domain=None, path=None, name=None):
        self[:] = [c for c in self if not (c.domain == domain and c.path == path and c.name == name)]

    def set_token(self, value):
        self.clear(".mangahub.io", "/", "mhub_access")
        self.append(SimpleNamespace(name="mhub_access", value=value, domain=".mangahub.io", path="/"))


class _Scraper:
    """Replays a script of POST responses/exceptions. GETs model the site's
    Set-Cookie as measured: a cookieless visit is dealt the next `pool` token,
    a plain visit with a cookie gets the same one echoed back, and a
    `reloadKey=1` visit replaces the cookie with the next `pool` token."""

    def __init__(self, script, pool=("tokA", "tokB", "tokC"), token="tok0"):
        self.script = list(script)
        self.pool = list(pool)
        self.cookies = _Jar()
        if token:
            self.cookies.set_token(token)
        self.calls = []
        self.gets = []

    def post(self, url, **kw):
        self.calls.append(kw)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def get(self, url, **kw):
        self.gets.append(url)
        assert kw.get("timeout")
        has = any(c.name == "mhub_access" for c in self.cookies)
        if (not has or "reloadKey=1" in url) and self.pool:
            self.cookies.set_token(self.pool.pop(0))
        return _Resp(200)

    def sent_tokens(self):
        return [c["headers"].get("x-mhub-access") for c in self.calls]


OK = _Resp(200, {"data": {"manga": {"id": 1}}})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(mangahub_mod.time, "sleep", lambda s: None)


def _post(scraper):
    return MangaHubSiteHandler()._post_graphql(scraper, "query{}", {"slug": "x"})


# ---------------------------------------------------------- transient retry
def test_525_burst_is_retried_until_success():
    s = _Scraper([_Resp(525), _Resp(525), OK])
    assert _post(s) == {"manga": {"id": 1}}
    assert len(s.calls) == 3
    # every attempt carries a timeout (the old bare POST could hang forever)
    assert all(c.get("timeout") for c in s.calls)
    assert json.loads(s.calls[0]["data"])["variables"] == {"slug": "x"}


def test_connection_error_is_retried():
    s = _Scraper([requests.exceptions.ConnectionError("reset"), OK])
    assert _post(s) == {"manga": {"id": 1}}
    assert len(s.calls) == 2


def test_persistent_5xx_raises_after_max_attempts():
    n = MangaHubSiteHandler._MAX_ATTEMPTS
    s = _Scraper([_Resp(525)] * n)
    with pytest.raises(requests.HTTPError):
        _post(s)
    assert len(s.calls) == n


def test_persistent_connection_error_raises_after_max_attempts():
    n = MangaHubSiteHandler._MAX_ATTEMPTS
    s = _Scraper([requests.exceptions.ConnectionError("down")] * n)
    with pytest.raises(requests.exceptions.ConnectionError):
        _post(s)
    assert len(s.calls) == n


@pytest.mark.parametrize("status", [400, 403, 404])
def test_other_4xx_is_not_retried(status):
    s = _Scraper([_Resp(status)])
    with pytest.raises(requests.HTTPError):
        _post(s)
    assert len(s.calls) == 1


def test_timeout_is_not_retried():
    s = _Scraper([requests.exceptions.ReadTimeout("slow")])
    with pytest.raises(requests.exceptions.Timeout):
        _post(s)
    assert len(s.calls) == 1


def test_non_rate_limit_graphql_errors_are_not_retried():
    s = _Scraper([_Resp(200, {"errors": [{"message": "bad slug"}]})])
    with pytest.raises(RuntimeError, match="MangaHub API error"):
        _post(s)
    assert len(s.calls) == 1
    assert s.gets == []


# ------------------------------------------------------------ access token
def test_token_is_minted_from_the_site_when_absent():
    s = _Scraper([OK], token=None)
    assert _post(s) == {"manga": {"id": 1}}
    assert s.gets == ["https://mangahub.io/search"]   # plain visit, no reload
    assert s.sent_tokens() == ["tokA"]


def test_existing_cookie_token_is_used_without_a_visit():
    s = _Scraper([OK])
    _post(s)
    assert s.gets == []
    assert s.sent_tokens() == ["tok0"]


def test_spent_token_is_reminted_and_the_call_retried():
    s = _Scraper([_Resp(200, RATE_LIMITED), _Resp(200, RATE_LIMITED), OK])
    assert _post(s) == {"manga": {"id": 1}}
    # each re-mint is a reloadKey visit sent WITH the spent cookie (the jar
    # is not cleared: a cookieless visit lands on the spent shared pool)
    assert s.sent_tokens() == ["tok0", "tokA", "tokB"]
    assert s.gets == ["https://mangahub.io/search?reloadKey=1"] * 2


def test_exhausted_pool_stops_and_raises_the_rate_limit():
    # the server deals a token this call already spent: stop, don't loop
    s = _Scraper([_Resp(200, RATE_LIMITED)] * 3, pool=("tokA", "tok0"))
    with pytest.raises(RuntimeError, match="rate limit"):
        _post(s)
    assert s.sent_tokens() == ["tok0", "tokA"]


def test_reminting_is_bounded():
    n = MangaHubSiteHandler._MAX_TOKEN_REFRESHES
    pool = [f"t{i}" for i in range(n + 5)]
    s = _Scraper([_Resp(200, RATE_LIMITED)] * (n + 5), pool=pool)
    with pytest.raises(RuntimeError, match="rate limit"):
        _post(s)
    assert len(s.calls) == n + 1


def test_token_replaced_by_another_thread_is_reused_without_a_visit():
    h = MangaHubSiteHandler()
    s = _Scraper([])
    s.cookies.set_token("fresh")   # a peer thread re-minted after "stale" was refused
    assert h._mint_token(s, "stale") == "fresh"
    assert s.gets == []


def test_configure_session_sets_no_hardcoded_token():
    s = SimpleNamespace(headers={})
    MangaHubSiteHandler().configure_session(s, None)
    assert "x-mhub-access" not in s.headers
