"""Chapter-list alignment across multiple sources.

Given chapter lists from N different sources for the same series, build a
unified chapter map: chapter_number → list of (source_name, chapter_entry)
sorted by source quality. Per the snappy-forging-waffle.md plan, this uses
*strict label matching* — chapter numbers extracted via regex, sources only
merge into the same map when their chapter-number sets agree closely enough
to be confident the merge is correct.

What this does NOT do (yet):
  - phash verification of actual chapter content (Phase 4b). Without phash,
    we can be fooled by chapter renumbering / season-restart series where
    "Ch 47" on source A is genuinely a different work than "Ch 47" on
    source B. Strict label-match catches the easy cases (cleanly-numbered
    licensed series mirrored across aggregators); phash is needed to catch
    the edge cases.
  - Translation between volume-numbered and absolute-numbered editions
    (e.g., One Piece's "Vol 100 Ch 1003" vs "Ch 1003" from another site).
    Sources that use different numbering schemes will NOT align here; they'll
    each appear as single-source-only entries in the map.

Cross-file:
  - Used by aio_search_cli when --multi-source is set with --search. The
    orchestrator passes a list of (handler, chapter_list) per candidate
    source; this module returns the aligned map.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Set, Tuple


# Compiled once. Matches the FIRST decimal-or-integer chapter number in a
# label string. Intentionally not anchored — chapter labels in the wild
# include "Ch 47", "Chapter 47", "047", "Ch.47.5", etc. We just want the
# first numeric token that can act as a chapter ordinal.
_CHAPTER_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")


# Fragment-shaped decimal values: typically partial-upload chunks of the
# integer parent, not canonical sub-chapters. Empirically, sites that emit
# X.Y as a real "Chapter X.5: Side Story" overwhelmingly use Y ∈ {.5..9};
# Y ∈ {.1..4} are almost always upload fragments (the publisher's first-
# pass partial release before the canonical chapter is bundled). The
# cross-source check still gates the drop — if a peer source also has
# X.1, we keep it. So this is a safe-by-default heuristic, not a hard ban.
# Grep target: _is_source_only_fragment, group_chapters_for_download Rule 2.
_FRAGMENT_DECIMAL_VALUES = (0.1, 0.2, 0.3, 0.4)


def _is_fragment_shaped_decimal(num: float, floor: int) -> bool:
    """True iff the decimal portion of ``num`` is one of .1/.2/.3/.4.

    Round to 1 dp to absorb 0.1+0.2-style float drift (same convention as
    _is_sequential_split_decimals). Pure value check — does NOT consult any
    consensus set; combine with _is_source_only_fragment for the gated drop.
    """
    rel = round(num - floor, 1)
    return rel in _FRAGMENT_DECIMAL_VALUES


def _is_source_only_fragment(
    num: float, floor: int, consensus_set: Optional[Set[float]],
) -> bool:
    """Should this decimal be treated as a duplicate fragment?

    True iff the decimal value is fragment-shaped (.1/.2/.3/.4) AND no peer
    source confirms it (it's not in the cross-source consensus). Requires
    a non-empty ``consensus_set`` to fire — when there's no peer signal
    (single-source run, direct URL, or just no overlap among sources) the
    function returns False so the existing in-source heuristics own the
    decision. Used by both ``group_chapters_for_download`` (the actual
    drop site) and ``_classify_chapter_breakdown`` (diagnostic counting),
    keeping the two perfectly in sync.
    """
    if consensus_set is None or not consensus_set:
        return False
    if num in consensus_set:
        return False
    return _is_fragment_shaped_decimal(num, floor)


@dataclass
class ChapterMapEntry:
    """One row of the aligned chapter map: a single chapter number + the list
    of sources that have it.

    `sources` is a list of (site_name, chapter_dict). chapter_dict is the
    handler's native chapter representation (the same dict shape returned by
    handler.get_chapters), so callers can pass it back to handler.get_chapter_images
    without further transformation.
    """
    chapter_num: float
    chapter_label: str  # human-readable label from the first source that had this chapter
    sources: List[Tuple[str, Dict]] = field(default_factory=list)

    def has_site(self, site: str) -> bool:
        return any(s[0] == site for s in self.sources)


@dataclass
class AlignmentResult:
    """Outcome of aligning chapter lists across multiple sources.

    `chapter_map` is the aligned data — each chapter number paired with every
    source that has it. Caller decides per-chapter which source to download
    from based on its own ranking.

    `unmergeable_sources` is the list of sources whose chapter-number sets
    diverged too much from the anchor to merge confidently — they appear in
    the map ONLY for chapters that overlap with the anchor's chapter set.
    Per-source notes about why each source diverged (what fraction of chapters
    overlapped) are in `merge_diagnostics` for the JSON output.

    `collapse_splits_applied` records whether the diagnostic counts in
    `merge_diagnostics` were computed with split-cluster collapse on. Surfaced
    so the UI knows which interpretation the displayed "X main / Y entries"
    reflects (Phase 3 / 2026-05-07). The `chapter_map` itself is structurally
    identical regardless — collapse only affects the diagnostic counts, not
    the per-chapter download alternatives. See `_classify_main_chapters` for
    the collapse rules.

    `consensus_set` is the set of chapter numbers that appear in ≥2 sources
    after alignment. Used by ``group_chapters_for_download`` (download side)
    and ``_classify_chapter_breakdown`` (display side) to identify source-only
    fragment-shaped decimals (.1/.2/.3/.4) for duplicate detection — see the
    Rule 2 refinement and the duplicate-detection-plan in
    ~/.claude/plans/ultrathink-mangafire-and-some-flickering-sparkle.md.
    Empty when only one source contributed (no peer signal); the consumers
    fall through to current single-source heuristics in that case.

    `consensus_max` is the highest chapter number in `consensus_set`, or
    None when consensus is empty. Used to distinguish "source-only latest"
    (legitimate fresh chapters above the peer-confirmed range) from
    "source-only orphan" (suspicious mid-range chapters peers don't have).
    """
    chapter_map: List[ChapterMapEntry]
    merge_diagnostics: Dict[str, Dict] = field(default_factory=dict)
    collapse_splits_applied: bool = True
    consensus_set: Set[float] = field(default_factory=set)
    consensus_max: Optional[float] = None


@dataclass
class ChapterBreakdown:
    """Per-source classification of a chapter list using cross-source consensus
    as ground truth. Computed by ``_classify_chapter_breakdown``; surfaced in
    ``merge_diagnostics[site]['breakdown']`` regardless of collapse state.

    Buckets are MUTUALLY EXCLUSIVE — every chapter falls into exactly one.
    Sum of all fields == count of chapters in this source's get_chapters
    output (including unparseable). The UI renders these as separate sub-
    labels alongside the main count so users can see where the "X main" /
    "Y entries" gap comes from.

    Bucket semantics:
      consensus_main           — integer ≥ 1, present in ≥2 sources (counted toward "main")
      source_only_latest       — integer ≥ 1, > consensus_max, source-only (legit fresh chapter, counted toward "main")
      consensus_side_stories   — fractional > 0, in consensus (peer-confirmed X.5-style side story)
      safe_decimals            — fractional > 0, source-only, value ≥ .5 (kept as side story; cannot confidently call duplicate)
      prologue_count           — chapter num ≤ 0 (chapter 0, negatives — kept but not in main count)
      source_only_orphans      — integer ≥ 1, ≤ consensus_max, NOT in consensus (suspicious re-release; kept but flagged)
      fragments_dropped        — fractional > 0, value ∈ {.1,.2,.3,.4}, NOT in consensus (Rule 2 refinement target: dropped when collapse ON)
      unparseable_passthrough  — _extract_chapter_num returned None (oneshots, omakes by name only)

    Why this dataclass exists alongside _classify_main_chapters:
    `_classify_main_chapters` returns a single ``effective_chapters`` count
    for the back-compat headline; ``ChapterBreakdown`` exposes the components
    so the UI can render "266 main · 16 side stories · 1 prologue · 22
    fragments dropped" instead of a single number. The two functions share
    the same predicates (``_is_source_only_fragment`` etc.) so the displayed
    breakdown is always consistent with what ``group_chapters_for_download``
    will actually emit when the user turns collapse on.

    Cross-file: SearchChapterMap.jsx reads ``breakdown`` from merge_diagnostics
    and renders the bucketed line. aio_search_cli.py serializes the dataclass
    via dataclasses.asdict() into the winner_chapter_map JSON.
    """
    # MAIN — counted toward the "X main" headline
    consensus_main: int = 0
    source_only_latest: int = 0
    # EXTRAS — kept as content, shown in a separate UI sub-label
    consensus_side_stories: int = 0
    safe_decimals: int = 0
    prologue_count: int = 0
    # SUSPICIOUS — kept but flagged
    source_only_orphans: int = 0
    # DROPPED when collapse ON — flagged in UI as "N fragments dropped"
    fragments_dropped: int = 0
    # PASSTHROUGH — non-numeric labels (oneshots, omakes by name only)
    unparseable_passthrough: int = 0


def _is_sequential_split_decimals(decimals_rel: List[float]) -> bool:
    """True iff the decimals form a sequential split starting at .1.

    A "sequential split" is the .1/.2/.3/... pattern that MangaDex (and other
    sources following the same convention) emits when one canonical chapter
    is uploaded as N consecutive fragments. The signature of a true split is
    that it starts at .1 AND has no gaps:

      [0.1, 0.2, 0.3]         → True  (3 sequential fragments)
      [0.1, 0.2, 0.3, 0.4]    → True
      [0.1, 0.5]              → False (gap — .5 is a sub-chapter, not a fragment)
      [0.2, 0.3]              → False (doesn't start at .1 — likely scattered)
      [0.1]                   → False (single decimal; caller handles separately)

    Used by both `_classify_main_chapters` (diagnostic count) and
    `group_chapters_for_download` (Rule 3 / Rule 5 vs Rule 6 fork) so the
    "split fragments collapse" interpretation is applied consistently. Values
    are rounded to 1 decimal place before comparison to absorb float drift
    from `0.1 + 0.2` style arithmetic.
    """
    if len(decimals_rel) < 2:
        return False
    sorted_decimals = sorted(round(d, 1) for d in decimals_rel)
    expected = [round(0.1 * (k + 1), 1) for k in range(len(sorted_decimals))]
    return sorted_decimals == expected


def _classify_main_chapters(
    numbers,
    *,
    collapse_splits: bool = True,
    consensus_set: Optional[Set[float]] = None,
) -> Tuple[int, int]:
    """Compute (unique_main_chapters, effective_chapters) for a chapter-number set.

    `unique_main_chapters` is the count of unique floor() values across the
    source's chapter numbers — a stable measurement of "how many distinct
    main chapters does this source cover" regardless of whether each is one
    entry or split across N decimals. The UI surfaces this as "X main".

    `effective_chapters` is what the UI shows as the headline count, and
    depends on `collapse_splits` AND ``consensus_set``:

      - ``collapse_splits=True`` (default): split-only clusters X.1 / X.2 /
        ... / X.k where the decimals form a sequential .1/.2/.3 pattern
        collapse to ONE chapter (split fragments of the integer parent).
        Scattered decimals like {X, X.1, X.5} keep ONE side-story slot for
        the highest decimal (the canonical "X.5"-style sub-chapter); the
        intermediate .1/.2/.3 fragments are not counted. {X, X.5} alone
        keeps both as effective (Rule 2: true side story).

        This catches both:
          (a) MangaDex/MangaFire-style inflation where chapter 1 is split
              into 1.1/1.2/1.3/1.4 separate rows — without collapse,
              MangaFire's 362-entry catalog for Talentless Nana shows
              alongside atsumaru's 119, misleadingly suggesting atsumaru is
              missing 2/3 of content. With collapse, both report ~119.
          (b) MangaFire's mixed-pattern case (e.g. Kagurabachi Ch 8) where
              {8, 8.1, 8.5} represents Chapter 8 + a partial-upload split
              (.1) + a canonical side-story (.5). The split fragment is
              counted out; .5 is preserved as a side story.

      - ``collapse_splits=False``: equals ``len(numbers)``. The toggle exists
        because some series legitimately use sequential decimal numbering
        (webnovel adaptations, manhwa with episodic season boundaries) where
        1.5 is a real distinct chapter, not a side story or split. Collapse
        would falsely merge those.

      - ``consensus_set`` (NEW 2026-05-27, cross-source duplicate detection):
        when provided AND collapse is on, refines Rule 2 / 3b / 6 by dropping
        fragment-shaped decimals (.1/.2/.3/.4) that no peer source confirms.
        Mirrors the actual drops in ``group_chapters_for_download`` so the
        displayed count matches the downloaded count. When ``consensus_set``
        is None or empty (single-source run / direct URL), the function
        falls through to in-source heuristics — same numbers as today.

    Examples (collapse_splits=True, consensus_set=None):
      {1, 2, 3}                 → (3, 3)   — all integers, no decimals
      {1.1, 1.2, 1.3, 1.4}      → (1, 1)   — split-cluster, no integer parent
      {4, 4.5}                  → (1, 2)   — main + side story (Rule 2)
      {1, 2, 2.5, 3}            → (3, 4)   — three mains + one side
      {1.1, 1.2, 2, 3}          → (3, 3)   — 1.x splits collapse, then 2 + 3
      {8, 8.1, 8.2, 8.3}        → (1, 1)   — sequential splits collapse
      {8, 8.1, 8.5}             → (1, 2)   — scattered: integer + highest .5

    Examples (collapse_splits=True, consensus_set={52}):
      {52, 52.1}                → (1, 1)   — Rule 2 refined: 52.1 is source-only fragment
      {52, 52.5}                → (1, 2)   — Rule 2 unchanged: .5 not fragment-shaped

    Examples (collapse_splits=False):
      Each example above yields (unique_main, len(numbers)) instead.

    Cross-file: align_chapter_lists() invokes this for each source's
    chapter-number set when populating merge_diagnostics; the results power
    the SearchChapterMap.jsx "X main / Y entries" display.
    `group_chapters_for_download` applies the same sub-classification to
    decide what to actually emit per floor.
    """
    if not numbers:
        return 0, 0
    if not collapse_splits:
        return len({int(n) for n in numbers}), len(numbers)

    by_floor: Dict[int, List[float]] = {}
    for n in numbers:
        by_floor.setdefault(int(n), []).append(n)

    unique_main = len(by_floor)
    effective = 0
    for floor, group in by_floor.items():
        integer_present = any(n == floor for n in group)
        decimal_values = sorted(n for n in group if n != floor)
        decimals_rel = [n - floor for n in decimal_values]
        if integer_present:
            effective += 1  # the integer
            if len(decimal_values) == 1:
                # Rule 2 refined: drop fragment-shaped source-only decimal
                # (the .1 in {52, 52.1} when peers don't have 52.1). The
                # check matches group_chapters_for_download's Rule 2 branch.
                if not _is_source_only_fragment(
                    decimal_values[0], floor, consensus_set,
                ):
                    effective += 1  # Rule 2 kept: true side story
            elif len(decimal_values) >= 2 and not _is_sequential_split_decimals(decimals_rel):
                # Rule 3b scattered: highest decimal is a canonical side
                # story IF not a source-only fragment. Mirrors the Rule 3b
                # refinement in group_chapters_for_download.
                highest = decimal_values[-1]
                if not _is_source_only_fragment(highest, floor, consensus_set):
                    effective += 1
            # else: Rule 3a sequential splits — decimals contribute nothing.
        else:
            # Rule 4/5/6: no integer.
            if len(decimal_values) == 1:
                # Rule 4: singleton partial — always kept (the only entry).
                effective += 1
            elif _is_sequential_split_decimals(decimals_rel):
                # Rule 5: sequential split-cluster collapses to one chapter.
                effective += 1
            else:
                # Rule 6 refined: highest decimal kept UNLESS it's a source-
                # only fragment AND the integer parent is in consensus
                # (peer says floor is the canonical chapter). Mirrors the
                # Rule 6 refinement in group_chapters_for_download.
                highest = decimal_values[-1]
                parent_in_consensus = float(floor) in (consensus_set or set())
                if not (
                    parent_in_consensus
                    and _is_source_only_fragment(highest, floor, consensus_set)
                ):
                    effective += 1
    return unique_main, effective


def _classify_chapter_breakdown(
    chapters: List[Dict],
    consensus_set: Optional[Set[float]],
    consensus_max: Optional[float],
) -> ChapterBreakdown:
    """Route each chapter dict into exactly one ChapterBreakdown bucket.

    Mirrors the predicates in ``group_chapters_for_download`` (same
    fragment-shape and consensus checks) so the displayed bucket counts
    always match what the download path actually emits when collapse is on.

    When ``consensus_set`` is empty / None (single-source run or no peer
    overlap), there's no signal: integers go to ``consensus_main`` (we
    can't downgrade them), decimals ≥ .5 go to ``safe_decimals``, fragment-
    shaped decimals also go to ``safe_decimals`` (not ``fragments_dropped``)
    because the download path won't drop them either without peer signal.

    Note: this operates on the post-handler-dedup chapter dicts (one entry
    per chapter number). align_chapter_lists deduplicates per source before
    calling this, so we don't see the same number twice from one source.
    """
    bd = ChapterBreakdown()
    has_consensus = bool(consensus_set)
    for ch in chapters:
        num = _extract_chapter_num(ch.get("chap"))
        if num is None:
            bd.unparseable_passthrough += 1
            continue
        if num <= 0:
            bd.prologue_count += 1
            continue
        floor = int(num)
        is_integer = (num == floor)
        if is_integer:
            if has_consensus and num in consensus_set:
                bd.consensus_main += 1
            elif has_consensus and consensus_max is not None and num > consensus_max:
                bd.source_only_latest += 1
            elif has_consensus:
                # Peer signal exists but this integer is mid-range and source-only.
                bd.source_only_orphans += 1
            else:
                # No peer signal — treat as main (can't downgrade without data).
                bd.consensus_main += 1
        else:
            # Fractional > 0
            if has_consensus and num in consensus_set:
                bd.consensus_side_stories += 1
            elif _is_source_only_fragment(num, floor, consensus_set):
                # Fragment-shaped (.1-.4), source-only, peer signal exists →
                # would be dropped by the download path when collapse ON.
                bd.fragments_dropped += 1
            else:
                # Either no peer signal, or decimal ≥ .5 — kept as safe extra.
                bd.safe_decimals += 1
    return bd


def _extract_chapter_num(label) -> Optional[float]:
    """Return the first numeric token in a chapter label as a float, or None.

    Handles:
      - "Chapter 47" → 47.0
      - "Ch 47.5" → 47.5
      - "047" → 47.0
      - "Vol 5 Ch 47" → 5.0 (NOT 47.0 — we take the FIRST token; this is a
        known limitation that biases volume-numbered series to use volume as
        chapter ordinal. Strict label-match treats this as expected behavior:
        sources that differ on volume-vs-absolute will simply not align, and
        the user gets independent per-source listings).
      - non-numeric labels (oneshot, omake, side-story-1.5) → None for
        non-numeric, .5 chapters → 0.5
      - None / empty → None
    """
    if label is None:
        return None
    if isinstance(label, (int, float)):
        try:
            return float(label)
        except (ValueError, TypeError):
            return None
    s = str(label).strip()
    if not s:
        return None
    m = _CHAPTER_NUM_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


# Threshold for declaring a source "compatible" with the anchor for merge
# purposes. If the fraction of source's chapters that have a numeric match in
# the anchor's chapter-number set is below this, we consider the source's
# numbering system incompatible (e.g., season-restart series where source B
# numbers from 1 again while source A continues from 60+) and don't merge it
# beyond the overlapping range.
DEFAULT_COMPATIBILITY_THRESHOLD = 0.5


def align_chapter_lists(
    sources_with_chapters: List[Tuple[str, List[Dict]]],
    *,
    compatibility_threshold: float = DEFAULT_COMPATIBILITY_THRESHOLD,
    collapse_splits: bool = True,
) -> AlignmentResult:
    """Align chapter lists across multiple sources via strict label-match.

    Args:
      sources_with_chapters: list of (site_name, chapter_dicts) tuples.
        chapter_dicts is whatever handler.get_chapters returns — typically
        a list of dicts with at least a 'chap' key (chapter label/number).
        Source order is the orchestrator's quality ranking (best first), but
        we pick the anchor as the source with the MOST chapters since the
        top-ranked source can be incomplete (DMCA-affected MangaDex with 1
        chapter, or get_chapters parse failures returning 0 entries).
      compatibility_threshold: minimum fraction of source's chapters that
        must overlap with the anchor's chapter-number set for a full merge.
        Sources below this threshold are partially merged (they appear in
        the map ONLY for chapter numbers that match anchor entries).
      collapse_splits: controls how the diagnostic counts are reported.
        When True (default), per-source `effective_chapters` collapses
        split-cluster decimals (X.1/X.2/X.3 with no X) to ONE main chapter
        each. When False, `effective_chapters == total_chapters`. See
        `_classify_main_chapters` for the full rules. The chapter_map's
        structure does NOT depend on this flag — collapse only affects the
        displayed counts, not the per-chapter download alternatives.

    Returns:
      AlignmentResult with chapter_map (list of ChapterMapEntry sorted by
      chapter_num ascending), merge_diagnostics per source, and a
      `collapse_splits_applied` field reflecting the input flag.

    The anchor source's chapter set is the canonical chapter list. Other
    sources contribute to existing entries when their chapter numbers match.
    Numbers present only in the anchor → single-source entries.
    Numbers present only in non-anchor sources → orphan entries (only added
    if the source's overall compatibility is above threshold).

    See snappy-forging-waffle.md for the design rationale (Phase 4 plan).
    """
    if not sources_with_chapters:
        return AlignmentResult(
            chapter_map=[],
            merge_diagnostics={},
            collapse_splits_applied=collapse_splits,
            consensus_set=set(),
            consensus_max=None,
        )

    # Pick anchor by largest chapter set, breaking ties by orchestrator order
    # (lower index = higher quality rank). A source returning 0 chapters
    # (handler parse failure, DMCA hollowing) cannot be a useful anchor.
    def _anchor_priority(idx_and_pair):
        idx, (_, chs) = idx_and_pair
        return (-len(chs or []), idx)

    indexed = list(enumerate(sources_with_chapters))
    indexed.sort(key=_anchor_priority)
    anchor_idx, (anchor_site, anchor_chapters) = indexed[0]

    # Reorder so anchor is first; the rest stay in original orchestrator order.
    reordered = [sources_with_chapters[anchor_idx]] + [
        sources_with_chapters[i] for i in range(len(sources_with_chapters)) if i != anchor_idx
    ]
    sources_with_chapters = reordered
    anchor_site, anchor_chapters = sources_with_chapters[0]

    # Build per-source {chapter_num → chapter_dict} maps in a single upfront
    # pass. Per-site dedup (keep first occurrence) matches the original loop
    # behavior — multiple chapter entries with the same number on the same
    # site (e.g. different scanlator versions on MangaDex) collapse to the
    # first. Doing this upfront lets us compute consensus_set AFTER the
    # cross-source merge but BEFORE writing diagnostics, so each source's
    # breakdown is graded against the full peer set.
    per_source_nums: Dict[str, Dict[float, Dict]] = {}
    for site_name, chapters in sources_with_chapters:
        source_nums: Dict[float, Dict] = {}
        for ch in (chapters or []):
            num = _extract_chapter_num(ch.get("chap"))
            if num is None:
                continue
            if num in source_nums:
                continue
            source_nums[num] = ch
        per_source_nums[site_name] = source_nums

    # Seed anchor_index from the anchor's per-source map.
    anchor_index: Dict[float, ChapterMapEntry] = {}
    for num, ch in per_source_nums[anchor_site].items():
        label = str(ch.get("chap") or "").strip() or f"{num:g}"
        anchor_index[num] = ChapterMapEntry(
            chapter_num=num,
            chapter_label=label,
            sources=[(anchor_site, ch)],
        )

    # Track per-source overlap / compatibility / skipped_reason now;
    # _classify_main_chapters + breakdown computation is deferred until
    # AFTER consensus is known so the diagnostic counts incorporate it.
    per_source_meta: Dict[str, Dict] = {
        anchor_site: {
            "overlap": len(anchor_index),
            "compatibility": 1.0,
            "role": "anchor",
            "skipped_reason": None,
        }
    }

    # Process remaining sources: compute overlap, merge into anchor_index.
    for site_name, _chapters in sources_with_chapters[1:]:
        source_nums = per_source_nums[site_name]
        if not source_nums:
            per_source_meta[site_name] = {
                "overlap": 0,
                "compatibility": 0.0,
                "role": "alternative",
                "skipped_reason": "empty chapter list",
            }
            continue

        overlap = len([n for n in source_nums if n in anchor_index])
        compatibility = overlap / len(source_nums)
        merge_orphans = compatibility >= compatibility_threshold
        per_source_meta[site_name] = {
            "overlap": overlap,
            "compatibility": compatibility,
            "role": "alternative",
            "skipped_reason": (
                None if merge_orphans else (
                    f"compatibility {compatibility:.0%} below "
                    f"{compatibility_threshold:.0%} threshold; only "
                    "overlapping chapters merged"
                )
            ),
        }

        # Always merge overlapping chapters — definitionally a safe match
        # per number. Non-overlapping chapters are added as orphan entries
        # ONLY if compatibility is above threshold, signaling the source
        # uses the same numbering scheme.
        for num, ch in source_nums.items():
            if num in anchor_index:
                anchor_index[num].sources.append((site_name, ch))
            elif merge_orphans:
                label = str(ch.get("chap") or "").strip() or f"{num:g}"
                anchor_index[num] = ChapterMapEntry(
                    chapter_num=num,
                    chapter_label=label,
                    sources=[(site_name, ch)],
                )

    # Compute cross-source consensus from the fully-populated anchor_index.
    # A chapter number is "in consensus" iff ≥2 sources contributed it.
    # Used by _classify_main_chapters / _classify_chapter_breakdown (display
    # side) AND group_chapters_for_download (download side) to identify
    # source-only fragment-shaped decimals for duplicate detection. See
    # plan: ~/.claude/plans/ultrathink-mangafire-and-some-flickering-sparkle.md.
    source_counts: Dict[float, int] = {}
    for entry in anchor_index.values():
        source_counts[entry.chapter_num] = len(entry.sources)
    consensus_set: Set[float] = {n for n, c in source_counts.items() if c >= 2}
    consensus_max: Optional[float] = max(consensus_set) if consensus_set else None

    # Now compute per-source diagnostics with consensus in hand. The
    # _classify_main_chapters call mirrors the consensus-refined drops in
    # group_chapters_for_download, so the displayed "X main / Y entries"
    # always matches what the download path emits when collapse is on.
    diagnostics: Dict[str, Dict] = {}
    for site_name, meta in per_source_meta.items():
        source_nums = per_source_nums[site_name]
        unique_main, effective = _classify_main_chapters(
            list(source_nums.keys()),
            collapse_splits=collapse_splits,
            consensus_set=consensus_set,
        )
        breakdown = _classify_chapter_breakdown(
            list(source_nums.values()),
            consensus_set=consensus_set,
            consensus_max=consensus_max,
        )
        diag: Dict = {
            "role": meta["role"],
            "total_chapters": len(source_nums),
            "unique_main_chapters": unique_main,
            "effective_chapters": effective,
            "matched_with_anchor": meta["overlap"],
            "compatibility": round(meta["compatibility"], 3),
            "breakdown": asdict(breakdown),
            "consensus_threshold_sources": 2,
        }
        if meta["skipped_reason"]:
            diag["skipped_reason"] = meta["skipped_reason"]
        diagnostics[site_name] = diag

    # Within each chapter row, prefer official-tagged sources. Stable sort
    # preserves the orchestrator's quality ranking for ties (both within
    # the official group and within the non-official group). `is_official`
    # is a chapter-dict field populated by handlers that match against
    # sites/official_publishers.json (currently mangadex.py via
    # _publishers.lookup_publisher; other handlers leave the field unset
    # and naturally fall to the non-official bucket). Catches the
    # licensed-but-DMCA-hollowed case where the few chapters MangaDex
    # retains are the official MangaPlus translation — they should rank
    # above fan scans of the same chapter number even when MangaDex's
    # measured img_quality is lower than the fan source's.
    for entry in anchor_index.values():
        entry.sources.sort(
            key=lambda s: 0 if (s[1].get("is_official") is True) else 1
        )

    # Sort the map by chapter number ascending.
    chapter_map = sorted(anchor_index.values(), key=lambda e: e.chapter_num)
    return AlignmentResult(
        chapter_map=chapter_map,
        merge_diagnostics=diagnostics,
        collapse_splits_applied=collapse_splits,
        consensus_set=consensus_set,
        consensus_max=consensus_max,
    )


@dataclass
class ChapterGroup:
    """A unit for the download loop. Either a singleton (one source chapter,
    label = its original) or a combined cluster (multiple decimal parts with
    no integer parent — label = the integer floor, parts = ordered decimals
    whose images get concatenated in download order).

    For the duplicate-prune cases (rule 3 / rule 6 in
    `group_chapters_for_download`), `parts` contains exactly one entry — the
    one we kept — and the others are dropped. The caller (aio-dl.py download
    loop) treats `len(parts) == 1` identically to today's per-chapter call,
    using `label` as the output filename label so {1.5, 1.6} renders as
    `Title Ch 1.pdf` rather than `Title Ch 1~5.pdf`.
    """
    label: str
    parts: List[Dict] = field(default_factory=list)


def _format_chapter_label(num: float) -> str:
    """Render a chapter number for human-readable output. Integer-valued
    floats lose their decimal (1.0 → "1"); fractional values render as
    decimals (1.5 → "1.5"). Trailing zeros stripped.
    """
    if num == int(num):
        return str(int(num))
    s = f"{num:.10f}".rstrip("0").rstrip(".")
    return s


def group_chapters_for_download(
    chapters: List[Dict],
    *,
    collapse_splits: bool = True,
    consensus_set: Optional[Set[float]] = None,
) -> List[ChapterGroup]:
    """Apply the cluster rule (snappy-forging-waffle.md item 8) to produce
    download-ready groups.

    For each integer floor X across the chapter list (extracted via
    `_extract_chapter_num`), partition entries into the integer entry
    (chap == X) and decimal entries (X.k, k > 0):

        Rule 1. Integer X only, no decimals
              → 1 group: ChapterGroup(label="X", parts=[X])
        Rule 2. Integer X + 1 decimal X.k (e.g. {1, 1.5})
              → 2 groups: ChapterGroup(label="X", parts=[X])
                          ChapterGroup(label="X.k", parts=[X.k])
                (true partial preserved)
                Refinement (consensus_set): if k ∈ {.1,.2,.3,.4} AND X.k is
                NOT in consensus_set AND consensus_set is non-empty, drop
                X.k as a source-only fragment. Catches the user's
                Shangri-La Frontier case where mangafire emits {52, 52.1}
                but peers only have {52} — the .1 is an upload fragment,
                not a real "Chapter 52.5: Side Story"-style sub-chapter.
                See ~/.claude/plans/ultrathink-mangafire-and-some-flickering-sparkle.md.
        Rule 3. Integer X + ≥2 decimals — sub-classified:
              3a (sequential .1/.2/.3...): e.g. {1, 1.1, 1.2, 1.3}
                → 1 group: ChapterGroup(label="X", parts=[X])
                  (decimals are MangaDex-style split fragments of X; dropped)
              3b (scattered, e.g. {8, 8.1, 8.5}):
                → 2 groups: ChapterGroup(label="X", parts=[X])
                            ChapterGroup(label="X.k_max", parts=[highest decimal])
                  (the highest decimal is a canonical sub-chapter like
                  MangaFire's "X.5: Side Story"; intermediate .1/.2/etc. are
                  partial-upload fragments and dropped)
                  Refinement (consensus_set): if the kept "highest" is
                  itself a source-only fragment-shape (.1/.2/.3/.4), drop
                  it too — peer evidence says it's noise. In practice
                  this is rare since "scattered" means there ARE
                  intermediate decimals, so the highest is usually .5+.
        Rule 4. No integer X, 1 decimal X.k
              → 1 group: ChapterGroup(label="X.k", parts=[X.k])
        Rule 5. No integer X, decimals form .1, .2, .3, ... starting at .1
              → 1 group: ChapterGroup(label="X", parts=[X.1, X.2, X.3, ...])
                (split-cluster: caller concatenates image_items)
        Rule 6. No integer X, decimals scattered or not starting at .1
                (e.g. {1.5, 1.6}, {1.5, 1.1}, {1.2, 1.3})
              → 1 group: ChapterGroup(label="X.k_max", parts=[highest decimal])
                (treat as duplicate partial uploads; emit the canonical one.
                Label uses the actual decimal value rather than the integer
                floor so the on-disk tdir doesn't collide with a real
                Chapter X from another source on resume.)
                Refinement (consensus_set): if highest is source-only
                fragment-shape AND the integer floor IS in consensus
                (peer source has the canonical chapter X), drop the
                whole cluster — it's fragment noise relative to the
                peer-confirmed integer. If floor is NOT in consensus,
                keep current behavior — we have no peer signal that
                floor is the canonical chapter, so the cluster might be
                its own legitimate sub-chapter.

    Why "highest decimal" in Rules 3b and 6:
        MangaFire (and similar aggregators) sometimes emit a chapter as
        both an early partial upload (X.1, X.2, ...) AND the eventual
        canonical sub-chapter (X.5 — the publisher's actual designation
        like "Chapter 8.5: Side Story"). The user-facing chapter to keep
        is the canonical one, which by convention is the higher decimal.
        Empirically: Kagurabachi on MangaFire returns {8, 8.1, 8.5},
        {58, 58.1, 58.5}, etc.; keeping the .5 surfaces the side story
        that other sources cleanly expose as a single ".5" entry.

    When `collapse_splits=False`, returns one group per chapter with its
    original label preserved (passthrough — no merging, no dropping).
    The ``consensus_set`` argument is also ignored in this mode — no
    drops happen regardless.

    Unparseable chapters (label has no numeric token) are emitted unchanged
    at the end of the list as singleton groups — they can't be bucketed but
    shouldn't be silently dropped.

    Cross-file: caller is aio-dl.py's chapter download loop. For groups
    where `len(parts) > 1` (rule 5 only), the caller synthesizes a combined
    chapter dict by concatenating `get_chapter_images(part)` across parts
    and uses `group.label` for the output filename. ``consensus_set`` is
    plumbed in from align_chapter_lists via the multi-source-prefetched
    payload (aio_search_cli.py builds it; aio-dl.py reads it on the way
    into this function). When peer data is unavailable (direct URL mode,
    single-source run), ``consensus_set=None`` falls through to the
    original in-source-only heuristics — no regressions.
    """
    if not chapters:
        return []
    if not collapse_splits:
        return [
            ChapterGroup(label=str(ch.get("chap") or ""), parts=[ch])
            for ch in chapters
        ]

    # Bucket by floor; unparseable chapters tracked separately so they ride
    # through to the output without being dropped.
    by_floor: Dict[int, List[Tuple[float, Dict]]] = {}
    unparseable: List[Dict] = []
    for ch in chapters:
        num = _extract_chapter_num(ch.get("chap"))
        if num is None:
            unparseable.append(ch)
            continue
        floor = int(num)
        by_floor.setdefault(floor, []).append((num, ch))

    groups: List[ChapterGroup] = []
    for floor in sorted(by_floor.keys()):
        entries = sorted(by_floor[floor], key=lambda x: x[0])
        integer_entries = [e for e in entries if e[0] == floor]
        decimal_entries = [e for e in entries if e[0] != floor]

        if integer_entries:
            # Use the first integer entry if duplicates exist (handlers
            # occasionally return chapter 1 twice from different scanlators).
            integer_ch = integer_entries[0][1]
            if len(decimal_entries) >= 2:
                # Rule 3: sub-classify the decimals.
                # _is_sequential_split_decimals returns True only when the
                # decimals form a contiguous .1/.2/.3... sequence — the
                # MangaDex-style split-fragment signature where the
                # decimals are PARTS of the integer chapter, not separate
                # sub-chapters. Round to 1 decimal place inside the helper
                # to absorb 0.1+0.2 float drift.
                decimals_rel = [e[0] - floor for e in decimal_entries]
                if _is_sequential_split_decimals(decimals_rel):
                    # Rule 3a: sequential splits — drop all decimals.
                    groups.append(ChapterGroup(
                        label=str(floor), parts=[integer_ch],
                    ))
                else:
                    # Rule 3b refined: keep integer X always; keep highest
                    # decimal UNLESS it's itself a source-only fragment-
                    # shape (.1-.4). The intermediate decimals are always
                    # dropped (existing behavior). When consensus_set is
                    # None, _is_source_only_fragment returns False so the
                    # behavior matches the original "always keep highest".
                    groups.append(ChapterGroup(
                        label=str(floor), parts=[integer_ch],
                    ))
                    highest_num, highest_ch = decimal_entries[-1]
                    if not _is_source_only_fragment(
                        highest_num, floor, consensus_set,
                    ):
                        groups.append(ChapterGroup(
                            label=_format_chapter_label(highest_num),
                            parts=[highest_ch],
                        ))
                    # else: highest is a source-only fragment — dropped.
            elif len(decimal_entries) == 1:
                # Rule 2 refined: emit X always; emit the decimal UNLESS
                # it's a source-only fragment-shape (.1-.4). This is the
                # critical fix for mangafire's {52, 52.1} / {75, 75.1}
                # etc. on Shangri-La Frontier where peers only have
                # {52}, {75} (no peer .1). When consensus_set is None or
                # empty, _is_source_only_fragment returns False so behavior
                # matches the original "always emit both". Single .5+
                # decimals are NEVER dropped here — they're overwhelmingly
                # real "X.5: Side Story"-style canonical sub-chapters,
                # so we treat them conservatively as content.
                groups.append(ChapterGroup(label=str(floor), parts=[integer_ch]))
                num, ch = decimal_entries[0]
                if not _is_source_only_fragment(num, floor, consensus_set):
                    groups.append(ChapterGroup(
                        label=_format_chapter_label(num), parts=[ch],
                    ))
                # else: lone source-only fragment — dropped.
            else:
                # Rule 1: just X.
                groups.append(ChapterGroup(label=str(floor), parts=[integer_ch]))
        else:
            # No integer X.
            if len(decimal_entries) == 1:
                # Rule 4: singleton partial. Always kept — the only entry
                # at this floor, dropping it would lose content with no
                # signal that it's a duplicate. Left untouched by the
                # consensus refinement.
                num, ch = decimal_entries[0]
                groups.append(ChapterGroup(
                    label=_format_chapter_label(num), parts=[ch],
                ))
            else:
                # Rules 5 or 6 — distinguish by sub-index shape via the
                # shared helper. See its docstring for the signature of
                # a true sequential split.
                decimals_rel = [e[0] - floor for e in decimal_entries]
                if _is_sequential_split_decimals(decimals_rel):
                    # Rule 5: sequential split-cluster — combine all parts.
                    groups.append(ChapterGroup(
                        label=str(floor),
                        parts=[e[1] for e in decimal_entries],
                    ))
                else:
                    # Rule 6 refined: keep the HIGHEST decimal UNLESS it's
                    # a source-only fragment-shape AND the implied integer
                    # parent (floor) is in consensus. The parent-in-
                    # consensus guard distinguishes "fragment noise around
                    # a peer-confirmed canonical chapter" (drop) from
                    # "scattered decimals at a floor no peer has either"
                    # (keep — it might be its own legitimate sub-chapter
                    # with no integer counterpart).
                    #
                    # The on-disk-tdir-collision rationale for using the
                    # decimal as the label (not the floor) still applies:
                    # main_tmp_dir/ch_<floor> would match both this rule-6
                    # group and a real X from a separate source on resume.
                    highest_num, highest_ch = decimal_entries[-1]  # ascending sort
                    parent_in_consensus = (
                        consensus_set is not None
                        and float(floor) in consensus_set
                    )
                    drop_as_fragment_noise = (
                        parent_in_consensus
                        and _is_source_only_fragment(
                            highest_num, floor, consensus_set,
                        )
                    )
                    if not drop_as_fragment_noise:
                        groups.append(ChapterGroup(
                            label=_format_chapter_label(highest_num),
                            parts=[highest_ch],
                        ))
                    # else: peer-confirmed parent exists; this scattered
                    # cluster of source-only fragments is noise — dropped.

    # Pass unparseable chapters through as singletons rather than dropping
    # them — these are typically "Oneshot" / "Omake" / non-numeric labels
    # that the existing _CHAPTER_NUM_RE can't extract.
    for ch in unparseable:
        groups.append(ChapterGroup(
            label=str(ch.get("chap") or "?"),
            parts=[ch],
        ))

    return groups


__all__ = [
    "ChapterMapEntry",
    "AlignmentResult",
    "ChapterBreakdown",
    "align_chapter_lists",
    "DEFAULT_COMPATIBILITY_THRESHOLD",
    "ChapterGroup",
    "group_chapters_for_download",
]
