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

from . import fuzzy_match


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

# rapidfuzz token_set_ratio (0..100) floor for treating a candidate's AniList
# staff as the same creator the *site* credited. token_set_ratio (not WRatio) so
# name order and extra tokens don't matter: "Hata Kenjirou" vs "Kenjirou Hata" =
# 100, "MASHIMA Hiro" vs "Hiro Mashima" = 100. Author is a TIEBREAK/booster
# layered on top of the 75 title gate, never a hard filter — series whose site
# romanizes the author differently from AniList (Eleceed's "Jeho Son / ZHENA",
# Tekyuu's "Roots") still match on title alone, they just don't get the boost.
# 85 is high enough that an unrelated creator won't trip it. Used by
# _author_match_score / _pick_best_candidate / the cached-ID self-heal in
# enrich_from_anilist.
ANILIST_AUTHOR_MATCH_THRESHOLD = 85.0

# Title-score band (points) for the composite match ranker. Candidates whose
# title score is within this many points of the BEST title score among the gated
# candidates form the "top band"; the author/popularity tiebreaks apply only
# WITHIN that band. This stops a spurious author hit on a LESS-similar entry from
# beating the obvious title match: site "Solo Leveling" scores 100 on the main
# series (id 105398) but only 90 on the sequel "Solo Leveling: Ragnarok" (id
# 179445), and the site's studio credit "Redice Studio" happens to match the
# sequel's staff exactly (100) while the main series credits individuals (81). An
# 8-point band keeps near-exact title matches together while separating subtitle-
# extended titles (must stay < 10 to split the Solo/Ragnarok 100-vs-90 gap). grep
# caller: _pick_best_candidate.
_TITLE_BAND_DELTA = 8.0

# Spacing between retries when AniList didn't tell us how long to wait.
# Budget at 90 req/min = 1 every 0.67s; 0.7s leaves ~5% margin so we
# never get caught by the burst limiter. Only applies inside the retry
# loop; one-shot calls run immediately.
ANILIST_RATE_LIMIT_SLEEP_S = 0.7

ANILIST_TIMEOUT_S = 15

ANILIST_MAX_RETRIES = 3

# Cap on AniList search requests per series on the no-match path. Each source
# title now expands to up to four queries (full, bracket-cleaned, trailing
# subtitle segment, shortened prefix — see enrich_from_anilist), so 6 leaves
# room for the primary's variants plus an alt's. Early-stop means the common
# case (full title matches on query 1) still costs a single request; this cap
# only bounds the worst case so a series genuinely absent from AniList can't
# trigger a request storm.
_MAX_SEARCH_QUERIES = 6

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
    in the ComicInfo.xml <TagsExtended> block, the Komikku details.json
    (flat `anilist_tags` / `anilist_spoiler_tags` keys), and
    .aio_series.json.

    Cross-file: serialized to dict in aio-dl.py's details.json writer (grep
    _build_aio_reader_extras) and .aio_series.json writer (grep _tag_to_dict),
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
  staff(perPage: 8) {
    edges { role node { name { full native } } }
  }
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

# Bracket/tilde families that wrap subtitle noise AniList's search index
# chokes on. Stripped (not split) so the core title survives; see
# _clean_search_title.
_TITLE_NOISE_RE = re.compile(r"~[^~]*~|\([^)]*\)|\[[^\]]*\]|[【「][^】」]*[】」]")


def _clean_search_title(title: str) -> str:
    """Return `title` with bracketed/tilde subtitle segments stripped.

    e.g. "Shangri-La Frontier ~ Kusoge Hunter, Kamige ni Idoman to su~"
    -> "Shangri-La Frontier". Used as a fallback AniList search query (and
    scoring title) when the full stored title returns no candidates — some
    sites bake a ~...~ or (...) subtitle into the title that defeats
    AniList's search index. Deliberately does NOT split on ':' / ' - '
    subtitle separators: that would surface a parent series for a spinoff
    (e.g. "...Spoils Me Rotten: After the Rain") and risk a wrong match even
    with full-title scoring. May return a string equal to the input (caller
    dedupes) or "" if nothing survives. Cross-file: called only from
    enrich_from_anilist's search path. Subtitle-separator SPLITTING does
    happen — but in _subtitle_segment, which is search-query-only (never a
    scoring title), so the parent-match risk above doesn't apply to it.
    """
    if not title:
        return ""
    s = _TITLE_NOISE_RE.sub(" ", str(title))
    return re.sub(r"\s+", " ", s).strip()


# Subtitle/volume separators: a ':' or a SPACED dash. Used to pull the trailing
# distinctive segment out of titles AniList's search can't match whole — e.g.
# "JoJo no Kimyou na Bouken: Part 4 - Diamond wa Kudakenai" (0 results) vs the
# trailing "Diamond wa Kudakenai" (id 33006). Hyphens WITHOUT surrounding
# spaces ("Shangri-La", "Boku-tachi") are deliberately NOT separators.
_SUBTITLE_SPLIT_RE = re.compile(r":\s+|\s+[-–—]\s+")


def _subtitle_segment(title: str) -> str:
    """Trailing segment after the last subtitle separator — a SEARCH query only,
    never a scoring title. "" when there's no separator or the trailing part is
    a <2-word fragment (too generic to anchor a search). Because the match is
    still gated by scoring against the FULL title in enrich_from_anilist,
    pulling a parent/other series into the candidate pool here is harmless: it
    fails the 75 threshold. grep caller: enrich_from_anilist.
    """
    parts = [p.strip() for p in _SUBTITLE_SPLIT_RE.split(str(title or "")) if p.strip()]
    if len(parts) < 2:
        return ""
    seg = parts[-1]
    return seg if len(seg.split()) >= 2 else ""


def _shortened_prefix(title: str, words: int = 4) -> str:
    """First `words` tokens of a LONG, separator-less title — a SEARCH query
    only. "" when the title has a subtitle separator (use _subtitle_segment
    instead) or is short enough (≤5 words) that the full-title query already
    covers it. Rescues long romaji whose spelling drifts between the site and
    AniList — e.g. AnoHana's "...Namae o Boku-tachi..." vs AniList's
    "...Namae wo Bokutachi..." returns nothing whole, but "Ano Hi Mita Hana"
    matches id 65733. grep caller: enrich_from_anilist.
    """
    s = str(title or "")
    if _SUBTITLE_SPLIT_RE.search(s):
        return ""
    toks = s.split()
    if len(toks) <= max(words, 5):
        return ""
    return " ".join(toks[:words])


def _candidate_titles_split(
    media: Dict[str, Any],
) -> Tuple[List[str], List[str]]:
    """(primary_titles, synonyms) for a candidate.

    Primary = the four official title slots (romaji/english/native/
    userPreferred). Synonyms = AniList's free-form `synonyms` list, which is
    where fan/alt spellings AND parody/doujin cross-references live. Keeping the
    buckets apart lets _score_candidate_detail flag a "synonym-only" match — e.g.
    a Hentai doujin (id 128087) that merely lists "Fairy Tail" as a synonym must
    not tie the real FAIRY TAIL (id 30598) whose PRIMARY title matches. grep
    callers: _candidate_titles, _score_candidate_detail.
    """
    title_block = media.get("title") or {}
    primary = [
        str(title_block[k])
        for k in ("romaji", "english", "native", "userPreferred")
        if title_block.get(k)
    ]
    synonyms = [str(s) for s in (media.get("synonyms") or []) if s]
    return primary, synonyms


def _candidate_titles(media: Dict[str, Any]) -> List[str]:
    """All title variants (primary + synonyms) a candidate exposes."""
    primary, synonyms = _candidate_titles_split(media)
    return primary + synonyms


def _candidate_author_names(media: Dict[str, Any]) -> List[str]:
    """Creator names from a candidate's `staff` connection — STORY/ART roles
    only.

    AniList staff edges carry a `role` string: authorship roles ("Story", "Art",
    "Story & Art", "Original Story") plus production/localization ones
    ("Translator", "Lettering (English)", "Character Design"). We keep roles
    mentioning story/art and drop the rest, then return BOTH name.full (romaji)
    and name.native (kanji/hangul) so a site storing either form can match. grep
    caller: _author_match_score. Requires the `staff{}` selection in
    _MEDIA_FRAGMENT.
    """
    edges = ((media.get("staff") or {}).get("edges")) or []
    names: List[str] = []
    for edge in edges:
        role = str(edge.get("role") or "").lower()
        if not any(k in role for k in ("story", "art")):
            continue
        if any(bad in role for bad in ("translator", "lettering", "design", "assistant", "editor")):
            continue
        name_block = (edge.get("node") or {}).get("name") or {}
        for val in (name_block.get("full"), name_block.get("native")):
            if val:
                names.append(str(val))
    return names


# Staff roles to DROP entirely when writing the author/artist credit — the
# production + localization edges AniList mixes into staff{}. Everything that
# survives is split by role in _staff_names_by_role: "story" -> author, "art"
# -> artist. Mirrors _candidate_author_names' drop-list (kept in sync); "edit"
# catches both "Editor" and "Editing (…)".
_STAFF_DROP_ROLE = (
    "translator",
    "lettering",
    "design",
    "assistant",
    "edit",
    "proofread",
)


def _staff_names_by_role(
    media: Dict[str, Any],
) -> Tuple[List[str], List[str]]:
    """(authors, artists) written from a candidate's `staff` connection.

    author = roles containing "story" (Story / Original Story / Story & Art);
    artist = roles containing "art" (Art / Story & Art) — so a single "Story &
    Art" credit (One Piece's Oda) lands in BOTH. Production/localization roles
    (_STAFF_DROP_ROLE) are dropped first. Prefers name.full (romaji) over
    name.native, dedupes case-insensitively, preserves AniList's edge order.

    Both lists are empty when the candidate carries no authorship staff, so the
    caller (_apply_anilist_match) leaves the site's own author/artist untouched
    instead of blanking a good credit. This is the WRITE side of staff{}; the
    MATCH side (_author_match_score / _candidate_author_names) keeps returning
    both romaji+native for fuzzy comparison. grep caller: _apply_anilist_match.
    """
    edges = ((media.get("staff") or {}).get("edges")) or []
    authors: List[str] = []
    artists: List[str] = []
    seen_a: set = set()
    seen_r: set = set()
    for edge in edges:
        role = str(edge.get("role") or "").lower()
        if any(bad in role for bad in _STAFF_DROP_ROLE):
            continue
        is_author = "story" in role
        is_artist = "art" in role
        if not (is_author or is_artist):
            continue
        name_block = (edge.get("node") or {}).get("name") or {}
        name = str(name_block.get("full") or name_block.get("native") or "").strip()
        if not name:
            continue
        key = name.lower()
        if is_author and key not in seen_a:
            seen_a.add(key)
            authors.append(name)
        if is_artist and key not in seen_r:
            seen_r.add(key)
            artists.append(name)
    return authors, artists


# Site author strings that carry no identifying signal — never let these
# manufacture an author "match". Compared after default_process lowercasing.
_AUTHOR_PLACEHOLDERS = frozenset(
    {"unknown", "n/a", "na", "none", "null", "-", "updating", "author", "artist"}
)


def _load_rapidfuzz():
    """Lazy-import (fuzz, default_process) from rapidfuzz. Module-wide pattern so
    a packager who strips rapidfuzz breaks only --metadata-source enrichment,
    with a clear ImportError.

    Kept as a named function even though sites/fuzzy_match now owns the seam:
    the message names the FEATURE the user was using, and the scorer wrappers
    below can't do that. Scores themselves go through fuzzy_match so the C++ and
    pure-Python rapidfuzz backends agree -- see that module's header.
    """
    try:
        return fuzzy_match.load_rapidfuzz()
    except ImportError as exc:
        raise ImportError(
            "rapidfuzz is required for --metadata-source enrichment. "
            "Install with: pip install rapidfuzz"
        ) from exc


def _author_match_score(
    source_authors: Optional[List[str]], candidate: Dict[str, Any]
) -> Optional[float]:
    """Best rapidfuzz token_set_ratio between the site's author(s) and the
    candidate's AniList staff. None when either side has no usable name — lets
    the caller tell "authors disagree" (a number below threshold) apart from
    "can't tell" (None).

    token_set_ratio is order- and extra-token-insensitive: "Hata Kenjirou" vs
    "Kenjirou Hata" = 100, "MASHIMA Hiro" vs "Hiro Mashima" = 100. Placeholder/
    empty/<3-char site strings are dropped so a bare "Unknown" can't match an
    AniList "Unknown". grep callers: _pick_best_candidate, enrich_from_anilist.
    """
    _load_rapidfuzz()  # surface the feature-named ImportError before scoring
    srcs: List[str] = []
    for author in source_authors or []:
        author = str(author or "").strip()
        if len(author) >= 3 and author.lower() not in _AUTHOR_PLACEHOLDERS:
            srcs.append(author)
    cand_names = _candidate_author_names(candidate)
    if not srcs or not cand_names:
        return None
    best = 0.0
    for s in srcs:
        for c in cand_names:
            score = fuzzy_match.processed_token_set_ratio(s, c)
            if score > best:
                best = score
    return best


def _score_candidate_detail(
    source_titles: List[str], candidate: Dict[str, Any]
) -> Tuple[float, bool]:
    """(best_title_score, primary_hit).

    best_title_score = max rapidfuzz WRatio over ALL candidate titles (primary +
    synonyms), processor=default_process — the value the 75 admission gate uses
    (unchanged from the old _score_candidate). primary_hit = True when the
    candidate's PRIMARY titles alone clear the gate, i.e. the match isn't
    synonym-only. _pick_best_candidate ranks primary hits above synonym-only
    hits so a doujin can't hijack a series via a parody synonym. See
    _candidate_titles_split.

    rapidfuzz lazy-imported here (module-wide pattern) so a packager who strips
    it breaks only enrichment, with a clear ImportError.
    """
    _load_rapidfuzz()  # surface the feature-named ImportError before scoring
    if not source_titles:
        return 0.0, False
    primary, synonyms = _candidate_titles_split(candidate)
    if not primary and not synonyms:
        return 0.0, False

    def _best_over(cands: List[str]) -> float:
        best = 0.0
        for s in source_titles:
            for c in cands:
                # processor=default_process lowercases, trims, strips non-
                # alphanumerics on both sides. Without it WRatio is brutally
                # case-sensitive: "FULL METAL ALCHEMIST" vs synonym "Full Metal
                # Alchemist" = 25 raw / 100 processed (id 30025); unrelated
                # "Hagane no Renkinjutsushi" stays 26. The 75 floor still
                # separates cleanly.
                score = fuzzy_match.processed_wratio(s, c)
                if score > best:
                    best = score
        return best

    primary_score = _best_over(primary)
    synonym_score = _best_over(synonyms)
    best_score = max(primary_score, synonym_score)
    primary_hit = primary_score >= ANILIST_TITLE_MATCH_THRESHOLD
    return best_score, primary_hit


def _score_candidate(
    source_titles: List[str], candidate: Dict[str, Any]
) -> float:
    """Back-compat shim: best title score only. New code wants the
    (score, primary_hit) pair from _score_candidate_detail.
    """
    return _score_candidate_detail(source_titles, candidate)[0]


def _pick_best_candidate(
    source_titles: List[str],
    candidates: List[Dict[str, Any]],
    *,
    source_authors: Optional[List[str]] = None,
    year: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], float]:
    """Return (best_candidate, best_score) — best_candidate is None when no
    candidate cleared ANILIST_TITLE_MATCH_THRESHOLD.

    Selection is a COMPOSITE rank over every candidate that passes the 75 title
    gate, in this priority order (each a tiebreak for the previous):
      0. title band   — candidates within _TITLE_BAND_DELTA points of the best
         title score. Only same-band (≈equally-similar) candidates compete on the
         keys below, so a less-similar sequel/spinoff can't steal the match on a
         fluke author/popularity hit (site "Solo Leveling" = 100 on the main
         series but 90 on "Solo Leveling: Ragnarok", whose staff the site's
         "Redice Studio" credit happens to match).
      1. primary_hit    — title matched on a PRIMARY title, not synonym-only
         (kills the doujin-synonym hijack: the Hentai "Fairy Tail" carries the
         title only as a SYNONYM, so it loses to the real series here even when
         author data is absent).
      2. year_matched   — startDate.year == site year hint (when provided).
      3. popularity     — AniList popularity; canonical entries dwarf parody/
         duplicate/obscure same-title ones (Fly Me: 40474 vs 1669). This is the
         main same-title disambiguator on a FRESH search (no cached id).
      4. author_matched — site author == candidate AniList staff (>= 85
         token_set_ratio). A TIEBREAK/booster that sits BELOW popularity, NOT a
         lever that overrides it: identically-titled series ("Fly Me to the Moon"
         returns 6 entries all scoring 100; "Fairy Tail" the real series + a
         same-titled doujin) are separated by popularity first, and a fuzzy
         author-string hit on an obscure franchise/studio entry must never
         outrank a far more popular in-band candidate whose author the site
         merely romanizes differently. Mirrors the self-heal popularity guard
         (grep 'best_pop >= cached_pop'); author's decisive role lives in the
         cached self-heal detection + tail override (enrich_from_anilist), not
         here.
      5. title_score    — raw WRatio, last resort.

    The 75 title floor stays a HARD gate, so the band/author/popularity tiebreaks
    can never admit an unrelated series — they only reorder title-plausible ones.
    best_score (the chosen candidate's title score, or the best seen when
    nothing passed) is returned for the caller's "no confident match (best NN)"
    log. The old behavior (pure max title score, first-wins on ties) was the
    Fly-Me-to-the-Moon / Fairy-Tail mismatch bug. grep caller:
    enrich_from_anilist.
    """
    best_seen = 0.0
    gated: List[Tuple[bool, bool, bool, int, float, Dict[str, Any]]] = []
    for cand in candidates:
        title_score, primary_hit = _score_candidate_detail(source_titles, cand)
        if title_score > best_seen:
            best_seen = title_score
        if title_score < ANILIST_TITLE_MATCH_THRESHOLD:
            continue
        author = _author_match_score(source_authors, cand)
        author_matched = author is not None and author >= ANILIST_AUTHOR_MATCH_THRESHOLD
        year_matched = False
        if year:
            cand_year = (cand.get("startDate") or {}).get("year")
            try:
                year_matched = cand_year is not None and int(cand_year) == int(year)
            except (TypeError, ValueError):
                year_matched = False
        popularity = int(cand.get("popularity") or 0)
        gated.append(
            (author_matched, primary_hit, year_matched, popularity, title_score, cand)
        )
    if not gated:
        return None, best_seen
    # Primary key is the TITLE BAND: only candidates whose title score is within
    # _TITLE_BAND_DELTA of the best title score compete on the keys below, so a
    # less-similar entry can't win on a spurious author/popularity hit (the
    # Solo-Leveling-vs-Ragnarok fix). Within the band: primary-title hit > year >
    # popularity > author match > title score. Python's stable sort preserves
    # AniList return order for exact all-key ties.
    best_title = max(r[4] for r in gated)

    def _rank_key(r: Tuple[bool, bool, bool, int, float, Dict[str, Any]]):
        author_matched, primary_hit, year_matched, popularity, title_score, _cand = r
        in_band = title_score >= best_title - _TITLE_BAND_DELTA
        # popularity OUTRANKS author_matched: on a fresh search (no cached id to
        # apply the self-heal popularity guard) a coincidental author-string hit
        # on an obscure franchise/studio entry would otherwise beat a far more
        # popular same-title candidate whose author the site romanizes
        # differently. Canonical entries dominate popularity, so the same-title
        # fixes (Fly Me, Fairy Tail) hold on popularity (+ primary_hit) without
        # leaning on author here; author stays a final tiebreak/booster. This is
        # the fresh-path analogue of enrich_from_anilist's 'best_pop >=
        # cached_pop' guard — author must never DOWNGRADE popularity.
        return (in_band, primary_hit, year_matched, popularity, author_matched, title_score)

    gated.sort(key=_rank_key, reverse=True)
    chosen = gated[0]
    return chosen[5], chosen[4]


# --- Internal: per-site trust gate -----------------------------------------

# Sites whose author metadata is reliable enough to use as a MATCH-REJECTION
# signal, not merely the tiebreak/booster it is everywhere else. LINE Webtoon
# exposes a clean author meta tag (sites/linewebtoon.py, grep
# 'com-linewebtoon:webtoon:author') and most of its originals simply aren't on
# AniList, so a title-only hit whose author disagrees is almost always a synonym
# collision — e.g. "unOrdinary" (by uru-chan) hitting the Japanese manga "Kanojo
# Iro no Kanojo" (id 40442) purely through its synonym "Unordinary Life" (WRatio
# 90). A frozenset so extending the policy to tapas / other webtoon handlers
# later is a one-line change. grep caller: _match_is_trustworthy_for_site.
_AUTHOR_GATED_SITES = frozenset({"linewebtoon"})


def _match_is_trustworthy_for_site(
    handler_name: str,
    media: Dict[str, Any],
    site_scoring_titles: List[str],
    source_authors: Optional[List[str]],
) -> bool:
    """Author/primary-title CORROBORATION gate for author-reliable sites.

    Returns False ⇒ the caller MUST reject this AniList match and fall back to
    the site's own metadata. Always True for handlers NOT in _AUTHOR_GATED_SITES,
    so every other site keeps its "author is a booster, never a filter" behavior
    (the Eleceed/Tekyuu romanization-difference protection documented on
    ANILIST_AUTHOR_MATCH_THRESHOLD stays intact library-wide).

    For a gated site a match is trusted iff EITHER corroborating signal holds:
      - author matches   — site author vs candidate staff ≥ ANILIST_AUTHOR_MATCH_
        THRESHOLD (the normal author-match test), OR
      - primary-title hit — the candidate matched on a PRIMARY title, not a
        synonym only (_score_candidate_detail's primary_hit).
    It is rejected only when BOTH fail: the author is absent or disagrees AND the
    sole title basis is a synonym. That is precisely the unOrdinary poison class
    (author 36 < 85, primary_hit False). Eleceed survives it — the site author
    "Jeho Son / ZHENA" scores 67 < 85 against AniList's "Jae-Ho Son / Hye-Jin
    Kim" (ZHENA is Hye-Jin Kim's pen name; Jeho vs Jae-Ho differ), but the
    PRIMARY title "Eleceed" is an exact 100 → primary_hit True → trusted.

    CRITICAL: site_scoring_titles MUST be the SITE title(s) only (the handler's
    live title, or the folder/meta title on refresh) — NEVER the cached
    alt_names / anilist_synonyms. A poisoned .aio_series.json carries the wrong
    entry's OWN synonyms (unOrdinary's are "Kanojoiro no Kanojo", "Unordinary
    Life"), and --refresh-library-metadata feeds them back in as alt_names; if
    those reached the primary-hit test they would score ~100 against the poison
    entry's primary title and fake corroboration, defeating the gate on exactly
    the refresh path meant to repair the poison. grep caller: enrich_from_anilist.
    """
    if handler_name not in _AUTHOR_GATED_SITES:
        return True
    if not site_scoring_titles:
        # No site title to corroborate against (pathological). Don't block —
        # the caller's normal title gate already vetted the candidate.
        return True
    author = _author_match_score(source_authors, media)
    if author is not None and author >= ANILIST_AUTHOR_MATCH_THRESHOLD:
        return True
    _, primary_hit = _score_candidate_detail(site_scoring_titles, media)
    return primary_hit


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


def _dedupe_genres(*genre_lists: List[str]) -> List[str]:
    """Concatenate genre/tag-name lists with case-insensitive dedupe.

    Order: earlier lists win; first-seen casing is preserved, so the
    leading list dictates the display form when two sources spell a genre
    differently. Empty/blank entries are dropped.

    Used by _apply_anilist_match to build the normalized visible genre
    field = AniList genres (coarse buckets) followed by AniList high-rank
    tag names (fine descriptors).

    Renamed from _union_genres (2026-06-06): the merge is no longer
    site ∪ AniList. On a confident match AniList is authoritative and the
    site's own genre list is dropped entirely — it was the source of the
    50+-tag taxonomy dumps (e.g. mangakatana leaking its whole nav genre
    dropdown). See the REPLACE semantics documented in
    _apply_anilist_match. grep callers: only _apply_anilist_match.
    """
    out: List[str] = []
    seen_lower = set()
    for genre_list in genre_lists:
        for src in genre_list or []:
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
      - genres: REPLACE with AniList genres + high-rank non-spoiler tag
        names (case-insensitive dedupe via _dedupe_genres). The site's
        genre list is dropped on a confident match — it's the source of
        the 50+-tag taxonomy dumps. Spoilers are excluded from this
        visible field. Falls back to the site genres only when AniList
        contributed nothing (changed from UNION 2026-06-06 per user req).
      - authors / artists: OVERWRITE from AniList `staff{}` on a confident
        match (user decision 2026-07-07 "AniList author always wins", via
        _staff_names_by_role: Story/Original Story -> authors, Art -> artists,
        Story & Art -> both). Only overwrites when AniList supplies staff of
        that kind, so a candidate lacking authorship data never blanks the
        site's own credit. staff{} is STILL the match disambiguator
        (_author_match_score / _pick_best_candidate); the overwrite runs only
        AFTER a match is chosen, so it can't feed its own result into that run's
        scoring. Trade-off accepted with eyes open: this replaces site author
        strings AniList romanizes differently (Eleceed "Jeho Son / ZHENA",
        One-Punch Man "ONE / Yusuke Murata") with AniList's spelling.
      - status: REPLACE with AniList enum spelling. The existing
        aio-dl.py:_komikku_status_to_digit helper already handles
        AniList's enum spellings via its lowercase mapping
        (FINISHED → "finished" → "2", RELEASING → "releasing" → "1",
        CANCELLED → "cancelled" → "5", HIATUS → "hiatus" → "6",
        NOT_YET_RELEASED → falls through to "0"). No helper change
        needed.
      - anilist_tags / anilist_spoiler_tags: SET (AniList-only fields)
      - cover: REPLACE with AniList coverImage (extraLarge → large) for
        cross-library cover normalization — every enriched series ends up
        with one cover source/quality/aspect. The site's own cover is
        stashed under `site_cover` (aio-dl.py's cover-download path falls
        back to it if the AniList CDN fetch fails) and the AniList URL is
        also mirrored to `anilist_cover` (read by --refresh-library-metadata
        to decide cover.jpg re-download). Only set when AniList actually
        supplied a cover.
      - country_of_origin / media_format / anilist_id / mal_id /
        anilist_synonyms: SET
    """
    if media.get("id"):
        comic_data["anilist_id"] = int(media["id"])
    if media.get("idMal"):
        comic_data["mal_id"] = int(media["idMal"])

    # authors / artists: OVERWRITE from AniList staff (see docstring; user
    # decision 2026-07-07 "AniList author always wins"). Guarded so a candidate
    # with no authorship staff leaves the site's own author/artist intact
    # rather than blanking it. Flows to ComicInfo <Writer>/<Penciller>, Komikku
    # details.json author/artist, and .aio_series.json authors (grep the two
    # details.json writers + _anilist_meta_fields in aio-dl.py).
    staff_authors, staff_artists = _staff_names_by_role(media)
    if staff_authors:
        comic_data["authors"] = staff_authors
    if staff_artists:
        comic_data["artists"] = staff_artists

    cleaned_desc = _strip_anilist_html(media.get("description"))
    if cleaned_desc:
        comic_data["desc"] = cleaned_desc

    non_spoiler, spoiler = _split_tags(media.get("tags") or [], tag_min_rank)
    comic_data["anilist_tags"] = non_spoiler
    comic_data["anilist_spoiler_tags"] = spoiler

    # REPLACE the visible genre list with AniList's curated set: AniList
    # genres (coarse buckets) followed by the high-rank non-spoiler tag
    # names (fine descriptors). The site's own genre list is dropped on a
    # confident match — many sites (mangakatana especially) leak their
    # entire genre taxonomy into this field, and the previous union-merge
    # preserved that garbage. Spoiler tags are deliberately excluded so the
    # default-visible genre field never leaks spoilers (they stay in
    # anilist_spoiler_tags / <SpoilerTags>). Only fall back to the existing
    # site genres if AniList contributed nothing at all (pathological:
    # matched but zero genres AND zero tags above tag_min_rank) — never
    # blank the field. Cross-file: flows to ComicInfo <Genre> (aio-dl.py
    # build_comic_info_xml / build_per_chapter_comic_info_xml) and Komikku
    # details.json `genre` (aio-dl.py, grep '"genre": list(comic_data').
    normalized_genres = _dedupe_genres(
        media.get("genres") or [],
        [t.name for t in non_spoiler],
    )
    if normalized_genres:
        comic_data["genres"] = normalized_genres

    if media.get("status"):
        comic_data["status"] = str(media["status"])

    country = media.get("countryOfOrigin")
    if country:
        comic_data["country_of_origin"] = str(country)
    media_format = _derive_media_format(country)
    if media_format:
        comic_data["media_format"] = media_format

    # Cover normalization: REPLACE the series cover with AniList's so the
    # whole library shares one cover source/quality/aspect. extraLarge is
    # AniList's highest-res variant; large is the smaller fallback. Stash the
    # site's own cover under `site_cover` so aio-dl.py's cover-download block
    # can fall back to it when the AniList CDN fetch fails (dl_image returns
    # None, never raises). Cross-file: aio-dl.py cover-download block (grep
    # 'site_cover'); the chosen cover flows to cover.jpg, the .aio_series.json
    # `cover` field, and the UI thumbnail cache (library.js keys on
    # seriesMeta.cover).
    cover_block = media.get("coverImage") or {}
    anilist_cover = cover_block.get("extraLarge") or cover_block.get("large")
    if anilist_cover:
        anilist_cover = str(anilist_cover)
        existing_cover = comic_data.get("cover")
        if existing_cover and existing_cover != anilist_cover:
            comic_data["site_cover"] = existing_cover
        comic_data["cover"] = anilist_cover
        # Record the AniList cover URL explicitly (parallel to anilist_id).
        # Not written to any sink today — cover.jpg + the `cover` field carry
        # it — but --refresh-library-metadata reads it to decide whether to
        # re-download cover.jpg, so it stays truthy ONLY when AniList actually
        # supplied a cover (not merely when a site cover is present). grep
        # anilist_cover in aio-dl.py.
        comic_data["anilist_cover"] = anilist_cover

    comic_data["anilist_synonyms"] = list(media.get("synonyms") or [])


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
         that Media by ID (1 GraphQL hit). Apply and return UNLESS the
         site author disagrees with the fetched entry's staff — then the
         cached id is suspect (a pre-fix wrong-series cache), so we stash
         it and fall through to re-search (self-heal). On 404 / network
         failure / API errors, fall through too so a stale cached ID can
         self-heal.
      2. Search AniList for the site title + alt_names, then rank every
         candidate that clears the 75 title gate by the composite key in
         _pick_best_candidate (author match > primary-title hit > year >
         popularity > title score). Author is what separates same-titled
         series that title scoring alone can't.
      3. On a confident match, apply fields. The match's anilist_id then
         gets persisted by aio-dl.py's .aio_series.json writer so
         subsequent runs take the cached fast path. When self-healing, the
         search result overrides the suspect cache only if it is itself
         author-matched AND at least as popular as the cached entry (so a
         franchise-owner author coincidence can't downgrade a good cache to
         an obscure same-series entry); otherwise the cached entry is restored.
      4. Otherwise leave comic_data untouched (no anilist_id key set)
         so the caller knows to log "no confident match" and the run
         continues with site-only metadata. The best-observed score
         is stashed under `_anilist_best_score` purely for the
         caller's log line; the underscore prefix marks it as
         non-persistable transient data and downstream writers ignore
         unknown keys.

    `year` feeds the _pick_best_candidate year tiebreak when the site
    supplied it (live path only; the refresh path passes year=None).
    `cover_url`, `hid`, `handler_name` are accepted-but-unused — forwarded
    for future scoring refinements (cover-image perceptual match) without
    breaking the API.
    """
    # Author(s) the SITE credited — the disambiguator the matcher leans on.
    # Present at both call sites: a live download sets comic_data["authors"] from
    # the handler; --refresh-library-metadata seeds it from .aio_series.json
    # (aio-dl.py, grep '"authors": list(meta'). May be [] for sites that omit
    # authorship, in which case author scoring is skipped end to end.
    src_authors = [a for a in (comic_data.get("authors") or []) if a]

    # Corroboration-gate titles for author-reliable sites (_AUTHOR_GATED_SITES).
    # The SITE title(s) ONLY — never comic_data["alt_names"], which on the
    # refresh path is the poisoned entry's own anilist_synonyms and would fake a
    # primary-title hit (see _match_is_trustworthy_for_site's CRITICAL note).
    # Empty for non-gated sites / titleless input, in which case the gate is a
    # no-op. Cross-file: consumed at every _apply_anilist_match site below.
    gate_titles: List[str] = []
    _seen_gate: set = set()
    for _t in (comic_data.get("title"), _clean_search_title(str(comic_data.get("title") or ""))):
        _t = str(_t or "").strip()
        if _t and _t.lower() not in _seen_gate:
            _seen_gate.add(_t.lower())
            gate_titles.append(_t)

    # Cached-ID fast path (+ self-heal). A cached anilist_id normally means one
    # GraphQL hit and we're done. BUT pre-fix caches can point at the WRONG
    # same-titled series (the Fly-Me-to-the-Moon / Fairy-Tail bug), and a pure
    # by-id fetch would re-apply that wrong series forever. So when the site gave
    # us an author AND it disagrees with the cached entry's staff, we stop
    # trusting the cache: stash the fetched media and fall through to the
    # author-aware search. The search only OVERRIDES the cache if it turns up an
    # author-MATCHED alternative (a genuine fix); otherwise the tail restores the
    # cached entry — so a series whose site merely romanizes the author
    # differently from AniList keeps its id, at the cost of one extra search.
    selfheal_cached_media: Optional[Dict[str, Any]] = None
    if cached_anilist_id and not force_refresh:
        media = _fetch_by_id(int(cached_anilist_id))
        if media:
            cached_author = _author_match_score(src_authors, media)
            if cached_author is None or cached_author >= ANILIST_AUTHOR_MATCH_THRESHOLD:
                if _match_is_trustworthy_for_site(
                    handler_name, media, gate_titles, src_authors
                ):
                    _apply_anilist_match(comic_data, media, tag_min_rank)
                    return comic_data
                # Author-gated site (linewebtoon) whose cached entry matched only
                # on a synonym and whose author doesn't corroborate: distrust the
                # cache. Fall through to search but DO NOT stash it — the tail must
                # not resurrect this poison via the self-heal restore. (Reaches
                # here only when cached_author is None, i.e. the poison entry has
                # no staff; a real author match would have returned above.)
            else:
                # Author disagrees with the cached entry — suspect. Remember it and
                # fall through; the tail decides the cache-vs-search winner.
                selfheal_cached_media = media
        # Stale ID / transient failure / author-suspect: fall through. If the
        # search also yields nothing the tail restores selfheal_cached_media (if
        # any) or leaves comic_data unchanged, and the caller logs accordingly.

    # Search path. Two deliberately decoupled lists:
    #   scoring_titles — the identity set: each source title + its bracket/
    #     tilde-cleaned variant. The match is ALWAYS gated by scoring against
    #     these full forms (75 threshold), so nothing below can admit a wrong
    #     series no matter how broad the search net gets.
    #   search_queries — the broader net used only to FIND candidates: the
    #     scoring titles plus two derived fallbacks per title — the trailing
    #     subtitle segment (_subtitle_segment) and a shortened prefix
    #     (_shortened_prefix). These rescue titles AniList returns nothing for
    #     on the full string: subtitle-prefixed romaji ("...Gaiden: Toaru
    #     Kagaku no Railgun" → id 37776), long romaji with spelling drift
    #     (AnoHana → id 65733), and ~...~/(...)-suffixed titles (Shangri-La
    #     Frontier → id 122063).
    # Per-title order is full → cleaned → subtitle → shortened so precise forms
    # are tried before loose fallbacks; early-stop means the common case (full
    # title matches) never fires a fallback query.
    scoring_titles: List[str] = []
    search_queries: List[str] = []
    seen_scoring = set()
    seen_query = set()

    def _add_scoring(v: str) -> None:
        v = (v or "").strip()
        k = v.lower()
        if v and k not in seen_scoring:
            seen_scoring.add(k)
            scoring_titles.append(v)

    def _add_query(v: str) -> None:
        v = (v or "").strip()
        k = v.lower()
        if v and k not in seen_query:
            seen_query.add(k)
            search_queries.append(v)

    raw_titles: List[str] = []
    if comic_data.get("title"):
        raw_titles.append(str(comic_data["title"]))
    for alt in comic_data.get("alt_names") or []:
        if alt:
            raw_titles.append(str(alt))
    for raw in raw_titles:
        cleaned = _clean_search_title(raw)
        _add_scoring(raw)
        _add_scoring(cleaned)
        _add_query(raw)
        _add_query(cleaned)
        _add_query(_subtitle_segment(cleaned or raw))
        _add_query(_shortened_prefix(cleaned or raw))
    if not scoring_titles:
        # No title to search on. If we bypassed a cached entry for self-heal,
        # restore it rather than dropping enrichment entirely — but still honor
        # the per-site trust gate (a no-op here: gate_titles is empty with no
        # title, so it passes; kept for uniformity should this path gain a title).
        if selfheal_cached_media is not None and _match_is_trustworthy_for_site(
            handler_name, selfheal_cached_media, gate_titles, src_authors
        ):
            _apply_anilist_match(comic_data, selfheal_cached_media, tag_min_rank)
        return comic_data

    # Query AniList with each search query in priority order, accumulating
    # candidates deduped by id; stop early once one clears the threshold when
    # scored against scoring_titles. Bounded to _MAX_SEARCH_QUERIES requests so
    # a series that genuinely isn't on AniList can't fan out into a storm.
    # best_score stays 0.0 across the "no hits" and "hits but all below
    # threshold" branches so the caller's log line is uniform.
    pool: List[Dict[str, Any]] = []
    seen_ids = set()
    best: Optional[Dict[str, Any]] = None
    score = 0.0
    for query in search_queries[:_MAX_SEARCH_QUERIES]:
        for cand in _search_candidates(query):
            cid = cand.get("id")
            if cid is not None and cid in seen_ids:
                continue
            if cid is not None:
                seen_ids.add(cid)
            pool.append(cand)
        if pool:
            best, score = _pick_best_candidate(
                scoring_titles, pool, source_authors=src_authors, year=year
            )
            if best is not None:
                break

    comic_data["_anilist_best_score"] = score

    # Decide what to apply, accounting for the self-heal fallthrough:
    #   - search winner AUTHOR-matches            -> use it (real fix, even when
    #     it overrides a suspect cached id).
    #   - search winner does NOT author-match but
    #     we were self-healing a cached entry     -> keep the CACHED entry (the
    #     site author just doesn't line up with AniList's romanization; re-
    #     picking by title alone could swap a correct cache for a wrong same-
    #     title hit).
    #   - search winner and no cached entry        -> use the winner.
    #   - no search winner                         -> restore the cached entry if
    #     we have one, else leave comic_data untouched.
    chosen: Optional[Dict[str, Any]] = None
    if best is not None:
        if selfheal_cached_media is None:
            chosen = best
        else:
            # Override the suspect cache ONLY when the search winner both
            # author-matches AND is at least as popular as the cached entry. The
            # popularity guard stops a self-heal from downgrading a good, popular
            # cache to an obscure same-franchise entry whose staff coincidentally
            # matches a studio/owner credit: site "Clannad" + author "Key
            # (Company)" author-matches a minor "CLANNAD" (id 149957, pop 364,
            # staff "Key") but the cached "CLANNAD: Official Comic" (id 32598,
            # pop 2290) is the better match — keep it. Real poison always
            # replaces UP in popularity (Fly Me 157566 pop 1669 -> 101177 pop
            # 40474; Fairy Tail doujin -> the real series), so the genuine fixes
            # still apply.
            best_author = _author_match_score(src_authors, best)
            cached_pop = int(selfheal_cached_media.get("popularity") or 0)
            best_pop = int(best.get("popularity") or 0)
            if (
                best_author is not None
                and best_author >= ANILIST_AUTHOR_MATCH_THRESHOLD
                and best_pop >= cached_pop
            ):
                chosen = best
            else:
                chosen = selfheal_cached_media
    elif selfheal_cached_media is not None:
        chosen = selfheal_cached_media

    # Per-site trust gate (author-reliable sites only — currently linewebtoon).
    # The composite ranker + self-heal above optimize for the RIGHT same-title
    # entry, but they never REJECT a title-plausible match; on LINE Webtoon a
    # synonym collision with a disagreeing author (unOrdinary → "Kanojo Iro no
    # Kanojo" via its "Unordinary Life" synonym) is poison, not a match. Drop it
    # here so the caller keeps the site's own metadata. Flag the rejection
    # (transient, underscore-prefixed → writers ignore it) so the caller's log
    # distinguishes "author-gate reject" from "nothing cleared the 75 floor".
    # No-op for every non-gated site: _match_is_trustworthy_for_site returns True.
    if chosen is not None and not _match_is_trustworthy_for_site(
        handler_name, chosen, gate_titles, src_authors
    ):
        comic_data["_anilist_gate_rejected"] = True
        chosen = None

    if chosen is None:
        return comic_data
    _apply_anilist_match(comic_data, chosen, tag_min_rank)
    return comic_data
