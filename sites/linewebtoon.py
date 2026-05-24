"""LINE Webtoon (webtoons.com) handler — English /en/ only.

What this module owns:
  - SiteComicContext + chapter list extraction for canonical and canvas series
    on webtoons.com. The chapter list is fetched from the **mobile JSON API**
    (`m.webtoons.com/api/v1/{webtoon|canvas}/{title_no}/episodes`) which
    returns the entire episode catalog in one HTTP request — same pattern
    Mihon and Zehina/Webtoon-Downloader use, ~5–20× faster than HakuNeko's
    HTML pagination loop.
  - Image URL extraction from the desktop chapter viewer
    (`#_imageList img[data-url]`), with the gallery-dl host rewrite
    (`webtoon-phinf` → `swebtoon-phinf`) and `?type=q90` stripping for
    archival-quality JPEG. Per-user choice (this session): always strip.
  - Motion-toon (animated) viewer fallback — the inline JS manifest is
    parsed with two regexes (Mihon's approach) and each layer image is
    surfaced as a separate page in the URL list. PIL alpha-compositing is
    deferred to v2; static layers are intact / lossless.
  - Cross-site search via the public HTML endpoint
    (`/en/search?keyword=…`). Combined originals + canvas results.
  - is_official=True / publisher="LINE Webtoon" annotation on every
    chapter dict so chapter_merger.py:367–370 ranks webtoons above fan
    aggregators within multi-source merging (mirrors the MangaPlus
    on-MangaDex case in mangadex.py).

What reads from it:
  - sites/__init__.py — registers a singleton in `_BASE_HANDLERS` and the
    URL dispatcher routes any *.webtoons.com paste here.
  - aio-dl.py main flow — calls fetch_comic_context, get_chapters, and
    get_chapter_images via the handler interface. Returns List[str] of URLs;
    aio-dl.py:dl_image handles the actual download with the session-level
    Referer (set in configure_session).
  - sites/search_orchestrator.py — picks up search() override via
    iter_search_capable_handlers and runs the 5-page chapter-quality probe
    (default _probe_chapter_aggregate is sufficient — pure HTTP).

Cross-file coupling worth flagging:
  - chapter dicts emit `is_official: True` + `publisher: "LINE Webtoon"`
    + `group_name: "LINE Webtoon"`. Read by:
      * sites/chapter_merger.py:367–370 (sources sorted by is_official within
        each chapter row).
      * sites/_publishers.py — NOT used here (catalog is MangaDex-UUID-keyed);
        we hard-code the publisher name. Keeps lookup_publisher specialized
        to MangaDex.
  - Quality seed key: `linewebtoon` in sites/quality_seed.json (0.92).
    Tier matches arcrelight; stripped-q90 JPEG is good but not PNG-tier.
  - Throttle-friendly knob: webtoons CDN can 429 on >5 parallel image
    fetches. The user can pass `--image-workers 1` if pstatic.net throttles
    a long chapter (Tower of God Ep 1 ≈ 70 panels, all from the same host).
    aio-dl.py:984 `_CHAPTER_HOST_POISON=5` guards against rate-limit storms.

References (full source quotes in the planning doc at
C:\\Users\\legoc\\.claude\\plans\\explore-the-codebase-and-crystalline-pinwheel-agent-ad9d2c5a477c35597.md):
  - gallery-dl `gallery_dl/extractor/webtoons.py` — quality-strip + host rewrite.
  - Mihon `extensions-source/.../Webtoons.kt` — selectors + mobile API.
  - Zehina/Webtoon-Downloader `core/webtoon/...` — title_no via canonical link.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup, FeatureNotFound

from .base import (
    BaseSiteHandler,
    IncompleteChapterError,
    SearchHit,
    SiteComicContext,
    _CURL_CFFI_AVAILABLE,
)


# ---------------------------------------------------------------- module constants

_DESKTOP_BASE = "https://www.webtoons.com"
_MOBILE_BASE = "https://m.webtoons.com"

# Conservative union of working cookie sets used by gallery-dl, mihon, Zehina.
# Domain `.webtoons.com` shares across www. and m. subdomains.
_AGE_GATE_COOKIES = (
    ("ageGatePass", "true"),
    ("needGDPR",    "false"),
    ("needCCPA",    "false"),
    ("needCOPPA",   "false"),
    ("pagGDPR",     "true"),
    ("atGDPR",      "AD_CONSENT"),
    ("locale",      "en"),
)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Match `?type=q90` or `&type=q90` (1-3 digit quality). Used after host
# rewrite so swebtoon-phinf URLs come out param-less for original quality.
# We don't use a single regex because cleanup of the lone `?` is fiddly;
# helper does parse-style trimming instead.
_TYPE_PARAM_RE = re.compile(r"^type=q\d{1,3}$", re.IGNORECASE)

# Motion-toon manifest extraction. The viewer page embeds inline JS like:
#     documentURL: 'https://.../webtoon/.../doc.json'
#     jpg: 'https://.../webtoon/.../{=filename}'
# Both regexes are non-greedy and tolerant of whitespace.
_MOTIONTOON_DOC_RE = re.compile(r"documentURL\s*:\s*['\"]([^'\"]+)['\"]")
_MOTIONTOON_PATH_RE = re.compile(r"jpg\s*:\s*['\"]([^'\"\{]+)\{")

# title_no extraction fallback (when canonical link is absent or malformed).
# Accepts both modern (title_no=) and legacy (titleNo=) param names.
_TITLE_NO_RE = re.compile(r"\b(?:title_no|titleNo)=(\d+)", re.IGNORECASE)


# ---------------------------------------------------------------- the handler

class LineWebtoonSiteHandler(BaseSiteHandler):
    """Handler for webtoons.com (LINE Webtoon, English).

    Domains substring-match `webtoons.com`, which catches `www.`, `m.`, and
    bare. Mobile URLs are normalized to `www.` at the entry of every public
    method so downstream parsers (selectors, folder-naming) see canonical
    desktop URLs.

    EXPENSIVE_PROBE stays False (default) — pure-HTTP, the 5-page aggregate
    probe runs at full sample count.
    """

    name = "linewebtoon"
    domains = ("webtoons.com",)
    # webtoons.com IS the publisher (LINE Webtoon) — opt into the
    # orchestrator's "official wins the tiebreaker" rule so aggregators
    # re-hosting these series (toonily, etc.) rank below us within a
    # SeriesCandidate. Without this, aggregators that upscale our PNGs
    # to higher-res JPEG were winning the image-quality probe despite
    # being generation-loss copies. See BaseSiteHandler.OFFICIAL_PUBLISHER
    # for the full rationale and search_orchestrator.py:_cmp for the
    # consuming sort.
    OFFICIAL_PUBLISHER = True

    # Opt into the curl_cffi fast image-download path (HTTP/2 multiplex over
    # one keep-alive TLS session). Webtoons.com chapters are 25-60 PNG pages
    # at ~2-3 MB each; the per-page TLS handshake dominates the legacy
    # ThreadPoolExecutor path's wall-clock. curl_cffi cuts a typical chapter
    # from ~25s → ~10-15s. The image CDN (typically swebtoon-phinf.pstatic.net)
    # is on a different host than the .webtoons.com cookies — so cookie
    # forwarding from the cloudscraper session is a no-op for normal series
    # (cookies don't ride to a different domain). Anti-hotlink Referer is
    # the only required header; static webtoons.com homepage URL satisfies it.
    SUPPORTS_FAST_DOWNLOAD = _CURL_CFFI_AVAILABLE
    FAST_DL_REFERER_FROM = "https://www.webtoons.com/"
    # FAST_DL_USER_AGENT not set — let curl_cffi's chrome120 default fill it.
    # The cloudscraper session's _DEFAULT_UA pins Chrome/120 too, so the two
    # sessions stay roughly in sync without us hard-coding it twice.

    def __init__(self) -> None:
        super().__init__()
        # lxml-with-fallback parser cache (mirrors weebcentral.py:21–37).
        # lxml is faster for the long viewer HTML; html.parser is the
        # always-available stdlib fallback so this handler still works
        # if a user hasn't installed lxml.
        try:
            import lxml  # type: ignore  # noqa: F401
            self._parser = "lxml"
        except Exception:
            self._parser = "html.parser"

    # ----------------------------------------------------------------- helpers

    def _make_soup(self, html: str) -> BeautifulSoup:
        try:
            return BeautifulSoup(html, self._parser)
        except FeatureNotFound:
            return BeautifulSoup(html, "html.parser")

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Mobile→desktop rewrite. The desktop site renders `#_imageList`;
        the mobile site uses a different viewer markup."""
        return url.replace("://m.webtoons.com", "://www.webtoons.com")

    @staticmethod
    def _normalize_image_url(url: str) -> str:
        """Apply gallery-dl's CDN host rewrite + drop `?type=qN` for original
        quality.

        - `webtoon-phinf` → `swebtoon-phinf`: HTTPS variant on Naver's CDN;
          both alias the same content, but the `s` form is what every modern
          scraper uses (gallery-dl convention).
        - Strip `type=qN` from query: webtoons serves recompressed JPEG by
          default (q90); the param-less URL returns the highest-quality
          variant on the same CDN. Per user preference (this session),
          archival-grade is the default.

        Other query params (rare on this CDN) are preserved.
        """
        url = url.replace("://webtoon-phinf.", "://swebtoon-phinf.")
        if "?" not in url:
            return url
        base, _, query = url.partition("?")
        kept = [p for p in query.split("&") if p and not _TYPE_PARAM_RE.match(p)]
        return base + ("?" + "&".join(kept) if kept else "")

    @staticmethod
    def _extract_title_no(url: str, soup: Optional[BeautifulSoup] = None) -> Optional[int]:
        """Extract title_no from a URL or canonical link tag.

        Preference: `<link rel="canonical">` href query (Zehina pattern —
        robust to URL casing, redirects, and `&page=N` paginator suffixes).
        Falls back to regex on the raw URL, accepting both `title_no=`
        (modern) and `titleNo=` (legacy /episodeList? URL form).
        """
        if soup is not None:
            link = soup.select_one('link[rel="canonical"]')
            if link is not None:
                href = (link.get("href") or "").strip()
                if href:
                    qs = parse_qs(urlparse(href).query)
                    raw = (qs.get("title_no") or qs.get("titleNo") or [None])[0]
                    if raw and raw.isdigit():
                        return int(raw)
        m = _TITLE_NO_RE.search(url or "")
        return int(m.group(1)) if m else None

    @staticmethod
    def _is_canvas_url(url: str) -> bool:
        """`/en/canvas/<slug>/list?title_no=N` → canvas; otherwise originals.

        Discriminator is path segment [1] (after `/en/`). Edge case: legacy
        `/episodeList?titleNo=N` URLs have NO genre segment — those are
        always originals (canvas legacy was `/challenge/episodeList?...`).
        """
        path = urlparse(url).path or ""
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[1].lower() == "canvas":
            return True
        if "challenge" in parts:
            return True
        return False

    @staticmethod
    def _is_age_gate(response) -> bool:
        """webtoons.com may either 302 to /ageGate OR serve the gate as a 200
        with HTML containing the verify-age prompt. Detect both."""
        try:
            final_path = urlparse(response.url).path or ""
        except Exception:
            final_path = ""
        if "/ageGate" in final_path:
            return True
        try:
            body = response.text or ""
        except Exception:
            body = ""
        # The gate page contains an obvious marker. Mihon doesn't bother
        # with body-text detection (their OkHttp interceptor catches the
        # redirect), but we do because requests-style follow_redirects
        # collapses history and the user might also see 200-served gates.
        return "Verify your age" in body or "ageGate" in body

    def _set_agn2_cookie(self, scraper, title_no: Optional[int]) -> bool:
        """Inject `agn2={title_no}` cookie for adult-flagged title bypass.
        Returns True if injection happened. HakuNeko discovered this
        per-title cookie; gallery-dl/mihon/Zehina don't bother with it
        because most English content passes with the standard cookie set.
        Used only on age-gate retry."""
        if not title_no or title_no <= 0:
            return False
        try:
            scraper.cookies.set("agn2", str(title_no), domain=".webtoons.com")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------- session lifecycle

    def configure_session(self, scraper, args) -> None:
        """Set Referer + UA + age-gate cookies once per scraper.

        `setdefault` (not `[...] =`) is the project convention — multi-source
        state-swap reuses the scraper across alt handlers (aio-dl.py:5874–5943);
        leaving any prior Referer untouched avoids overwriting a value that
        was meaningful for the previous handler.

        Cookies are scoped to `.webtoons.com` so they ride along on both
        `www.` (HTML pages, search) and `m.` (mobile API) requests.
        """
        scraper.headers.setdefault("Referer", _DESKTOP_BASE + "/")
        scraper.headers.setdefault("User-Agent", _DEFAULT_UA)
        scraper.headers.setdefault("Accept-Language", "en-US,en;q=0.9")
        for key, value in _AGE_GATE_COOKIES:
            try:
                scraper.cookies.set(key, value, domain=".webtoons.com")
            except Exception:
                # Some adapter cookie jars reject domain= on a fresh set.
                # Set domain-less; same-host requests will still send them.
                scraper.cookies.set(key, value)

    # ------------------------------------------------------------- comic context

    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        """Parse a webtoons.com series page into SiteComicContext.

        Flow:
          1. Normalize m. → www. so we get the desktop HTML.
          2. Single make_request; if age-gated, set agn2 + retry once.
          3. Selector-based metadata extraction with documented fallbacks
             (each field has 1-3 alternatives; first non-empty wins).
          4. Stash `_title_no`/`_is_canvas`/`_slug` on the comic dict so
             get_chapters can construct the API URL without re-parsing.
        """
        canonical_url = self._normalize_url(url)
        response = make_request(canonical_url, scraper)

        # Age-gate: try once with agn2 cookie. Second gate → fail loudly.
        if self._is_age_gate(response):
            seed_title_no = self._extract_title_no(canonical_url)
            self._set_agn2_cookie(scraper, seed_title_no)
            response = make_request(canonical_url, scraper)
            if self._is_age_gate(response):
                raise IncompleteChapterError(
                    pages_ok=0,
                    pages_total=0,
                    host="webtoons.com",
                    reason="age_gate_blocked",
                )

        soup = self._make_soup(response.text)

        # title_no is the canonical key for the mobile API. Prefer the
        # canonical-link form because the URL the user pasted may contain
        # `&page=N` (paginator) which we want to strip.
        title_no = self._extract_title_no(canonical_url, soup=soup)
        if title_no is None:
            raise IncompleteChapterError(
                pages_ok=0,
                pages_total=0,
                host="webtoons.com",
                reason="title_no_not_found",
            )

        # Title: h1.subj | h3.subj | .subj. Mihon-confirmed.
        title_node = soup.select_one("h1.subj, h3.subj, .subj")
        title = title_node.get_text(strip=True) if title_node else ""
        if not title:
            # Last-ditch: derive from the URL slug.
            parts = [p for p in urlparse(canonical_url).path.split("/") if p]
            slug = parts[-2] if len(parts) >= 2 else f"webtoon-{title_no}"
            title = slug.replace("-", " ").strip().title() or f"Webtoon {title_no}"

        # Author: meta[property="com-linewebtoon:webtoon:author"] is the
        # cleanest source (Zehina-discovered). Fallbacks read DOM ownText.
        author = None
        meta_author = soup.find(
            "meta", attrs={"property": "com-linewebtoon:webtoon:author"}
        )
        if meta_author and meta_author.get("content"):
            author = meta_author["content"].strip() or None
        if not author:
            a_first = soup.select_one(
                ".detail_header .info .author:nth-of-type(1)"
            )
            if a_first is not None:
                # ownText: text directly under tag, excluding child elements.
                texts = [c for c in a_first.children if isinstance(c, str)]
                stripped = "".join(texts).strip()
                if stripped:
                    author = stripped
        if not author:
            a_area = soup.select_one(".detail_header .info .author_area")
            if a_area is not None:
                texts = [c for c in a_area.children if isinstance(c, str)]
                stripped = "".join(texts).strip()
                if stripped:
                    author = stripped

        # Artist: separate slot when the series credits writer + illustrator;
        # most webtoons have a single creator (default to author).
        artist = None
        a_second = soup.select_one(".detail_header .info .author:nth-of-type(2)")
        if a_second is not None:
            texts = [c for c in a_second.children if isinstance(c, str)]
            stripped = "".join(texts).strip()
            if stripped:
                artist = stripped
        artist = artist or author

        # Genre: modern layout uses h2[class*=genre]; legacy uses
        # `.detail_header .info .genre` (multiple, joined). Both surfaces
        # carry single genre strings (e.g. "Fantasy"); webtoons.com doesn't
        # multi-tag like MangaDex.
        genre_label = None
        h2_genre = soup.find("h2", class_=re.compile(r"\bgenre\b"))
        if h2_genre is not None:
            genre_label = h2_genre.get_text(strip=True) or None
        if not genre_label:
            legacy = soup.select(".detail_header .info .genre")
            if legacy:
                genre_label = ", ".join(g.get_text(strip=True) for g in legacy) or None

        # Summary: #_asideDetail p.summary is the dedicated summary block;
        # `.summary` alone catches the older layout.
        summary = None
        s_tag = soup.select_one("#_asideDetail p.summary, .summary")
        if s_tag is not None:
            summary = s_tag.get_text(separator=" ", strip=True) or None

        # Status: derived from .day_info text. "UP EVERY <DAY>" / "EVERY
        # <DAY>" → ongoing; "END" / "COMPLETED" → completed; otherwise
        # unknown. Mihon's mapping at Webtoons.kt:214–220.
        status = "unknown"
        di = soup.select_one("#_asideDetail p.day_info, .day_info")
        if di is not None:
            di_text = di.get_text(strip=True).upper()
            if "END" in di_text or "COMPLETED" in di_text:
                status = "completed"
            elif "UP" in di_text or "EVERY" in di_text:
                status = "ongoing"

        # Cover: og:image is the official metadata channel and gives a clean
        # CDN URL. Don't apply the phinf rewrite — covers come from a
        # different CDN path that may not honor the rewrite.
        cover_url: Optional[str] = None
        og_image = soup.find("meta", attrs={"property": "og:image"})
        if og_image and og_image.get("content"):
            cover_url = og_image["content"].strip() or None
        if not cover_url:
            t_img = soup.select_one(".detail_header .thmb img")
            if t_img is not None:
                cover_url = (t_img.get("src") or "").strip() or None

        is_canvas = self._is_canvas_url(canonical_url)

        # Slug: second-to-last path segment for /en/<genre>/<slug>/list URLs;
        # third-to-last when the URL has a paginator. Used for fingerprinting
        # in case of folder-naming collisions but not load-bearing.
        path_parts = [p for p in urlparse(canonical_url).path.split("/") if p]
        slug = ""
        if "list" in path_parts:
            list_idx = path_parts.index("list")
            if list_idx >= 1:
                slug = path_parts[list_idx - 1]
        elif path_parts:
            slug = path_parts[-1]

        comic = {
            "hid": str(title_no),
            "title": title,
            "desc": summary,
            "cover": cover_url,
            "genres": [genre_label] if genre_label else [],
            "authors": [author] if author else [],
            "status": status,
            "url": canonical_url,
            # Private parser-state fields, consumed by get_chapters. Same
            # pattern as dynasty.py's `_directory`/`_slug`.
            "_title_no": title_no,
            "_is_canvas": is_canvas,
            "_slug": slug,
        }
        # Always populate `artists` when we determined an artist, even when
        # it equals author. Komikku's details.json `artist` field is independent
        # of `author`; leaving it empty for solo creators (the common
        # Webtoons.com case — singNsong, lilprincessember, etc.) made the
        # Komikku Library show no artist at all. `artist = artist or author`
        # above guarantees a non-empty value when author was found.
        if artist:
            comic["artists"] = [artist]

        return SiteComicContext(
            comic=comic,
            title=title,
            identifier=str(title_no),
            soup=soup,
        )

    # ------------------------------------------------------------- chapter list

    def get_chapters(
        self,
        context: SiteComicContext,
        scraper,
        language: str,
        make_request,
    ) -> List[Dict]:
        """Fast path: one mobile-API request returns the full chapter list.

        Endpoint: `m.webtoons.com/api/v1/{webtoon|canvas}/{title_no}/episodes
                   ?pageSize=99999`

        Verified live (2026-05-10): title_no=95 (Tower of God) returns the
        complete catalog with episodeNo, thumbnail, episodeTitle, viewerLink,
        exposureDateMillis, displayUp, hasBgm fields. Order: oldest → newest;
        we preserve it (aio-dl.py:4476 sorts by float(chap) anyway).

        Filtering:
          - exposureDateMillis <= 0  → unpublished/draft canvas episode; skip.
          - episodeNo <= 0           → defensive, never seen in practice.
          - viewerLink missing       → defensive; skip.

        chap=str(episodeNo) is monotonic sparse — matches Mihon. Don't
        attempt to parse "S2 Ep 1" out of the title; that produces colliding
        keys (S1E1 + S2E1 both → chap="1") which aio-dl.py:4527 would treat
        as alternate versions of the same chapter.
        """
        title_no = context.comic.get("_title_no")
        is_canvas = bool(context.comic.get("_is_canvas"))
        if not title_no:
            raise RuntimeError("Webtoons context missing _title_no")

        kind = "canvas" if is_canvas else "webtoon"
        api_url = (
            f"{_MOBILE_BASE}/api/v1/{kind}/{title_no}/episodes?pageSize=99999"
        )
        response = make_request(api_url, scraper)
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Webtoons API returned non-JSON: {exc}") from exc

        # Canvas (/en/canvas/...) is user-uploaded content. webtoons.com hosts
        # the bytes but LINE Webtoon doesn't curate or publish them — so the
        # is_official=True / publisher="LINE Webtoon" annotation we emit for
        # Originals would be a false claim for Canvas. chapter_merger.py:424
        # (`s[1].get("is_official") is True`) sorts official sources first; a
        # Canvas chapter outranking a real fan-translation source via that
        # mechanism is the bug we're fixing. group_name kept distinct ("LINE
        # Webtoon Canvas") so multi-source diagnostics show which webtoons
        # surface each chapter actually came from.
        publisher_label = "LINE Webtoon" if not is_canvas else "LINE Webtoon Canvas"
        episode_list = (data.get("result") or {}).get("episodeList") or []
        chapters: List[Dict] = []
        for ep in episode_list:
            try:
                exp_ms = int(ep.get("exposureDateMillis") or 0)
            except (TypeError, ValueError):
                exp_ms = 0
            if exp_ms <= 0:
                continue
            try:
                episode_no = int(ep.get("episodeNo") or 0)
            except (TypeError, ValueError):
                continue
            if episode_no <= 0:
                continue
            viewer_link = (ep.get("viewerLink") or "").strip()
            if not viewer_link:
                continue
            # Resolve relative or m.-absolute viewerLinks to www. desktop.
            viewer_url = self._normalize_url(
                urljoin(_DESKTOP_BASE + "/", viewer_link)
            )
            episode_title = (ep.get("episodeTitle") or "").strip() or f"Episode {episode_no}"
            thumb = (ep.get("thumbnail") or "").strip() or None
            if thumb and thumb.startswith("/"):
                # Thumbnails come back as paths off the CDN root — webtoons
                # hosts them on swebtoon-phinf.pstatic.net.
                thumb = "https://swebtoon-phinf.pstatic.net" + thumb

            chapters.append(
                {
                    "hid": str(episode_no),
                    "chap": str(episode_no),
                    "title": episode_title,
                    "url": viewer_url,
                    "uploaded": exp_ms // 1000,  # epoch seconds for EPUB
                    # Publisher annotation — Originals are the canonical
                    # LINE source (chapter_merger.py:424 sorts each chapter
                    # row's source list with is_official=True first, so an
                    # Originals series on both webtoons and a fan aggregator
                    # ranks webtoons first regardless of measured img_quality;
                    # same mechanism mangadex.py uses for MangaPlus chapters).
                    # Canvas is user-uploaded — is_official=False, group_name
                    # kept distinct so diagnostics can tell them apart.
                    "is_official": (not is_canvas),
                    "publisher": publisher_label,
                    "group_name": publisher_label,
                    "thumbnail": thumb,
                }
            )
        return chapters

    def get_group_name(self, chapter_version: Dict) -> Optional[str]:
        """Surface our hardcoded `LINE Webtoon` to the orchestrator's
        per-source diagnostics. Mirrors weebcentral.py:246–248."""
        name = chapter_version.get("group_name")
        return name if isinstance(name, str) and name else None

    # ------------------------------------------------------------- chapter images

    def get_chapter_images(
        self,
        chapter: Dict,
        scraper,
        make_request,
    ) -> List[str]:
        """Fetch the desktop viewer page; extract `data-url` from each
        `#_imageList img` (or motion-toon manifest, if applicable).

        Selectors are tried in priority order — first non-empty match wins:
          1. `div#_imageList img[data-url]`        (mihon — most-tested)
          2. `img._images[data-url]`                (gallery-dl, typingbeaver)
          3. `div.viewer_img._img_viewer_area img[data-url]` (Zehina, HakuNeko)

        Quality + host normalization applied via `_normalize_image_url`.
        """
        chapter_url = chapter.get("url")
        if not chapter_url:
            raise IncompleteChapterError(
                pages_ok=0,
                pages_total=0,
                host="webtoons.com",
                reason="missing_chapter_url",
            )
        chapter_url = self._normalize_url(chapter_url)
        response = make_request(chapter_url, scraper)

        # Age-gate handling — agn2 retry, same as fetch_comic_context.
        if self._is_age_gate(response):
            qs = parse_qs(urlparse(chapter_url).query)
            try:
                title_no = int((qs.get("title_no") or qs.get("titleNo") or ["0"])[0])
            except (TypeError, ValueError):
                title_no = 0
            self._set_agn2_cookie(scraper, title_no)
            response = make_request(chapter_url, scraper)
            if self._is_age_gate(response):
                raise IncompleteChapterError(
                    pages_ok=0,
                    pages_total=0,
                    host="webtoons.com",
                    reason="age_gate_blocked",
                )

        soup = self._make_soup(response.text)

        # Motion-toon detection: standard viewer uses #_imageList; motion
        # toons use #ozViewer + a JS-driven canvas pipeline. The data lives
        # in an inline script's `documentURL` + `jpg` template literals.
        if soup.select_one("#ozViewer") is not None:
            return self._extract_motion_toon_pages(
                response.text, scraper, make_request
            )

        image_urls: List[str] = []
        for selector in (
            "div#_imageList img[data-url]",
            "img._images[data-url]",
            "div.viewer_img._img_viewer_area img[data-url]",
        ):
            candidates = soup.select(selector)
            if not candidates:
                continue
            for img in candidates:
                data_url = (img.get("data-url") or "").strip()
                if data_url:
                    image_urls.append(self._normalize_image_url(data_url))
            if image_urls:
                break

        if not image_urls:
            # Could be a removed chapter, network blip, or a viewer variant
            # we haven't seen. Surface as IncompleteChapterError so the
            # multi-source fallback machinery can try alternative sources.
            raise IncompleteChapterError(
                pages_ok=0,
                pages_total=0,
                host=urlparse(chapter_url).netloc or "webtoons.com",
                reason="no_images_found",
            )
        return image_urls

    def _extract_motion_toon_pages(
        self,
        html: str,
        scraper,
        make_request,
    ) -> List[str]:
        """Mihon-style motion-toon support: each layer becomes a separate
        page in the URL list.

        The viewer page embeds inline JS:
            documentURL: '<full URL to JSON manifest>'
            jpg: '<URL template prefix>{=filename}'
        The manifest's `assets.images` map keys filenames; keys containing
        `layer` are the rendered panel layers (other keys are masks/sounds).

        Trade-off: animation is lost (we don't run the canvas timeline);
        static layer images are intact and lossless. PIL alpha-composite is
        deferred to v2 — the layer-as-page approach is what Mihon ships in
        production.
        """
        doc_match = _MOTIONTOON_DOC_RE.search(html or "")
        path_match = _MOTIONTOON_PATH_RE.search(html or "")
        if not doc_match or not path_match:
            raise IncompleteChapterError(
                pages_ok=0,
                pages_total=0,
                host="webtoons.com",
                reason="motion_toon_manifest_not_found",
            )
        manifest_url = doc_match.group(1)
        template_path = path_match.group(1)

        try:
            response = make_request(manifest_url, scraper)
            manifest = response.json()
        except Exception as exc:
            raise IncompleteChapterError(
                pages_ok=0,
                pages_total=0,
                host=urlparse(manifest_url).netloc or "webtoons.com",
                reason=f"motion_toon_manifest_fetch_failed:{type(exc).__name__}",
            ) from exc

        images = (manifest.get("assets") or {}).get("images") or {}
        if not isinstance(images, dict):
            raise IncompleteChapterError(
                pages_ok=0,
                pages_total=0,
                host="webtoons.com",
                reason="motion_toon_manifest_malformed",
            )
        # Stable order: sort layer keys so successive runs produce identical
        # page sequences. Webtoons motion-toon manifests use lexicographic
        # layer keys (e.g. layer_001, layer_002) — sorted() on the keys
        # gives the natural rendering order.
        page_urls: List[str] = []
        for key in sorted(images.keys()):
            if "layer" not in key.lower():
                continue
            filename = images[key]
            if not filename or not isinstance(filename, str):
                continue
            page_urls.append(template_path + filename)

        if not page_urls:
            raise IncompleteChapterError(
                pages_ok=0,
                pages_total=0,
                host="webtoons.com",
                reason="motion_toon_no_layers",
            )
        # No phinf rewrite / quality strip — motion-toon URLs use a different
        # CDN template that may not honor those query knobs.
        return page_urls

    # ------------------------------------------------------------- search

    def search(
        self,
        query: str,
        scraper,
        make_request,
        *,
        language: str = "en",
        limit: int = 20,
    ) -> List[SearchHit]:
        """HTML scrape of `/en/search?keyword=…`. webtoons.com has no public
        JSON search endpoint — none of the open-source scrapers (gallery-dl,
        mihon, Zehina, manga-py, typingbeaver, HakuNeko) use one.

        Returns combined originals + canvas hits. Selector `.webtoon_list li a`
        is mihon-confirmed. Each anchor contains `.title` (display name) and
        an `<img>` (cover thumbnail). The path tells us originals vs canvas
        (`/en/canvas/<slug>/list...` vs `/en/<genre>/<slug>/list...`).

        chapter_count_hint stays None — search HTML doesn't carry counts;
        the orchestrator's image-quality probe (which is gated by seed=0.92
        > CHAPTER_PROBE_MIN_SEED=0.65) will fetch live chapters anyway.
        """
        clean = (query or "").strip()
        if not clean:
            return []

        url = f"{_DESKTOP_BASE}/en/search?keyword={quote_plus(clean)}"
        response = make_request(url, scraper)
        html = response.text or ""
        if len(html) < 200:
            return []

        soup = self._make_soup(html)
        anchors = soup.select(".webtoon_list li a[href]")
        hits: List[SearchHit] = []
        seen: set = set()

        for idx, a in enumerate(anchors):
            if len(hits) >= limit:
                break
            href = (a.get("href") or "").strip()
            if not href or "/ageGate" in href:
                continue
            abs_url = urljoin(_DESKTOP_BASE, href).split("#")[0]
            parsed = urlparse(abs_url)
            qs = parse_qs(parsed.query)
            title_no_str = (qs.get("title_no") or qs.get("titleNo") or [None])[0]
            if not title_no_str or not title_no_str.isdigit():
                continue
            # Canonical form: drop ?page= and any other query so identical
            # series with different paginator pos dedupe.
            canonical_url = (
                f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                f"?title_no={title_no_str}"
            )
            if canonical_url in seen:
                continue
            seen.add(canonical_url)

            # Title: `.title` is mihon-confirmed for the desktop search
            # results layout. Fallback to derive from URL slug.
            title = None
            title_node = a.select_one(".title")
            if title_node is not None:
                title = title_node.get_text(strip=True) or None
            if not title:
                parts = [p for p in parsed.path.split("/") if p]
                if "list" in parts:
                    list_idx = parts.index("list")
                    if list_idx >= 1:
                        title = parts[list_idx - 1].replace("-", " ").title()
            if not title:
                continue

            cover: Optional[str] = None
            img = a.select_one("img[src], img[data-src]")
            if img is not None:
                src = (img.get("src") or img.get("data-src") or "").strip()
                if src:
                    if src.startswith("//"):
                        src = "https:" + src
                    elif src.startswith("/"):
                        src = urljoin(_DESKTOP_BASE, src)
                    cover = src
                    # Don't apply the phinf rewrite — covers use a different
                    # CDN path than chapter images.

            # Slug as alt_title — helps cross-site dedupe match against
            # romaji/EN slugs surfaced by other handlers (mangadex's
            # alt-title list, mangafire's typeahead, etc.).
            alt_titles: List[str] = []
            parts = [p for p in parsed.path.split("/") if p]
            if "list" in parts:
                list_idx = parts.index("list")
                if list_idx >= 1:
                    slug = parts[list_idx - 1]
                    slug_form = slug.replace("-", " ").strip()
                    if slug_form and slug_form.lower() != title.lower():
                        alt_titles.append(slug_form)

            raw_score = max(0.05, 1.0 - (idx / max(1, len(anchors))))
            # Per-hit is_official: Originals (/en/<genre>/.../list) are LINE-
            # curated and inherit handler-level OFFICIAL_PUBLISHER=True. Canvas
            # (/en/canvas/.../list) is user-uploaded — webtoons.com hosts but
            # doesn't publish — so canvas hits MUST NOT claim official-
            # publisher status. search_orchestrator.py ANDs this per-hit
            # value with the site-level flag when populating
            # SourceEntry.is_official. Fixes the failure mode where a Canvas
            # series literally titled "One Piece" (or any famous title)
            # union-find-merged with the real series and won the
            # within-candidate tiebreaker via is_official=True.
            hit_is_official = not self._is_canvas_url(canonical_url)
            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=canonical_url,
                    cover=cover,
                    alt_titles=alt_titles,
                    year=None,
                    language="en",
                    chapter_count_hint=None,
                    raw_score=raw_score,
                    is_official=hit_is_official,
                )
            )

        # Populate actual_chapter_count for each hit via the mobile API so
        # the orchestrator's cross-site count-divergence check (search_
        # orchestrator.py around line 5213) can sink a wrong-match Canvas
        # hit whose normalized title collides with a real series. Without
        # this signal a 20-episode Canvas "One Piece" stays clustered with
        # MangaDex's 1100-chapter One Piece purely on title-normalize, and
        # the per-hit is_official=False alone doesn't sink it below all
        # the real sources — _cmp would still let it stay where its
        # title_match places it. Probing the API costs ~1.6s per hit so we
        # parallelize + cap the total budget. See _populate_chapter_counts
        # docstring for the bandwidth/timeout tradeoff.
        if hits:
            self._populate_chapter_counts(hits, scraper, make_request)
        return hits

    def _populate_chapter_counts(
        self,
        hits: List[SearchHit],
        scraper,
        make_request,
        *,
        max_workers: int = 5,
        total_budget_s: float = 4.0,
    ) -> None:
        """Parallel-fetch episode counts via the mobile API; populate each
        hit's actual_chapter_count in place.

        Why: the orchestrator's cross-site chapter-count divergence check
        (sites/search_orchestrator.py around line 5213) compares each
        source's actual_chapter_count against the max chapter_count_hint
        across all sources in the same candidate. When a Canvas series with
        ~20 episodes is union-find-merged with a real 1100-chapter series
        (because their normalized titles collide), this signal lets the
        orchestrator demote the Canvas source — labelled count_outlier
        (NOT dmca_likely, to avoid the UI's DMCA flag being raised for
        wrong-match cases) — so it sinks to the back of the candidate's
        source list.

        Budget tradeoff: each API call is ~1.6s + ~60-110 KB. At
        max_workers=5 and total_budget_s=4.0, up to ~12 hits complete in
        the budget; any that don't keep actual_chapter_count=None and the
        cross-site check silently skips them (the source then stays
        ranked by title_match alone, which is the pre-fix behavior).

        Same endpoint get_chapters uses (m.webtoons.com/api/v1/
        {webtoon|canvas}/{title_no}/episodes?pageSize=99999). No count
        field is exposed by the API (only episodeList + nextCursor),
        verified 2026-05-24 — pulling the full list is the only path.
        """
        import concurrent.futures as _cf
        import time as _t
        deadline = _t.monotonic() + total_budget_s

        def _count_one(hit: SearchHit) -> None:
            try:
                parsed = urlparse(hit.url)
                qs = parse_qs(parsed.query)
                tn = (qs.get("title_no") or qs.get("titleNo") or [None])[0]
                if not tn or not str(tn).isdigit():
                    return
                kind = "canvas" if self._is_canvas_url(hit.url) else "webtoon"
                api = (
                    f"{_MOBILE_BASE}/api/v1/{kind}/{tn}/episodes"
                    f"?pageSize=99999"
                )
                response = make_request(api, scraper)
                episodes = (
                    (response.json().get("result") or {}).get("episodeList")
                    or []
                )
                # Mirror get_chapters' filter: exposureDateMillis > 0 drops
                # unpublished drafts (rare on Originals, common on Canvas
                # series the author left mid-edit).
                hit.actual_chapter_count = sum(
                    1
                    for ep in episodes
                    if (ep.get("exposureDateMillis") or 0) > 0
                )
            except Exception:
                # Network/parse failure: leave actual_chapter_count=None.
                # The cross-site check skips None values, so this is the
                # safe-degrade outcome (source ranked by title_match alone).
                pass

        with _cf.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="webtoons-count"
        ) as pool:
            futures = [pool.submit(_count_one, h) for h in hits]
            for fut in _cf.as_completed(futures):
                remaining = deadline - _t.monotonic()
                if remaining <= 0:
                    break
                try:
                    fut.result(timeout=max(0.1, remaining))
                except Exception:
                    pass


__all__ = ["LineWebtoonSiteHandler"]
