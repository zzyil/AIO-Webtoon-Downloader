from __future__ import annotations

import builtins as _builtins
import json
import os
import re
import sys
import time

import random


# All bare print() calls in this module emit to stderr by default. Why: this
# handler's get_chapters / get_chapter_images log progress + retry/VRF state
# via plain print(). When called from the orchestrator's search-time image-
# quality probe path (sites/search_orchestrator.py:_probe_one), that chatter
# would land in stdout — which carries the JSON --search output for piped
# consumers. This shim keeps stdout clean without touching every print site.
# Explicit file= overrides still work (e.g., pass file=sys.stdout to opt out).
def _stderr_print(*args, **kwargs):
    kwargs.setdefault("file", sys.stderr)
    return _builtins.print(*args, **kwargs)


print = _stderr_print  # noqa: A001 — intentional shadow of builtins.print

# Cache throttle delays at module load – these env vars don't change at runtime.
# Reading os.getenv on every throttle call adds unnecessary overhead.
_MF_DELAY_REQUEST: float = 0.0
_MF_DELAY_CHAPTER: float = 0.0
try:
    _MF_DELAY_REQUEST = float(os.getenv("MANGAFIRE_DELAY_REQUEST", "0.0"))
except Exception:
    pass
try:
    _MF_DELAY_CHAPTER = float(os.getenv("MANGAFIRE_DELAY_CHAPTER", "0.0"))
except Exception:
    pass

def _mf_throttle(tag: str = "request") -> None:
    """Optional jittered delay to reduce burstiness when MangaFire/Cloudflare is sensitive.

    Env vars (read once at import):
      - MANGAFIRE_DELAY_REQUEST (seconds, default 0.0)
      - MANGAFIRE_DELAY_CHAPTER (seconds, default 0.0)
    """
    base = _MF_DELAY_CHAPTER if tag == "chapter" else _MF_DELAY_REQUEST
    if base <= 0:
        return
    time.sleep(base * random.uniform(0.7, 1.3))

from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BaseSiteHandler, SearchHit, SiteComicContext
# finalize_pending_image and Callable were used by the local
# fast_download_images method (now in BaseSiteHandler since 2026-05-13).
# Drop the imports since nothing else in this module uses them.

try:
    from .mangafire_vrf_simple import get_vrf_generator
    VRF_AVAILABLE = True
except Exception:
    VRF_AVAILABLE = False

# curl_cffi capability flag re-exported from sites/base.py for back-compat
# with anything that grepped this symbol on mangafire.py before the
# 2026-05-13 generalization. The actual fast-download infrastructure lives
# on BaseSiteHandler now; MangaFire opts in by setting SUPPORTS_FAST_DOWNLOAD
# below and overriding the FAST_DL_* attributes.
from .base import _CURL_CFFI_AVAILABLE  # noqa: F401 — back-compat re-export


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _short(s: str, n: int = 240) -> str:
    s = (s or "").replace("\r", " ").replace("\n", " ")
    return s[:n] + ("…" if len(s) > n else "")


class MangaFireSiteHandler(BaseSiteHandler):
    name = "mangafire"
    domains = ("mangafire.to",)
    # MangaFire's per-chapter fetch needs a Playwright VRF capture (~3-5s
    # per chapter) on top of the actual image downloads. The orchestrator's
    # search-phase probe runs this for every match, including low-confidence
    # ones (spinoffs, doujinshi, wrong series sharing a token). Flagging
    # EXPENSIVE_PROBE=True lets the orchestrator clamp low-title-match
    # results to a single image sample instead of the usual 5 — saving 4
    # image fetches × N noise results per search. See
    # search_orchestrator.py:EXPENSIVE_PROBE_QUICK_THRESHOLD.
    EXPENSIVE_PROBE = True

    # Class-level capability flag picked up by aio-dl.py's chapter loop. When
    # True (and curl_cffi is importable), Phase 2 of _process_chapter_impl and
    # the inter-chapter image prefetch route through fast_download_images
    # (curl_cffi async + HTTP/2) instead of the generic ThreadPoolExecutor +
    # dl_image cloudscraper path. Bench (2026-05-09, 83-page chapter):
    # cloudscraper 3-thread = 10.20s; curl_cffi async @ conc=8 = 6.04s. The
    # ceiling is the local network bandwidth (~5 MB/s on this test network);
    # higher concurrency past ~12 is diminishing returns. Toggled off if
    # curl_cffi failed to import — main loop falls back gracefully.
    SUPPORTS_FAST_DOWNLOAD = _CURL_CFFI_AVAILABLE

    # Fast-download config consumed by BaseSiteHandler.fast_download_images.
    # Referer satisfies MangaFire's anti-hotlink protection on the image CDN.
    # User-Agent pins Chrome/122 to match the Patchright session UA so that
    # any CF cookie-validation against the cf_clearance cookie's UA
    # fingerprint stays consistent (currently the image hits are cookieless
    # edge-cache HITs, but be defensive in case CF's policy changes). The
    # impersonate profile inherits chrome120 from base — distinct from the
    # UA but related (UA is a header string; impersonate sets the JA3/JA4
    # TLS fingerprint).
    FAST_DL_REFERER_FROM = "https://mangafire.to/"
    FAST_DL_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )

    _BASE_URL = "https://mangafire.to"

    # Retry knobs (fixed delays; no exponential backoff)
    _JSON_RETRIES = _env_int("MANGAFIRE_JSON_RETRIES", 3)
    _JSON_RETRY_DELAY = _env_float("MANGAFIRE_JSON_RETRY_DELAY", 3.0)
    _VRF_RETRIES = _env_int("MANGAFIRE_CHAPTER_VRF_RETRIES", 3)
    _VRF_RETRY_DELAY = _env_float("MANGAFIRE_CHAPTER_VRF_RETRY_DELAY", 3.0)

    def configure_session(self, scraper, args) -> None:
        scraper.headers.update(
            {
                "Referer": self._BASE_URL + "/",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "X-Requested-With": "XMLHttpRequest",
            }
        )

    def _make_soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def _extract_id_from_url(self, url: str) -> str:
        # URL format: https://mangafire.to/manga/name.id
        path = urlparse(url).path
        if "." in path:
            return path.split(".")[-1]
        return ""

    def _resp_diag(self, response) -> str:
        """A compact diagnostic string for logging."""
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
        head = _short(text, 180)
        return f"status={status} ctype={_short(ctype, 60)} body='{head}'"

    def _safe_json(self, response, *, label: str, url: str) -> Optional[Dict]:
        """Parse JSON with a clear log if it fails (often the server returns HTML while status=200)."""
        try:
            return response.json()
        except Exception as e:
            print(f"[!] {label}: JSON decode failed for {url}: {e}")
            print(f"    {self._resp_diag(response)}")
            return None

    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        _mf_throttle('request')

        # If the caller passed a /read/<slug>... URL (chapter reader URL,
        # possibly truncated to just /read/<slug>), rewrite it to the series
        # URL (/manga/<slug>) BEFORE the HTTP fetch. The chapter reader page
        # doesn't render h1[itemprop=name] so title extraction would return
        # "Unknown Title" — which then propagates to the aio-dl.py header
        # printout (the UI's queue-text source), file naming, multi-source
        # search, etc. _extract_id_from_url's path.split('.')[-1] gives the
        # same hid for both URL forms, so the manga_id stays consistent.
        # Mirror of the same rewrite in mangafire_vrf_simple.capture_series_meta.
        _read_url_match = re.match(
            r"^https?://(?:www\.)?mangafire\.to/read/([^/?#]+)",
            url,
            re.IGNORECASE,
        )
        if _read_url_match:
            url = f"https://mangafire.to/manga/{_read_url_match.group(1)}"

        response = make_request(url, scraper)

        # MangaFire sometimes returns JSON-wrapped HTML
        html_content = response.text
        try:
            from .crawlee_utils import ZENDRIVER_AVAILABLE, fetch_html_with_cf_cookies, is_cf_challenge

            if ZENDRIVER_AVAILABLE and is_cf_challenge(response.status_code, html_content):
                html_content = fetch_html_with_cf_cookies(url, base_url=self._BASE_URL)
        except Exception:
            pass
        if html_content.strip().startswith("{"):
            try:
                data = response.json()
                if data.get("status") == 200 and "result" in data:
                    html_content = data["result"]
            except Exception:
                pass  # Not JSON, use as-is

        soup = self._make_soup(html_content)

        manga_id = self._extract_id_from_url(url)

        title_node = soup.select_one("h1[itemprop='name']")
        title = title_node.get_text(strip=True) if title_node else "Unknown Title"

        cover_node = soup.select_one(".poster img[itemprop='image']")
        cover = cover_node.get("src") if cover_node else None

        desc = None
        desc_modal = soup.select_one("#synopsis")
        if desc_modal:
            close_btn = desc_modal.select_one(".modal-close")
            if close_btn:
                close_btn.decompose()
            desc = desc_modal.get_text(strip=True)
        else:
            desc_node = soup.select_one(".description")
            if desc_node:
                desc = desc_node.get_text(strip=True)

        status = None
        status_node = soup.select_one(".info p")
        if status_node:
            status_text = status_node.get_text(strip=True)
            if status_text in ["Releasing", "Ongoing"]:
                status = "Ongoing"
            elif status_text in ["Completed", "Finished"]:
                status = "Completed"
            else:
                status = status_text

        authors: List[str] = []
        for author_link in soup.select(".meta a[itemprop='author']"):
            author_name = author_link.get_text(strip=True)
            if author_name:
                authors.append(author_name)

        # Artists — MangaFire's series page exposes a separate "Artist" field
        # via the same schema.org itemprop pattern as authors. Without this
        # extraction Komikku's details.json `artist` field stayed empty even
        # though MangaFire ships the data. See dry_run_komikku_findings.md §A.
        # Fallback selectors cover layouts where the itemprop attribute was
        # dropped: `.meta` block typically renders rows as `<div>Label:
        # <a>Value</a></div>` so we also try an Artist-label-prefixed anchor
        # walk.
        artists: List[str] = []
        for artist_link in soup.select(".meta a[itemprop='artist']"):
            artist_name = artist_link.get_text(strip=True)
            if artist_name:
                artists.append(artist_name)
        if not artists:
            # Fallback: scan `.meta div` rows for a label starting with "Artist"
            # and pull anchor text from inside. Mirrors the existing genre
            # row walk a few lines below.
            for div in soup.select(".meta div"):
                span = div.select_one("span")
                if span and "Artist" in span.get_text():
                    for anchor in div.select("a[href]"):
                        text = anchor.get_text(strip=True)
                        if text:
                            artists.append(text)
                    break

        genres: List[str] = []
        for div in soup.select(".meta div"):
            span = div.select_one("span")
            if span and "Genres:" in span.get_text():
                for genre_link in div.select("a[href^='/genre/']"):
                    g = genre_link.get_text(strip=True)
                    if g:
                        genres.append(g)
                break

        # Year: "Published: Mar 21, 2017 to Sep 21, 2018" → 2017
        year: Optional[int] = None
        for div in soup.select(".meta div"):
            span = div.select_one("span")
            if span and "Published" in span.get_text():
                year_match = re.search(r"\b(\d{4})\b", div.get_text(" ", strip=True))
                if year_match:
                    year = int(year_match.group(1))
                break

        # Alt names: `<h6 class="original">Native Title; English Title</h6>`
        # under the series title. Mihon's MangaFire extension uses the same hook.
        alt_names: List[str] = []
        alt_node = soup.select_one("h6.original")
        if alt_node:
            alt_text = alt_node.get_text(strip=True)
            if alt_text:
                alt_names = [p.strip() for p in re.split(r"[;,/]", alt_text) if p.strip()]

        comic = {
            "hid": manga_id,
            "title": title,
            "desc": desc,
            "cover": cover,
            "authors": authors,
            "artists": artists,
            "genres": genres,
            "status": status,
            "url": url,
        }
        if year is not None:
            comic["year"] = year
        if alt_names:
            comic["alt_names"] = alt_names
        return SiteComicContext(comic=comic, title=title, identifier=manga_id, soup=soup)

    # ----------------------------- Chapters -----------------------------

    def get_chapters(self, context: SiteComicContext, scraper, language: str, make_request) -> List[Dict]:
        manga_id = context.identifier
        if not manga_id:
            return []

        lang_code = language if language else "en"

        # Primary endpoint (VRF-protected):
        # https://mangafire.to/ajax/read/{id}/chapter/{lang}?vrf=...
        read_ajax_path = f"/ajax/read/{manga_id}/chapter/{lang_code}"
        read_ajax_url = self._BASE_URL + read_ajax_path

        if VRF_AVAILABLE:
            try:
                vrf_gen = get_vrf_generator()
                init_reader_url = f"{self._BASE_URL}/read/manga.{manga_id}/{lang_code}/chapter-1"
                vrf = vrf_gen.ensure_vrf(read_ajax_path, init_url=init_reader_url)
                read_ajax_url = f"{read_ajax_url}?vrf={vrf}"
            except Exception as e:
                print(f"[!] Chapter list VRF failed: {e}")
                print("    (continuing without VRF; fallback endpoint may still work)")

        # Try read endpoint (with IDs).
        # Playwright fetch_ajax fallback (in-browser via live VRF bridge with
        # CF cookies in session) attempted first; if that returns nothing,
        # fall back to the direct cloudscraper hit. Defensive against CF
        # tightening on /ajax/read — currently both paths work, but the
        # cost is one extra method dispatch + a try/except, so cheap insurance.
        try:
            print(f"[*] Fetching chapters from: {read_ajax_url}")
            _mf_throttle('request')
            data = None
            if VRF_AVAILABLE:
                try:
                    resp_text = vrf_gen.fetch_ajax(read_ajax_url)
                    if resp_text:
                        data = json.loads(resp_text)
                except Exception as e:
                    print(f"[-] Playwright fetch_ajax chapter list warning: {e}")
            if not data:
                resp = make_request(read_ajax_url, scraper)
                data = self._safe_json(resp, label="chapter-list", url=read_ajax_url)
            if not data or data.get("status") != 200:
                raise RuntimeError(f"status={None if not data else data.get('status')}")

            html_content = None
            result = data.get("result")
            if isinstance(result, dict):
                html_content = result.get("html") or result.get("result") or result.get("data")
            elif isinstance(result, str):
                html_content = result
            if not html_content:
                raise RuntimeError("missing result HTML")

            soup = self._make_soup(html_content)
            chapters: List[Dict] = []
            # Phase 1 dedupe: MangaFire's chapter list HTML often contains
            # multiple <a data-id> rows for the same chapter — one row per
            # scanlation group / language variant / re-upload. They all share
            # the same data-number. Without dedupe, MangaFire's chapter count
            # gets inflated 2-3× over reality (e.g., Talentless Nana reports
            # 362 entries vs ~118 actual main chapters), which then poisons
            # both the per-source coverage display AND the alignment-anchor
            # selection in chapter_merger (anchor-by-largest-set picks
            # MangaFire even when other sources have the truer chapter list).
            #
            # Strategy: keep the first <a data-id> per data-number. MangaFire
            # serves rows ordered by group popularity, so the first one is
            # typically the most-viewed translation — best heuristic match
            # for "the" canonical chapter when we have to pick one.
            seen_numbers: set = set()
            for a in soup.select("a[data-id]"):
                chap_id = a.get("data-id")
                if not chap_id:
                    continue
                chap_num = a.get("data-number") or a.get("data-num") or a.get("data-chapter")
                dedupe_key = (chap_num or "").strip()
                if dedupe_key:
                    if dedupe_key in seen_numbers:
                        continue
                    seen_numbers.add(dedupe_key)
                # Empty/missing data-number: keep the entry (rare anomaly;
                # silently dropping would lose data). Such rows can't be
                # deduped anyway since we have no key.
                title = a.get("title") or a.get_text(strip=True)
                href = a.get("href") or ""
                full_url = urljoin(self._BASE_URL, href)
                chapters.append(
                    {
                        "hid": chap_id,
                        "chap": chap_num,
                        "title": title,
                        "url": full_url,
                        "uploaded": 0,
                    }
                )
            if chapters:
                return chapters
            print("[!] Read endpoint returned no <a data-id> items; will fall back.")
        except Exception as e:
            print(f"[!] Read endpoint failed: {e}")

        # Fallback endpoint (often works, but lacks internal IDs)
        fallback_url = f"{self._BASE_URL}/ajax/manga/{manga_id}/chapter/{lang_code}"
        print(f"[*] Falling back to: {fallback_url}")

        try:
            _mf_throttle('request')
            resp = make_request(fallback_url, scraper)
            data = self._safe_json(resp, label="chapter-list-fallback", url=fallback_url)
            if not data or data.get("status") != 200:
                return []
            html = data.get("result")
            if not html:
                return []
            soup = self._make_soup(html)
        except Exception as e:
            print(f"[!] Fallback chapter list failed: {e}")
            return []

        chapters: List[Dict] = []
        # Phase 1 dedupe (same rationale as primary endpoint above) — fallback
        # /ajax/manga path occasionally also returns duplicate rows when the
        # series has multiple language editions queued.
        seen_numbers: set = set()
        for li in soup.select("li.item"):
            a_tag = li.select_one("a")
            if not a_tag:
                continue
            chap_num = li.get("data-number")
            dedupe_key = (chap_num or "").strip() if isinstance(chap_num, str) else (str(chap_num) if chap_num is not None else "")
            if dedupe_key:
                if dedupe_key in seen_numbers:
                    continue
                seen_numbers.add(dedupe_key)
            href = a_tag.get("href") or ""
            title = a_tag.get("title") or a_tag.get_text(strip=True)
            full_url = urljoin(self._BASE_URL, href)
            chapters.append({"hid": chap_num, "chap": chap_num, "title": title, "url": full_url, "uploaded": 0})
        return chapters

    # ----------------------------- Images -----------------------------

    def _parse_images_from_result(self, result) -> List[str]:
        # result can be str(html) or dict(json)
        if isinstance(result, dict):
            images = result.get("images") or result.get("pages") or []
            if images and isinstance(images[0], list):
                images = [img[0] if isinstance(img, list) and img else img for img in images]
            return [u for u in images if isinstance(u, str) and u]

        if isinstance(result, str):
            soup = self._make_soup(result)
            images: List[str] = []

            for img in soup.select("img[data-url]"):
                u = img.get("data-url")
                if u:
                    images.append(u)

            if not images:
                for img in soup.select("img.page-img, img.img-fluid"):
                    u = img.get("src") or img.get("data-src")
                    if u:
                        images.append(u)

            if not images:
                # Look for inline JSON arrays in scripts
                for script in soup.find_all("script"):
                    st = script.string
                    if not st:
                        continue
                    if ("images" in st) or ("pages" in st):
                        m = re.search(r"(?:images|pages)\s*[:=]\s*(\[[^\]]+\])", st)
                        if m:
                            try:
                                arr = json.loads(m.group(1))
                                if isinstance(arr, list):
                                    images = [x[0] if isinstance(x, list) and x else x for x in arr]
                                    images = [u for u in images if isinstance(u, str) and u]
                                    break
                            except Exception:
                                continue

            return images

        return []

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        if not VRF_AVAILABLE:
            raise NotImplementedError(
                "MangaFire image downloading requires Patchright for VRF generation. "
                "Install with: pip install patchright && python -m patchright install chromium"
            )

        chapter_id = chapter.get("hid")
        chapter_url = chapter.get("url")
        if not chapter_id:
            print("[!] Chapter missing ID; cannot fetch images.")
            return []

        ajax_path = f"/ajax/read/chapter/{chapter_id}"
        ajax_url_base = self._BASE_URL + ajax_path

        vrf_gen = get_vrf_generator()

        last_err: Optional[Exception] = None

        for attempt in range(1, self._JSON_RETRIES + 1):
            stage = "start"
            try:
                # 1) VRF
                stage = "vrf"
                if chapter_url:
                    # The actual capture path (in-page fetch vs navigation) is
                    # decided inside ensure_vrf and logged with its own per-
                    # attempt diagnostic lines — don't pre-claim "navigation".
                    print(f"[*] Capturing chapter VRF for: {chapter_url}")
                    vrf = vrf_gen.ensure_vrf(ajax_path, page_url=chapter_url, init_url=chapter_url)
                else:
                    vrf = vrf_gen.ensure_vrf(ajax_path)
                ajax_url = f"{ajax_url_base}?vrf={vrf}"

                # 2) AJAX fetch — Playwright fetch_ajax first (live VRF bridge,
                # has fresh CF cookies), fall back to direct cloudscraper hit.
                # Defensive against CF tightening on /ajax/read/chapter (the
                # cloudscraper hit currently works; the in-browser fetch is
                # cheap insurance plus it reuses the existing VRF browser
                # session so there's no extra browser launch cost).
                stage = "ajax"
                print(f"[*] Fetching images for chapter {chapter_id} (attempt {attempt}/{self._JSON_RETRIES})…")
                _mf_throttle('request')
                resp_text = None
                try:
                    resp_text = vrf_gen.fetch_ajax(ajax_url)
                except Exception as e:
                    print(f"[-] Playwright fetch_ajax warning: {e}")

                # 3) JSON parse
                stage = "json"
                if resp_text:
                    try:
                        data = json.loads(resp_text)
                    except Exception as e:
                        raise RuntimeError(f"fetch_ajax JSON decode failed: {e}")
                else:
                    resp = make_request(ajax_url, scraper)
                    data = self._safe_json(resp, label=f"chapter-{chapter_id}", url=ajax_url)
                if not data:
                    raise RuntimeError("non-json response")

                if data.get("status") != 200:
                    raise RuntimeError(f"api status={data.get('status')}")

                # 4) parse result
                stage = "parse"
                result = data.get("result")
                images = self._parse_images_from_result(result)
                if not images:
                    raise RuntimeError("parsed 0 images from result")

                # 5) Rewrite malfunctioning CDN servers to working mirrors
                # (k99.*). Lives HERE (not in fast_download_images) so all
                # downstream paths see rewritten URLs: BaseSiteHandler's
                # curl_cffi fast path, the legacy cloudscraper fallback,
                # the inter-chapter prefetch pool, and the search-time
                # image-quality probe. Previously the rewrite lived inside
                # the now-deleted mangafire-specific fast_download_images,
                # which crashed the legacy path when curl_cffi was
                # unavailable. Rewriting at the URL-generation site fixes
                # that and keeps fast_download_images on the base class.
                #   nw8.mfcdn{N}.xyz → k99.mfcdn{N}.xyz (live CDN cluster)
                #   {nw8|fmcdn|static}.mfcdn.{nl|net} → k99.mfcdn3.xyz (dead-domain remap)
                rewritten_images = []
                for img_url in images:
                    if "mfcdn" in img_url:
                        img_url = re.sub(r'https://nw8\.mfcdn([0-9])\.xyz/', r'https://k99.mfcdn\1.xyz/', img_url)
                        img_url = img_url.replace("https://nw8.mfcdn.nl/", "https://k99.mfcdn3.xyz/")
                        img_url = img_url.replace("https://nw8.mfcdn.net/", "https://k99.mfcdn3.xyz/")
                        img_url = img_url.replace("https://fmcdn.mfcdn.net/", "https://k99.mfcdn3.xyz/")
                        img_url = img_url.replace("https://static.mfcdn.nl/", "https://k99.mfcdn3.xyz/")
                    rewritten_images.append(img_url)

                return rewritten_images

            except Exception as e:
                last_err = e
                print(f"[!] Chapter {chapter_id} attempt {attempt}/{self._JSON_RETRIES} failed at stage={stage}: {e}")
                if attempt < self._JSON_RETRIES:
                    time.sleep(self._JSON_RETRY_DELAY)
                    continue
                break

        # Final: help debugging
        print(f"[!] Giving up on chapter {chapter_id}. Last error: {last_err}")
        try:
            if hasattr(vrf_gen, "dump_state"):
                vrf_gen.dump_state()
        except Exception:
            pass
        return []

    # ----------------------------- Probe -----------------------------
    def _probe_cover_image(self, hit, scraper, make_request):
        """MangaFire-specific cover fallback. Strips the @<digits>
        thumbnail-size suffix so we fetch the full available cover
        rather than the search-card 100px thumbnail.

        MangaFire serves cover thumbnails at URLs like
            https://static.mfcdn.nl/<hash>/...<filename>@100.jpg
        where '@100' is a 100px-width thumbnail hint. The underlying CDN
        ignores the size token (all variants return the same 280×400
        image) so removing it at least gets us the full cover.

        Note: this is the cover-FALLBACK path. BaseSiteHandler.probe_sample_image
        first calls _probe_chapter_image which uses our get_chapter_images
        (VRF-protected) for an accurate chapter-image measurement — that's
        the preferred signal. Cover only fires when chapter-fetch fails (CF
        challenge on series page, VRF init failure, Playwright unavailable).
        Even with the @<digits> strip, cover stays at 280×400 which
        underranks MangaFire vs MangaDex/MangaReader covers — so the chapter
        path winning is important. Cross-file: search() returns hit.cover
        with the @100 suffix straight from the autocomplete payload.
        """
        if not hit or not getattr(hit, "cover", None):
            return None
        cover_url = hit.cover
        # Strip @<digits> tokens immediately before the file extension.
        cleaned = re.sub(r"@\d+(\.\w+)$", r"\1", cover_url)
        try:
            response = scraper.get(cleaned, timeout=10)
            if response.status_code >= 400:
                return None
            data = response.content
            if not data or len(data) < 256:
                return None
            return data
        except Exception:
            return None

    # Bulk chapter-image fetch lives on BaseSiteHandler.fast_download_images
    # (2026-05-13 generalization — same curl_cffi async + HTTP/2 + chrome120
    # impersonation + Semaphore(8) + finalize_pending_image semantics that
    # used to live here, plus cookie forwarding for handlers that need it).
    # MangaFire opts in via SUPPORTS_FAST_DOWNLOAD above; per-handler
    # customization comes from FAST_DL_REFERER_FROM and FAST_DL_USER_AGENT
    # class attrs (also above). The mangafire-specific bit that DOESN'T
    # belong in fast_download_images — CDN URL rewrites — moved up into
    # get_chapter_images so every downstream path (fast curl_cffi path,
    # legacy cloudscraper fallback, prefetch pool, search-time probe) sees
    # already-rewritten URLs. See conflict-resolution notes in commit msg.

    # ----------------------------- Search -----------------------------
    # MangaFire search: driven by the persistent Playwright bridge in
    # mangafire_vrf_simple.py:capture_search.
    #
    # Why not /filter?keyword= via cloudscraper?
    #   /filter is CF-WAF-blocked for HTTP scrapers (verified 2026-05-06:
    #   cloudscraper, curl_cffi-Chrome131..133, edge101 all return 403).
    #   /ajax/manga/search is also blocked when called directly because the
    #   site's bot defenses gate it on a VRF token that's only minted by the
    #   typeahead's keyup handler bound to .search-inner input[name=keyword].
    #   Mirrors the keiyoushi/extensions-source PR #11396 strategy: drive the
    #   live JS, capture the XHR, parse its result.html.
    #
    # Why not the canonical /filter UI scrape?
    #   The autocomplete payload (used here) is richer: it includes chapter
    #   count + status badges per result, which feed our Phase 4 DMCA-detection
    #   heuristics. /filter's full results page would only buy us ~30 entries
    #   vs autocomplete's ~5-8 — but the top entries are what matter for
    #   title-match scoring anyway.
    #
    # Returns [] when Playwright isn't available — matches the existing
    # VRF_AVAILABLE guard pattern. Errors that indicate "the host is broken
    # right now" raise so the orchestrator's probe-failure cache catches them.
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
        if not VRF_AVAILABLE:
            # Playwright not installed → graceful no-op. Orchestrator's
            # probe-failure cache treats this as a no-result, not a failure.
            return []

        # capture_search may raise on bridge/browser/CF problems; let it
        # propagate so the orchestrator's _run_one catches and caches.
        vrf_gen = get_vrf_generator()
        payload = vrf_gen.capture_search(clean)

        result = (payload or {}).get("result") or {}
        html = result.get("html") or ""
        if not html:
            return []

        soup = self._make_soup(html)
        # Autocomplete cards: <a class="unit" href="/manga/<slug>.<id>">
        #   .poster img[src]      -> cover
        #   .info h6              -> title
        #   .info span (multiple) -> status, "Chap N", "Vol N" (in that order;
        #                            we extract Chap N as chapter_count_hint
        #                            for the Phase 4 DMCA-detection compare).
        result_re = re.compile(r"^/manga/[\w\-]+\.\w+")
        anchors = soup.select("a.unit[href]")
        hits: List[SearchHit] = []
        seen: set = set()
        for idx, a in enumerate(anchors):
            if len(hits) >= limit:
                break
            href = (a.get("href") or "").strip()
            if not result_re.match(href):
                continue
            abs_url = urljoin(self._BASE_URL, href).split("?")[0].split("#")[0]
            if abs_url in seen:
                continue
            seen.add(abs_url)

            title_node = a.select_one(".info h6, .info .title, h6, h3")
            title = title_node.get_text(strip=True) if title_node else ""
            if not title:
                continue

            cover: Optional[str] = None
            img = a.select_one(".poster img, img")
            if img is not None:
                src = img.get("data-src") or img.get("src")
                if src:
                    cover = src if src.startswith("http") else urljoin(self._BASE_URL, src)

            chapter_count_hint: Optional[int] = None
            for span in a.select(".info span"):
                text = span.get_text(strip=True)
                m = re.match(r"(?:Chap|Chapter)\s*([\d.]+)", text, re.I)
                if m:
                    try:
                        chapter_count_hint = int(float(m.group(1)))
                    except ValueError:
                        pass
                    break

            raw_score = max(0.05, 1.0 - (idx / max(1, len(anchors))))
            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=abs_url,
                    cover=cover,
                    alt_titles=[],
                    year=None,
                    language=language if language and language.lower() != "all" else None,
                    chapter_count_hint=chapter_count_hint,
                    raw_score=raw_score,
                )
            )
        return hits
