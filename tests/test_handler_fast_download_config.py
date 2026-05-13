"""Tests for the per-handler fast-download configuration introduced in the
Phase A generalization (2026-05-13).

The curl_cffi fast download path used to live on MangaFireSiteHandler. It's
now on BaseSiteHandler with FAST_DL_* class attributes that subclasses
override for handler-specific Referer / UA / impersonate / extra headers.

Cross-file: sites/base.py:fast_download_images is the lifted body;
aio-dl.py:5992-6008 + 3884-3902 are the call sites that read
SUPPORTS_FAST_DOWNLOAD before invoking. These tests are wire-level checks
on the class attributes, not behavioral tests of the curl_cffi async
machinery (which would need a mocked event loop and HTTP fixtures).
"""

from __future__ import annotations

from sites.base import BaseSiteHandler, _CURL_CFFI_AVAILABLE
from sites.linewebtoon import LineWebtoonSiteHandler
from sites.mangadex import MangaDexSiteHandler
from sites.mangafire import MangaFireSiteHandler


# ────────────────────────────────────────────────────────────────────────
# BaseSiteHandler defaults
# ────────────────────────────────────────────────────────────────────────

def test_base_handler_defaults_to_no_fast_download():
    """Subclasses must opt in explicitly. Default False protects against
    accidentally enabling a curl_cffi path on a handler whose CDN doesn't
    tolerate Chrome impersonation."""
    assert BaseSiteHandler.SUPPORTS_FAST_DOWNLOAD is False


def test_base_handler_default_impersonate_is_chrome120():
    """chrome120 is the bench-validated default JA3/JA4 + h2 settings frame.
    Cloudflare-fronted CDNs largely accept any modern Chrome profile."""
    assert BaseSiteHandler.FAST_DL_IMPERSONATE == "chrome120"


def test_base_handler_default_user_agent_is_none():
    """None = let curl_cffi fill from the impersonate profile. Subclasses
    override only when consistency with their cloudscraper session matters."""
    assert BaseSiteHandler.FAST_DL_USER_AGENT is None


def test_base_handler_default_referer_is_empty():
    """Empty string = no Referer header. Most subclasses set this to their
    homepage URL to satisfy anti-hotlink CDN protection."""
    assert BaseSiteHandler.FAST_DL_REFERER_FROM == ""


def test_base_handler_default_extra_headers_empty():
    """Mutable class-level default; tests that nothing has accidentally
    added a global header that would leak across subclasses."""
    assert BaseSiteHandler.FAST_DL_EXTRA_HEADERS == {}


# ────────────────────────────────────────────────────────────────────────
# MangaFire opt-in
# ────────────────────────────────────────────────────────────────────────

def test_mangafire_opts_into_fast_download_when_curl_cffi_available():
    """MangaFire is the original opt-in. SUPPORTS_FAST_DOWNLOAD tracks
    the module-level curl_cffi capability flag — degrades to False if
    curl_cffi failed to import for any reason."""
    assert MangaFireSiteHandler.SUPPORTS_FAST_DOWNLOAD == _CURL_CFFI_AVAILABLE


def test_mangafire_referer_set_to_homepage():
    """Anti-hotlink Referer for MangaFire's image CDN. Must end with /."""
    assert MangaFireSiteHandler.FAST_DL_REFERER_FROM == "https://mangafire.to/"


def test_mangafire_user_agent_pinned_to_chrome_122():
    """Pinned to match the Patchright session UA so cf_clearance cookie's
    UA fingerprint stays consistent if CF starts cookie-validating image
    hits. Magic string check — value matters for cookie-validation parity."""
    ua = MangaFireSiteHandler.FAST_DL_USER_AGENT
    assert ua is not None
    assert "Chrome/122.0.0.0" in ua


def test_mangafire_inherits_default_impersonate():
    """MangaFire doesn't override impersonate; gets chrome120 from base."""
    assert MangaFireSiteHandler.FAST_DL_IMPERSONATE == "chrome120"


# ────────────────────────────────────────────────────────────────────────
# LineWebtoon opt-in (the user's primary motivation for this work)
# ────────────────────────────────────────────────────────────────────────

def test_linewebtoon_opts_into_fast_download_when_curl_cffi_available():
    """LineWebtoon was the originally-affected handler — vertical-scroll
    PNGs at 720-800px on webtoons.com had no fast path before this work."""
    assert LineWebtoonSiteHandler.SUPPORTS_FAST_DOWNLOAD == _CURL_CFFI_AVAILABLE


def test_linewebtoon_referer_set_to_homepage():
    """Anti-hotlink protection on swebtoon-phinf.pstatic.net (the actual
    image CDN) requires Referer from a webtoons.com origin."""
    assert LineWebtoonSiteHandler.FAST_DL_REFERER_FROM == "https://www.webtoons.com/"


def test_linewebtoon_does_not_pin_user_agent():
    """LineWebtoon doesn't have CF cookie-validation concerns like MangaFire,
    so the UA can fall through to curl_cffi's chrome120 default. Keeps the
    handler config minimal."""
    assert LineWebtoonSiteHandler.FAST_DL_USER_AGENT is None


# ────────────────────────────────────────────────────────────────────────
# MangaDex regression guard
# ────────────────────────────────────────────────────────────────────────

def test_mangadex_does_not_opt_into_fast_download():
    """MangaDex's API ToS mandates a non-browser User-Agent — opting it into
    SUPPORTS_FAST_DOWNLOAD would force the curl_cffi `impersonate=` path,
    which sets Chrome's UA at the JA3/JA4 level too. The MangaDex API may
    reject the impersonated traffic. Regression guard."""
    assert MangaDexSiteHandler.SUPPORTS_FAST_DOWNLOAD is False


# ────────────────────────────────────────────────────────────────────────
# _fast_dl_build_headers behavior
# ────────────────────────────────────────────────────────────────────────

def test_base_handler_build_headers_returns_empty_dict_by_default():
    """Default handler config has no Referer, no UA, no extras → empty headers."""
    h = BaseSiteHandler()
    assert h._fast_dl_build_headers("anyhost.example") == {}


def test_mangafire_build_headers_includes_referer_and_ua():
    """MangaFire's class attrs flow into the headers dict via the helper."""
    h = MangaFireSiteHandler()
    headers = h._fast_dl_build_headers("img.mfcdn.net")
    assert headers["Referer"] == "https://mangafire.to/"
    assert "Chrome/122.0.0.0" in headers["User-Agent"]


def test_linewebtoon_build_headers_includes_referer_no_ua():
    """LineWebtoon sets Referer but not UA — verify the helper omits the
    UA key when None rather than emitting an empty-string value."""
    h = LineWebtoonSiteHandler()
    headers = h._fast_dl_build_headers("swebtoon-phinf.pstatic.net")
    assert headers["Referer"] == "https://www.webtoons.com/"
    assert "User-Agent" not in headers


def test_build_headers_extra_headers_merged_first():
    """FAST_DL_EXTRA_HEADERS is merged first; Referer/UA override on key
    collision. Use a throwaway subclass to avoid mutating real handlers."""

    class _ProbeHandler(BaseSiteHandler):
        FAST_DL_REFERER_FROM = "https://test.example/"
        FAST_DL_EXTRA_HEADERS = {
            "X-Custom": "custom-value",
            "Referer": "should-be-overridden",
        }

    h = _ProbeHandler()
    headers = h._fast_dl_build_headers("any.host")
    assert headers["X-Custom"] == "custom-value"
    # Referer attribute wins over the EXTRA_HEADERS entry of the same key.
    assert headers["Referer"] == "https://test.example/"


# ────────────────────────────────────────────────────────────────────────
# Method delegation: subclasses inherit base impl, no override required
# ────────────────────────────────────────────────────────────────────────

def test_mangafire_inherits_base_fast_download_impl():
    """After the Phase A refactor, MangaFire no longer defines its own
    fast_download_images method — it inherits from BaseSiteHandler. This
    test catches an accidental override that would silently re-introduce
    the duplicated implementation."""
    # __dict__ skips inherited attributes, so this asserts MangaFire's
    # OWN class doesn't define fast_download_images locally.
    assert "fast_download_images" not in MangaFireSiteHandler.__dict__
    # But it IS callable via inheritance.
    assert callable(MangaFireSiteHandler.fast_download_images)


def test_linewebtoon_inherits_base_fast_download_impl():
    """Same regression guard for LineWebtoon — opts in via class attrs only."""
    assert "fast_download_images" not in LineWebtoonSiteHandler.__dict__
    assert callable(LineWebtoonSiteHandler.fast_download_images)


# ────────────────────────────────────────────────────────────────────────
# Empty-input behavior
# ────────────────────────────────────────────────────────────────────────

def test_fast_download_empty_tasks_returns_empty_list():
    """Caller may pass an empty list (e.g. all binary_image entries already
    written to disk in Phase 1). Method must return [] without exception
    and without trying to construct a curl_cffi session."""
    h = MangaFireSiteHandler()
    result = h.fast_download_images([], concurrency=8, timeout=10.0)
    assert result == []
