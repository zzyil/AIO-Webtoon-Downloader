"""AniList GraphQL metadata enrichment for AIO-Webtoon-Downloader.

This module owns the external-metadata enrichment path used when the user
passes --metadata-source anilist. It queries https://graphql.anilist.co
(free, anonymous-readable, 90 req/min, no auth) to fetch normalized tags,
descriptions, country of origin, media format, and cross-reference IDs
(AniList + MAL), then merges those into the per-series `comic_data` dict
that aio-dl.py passes around.

What reads from this module:
  - aio-dl.py:main() — calls enrich_from_anilist right after
    allocate_series_output_dir; threads results through the ComicInfo.xml
    builders, the Komikku details.json writer, and the .aio_series.json
    writer.
  - aio_search_cli.py registers the same CLI flags for parity but does
    NOT call this module in v1 (the search CLI doesn't end-to-end fetch
    comic context yet; reserved for the next search refactor).

What this module depends on:
  - requests (already a project dep; aio-dl.py:454, requirements.txt)
  - rapidfuzz (already a project dep; sites/search_orchestrator.py uses
    fuzz.WRatio the same way, requirements.txt:15) — imported lazily
    inside _score_candidate so a packager who strips rapidfuzz only
    breaks --metadata-source enrichment, not the rest of the project.
  - Standard library only otherwise (html, re, time, dataclasses, typing).

Network resilience notes:
  - This module does NOT route through aio-dl.py's make_request /
    scraper / cloudscraper / cooldown plumbing. AniList isn't a comic
    source — it's a metadata API with documented rate limits — so the
    per-handler hardening is overkill. We do our own 429 + 5xx retry
    with the published Retry-After header.
  - All public functions are best-effort. Network failures, malformed
    responses, and no-match-found are signalled by returning the
    comic_data dict unchanged (no `anilist_id` key set). Callers MUST
    handle that path; this module never raises into the caller for a
    network-level problem. The single exception is ImportError on
    rapidfuzz (project-wide hard dep) which propagates so the user
    knows what to install.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests


# --- Constants -------------------------------------------------------------

ANILIST_GRAPHQL_URL = "https://graphql.anilist.co"

# rapidfuzz WRatio (0..100) threshold for accepting an AniList match.
# 75 was chosen empirically: at 60+ rapidfuzz starts admitting same-genre-
# different-series matches (e.g. "Solo Leveling" vs "Solo Login" scores
# ~64 on live data); at 75+ matches are reliably the right series under
# any reasonable title variant including translit drift between sites.
# Not exposed as a CLI flag — when users want more matches they should
# fix their site's title field, not lower this floor.
ANILIST_TITLE_MATCH_THRESHOLD = 75.0

# Spacing between retries when AniList didn't tell us how long to wait.
# Budget at 90 req/min = 1 every 0.67s; 0.7s leaves ~5% margin so we
# never get caught by the burst limiter. Only applies inside the retry
# loop; one-shot calls run immediately.
ANILIST_RATE_LIMIT_SLEEP_S = 0.7

ANILIST_TIMEOUT_S = 15

ANILIST_MAX_RETRIES = 3

# Drop candidates with these formats from the search-result pool. NOVEL
# poisons comic enrichment (light novels share titles with their manga
# adaptations and AniList lists them as separate Media entries). ONE_SHOT
# is kept — some downloads are legitimate one-shots and rapidfuzz scoring
# usually picks the serialized entry first anyway.
_EXCLUDED_FORMATS = frozenset({"NOVEL"})


# --- Public dataclass ------------------------------------------------------

@dataclass
class AnilistTag:
    """One AniList Media tag, normalized.

    `rank` is the 0..100 relevance score AniList computes; we filter on it
    in _split_tags. `is_*_spoiler` are the two flavours of spoiler flag:
    media-specific (e.g. "Tragedy" spoils *this* series) vs general (e.g.
    "Time Travel" is broadly spoilerish for any story). Either flag puts
    the tag into the spoiler bucket — the user's reader can decide
    granularity at display time using the per-tag attributes preserved
    in the ComicInfo.xml <TagsExtended> block and .aio_series.json.

    Cross-file: serialized to dict in aio-dl.py's .aio_series.json writer
    and to XML in aio-dl.py:_emit_tags_extended.
    """
    name: str
    category: str
    rank: int
    is_media_spoiler: bool
    is_general_spoiler: bool


# --- Internal: GraphQL document --------------------------------------------

# The full Media fragment used by both fetch-by-id and search-by-title.
# Field selection optimized for ComicInfo.xml enrichment + library
# display + MAL cross-reference. asHtml:false is requested per the
# AniList docs convention but the API still returns <br>/<i>/<b> tags
# in practice (verified 2026-05-28) — _strip_anilist_html handles both.
_MEDIA_FRAGMENT = """
fragment MediaFields on Media {
  id
  idMal
  type
  format
  status
  countryOfOrigin
  isAdult
  title { romaji english native userPreferred }
  synonyms
  description(asHtml: false)
  startDate { year }
  chapters
  volumes
  averageScore
  meanScore
  popularity
  coverImage { extraLarge large }
  siteUrl
  genres
  tags { name category rank isAdult isMediaSpoiler isGeneralSpoiler }
}
"""

_QUERY_BY_ID = f"""
query($id: Int!) {{
  Media(id: $id, type: MANGA) {{
    ...MediaFields
  }}
}}
{_MEDIA_FRAGMENT}
"""

_QUERY_BY_SEARCH = f"""
query($search: String!, $perPage: Int = 8) {{
  Page(perPage: $perPage) {{
    media(search: $search, type: MANGA) {{
      ...MediaFields
    }}
  }}
}}
{_MEDIA_FRAGMENT}
"""


# --- Internal: HTTP client -------------------------------------------------

def _query_anilist(
    query: str, variables: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """POST a GraphQL query to AniList with 429/5xx retry.

    Returns the parsed `data` block on success, None on definitive
    failure (network unreachable, 4xx other than 429, exhausted retries,
    or GraphQL `errors` field in the payload — usually a stale ID).
    Never raises — callers handle None by skipping enrichment or falling
    through to search.

    Cross-file: caller-side error handling at aio-dl.py's enrichment
    hook (try/except around the enrich_from_anilist call) is the final
    safety net; this function should already absorb every transient.
    """
    for attempt in range(ANILIST_MAX_RETRIES):
        try:
            response = requests.post(
                ANILIST_GRAPHQL_URL,
                json={"query": query, "variables": variables},
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=ANILIST_TIMEOUT_S,
            )
        except requests.RequestException:
            if attempt + 1 < ANILIST_MAX_RETRIES:
                time.sleep(ANILIST_RATE_LIMIT_SLEEP_S)
                continue
            return None

        status = response.status_code
        if status == 200:
            try:
                payload = response.json()
            except ValueError:
                return None
            # GraphQL errors come back inside a 200 with an `errors` key;
            # treat a non-empty errors list as a query failure (typically
            # a stale ID for fetch-by-id) so the caller can fall through.
            if payload.get("errors"):
                return None
            return payload.get("data") or {}

        if status == 429:
            # AniList sends Retry-After in seconds. Cap at 10s so a
            # misconfigured header can't wedge the run.
            retry_after_raw = response.headers.get("Retry-After", "")
            try:
                retry_after = min(10.0, float(retry_after_raw))
            except ValueError:
                retry_after = ANILIST_RATE_LIMIT_SLEEP_S
            if attempt + 1 < ANILIST_MAX_RETRIES:
                time.sleep(max(0.1, retry_after))
                continue
            return None

        if 500 <= status < 600:
            if attempt + 1 < ANILIST_MAX_RETRIES:
                time.sleep(ANILIST_RATE_LIMIT_SLEEP_S)
                continue
            return None

        # 4xx other than 429: don't retry. AniList returns 404 for
        # missing media IDs and 400 for malformed queries — either way
        # a retry won't help.
        return None

    # Unreachable in practice (the loop always returns inside an
    # iteration), but kept for static-analysis quieting and as a safety
    # net against future refactors of the loop body.
    return None


def _fetch_by_id(anilist_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a single Media by its AniList ID. Returns None on miss."""
    data = _query_anilist(_QUERY_BY_ID, {"id": int(anilist_id)})
    if not data:
        return None
    return data.get("Media")


def _search_candidates(
    title: str, *, per_page: int = 8
) -> List[Dict[str, Any]]:
    """Search AniList by free-text title. Returns up to per_page candidates.

    Filters out NOVEL-format hits so a light novel adaptation can't win
    the score over its manga sibling. ONE_SHOT is kept (some downloads
    legitimately ARE oneshots; rapidfuzz usually picks the serialized
    entry first when both exist).
    """
    if not title:
        return []
    data = _query_anilist(
        _QUERY_BY_SEARCH, {"search": title, "perPage": int(per_page)}
    )
    if not data:
        return []
    page = data.get("Page") or {}
    candidates = page.get("media") or []
    return [c for c in candidates if (c.get("format") not in _EXCLUDED_FORMATS)]


# --- Internal: HTML cleanup ------------------------------------------------

# Strip-but-keep-content tag families. Block-level tags like <p>/<div>
# don't appear in AniList descriptions in practice — kept narrow on
# purpose so a future API change doesn't silently swallow useful markup.
_HTML_STRIP_PAIRS = re.compile(
    r"</?(?:i|b|em|strong|u|s|del|ins|small|sup|sub|span)\b[^>]*>",
    re.IGNORECASE,
)
# <br>, <br/>, <br /> all collapse to a single newline.
_HTML_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
# Three-or-more newlines collapse to two (preserves paragraph breaks
# while killing the "AniList double-<br>" → "\n\n\n" inflation pattern).
_NEWLINE_COLLAPSE = re.compile(r"\n{3,}")


def _strip_anilist_html(desc: Optional[str]) -> str:
    """Convert AniList's HTML-flavoured description to plain text.

    AniList's `description(asHtml: false)` still emits `<br>`, `<i>`,
    `<b>` etc. in practice (verified 2026-05-28 against the live API).
    We strip the tags, decode HTML entities, normalize line endings,
    and collapse runaway blank lines so the output is suitable for
    ComicInfo.xml `<Summary>`, Komikku `details.json` `description`,
    and library UI display.

    Attribution lines like `(Source: Tappytoon)` and any prose-level
    structure are preserved unchanged.
    """
    if not desc:
        return ""
    s = str(desc)
    s = _HTML_BR.sub("\n", s)
    s = _HTML_STRIP_PAIRS.sub("", s)
    s = html.unescape(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _NEWLINE_COLLAPSE.sub("\n\n", s)
    return s.strip()


# --- Internal: matching ----------------------------------------------------

def _candidate_titles(media: Dict[str, Any]) -> List[str]:
    """All title variants a candidate exposes — used for fuzzy matching."""
    title_block = media.get("title") or {}
    out: List[str] = []
    for key in ("romaji", "english", "native", "userPreferred"):
        val = title_block.get(key)
        if val:
            out.append(str(val))
    for syn in media.get("synonyms") or []:
        if syn:
            out.append(str(syn))
    return out


def _score_candidate(
    source_titles: List[str], candidate: Dict[str, Any]
) -> float:
    """Best (max) rapidfuzz WRatio across all (source x candidate) pairs.

    rapidfuzz is project-wide required for cross-site search; this lazy
    import keeps the failure mode consistent (clear ImportError naming
    the install command) instead of failing at module-load time.
    """
    try:
        from rapidfuzz import fuzz
    except ImportError as exc:
        raise ImportError(
            "rapidfuzz is required for --metadata-source enrichment. "
            "Install with: pip install rapidfuzz"
        ) from exc
    if not source_titles:
        return 0.0
    cand_titles = _candidate_titles(candidate)
    if not cand_titles:
        return 0.0
    best = 0.0
    for s in source_titles:
        for c in cand_titles:
            score = float(fuzz.WRatio(s, c))
            if score > best:
                best = score
    return best


def _pick_best_candidate(
    source_titles: List[str],
    candidates: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], float]:
    """Return (best_candidate, best_score) — best_candidate is None when
    no candidate cleared ANILIST_TITLE_MATCH_THRESHOLD.

    The score is returned in both branches so the caller can log the
    best-seen-score when a match was rejected ("no confident match
    for X — best score 42 < 75").
    """
    best_cand: Optional[Dict[str, Any]] = None
    best_score = 0.0
    for cand in candidates:
        score = _score_candidate(source_titles, cand)
        if score > best_score:
            best_cand = cand
            best_score = score
    if best_cand is None or best_score < ANILIST_TITLE_MATCH_THRESHOLD:
        return None, best_score
    return best_cand, best_score


# --- Internal: derived fields ----------------------------------------------

def _derive_media_format(country_code: Optional[str]) -> Optional[str]:
    """Map AniList countryOfOrigin → user-friendly format label.

    AniList stores everything as format=MANGA regardless of origin. We
    derive a label so the user's reader can badge titles "Manhwa"/
    "Manhua"/"Manga" without re-deriving the mapping on the read side.
    Same convention MangaUpdates uses for its `type` field.
    """
    if not country_code:
        return None
    code = str(country_code).upper()
    if code == "KR":
        return "MANHWA"
    if code in ("CN", "TW"):
        return "MANHUA"
    if code == "JP":
        return "MANGA"
    return "MANGA"


def _split_tags(
    raw_tags: List[Dict[str, Any]], tag_min_rank: int
) -> Tuple[List[AnilistTag], List[AnilistTag]]:
    """Filter raw AniList tag dicts → (non_spoiler, spoiler) AnilistTag lists.

    Tags below `tag_min_rank` are dropped entirely. Adult-only tags
    are NOT filtered here — the per-Media `isAdult` flag governs that
    at the caller level; suppressing here would defeat enrichment for
    the cases where it matters most.

    Both lists are sorted (-rank, name) for stable XML output and
    predictable cross-run diffs.
    """
    non_spoiler: List[AnilistTag] = []
    spoiler: List[AnilistTag] = []
    for raw in raw_tags or []:
        try:
            rank = int(raw.get("rank") or 0)
        except (TypeError, ValueError):
            rank = 0
        if rank < int(tag_min_rank):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        tag = AnilistTag(
            name=name,
            category=str(raw.get("category") or "").strip(),
            rank=rank,
            is_media_spoiler=bool(raw.get("isMediaSpoiler")),
            is_general_spoiler=bool(raw.get("isGeneralSpoiler")),
        )
        if tag.is_media_spoiler or tag.is_general_spoiler:
            spoiler.append(tag)
        else:
            non_spoiler.append(tag)
    non_spoiler.sort(key=lambda t: (-t.rank, t.name.lower()))
    spoiler.sort(key=lambda t: (-t.rank, t.name.lower()))
    return non_spoiler, spoiler


def _union_genres(
    site_genres: List[str], anilist_genres: List[str]
) -> List[str]:
    """Union site + AniList genres, case-insensitive dedupe, site order first.

    Site genres often have site-specific casing or wording (e.g.
    "Action", "Sci-fi" vs AniList's "Action", "Sci-Fi"). The lower-case
    dedupe keeps the first-seen casing, which is the site's — preserves
    the user-recognizable display form when both sources agree.
    """
    out: List[str] = []
    seen_lower = set()
    for genre_list in (site_genres or [], anilist_genres or []):
        for src in genre_list:
            if not src:
                continue
            key = str(src).strip().lower()
            if key and key not in seen_lower:
                seen_lower.add(key)
                out.append(str(src).strip())
    return out


# --- Internal: apply -------------------------------------------------------

def _apply_anilist_match(
    comic_data: Dict[str, Any],
    media: Dict[str, Any],
    tag_min_rank: int,
) -> None:
    """Mutate comic_data with fields from an AniList Media doc.

    Field merge semantics (per plan-locked user decisions):
      - desc: REPLACE with AniList description (per user choice — max
        uniformity for filtering)
      - genres: UNION (site first, AniList appended, case-insensitive
        dedupe — preserves site display casing)
      - authors / artists: FILL-MISSING (v1 doesn't fetch AniList
        staff{} connection so this is effectively a no-op today; the
        semantic is documented for future expansion)
      - status: REPLACE with AniList enum spelling. The existing
        aio-dl.py:_komikku_status_to_digit helper already handles
        AniList's enum spellings via its lowercase mapping
        (FINISHED → "finished" → "2", RELEASING → "releasing" → "1",
        CANCELLED → "cancelled" → "5", HIATUS → "hiatus" → "6",
        NOT_YET_RELEASED → falls through to "0"). No helper change
        needed.
      - anilist_tags / anilist_spoiler_tags: SET (AniList-only fields)
      - country_of_origin / media_format / anilist_id / mal_id /
        anilist_synonyms: SET
    """
    if media.get("id"):
        comic_data["anilist_id"] = int(media["id"])
    if media.get("idMal"):
        comic_data["mal_id"] = int(media["idMal"])

    cleaned_desc = _strip_anilist_html(media.get("description"))
    if cleaned_desc:
        comic_data["desc"] = cleaned_desc

    comic_data["genres"] = _union_genres(
        comic_data.get("genres") or [],
        media.get("genres") or [],
    )

    if media.get("status"):
        comic_data["status"] = str(media["status"])

    country = media.get("countryOfOrigin")
    if country:
        comic_data["country_of_origin"] = str(country)
    media_format = _derive_media_format(country)
    if media_format:
        comic_data["media_format"] = media_format

    comic_data["anilist_synonyms"] = list(media.get("synonyms") or [])

    non_spoiler, spoiler = _split_tags(media.get("tags") or [], tag_min_rank)
    comic_data["anilist_tags"] = non_spoiler
    comic_data["anilist_spoiler_tags"] = spoiler


# --- Public entry point ----------------------------------------------------

def enrich_from_anilist(
    comic_data: Dict[str, Any],
    *,
    hid: str,
    handler_name: str,
    year: Optional[int],
    cover_url: Optional[str],
    tag_min_rank: int,
    force_refresh: bool,
    cached_anilist_id: Optional[int],
) -> Dict[str, Any]:
    """Enrich `comic_data` in place from AniList; return the same dict.

    Flow:
      1. If `cached_anilist_id` is set AND NOT `force_refresh`: fetch
         that Media by ID (1 GraphQL hit). On success, apply fields and
         return immediately. On 404 / network failure / API errors,
         fall through to the search path so a stale cached ID can
         self-heal.
      2. Search AniList for the site title + alt_names. Score every
         candidate via rapidfuzz WRatio across every source-title ×
         candidate-title pair; pick the highest.
      3. If best score >= ANILIST_TITLE_MATCH_THRESHOLD (75), apply
         fields. The match's anilist_id then gets persisted by
         aio-dl.py's .aio_series.json writer so subsequent runs
         take the cached fast path.
      4. Otherwise leave comic_data untouched (no anilist_id key set)
         so the caller knows to log "no confident match" and the run
         continues with site-only metadata. The best-observed score
         is stashed under `_anilist_best_score` purely for the
         caller's log line; the underscore prefix marks it as
         non-persistable transient data and downstream writers ignore
         unknown keys.

    `year`, `cover_url`, `hid`, `handler_name` are currently accepted-
    but-unused — forwarded for future scoring refinements (year
    tiebreak, cover-image perceptual match) without breaking the API.
    """
    # Cached-ID fast path.
    if cached_anilist_id and not force_refresh:
        media = _fetch_by_id(int(cached_anilist_id))
        if media:
            _apply_anilist_match(comic_data, media, tag_min_rank)
            return comic_data
        # Stale ID or transient network failure: fall through. If the
        # search step also fails, comic_data ends up unchanged and the
        # caller logs accordingly.

    # Search path.
    source_titles: List[str] = []
    if comic_data.get("title"):
        source_titles.append(str(comic_data["title"]))
    for alt in comic_data.get("alt_names") or []:
        if alt:
            source_titles.append(str(alt))
    if not source_titles:
        return comic_data

    candidates = _search_candidates(source_titles[0])
    if not candidates:
        # Empty pool (network failure / 0 hits / API rate-limit exhausted).
        # Set the best-score sentinel so the caller's log line is uniform
        # across "no hits" and "hits but all below threshold" branches.
        comic_data["_anilist_best_score"] = 0.0
        return comic_data

    best, score = _pick_best_candidate(source_titles, candidates)
    comic_data["_anilist_best_score"] = score
    if best is None:
        return comic_data
    _apply_anilist_match(comic_data, best, tag_min_rank)
    return comic_data
