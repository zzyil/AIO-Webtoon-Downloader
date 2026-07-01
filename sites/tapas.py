"""Tapas (tapas.io) handler — English webcomics.

What this module owns:
  - SiteComicContext + chapter list for tapas.io series. The chapter list
    comes from the site's own JSON episodes endpoint
    (`tapas.io/series/{series_id}/episodes?page=N&sort=OLDEST&max_limit=M`),
    which returns a structured `data.episodes` array (id, title, `scene`
    sequence number, free/must_pay/unlocked flags, publish_date, has_bgm,
    bgm_url). We use that array directly — NOT the redundant `data.body` HTML
    fragment in the same payload. Verified live 2026-07-01 against a real
    long-running series (285 episodes, paginated has_next).
  - Image URL extraction from the episode viewer (`.content__img[data-src]`).
    The `data-src` is a CDN URL carrying a short-lived signed `__token__`
    query (expires ~1h) — preserved verbatim; aio-dl.py:dl_image downloads it
    promptly and sniffs the real extension from magic bytes, so animated GIF
    panels land as `.gif` and survive the CBZ byte-passthrough fast-path.
  - Auxiliary-asset capture (faithful-archival feature — see
    ~/.claude/plans/i-want-to-add-rustling-penguin.md): Tapas episodes can
    carry a background music track (`bgm_url` in the episodes API — a REAL
    downloadable URL, unlike webtoons which only exposes a flag) and/or a
    SoundCloud embed (`iframe[src*=soundcloud]`). The handler stashes these as
    sites.base.AssetSpec on the chapter dict (`chapter["_aux_assets"]`); the
    packaging loop embeds them INSIDE the chapter CBZ under the reserved
    `_aio/` prefix (grep _materialize_chapter_aux in aio-dl.py). SoundCloud
    stays a reference URL only (locked decision); bgm_url is downloaded.
  - Cross-site search via `tapas.io/search?q=…&t=COMICS` (HTML scrape).

What reads from it:
  - sites/__init__.py — registers a singleton in `_BASE_HANDLERS`; the URL
    dispatcher routes any tapas.io paste here (domains substring match).
  - aio-dl.py main flow — fetch_comic_context / get_chapters /
    get_chapter_images via the handler interface. get_chapter_images returns
    List[str] of image URLs; aux never enters that return (it rides the
    chapter dict). Cross-file: _materialize_chapter_aux reads
    chapter["_aux_assets"] and embeds them into the chapter CBZ under _aio/.

Reference model: sites/dynasty.py (clean JSON-API handler; _slug/_series_id
stash pattern) + sites/linewebtoon.py (episode-number-as-chapter, aux-on-dict).

Known v1 limitations:
  - No Tapas login: mature/age-gated and premium ("ink"-locked) episodes are
    skipped. A future --cookies FILE -> MozillaCookieJar would unlock them.
  - Novel (text) episodes (`book: true` in the API) are skipped — this handler
    is comic-only; the image gate would raise on them anyway.
"""

from __future__ import annotations

import datetime as dt
import html
import os
import re
from typing import Dict, List, Optional
from urllib.parse import quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup, FeatureNotFound

from .base import (
    AssetSpec,
    BaseSiteHandler,
    IncompleteChapterError,
    SearchHit,
    SiteComicContext,
)


_BASE_URL = "https://tapas.io"

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# Series id lives in the info page as a Kakao "tiara" tracking attribute
# (data-tiara-page-meta-type="series_id" -> data-tiara-page-meta-id). The
# episodes JSON endpoint requires the NUMERIC id — the slug returns HTTP 400
# (verified 2026-07-01). Regex fallback scrapes the first `/series/<digits>/`
# occurrence, which on the info page is the canonical self-reference.
_SERIES_ID_RE = re.compile(r"/series/(\d+)(?:[/?\"']|$)")

# Cap pagination so a malformed has_next can't loop forever. 3000 pages *
# max_limit(20) = 60k episodes — orders of magnitude past any real series.
_MAX_EPISODE_PAGES = 3000
# max_limit is capped at 20 server-side: any value >=21 returns HTTP 500
# (verified 2026-07-01 — 10/20 -> 200, 21..100 -> 500). So the plan's
# "max_limit=99999999" would have failed; we page at the hard ceiling.
_EPISODE_PAGE_SIZE = 20


class TapasSiteHandler(BaseSiteHandler):
    name = "tapas"
    domains = ("tapas.io",)

    def __init__(self) -> None:
        super().__init__()
        try:
            import lxml  # type: ignore  # noqa: F401
            self._parser = "lxml"
        except Exception:
            self._parser = "html.parser"

    # ----------------------------------------------------------------- helpers
    def _make_soup(self, html: str) -> BeautifulSoup:
        try:
            return BeautifulSoup(html or "", self._parser)
        except FeatureNotFound:
            return BeautifulSoup(html or "", "html.parser")

    @staticmethod
    def _slug_from_url(url: str) -> Optional[str]:
        """Extract the series slug from a /series/<slug>[/info] URL. Returns
        None for /episode/<id> URLs (handled separately)."""
        parts = [p for p in urlparse(url).path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "series":
            return parts[1]
        return None

    def configure_session(self, scraper, args) -> None:
        # setdefault (not [...] =) — multi-source state-swap reuses the scraper
        # across handlers; don't clobber a prior handler's Referer. X-Requested-
        # With is safe on every request here: the episodes endpoint accepts it,
        # and the HTML info/episode pages still return full markup with it set
        # (verified 2026-07-01), so we set it globally rather than per-call
        # (make_request can't pass per-request headers).
        scraper.headers.setdefault("User-Agent", _DEFAULT_UA)
        scraper.headers.setdefault("Referer", _BASE_URL + "/")
        scraper.headers.setdefault("Accept-Language", "en-US,en;q=0.9")
        scraper.headers.setdefault("X-Requested-With", "XMLHttpRequest")

    # ----------------------------------------------------------- comic context
    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        slug = self._slug_from_url(url)
        if not slug:
            # /episode/<id> or other — try to resolve to the parent series by
            # scraping a /series/<slug> link off the page.
            resolved = self._resolve_series_url(url, scraper, make_request)
            if not resolved:
                raise RuntimeError(
                    "Tapas: paste a series URL like "
                    "https://tapas.io/series/<slug>/info"
                )
            slug = resolved

        info_url = f"{_BASE_URL}/series/{slug}/info"
        response = make_request(info_url, scraper)
        soup = self._make_soup(response.text)

        series_id = self._extract_series_id(soup, response.text)
        if not series_id:
            raise RuntimeError(f"Tapas: could not find series id for '{slug}'.")

        title = self._extract_title(soup)
        if not title:
            title = slug.replace("-", " ").strip().title() or f"Tapas {series_id}"

        authors = self._extract_creators(soup)
        description = self._extract_text(
            soup, [".description", ".row-frame__body", ".js-description"]
        )
        genres = self._extract_genres(soup)
        status = self._extract_status(soup)

        cover_url: Optional[str] = None
        og_image = soup.find("meta", attrs={"property": "og:image"})
        if og_image and og_image.get("content"):
            cover_url = og_image["content"].strip() or None

        comic: Dict = {
            "hid": str(series_id),
            "title": title,
            "desc": description,
            "cover": cover_url,
            "genres": genres,
            "authors": authors,
            "status": status,
            "url": info_url,
            # Private parser-state (dynasty.py `_slug` pattern).
            "_series_id": series_id,
            "_slug": slug,
        }
        return SiteComicContext(
            comic=comic, title=title, identifier=str(series_id), soup=soup
        )

    def _resolve_series_url(self, url: str, scraper, make_request) -> Optional[str]:
        """Best-effort: given an /episode/<id> URL, fetch it and pull the
        parent series slug from a `/series/<slug>` link. Returns the slug."""
        try:
            response = make_request(url, scraper)
        except Exception:
            return None
        soup = self._make_soup(response.text)
        for a in soup.select("a[href*='/series/']"):
            href = (a.get("href") or "").strip()
            slug = self._slug_from_url(urljoin(_BASE_URL, href))
            if slug and slug != "info":
                return slug
        return None

    @staticmethod
    def _extract_series_id(soup: BeautifulSoup, html: str) -> Optional[int]:
        node = soup.select_one('[data-tiara-page-meta-type="series_id"]')
        if node is not None:
            raw = (node.get("data-tiara-page-meta-id") or "").strip()
            if raw.isdigit():
                return int(raw)
        m = _SERIES_ID_RE.search(html or "")
        return int(m.group(1)) if m else None

    def _extract_title(self, soup: BeautifulSoup) -> Optional[str]:
        for sel in (
            "h1.title", ".center-info__title", ".series-header__title",
            ".title",
        ):
            node = soup.select_one(sel)
            if node is not None:
                txt = node.get_text(strip=True)
                if txt:
                    return txt
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            # og:title is "Read <Title> | Tapas Web Comics" — strip the chrome.
            t = og["content"].strip()
            t = re.sub(r"^Read\s+", "", t)
            t = re.sub(r"\s*\|\s*Tapas.*$", "", t)
            return t or None
        return None

    @staticmethod
    def _extract_creators(soup: BeautifulSoup) -> List[str]:
        """Creators from the `.creator` block. Tapas markup is
        `<a>Name</a><span>&comma;</span><a>Name</a>…` where the separator is a
        literal `&comma;` HTML entity that lxml leaves un-decoded — so we read
        the per-creator anchors directly, falling back to splitting the joined
        text (after html.unescape turns `&comma;` back into `,`). Tapas doesn't
        expose a writer/artist role split, so all creators become authors (the
        ComicInfo <Writer> field), matching other role-less handlers."""
        node = soup.select_one(".creator")
        if node is None:
            return []

        seen: set = set()
        out: List[str] = []

        def _add(name: str) -> None:
            n = html.unescape(name or "").strip().strip(",").strip()
            if n and n.lower() not in seen:
                seen.add(n.lower())
                out.append(n)

        anchors = node.select("a")
        if anchors:
            for a in anchors:
                _add(a.get_text(" ", strip=True))
        if not out:
            raw = html.unescape(node.get_text(" ", strip=True))
            for part in re.split(r"\s*,\s*", raw):
                _add(part)
        return out

    @staticmethod
    def _extract_text(soup: BeautifulSoup, selectors: List[str]) -> Optional[str]:
        for sel in selectors:
            node = soup.select_one(sel)
            if node is not None:
                txt = node.get_text(" ", strip=True)
                if txt:
                    return txt
        return None

    # "Comic"/"Novel" are the content TYPE, not a genre; Tapas surfaces them in
    # the same tag strip so we filter them out of the genres list.
    _NON_GENRE = frozenset({"comic", "comics", "novel", "novels", "book", "books"})

    @classmethod
    def _extract_genres(cls, soup: BeautifulSoup) -> List[str]:
        genres: List[str] = []
        seen: set = set()
        for a in soup.select(
            ".info-detail__row a[href*='genre'], a.genre, .genre-tag, "
            "a[href*='/comics?browse'], a[href*='category=']"
        ):
            g = a.get_text(strip=True)
            if g and g.lower() not in seen and g.lower() not in cls._NON_GENRE:
                seen.add(g.lower())
                genres.append(g)
        return genres

    @staticmethod
    def _extract_status(soup: BeautifulSoup) -> str:
        for sel in (".completed", ".ongoing", ".info-detail__stats", ".stat"):
            node = soup.select_one(sel)
            if node is not None:
                text = node.get_text(" ", strip=True).upper()
                if "COMPLETED" in text or "END" in text:
                    return "completed"
                if "ONGOING" in text or "UP" in text or "HIATUS" in text:
                    return "ongoing" if "HIATUS" not in text else "hiatus"
        return "unknown"

    # ------------------------------------------------------------- chapter list
    def get_chapters(
        self,
        context: SiteComicContext,
        scraper,
        language: str,
        make_request,
    ) -> List[Dict]:
        series_id = context.comic.get("_series_id")
        if not series_id:
            raise RuntimeError("Tapas context missing _series_id.")

        chapters: List[Dict] = []
        skipped: Dict[str, int] = {}
        page = 1
        while page <= _MAX_EPISODE_PAGES:
            api = (
                f"{_BASE_URL}/series/{series_id}/episodes"
                f"?page={page}&sort=OLDEST&max_limit={_EPISODE_PAGE_SIZE}"
            )
            response = make_request(api, scraper)
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"Tapas episodes API returned non-JSON: {exc}"
                ) from exc
            data = payload.get("data") or {}
            episodes = data.get("episodes") or []
            pagination = data.get("pagination") or {}

            for ep in episodes:
                reason = self._skip_reason(ep)
                if reason:
                    skipped[reason] = skipped.get(reason, 0) + 1
                    continue
                chap = self._episode_to_chapter(ep)
                if chap is not None:
                    chapters.append(chap)

            if not bool(pagination.get("has_next")):
                break
            page += 1

        # Surface why episodes were dropped — the "locked" bucket is the one
        # users notice (a popular series shows far fewer chapters than its
        # episode count) and is actionable (a Tapas login unlocks them).
        if skipped:
            summary = ", ".join(f"{v} {k}" for k, v in sorted(skipped.items()))
            note = f"  [tapas] skipped {sum(skipped.values())} episode(s): {summary}"
            if skipped.get("locked"):
                note += (
                    " — 'locked' are premium/wait-to-unlock and need a Tapas "
                    "login (no --cookies support yet)"
                )
            print(note)

        return chapters

    @staticmethod
    def _skip_reason(ep: Dict) -> Optional[str]:
        """Classify an episode for skipping, or None to keep. Split from the
        mapper so get_chapters can COUNT reasons for a user-facing summary.

        - "scheduled": not yet published.
        - "novel":     `book` text episode — this handler is comic-only.
        - "locked":    not free AND not unlocked AND not free_access. Tapas's
                       WAIT_OR_MUST_PAY model marks these free=false/
                       must_pay=false, but logged-out they render a paywall with
                       zero .content__img (verified 2026-07-01 on ep 1477227),
                       so we can't fetch them without a login.
        - "invalid":   missing/zero id."""
        try:
            ep_id = int(ep.get("id") or 0)
        except (TypeError, ValueError):
            return "invalid"
        if ep_id <= 0:
            return "invalid"
        if ep.get("scheduled"):
            return "scheduled"
        if ep.get("book"):
            return "novel"
        accessible = (
            bool(ep.get("free"))
            or bool(ep.get("unlocked"))
            or bool(ep.get("free_access"))
        )
        if not accessible:
            return "locked"
        return None

    @staticmethod
    def _episode_to_chapter(ep: Dict) -> Optional[Dict]:
        """Map one accessible episodes-API entry to a chapter dict. Gating is
        done by _skip_reason (called first in get_chapters); this only maps.
        Chapter number is `scene` (the site's monotonic per-episode sequence,
        which counts Extras too) — NOT parsed from the freeform title ("1.",
        "Extra", "Side Story"), which would collide (linewebtoon.py documents
        the same pitfall)."""
        try:
            ep_id = int(ep.get("id") or 0)
        except (TypeError, ValueError):
            return None
        if ep_id <= 0:
            return None

        try:
            scene = int(ep.get("scene") or 0)
        except (TypeError, ValueError):
            scene = 0
        chap = str(scene) if scene > 0 else str(ep_id)

        title = (ep.get("title") or ep.get("escape_title") or "").strip()
        uploaded = TapasSiteHandler._parse_iso_epoch(ep.get("publish_date"))

        bgm_url = (ep.get("bgm_url") or "").strip()
        has_bgm = bool(ep.get("has_bgm"))

        return {
            "hid": str(ep_id),
            "chap": chap,
            "title": title or f"Episode {scene or ep_id}",
            "url": f"{_BASE_URL}/episode/{ep_id}",
            "uploaded": uploaded,
            "group_name": "Tapas",
            # Aux hints consumed by get_chapter_images (below) to build
            # AssetSpecs. bgm_url is a REAL downloadable URL when present.
            "_bgm_url": bgm_url,
            "_has_bgm": has_bgm,
            "_bgm_title": (ep.get("bgm_title") or "").strip(),
        }

    @staticmethod
    def _parse_iso_epoch(value: Optional[str]) -> int:
        if not value:
            return 0
        try:
            parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            return int(
                parsed.replace(tzinfo=dt.timezone.utc).timestamp()
            )
        except (TypeError, ValueError):
            return 0

    def get_group_name(self, chapter_version: Dict) -> Optional[str]:
        name = chapter_version.get("group_name")
        return name if isinstance(name, str) and name else None

    # ------------------------------------------------------------- chapter images
    def get_chapter_images(
        self,
        chapter: Dict,
        scraper,
        make_request,
    ) -> List[str]:
        chapter_url = chapter.get("url")
        if not chapter_url:
            raise IncompleteChapterError(
                pages_ok=0, pages_total=0, host="tapas.io",
                reason="missing_chapter_url",
            )
        response = make_request(chapter_url, scraper)
        soup = self._make_soup(response.text)

        # Comic pages carry .content__img with the real URL in data-src (the
        # src attribute is a 1x1 placeholder data-URI until JS lazy-loads).
        img_nodes = soup.select(".content__img")
        image_urls: List[str] = []
        for img in img_nodes:
            data_src = (img.get("data-src") or "").strip()
            if not data_src:
                # Fall back to a real src (non-data-URI) if present.
                src = (img.get("src") or "").strip()
                if src and not src.startswith("data:"):
                    data_src = src
            if data_src:
                image_urls.append(urljoin(_BASE_URL, data_src))

        # Capture auxiliary assets (bgm audio + SoundCloud references) onto the
        # chapter dict for the packaging loop. Done regardless of image outcome
        # so a paid/gated episode we can still see the embed on records it.
        self._stash_aux_assets(chapter, soup)

        if not image_urls:
            # Distinguish "mature/login gate" from a genuinely empty/removed
            # episode so the reason string is actionable. A logged-out mature
            # episode renders a warning interstitial instead of the panels.
            body_text = (response.text or "").lower()
            if (
                "mature content" in body_text
                or "log in to read" in body_text
                or "age verification" in body_text
            ):
                raise IncompleteChapterError(
                    pages_ok=0, pages_total=0, host="tapas.io",
                    reason="mature_login_required",
                )
            raise IncompleteChapterError(
                pages_ok=0, pages_total=0,
                host=urlparse(chapter_url).netloc or "tapas.io",
                reason="no_images_found",
            )
        return image_urls

    @staticmethod
    def _stash_aux_assets(chapter: Dict, soup: BeautifulSoup) -> None:
        specs: List[AssetSpec] = []

        # Background music: Tapas exposes a real downloadable bgm_url in the
        # episodes API (stashed on the chapter dict by _episode_to_chapter).
        bgm_url = chapter.get("_bgm_url")
        if bgm_url:
            name = os.path.basename(urlparse(bgm_url).path) or "bgm.mp3"
            specs.append(
                AssetSpec(
                    type="audio_download",
                    source_url=bgm_url,
                    filename=name,
                    mime="audio/mpeg",
                    meta={
                        "kind": "bgm",
                        "title": chapter.get("_bgm_title") or "",
                    },
                )
            )
        elif chapter.get("_has_bgm"):
            # Flag set but no URL — record presence for the reader.
            specs.append(
                AssetSpec(
                    type="audio_reference",
                    source_url=chapter.get("url"),
                    meta={"provider": "tapas_bgm", "has_bgm": True},
                )
            )

        # SoundCloud embeds — reference URL only (locked decision, never
        # downloaded). The widget iframe src carries the track/playlist URL.
        for iframe in soup.select("iframe[src*='soundcloud']"):
            src = (iframe.get("src") or "").strip()
            if src:
                specs.append(
                    AssetSpec(
                        type="audio_reference",
                        source_url=src,
                        meta={"provider": "soundcloud"},
                    )
                )

        if specs:
            chapter["_aux_assets"] = specs

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
        clean = (query or "").strip()
        if not clean:
            return []
        url = f"{_BASE_URL}/search?q={quote_plus(clean)}&t=COMICS"
        try:
            response = make_request(url, scraper)
        except Exception:
            return []
        soup = self._make_soup(response.text)

        hits: List[SearchHit] = []
        seen: set = set()
        for a in soup.select("a[data-series-id][href*='/series/']"):
            if len(hits) >= limit:
                break
            sid = (a.get("data-series-id") or "").strip()
            href = (a.get("href") or "").strip()
            slug = self._slug_from_url(urljoin(_BASE_URL, href))
            if not slug or slug in seen:
                continue
            seen.add(slug)

            title = (a.get("data-series-title") or "").strip()
            if not title:
                # Title lives elsewhere in the result card; scan the anchor +
                # its parent for a .title/.name, else derive from the slug.
                card = a.find_parent() or a
                node = card.select_one(".title, .name, .series__title")
                if node is not None:
                    title = node.get_text(strip=True)
            if not title:
                title = slug.replace("-", " ").strip().title()
            if not title:
                continue

            cover: Optional[str] = None
            img = a.select_one("img[src], img[data-src]")
            if img is not None:
                src = (img.get("data-src") or img.get("src") or "").strip()
                if src and not src.startswith("data:"):
                    cover = urljoin(_BASE_URL, src)

            raw_score = max(0.05, 1.0 - (len(hits) / max(1, limit)))
            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=f"{_BASE_URL}/series/{slug}",
                    cover=cover,
                    alt_titles=[],
                    year=None,
                    language="en",
                    chapter_count_hint=None,
                    raw_score=raw_score,
                    is_official=False,
                )
            )
        return hits


__all__ = ["TapasSiteHandler"]
