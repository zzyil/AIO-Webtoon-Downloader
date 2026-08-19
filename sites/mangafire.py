from __future__ import annotations

import builtins as _builtins
import html as _html
import os
import random
import re
import sys
import time
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from .base import BaseSiteHandler, SearchHit, SiteComicContext
# _CURL_CFFI_AVAILABLE gates SUPPORTS_FAST_DOWNLOAD (curl_cffi async image path
# lives on BaseSiteHandler.fast_download_images). Re-exported for back-compat
# with anything that grepped this symbol on mangafire.py historically.
from .base import _CURL_CFFI_AVAILABLE  # noqa: F401
from . import mangafire_vrf


# ---------------------------------------------------------------------------
# MangaFire handler — 2026 relaunch (Laravel REST API, signed since 2026-08).
#
# What this module owns: the mangafire.to site handler. It is a THIN JSON
# client over MangaFire's `/api/*` endpoints. Response bodies are plain JSON
# (never encrypted, unlike comix.to) and images have no hotlink protection, so
# every fetch here still runs through plain `make_request` (cloudscraper) and
# images download via the inherited curl_cffi fast path.
#
# TWO THINGS NEED A BROWSER NOW. The second one is Cloudflare: mangafire.to
# went behind a Managed Challenge sitewide around 2026-08-16, so while it is up
# `make_request` gets an interstitial for every call and `_api_get` transparently
# switches to the browser-backed read (grep _api_via_browser). Images are on the
# separate `*.mfcdn*.xyz` CDN, which is NOT challenged, so they stay on the
# inherited curl_cffi fast path either way.
#
# THE ONE THING THAT ALWAYS NEEDS A BROWSER: every `/api/*` call must carry a `vrf=`
# query parameter or the server answers `403 {"message":"Missing token."}`
# (and `"Invalid token."` if the token doesn't match the params sent). Minting
# that token is delegated to sites/mangafire_vrf.py — read its header for why
# the cipher is not reimplementable and why the browser cost is a one-time
# boot rather than comix's per-series page render. Nothing else in this file
# needs to know about it: `_api_get` signs, everything downstream is unchanged.
#
# History: MangaFire relaunched as a Vite/Rolldown SPA on a Laravel API,
# retiring the old `/ajax/read/*` HTML-fragment endpoints. Its old obfuscated
# `vrf=` token briefly disappeared with that relaunch — the whole
# `mangafire_vrf_simple` / `mangafire_vrf_async_batch` stack, the aio-dl.py
# VRF-prefetch machinery and the `--mangafire-vrf-*` flags were deleted then,
# and they stay deleted: the new signer needs no CLI surface and no prefetch
# pipeline. Signing is transparent and cached.
#
# Endpoints (all GET, all return JSON, all require `vrf`):
#   search        /api/titles?keyword={q}&limit={n}          -> {items[], meta{}}
#   detail        /api/titles/{hid}                          -> {data{…}}
#   chapter list  /api/titles/{hid}/chapters?language=&sort=number&order=asc
#                     &page=N&limit=100                       -> {items[], meta{}}
#                 (limit caps at 100 server-side; paginate on meta.hasNext)
#   chapter pages /api/chapters/{chapterId}                  -> {data:{pages:[{url,…}]}}
#
# ID model: the URL-facing id is the short base-36 `hid` (e.g. "dkw", "0w5k").
# The API only accepts hid (numeric `id` 404s). Old library URLs of the form
# /manga/{slug}.{hid} 301-redirect to /title/{hid}-{slug} AND their hid is
# preserved on the new API, so existing series resume with no re-match — see
# _extract_hid, which parses both URL shapes.
#
# Cross-file: BaseSiteHandler.fast_download_images (sites/base.py) consumes the
# List[str] from get_chapter_images + the FAST_DL_* attrs below. The search
# orchestrator (sites/search_orchestrator.py) probes via get_chapter_images;
# EXPENSIVE_PROBE is now False because a page fetch is a single cheap JSON GET.
# ---------------------------------------------------------------------------


# All bare print() calls in this module emit to stderr by default. Why: this
# handler logs progress/diagnostics via plain print(); when called from the
# orchestrator's search-time probe (sites/search_orchestrator.py:_probe_one),
# that chatter would otherwise land in stdout — which carries the JSON --search
# output for piped consumers. Explicit file=sys.stdout still opts out.
def _stderr_print(*args, **kwargs):
    kwargs.setdefault("file", sys.stderr)
    return _builtins.print(*args, **kwargs)


print = _stderr_print  # noqa: A001 — intentional shadow of builtins.print


# Optional jittered throttle. Env vars read once at import (they don't change
# at runtime). Default 0.0 → no delay; useful only if the API ever starts
# rate-limiting bursts.
_MF_DELAY_REQUEST: float = 0.0
try:
    _MF_DELAY_REQUEST = float(os.getenv("MANGAFIRE_DELAY_REQUEST", "0.0"))
except Exception:
    pass


def _mf_throttle() -> None:
    if _MF_DELAY_REQUEST <= 0:
        return
    time.sleep(_MF_DELAY_REQUEST * random.uniform(0.7, 1.3))


def _short(s: str, n: int = 220) -> str:
    s = (s or "").replace("\r", " ").replace("\n", " ")
    return s[:n] + ("…" if len(s) > n else "")


def _html_to_text(raw: Optional[str]) -> Optional[str]:
    """Flatten MangaFire's `synopsisHtml` (contains <br>, <b>, <a>, &quot;, …)
    into plain text for the `desc` field. Kept dependency-free (no bs4) since
    the rest of the handler is pure JSON."""
    if not raw:
        return None
    s = re.sub(r"(?i)<\s*br\s*/?>", "\n", raw)
    s = re.sub(r"(?i)</\s*(?:p|div|li)\s*>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s or None


class _BrowserApiResponse:
    """A minimal requests-shaped view over an in-page fetch result.

    Exists so `_is_token_rejection`, `_resp_diag` and the plain `status != 200`
    check keep working byte-for-byte whichever transport produced the response.
    Adding a second shape for them to handle is how the token-rejection retry
    would quietly stop firing on the browser path.
    """

    __slots__ = ("status_code", "text", "headers")

    def __init__(self, status: int, body: str) -> None:
        self.status_code = status
        self.text = body
        self.headers = {"content-type": "application/json"}

    def json(self):
        import json as _json

        return _json.loads(self.text)


class MangaFireSiteHandler(BaseSiteHandler):
    name = "mangafire"
    domains = ("mangafire.to",)

    # Per-chapter image fetch is now a single JSON GET (was a ~3-5s Patchright
    # VRF capture, which is why this used to be True to clamp low-confidence
    # search probes). False lets the orchestrator probe at full breadth for an
    # accurate image-quality signal — cost is negligible now.
    EXPENSIVE_PROBE = False

    # Opt into BaseSiteHandler.fast_download_images (curl_cffi async + HTTP/2).
    SUPPORTS_FAST_DOWNLOAD = _CURL_CFFI_AVAILABLE
    # Image CDN (l1n.mfcdn2.xyz) serves 200 with or without a Referer, but we
    # send one defensively in case hotlink policy tightens. UA pins a current
    # desktop Chrome; impersonate profile (JA3/JA4) comes from base.
    FAST_DL_REFERER_FROM = "https://mangafire.to/"
    FAST_DL_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )

    _BASE_URL = "https://mangafire.to"
    _API = "https://mangafire.to/api"

    # Set once by _warn_signing_unavailable_once; class-level so the suppression
    # spans every handler instance the search fan-out creates.
    _signing_warned = False

    # Sticky once Cloudflare has challenged the plain-HTTP path: every later call
    # goes straight to the browser rather than re-paying a guaranteed-403 round
    # trip. Class-level for the same reason as _signing_warned — the search
    # fan-out builds several handler instances and they should share one verdict.
    _api_via_browser = False

    # When both an "official" and an "unofficial" scan exist for the same
    # chapter number (common), keep this one; fall back to the other when the
    # preferred type is absent (e.g. newest chapters often only have
    # unofficial). Overridable via env MANGAFIRE_CHAPTER_TYPE. Both types serve
    # real pages — this is a quality/consistency choice, not availability.
    _DEFAULT_CHAPTER_TYPE = "official"

    # MangaFire status strings → the app's conventional labels.
    _STATUS_MAP = {
        "releasing": "Ongoing",
        "finished": "Completed",
        "on_hiatus": "Hiatus",
        "discontinued": "Cancelled",
        "not_yet_released": "Upcoming",
    }

    # ----------------------------- session -----------------------------

    def configure_session(self, scraper, args) -> None:
        scraper.headers.update(
            {
                "Referer": self._BASE_URL + "/",
                "User-Agent": self.FAST_DL_USER_AGENT,
                "X-Requested-With": "XMLHttpRequest",
                # API returns JSON regardless, but this matches the site and
                # does NOT break image fetches through the same scraper (the
                # CDN ignores the mismatched Accept and still returns 200).
                "Accept": "application/json",
            }
        )

    # ----------------------------- helpers -----------------------------

    def _resp_diag(self, response) -> str:
        try:
            status = getattr(response, "status_code", None)
        except Exception:
            status = None
        try:
            ctype = response.headers.get("content-type", "")
        except Exception:
            ctype = ""
        try:
            text = response.text or ""
        except Exception:
            text = ""
        return f"status={status} ctype={_short(ctype, 60)} body='{_short(text, 180)}'"

    def _extract_hid(self, url: str) -> str:
        """Return the short base-36 series id (hid) from any MangaFire URL.

        Handles both schemes:
          new:  /title/{hid}-{slug}[/{chapterId}]  -> hid before the first '-'
          old:  /manga/{slug}.{hid}                -> hid after the last '.'
        hids are [a-z0-9] and never contain '-', so the split is unambiguous.
        Old library URLs still resolve because their hid was preserved on the
        relaunch (and the old URL 301-redirects anyway)."""
        path = urlparse(url or "").path
        m = re.match(r"^/title/([^/?#-]+)-", path)
        if m:
            return m.group(1)
        m = re.match(r"^/title/([^/?#-]+)/?$", path)  # bare /title/{hid}
        if m:
            return m.group(1)
        if "." in path:
            return path.split(".")[-1].split("/")[0]
        return ""

    def _warn_signing_unavailable_once(self, exc: Exception) -> None:
        """One warning per process. Search fan-out probes several candidates and
        the orchestrator may retry, so an un-suppressed message would repeat
        dozens of times for a single missing dependency."""
        if getattr(type(self), "_signing_warned", False):
            return
        type(self)._signing_warned = True
        print(f"[!] MangaFire: skipping search — {exc}")

    @staticmethod
    def _is_token_rejection(resp) -> bool:
        """True for the site's two signature errors: `Missing token.` (we sent
        no vrf) and `Invalid token.` (the vrf didn't match the params). Both are
        403s, and both are DETERMINISTIC — retrying the identical request can
        never help, which is also why sites/hardening.py must not treat them as
        rate limiting."""
        if getattr(resp, "status_code", None) != 403:
            return False
        try:
            msg = str((resp.json() or {}).get("message") or "")
        except Exception:
            return False
        return "token" in msg.lower()

    @staticmethod
    def _is_cf_challenge_response(resp) -> bool:
        """True when *resp* is Cloudflare's interstitial rather than the API.

        Cross-file: sites/crawlee_utils.py:is_cf_challenge owns the phrase list.
        Soft-imported and failure-swallowing on purpose — a missing crawlee is a
        reason to keep the old behaviour, not to break every MangaFire call.
        """
        try:
            from .crawlee_utils import is_cf_challenge
        except Exception:
            return False
        try:
            return bool(
                is_cf_challenge(getattr(resp, "status_code", 0) or 0, resp.text or "")
            )
        except Exception:
            return False

    def _fetch_signed(self, url: str, scraper, make_request):
        """GET a signed API URL, over plain HTTP for as long as that works.

        mangafire.to has been behind a Cloudflare Managed Challenge since
        ~2026-08-16 and NO headless client gets past it — measured 2026-08-19,
        plain requests / impit chrome / curl_cffi chrome / cloudscraper were all
        served the interstitial for both the homepage and /api/*. While that is
        up, the browser the signer already keeps open is the only client that can
        read this API, so the first challenged response flips `_api_via_browser`
        and everything after it reads in-page.

        Costs one bool when Cloudflare is off, which is the point: the "only
        signing needs a browser" property still holds in the normal case.

        Returns a response-shaped object, or None when the browser could not
        produce one. Retryable/rate-limit failures raised by make_request
        propagate per its contract.
        """
        if not type(self)._api_via_browser:
            resp = make_request(url, scraper)
            if not self._is_cf_challenge_response(resp):
                return resp
            type(self)._api_via_browser = True
            print(
                "[!] MangaFire: Cloudflare is challenging the API over HTTP; "
                "switching to the browser-backed fetch for the rest of this run."
            )
        out = mangafire_vrf.fetch_api_json(url)
        if not isinstance(out, dict):
            return None
        return _BrowserApiResponse(
            int(out.get("status") or 0), str(out.get("body") or "")
        )

    def _api_get(
        self,
        path: str,
        pairs: List[tuple],
        scraper,
        make_request,
        *,
        label: str,
    ) -> Optional[Dict]:
        """GET a signed JSON endpoint. `path` is the API path WITHOUT the `/api`
        prefix; `pairs` is the ordered query params (order matters — it is what
        the vrf cipher covers, see sites/mangafire_vrf.py:_cache_key).

        Returns the parsed dict, or None on a clean non-200 / non-JSON response.
        Retryable/rate-limit failures raised by make_request propagate (per its
        contract). A signing failure raises MangaFireSigningError, which callers
        on download paths deliberately let through.

        On a token rejection we re-sign ONCE against a freshly bootstrapped
        bundle: that is the site having redeployed mid-run, rotating the key our
        cached token was minted under.
        """
        for attempt in (0, 1):
            signed = mangafire_vrf.sign_api_query(path, pairs)
            url = f"{self._API}{path}"
            query = signed.get("query") or ""
            if query:
                url = f"{url}?{query}"

            _mf_throttle()
            resp = self._fetch_signed(url, scraper, make_request)
            if resp is None:
                print(
                    f"[!] {label}: could not reach the MangaFire API "
                    f"(Cloudflare challenge and no browser-backed fetch available)."
                )
                return None
            status = getattr(resp, "status_code", None)

            if self._is_token_rejection(resp):
                if attempt == 0:
                    print(
                        f"[!] {label}: MangaFire rejected the vrf token; "
                        "re-signing against the current bundle."
                    )
                    mangafire_vrf.invalidate()
                    continue
                print(f"[!] {label}: MangaFire still rejects the vrf token after re-signing.")
                print(f"    {self._resp_diag(resp)}")
                return None

            if status != 200:
                print(f"[!] {label}: HTTP {status} for {url}")
                print(f"    {self._resp_diag(resp)}")
                return None
            try:
                data = resp.json()
            except Exception as e:
                print(f"[!] {label}: JSON decode failed for {url}: {e}")
                print(f"    {self._resp_diag(resp)}")
                return None
            return data if isinstance(data, dict) else None
        return None

    def _map_status(self, raw) -> Optional[str]:
        if not raw:
            return None
        return self._STATUS_MAP.get(str(raw).lower(), str(raw).replace("_", " ").title())

    @staticmethod
    def _fmt_num(num) -> str:
        """Format a chapter `number` (JSON int or float) as the `chap` string.
        Integer-valued -> "1187"; fractional -> "24.1" (no trailing zeros)."""
        try:
            f = float(num)
        except (TypeError, ValueError):
            return str(num).strip()
        if f.is_integer():
            return str(int(f))
        return ("%f" % f).rstrip("0").rstrip(".")

    @staticmethod
    def _num_sort_key(chap: str):
        try:
            return (0, float(chap))
        except (TypeError, ValueError):
            return (1, 0.0)

    def _chapter_type_preference(self) -> str:
        v = (os.getenv("MANGAFIRE_CHAPTER_TYPE", "") or "").strip().lower()
        return v if v in ("official", "unofficial") else self._DEFAULT_CHAPTER_TYPE

    def _prefer_chapter(self, new: Dict, cur: Dict) -> bool:
        """True if `new` should replace `cur` as the kept entry for a chapter
        number. Prefers the configured type; within the same type keeps the
        newer createdAt."""
        pref = self._chapter_type_preference()
        new_t = (new.get("type") or "").lower()
        cur_t = (cur.get("type") or "").lower()
        if new_t == pref and cur_t != pref:
            return True
        if cur_t == pref and new_t != pref:
            return False
        return int(new.get("createdAt") or 0) > int(cur.get("createdAt") or 0)

    # ----------------------------- context -----------------------------

    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        hid = self._extract_hid(url)
        if not hid:
            raise RuntimeError(f"MangaFire: could not extract series id from URL: {url}")

        data = self._api_get(
            f"/titles/{hid}", [], scraper, make_request, label="detail"
        )
        payload = data.get("data") if isinstance(data, dict) else None
        if not isinstance(payload, dict) or not payload:
            raise RuntimeError(f"MangaFire: empty/invalid detail for hid={hid} ({url})")

        title = (payload.get("title") or "").strip() or "Unknown Title"
        slug = payload.get("slug") or ""
        poster = payload.get("poster") or {}
        cover = poster.get("large") or poster.get("medium") or poster.get("small")
        authors = [a.get("title") for a in (payload.get("authors") or []) if isinstance(a, dict) and a.get("title")]
        artists = [a.get("title") for a in (payload.get("artists") or []) if isinstance(a, dict) and a.get("title")]
        genres = [g.get("title") for g in (payload.get("genres") or []) if isinstance(g, dict) and g.get("title")]
        alt_names = [t.strip() for t in (payload.get("altTitles") or []) if isinstance(t, str) and t.strip()]
        languages = [l for l in (payload.get("languages") or []) if isinstance(l, str) and l]
        year = payload.get("year") if isinstance(payload.get("year"), int) else None
        canonical_url = urljoin(self._BASE_URL, payload.get("url") or f"/title/{hid}-{slug}")

        comic = {
            "hid": hid,
            "title": title,
            "slug": slug,
            "desc": _html_to_text(payload.get("synopsisHtml")),
            "cover": cover,
            "authors": authors,
            "artists": artists,
            "genres": genres,
            "status": self._map_status(payload.get("status")),
            "url": canonical_url,
            # Stashed for get_chapters + any downstream language selection.
            "_languages": languages,
        }
        if year is not None:
            comic["year"] = year
        if alt_names:
            comic["alt_names"] = alt_names

        return SiteComicContext(comic=comic, title=title, identifier=hid, soup=None)

    # ----------------------------- chapters -----------------------------

    def get_chapters(self, context: SiteComicContext, scraper, language: str, make_request) -> List[Dict]:
        hid = context.identifier
        if not hid:
            return []
        slug = (context.comic or {}).get("slug") or ""
        lang = (language or "en").strip() or "en"

        # Collect every page (limit caps at 100; paginate on meta.hasNext).
        raw: List[Dict] = []
        page = 1
        _MAX_PAGES = 500  # defensive backstop (≈50k chapters)
        while page <= _MAX_PAGES:
            data = self._api_get(
                f"/titles/{hid}/chapters",
                [
                    ("language", lang),
                    ("sort", "number"),
                    ("order", "asc"),
                    ("page", page),
                    ("limit", 100),
                ],
                scraper,
                make_request,
                label=f"chapters p{page}",
            )
            if not isinstance(data, dict):
                break
            items = data.get("items") or []
            if isinstance(items, list):
                raw.extend(x for x in items if isinstance(x, dict))
            meta = data.get("meta") or {}
            if not meta.get("hasNext"):
                break
            page += 1

        # Dedup by chapter number, preferring the configured scan type.
        chosen: Dict[str, Dict] = {}
        for it in raw:
            if it.get("number") is None or it.get("id") is None:
                continue
            key = self._fmt_num(it.get("number"))
            cur = chosen.get(key)
            if cur is None or self._prefer_chapter(it, cur):
                chosen[key] = it

        chapters: List[Dict] = []
        for it in chosen.values():
            cid = it.get("id")
            chap = self._fmt_num(it.get("number"))
            name = (it.get("name") or "").strip() or f"Chapter {chap}"
            chapters.append(
                {
                    "hid": str(cid),  # numeric chapter id → get_chapter_images
                    "chap": chap,
                    "title": name,
                    "url": f"{self._BASE_URL}/title/{hid}-{slug}/{cid}",
                    "uploaded": int(it.get("createdAt") or 0),
                    "type": it.get("type"),
                }
            )
        chapters.sort(key=lambda c: self._num_sort_key(c["chap"]))
        return chapters

    # ----------------------------- images -----------------------------

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        cid = chapter.get("hid")
        if not cid:
            print("[!] Chapter missing id; cannot fetch images.")
            return []
        data = self._api_get(
            f"/chapters/{cid}", [], scraper, make_request, label=f"pages {cid}"
        )
        payload = data.get("data") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            return []
        pages = payload.get("pages") or []
        return [
            p["url"]
            for p in pages
            if isinstance(p, dict) and isinstance(p.get("url"), str) and p.get("url")
        ]

    # ----------------------------- search -----------------------------

    def search(
        self,
        query: str,
        scraper,
        make_request,
        *,
        language: str = "en",
        limit: int = 20,
    ) -> List[SearchHit]:
        clean = (query or "").strip()
        if not clean:
            return []
        try:
            n_limit = max(1, int(limit))
        except (TypeError, ValueError):
            n_limit = 20

        # Signing failures degrade to "no hits", never an exception. WHY: the
        # orchestrator's persistent ProbeFailureCache (sites/search_orchestrator.py
        # :_run_one -> record_failure, threshold 2, TTL 1h) would blocklist
        # mangafire.to for an hour after two raised searches — and if the user
        # then installs the browser dependency they'd still be locked out. Same
        # deliberate override of base.py:search's "let errors propagate" rule
        # that sites/comix.py:search makes, for the same reason. Genuine HTTP
        # errors from make_request still propagate.
        try:
            data = self._api_get(
                "/titles",
                [("keyword", clean), ("limit", n_limit)],
                scraper,
                make_request,
                label="search",
            )
        except mangafire_vrf.MangaFireSigningError as e:
            self._warn_signing_unavailable_once(e)
            return []
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            return []

        hits: List[SearchHit] = []
        seen: set = set()
        total = len(items)
        for idx, it in enumerate(items):
            if len(hits) >= n_limit:
                break
            if not isinstance(it, dict):
                continue
            title = (it.get("title") or "").strip()
            rel = it.get("url") or ""
            if not title or not rel:
                continue
            abs_url = urljoin(self._BASE_URL, rel).split("?")[0].split("#")[0]
            if abs_url in seen:
                continue
            seen.add(abs_url)

            poster = it.get("poster") or {}
            cover = poster.get("large") or poster.get("medium") or poster.get("small")

            hint: Optional[int] = None
            latest = it.get("latestChapter")
            try:
                if latest is not None:
                    hint = int(float(latest))
            except (TypeError, ValueError):
                hint = None

            year = it.get("year") if isinstance(it.get("year"), int) else None
            raw_score = max(0.05, 1.0 - (idx / max(1, total)))

            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=abs_url,
                    cover=cover,
                    alt_titles=[],
                    year=year,
                    language=language if language and language.lower() != "all" else None,
                    chapter_count_hint=hint,
                    raw_score=raw_score,
                    # MangaFire is an aggregator, not an official publisher.
                    is_official=False,
                )
            )
        return hits
