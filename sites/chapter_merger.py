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
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# Compiled once. Matches the FIRST decimal-or-integer chapter number in a
# label string. Intentionally not anchored — chapter labels in the wild
# include "Ch 47", "Chapter 47", "047", "Ch.47.5", etc. We just want the
# first numeric token that can act as a chapter ordinal.
_CHAPTER_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")


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
    """
    chapter_map: List[ChapterMapEntry]
    merge_diagnostics: Dict[str, Dict] = field(default_factory=dict)
    collapse_splits_applied: bool = True


def _classify_main_chapters(numbers, *, collapse_splits: bool = True) -> Tuple[int, int]:
    """Compute (unique_main_chapters, effective_chapters) for a chapter-number set.

    `unique_main_chapters` is the count of unique floor() values across the
    source's chapter numbers — a stable measurement of "how many distinct
    main chapters does this source cover" regardless of whether each is one
    entry or split across N decimals. The UI surfaces this as "X main".

    `effective_chapters` is what the UI shows as the headline count, and
    depends on `collapse_splits`:

      - ``collapse_splits=True`` (default): split-only clusters X.1 / X.2 /
        ... / X.k where the integer X is *absent* collapse to ONE main
        chapter (treat them as parts of the missing main). X.5 alongside an
        existing X stays distinct (treated as a side story). Catches the
        MangaFire-style inflation where chapter 1 is split into 1.1/1.2/1.3/
        1.4 separate rows — without collapse, MangaFire's 362-entry catalog
        for Talentless Nana shows alongside atsumaru's 119, misleadingly
        suggesting atsumaru is missing 2/3 of content. With collapse, both
        report ~119 main chapters.

      - ``collapse_splits=False``: equals ``len(numbers)``. The toggle exists
        because some series legitimately use sequential decimal numbering
        (webnovel adaptations, manhwa with episodic season boundaries) where
        1.5 is a real distinct chapter, not a side story or split. Collapse
        would falsely merge those.

    Examples (collapse_splits=True):
      {1, 2, 3}            → (3, 3)   — all integers, no decimals
      {1.1, 1.2, 1.3, 1.4} → (1, 1)   — split-cluster, no integer parent
      {4, 4.5}             → (1, 2)   — main + side story
      {1, 2, 2.5, 3}       → (3, 4)   — three mains + one side
      {1.1, 1.2, 2, 3}     → (3, 3)   — 1.x splits collapse, then 2 + 3

    Examples (collapse_splits=False):
      Each example above yields (unique_main, len(numbers)) instead.

    Cross-file: align_chapter_lists() invokes this for each source's
    chapter-number set when populating merge_diagnostics; the results power
    the SearchChapterMap.jsx "X main / Y entries" display.
    """
    if not numbers:
        return 0, 0
    floors = {int(n) for n in numbers}
    unique_main = len(floors)
    if not collapse_splits:
        return unique_main, len(numbers)
    integers_present = {int(n) for n in numbers if n == int(n)}
    side_stories = sum(
        1 for n in numbers if n != int(n) and int(n) in integers_present
    )
    return unique_main, unique_main + side_stories


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
    anchor_index: Dict[float, ChapterMapEntry] = {}
    for ch in anchor_chapters or []:
        num = _extract_chapter_num(ch.get("chap"))
        if num is None:
            continue
        if num in anchor_index:
            # Multiple chapter entries with the same number on the same site
            # (rare but happens — different scanlator versions of the same
            # chapter on MangaDex). Keep the first; subsequent ones are
            # ignored at the merger level. Per-site dedup happened upstream.
            continue
        label = str(ch.get("chap") or "").strip() or f"{num:g}"
        anchor_index[num] = ChapterMapEntry(
            chapter_num=num,
            chapter_label=label,
            sources=[(anchor_site, ch)],
        )

    # Phase 2/3 diagnostic enrichment: compute (unique_main_chapters,
    # effective_chapters) for the anchor as well so the UI can render its
    # "X main / Y entries" badge consistently across all rows. The anchor's
    # chapter-number set is anchor_index.keys() — by construction every
    # number is unique because anchor_index dedupes by chapter number.
    anchor_unique_main, anchor_effective = _classify_main_chapters(
        list(anchor_index.keys()), collapse_splits=collapse_splits
    )
    diagnostics: Dict[str, Dict] = {
        anchor_site: {
            "role": "anchor",
            "total_chapters": len(anchor_index),
            "unique_main_chapters": anchor_unique_main,
            "effective_chapters": anchor_effective,
            "matched_with_anchor": len(anchor_index),
            "compatibility": 1.0,
        }
    }

    # Process remaining sources.
    for site_name, chapters in sources_with_chapters[1:]:
        if not chapters:
            diagnostics[site_name] = {
                "role": "alternative",
                "total_chapters": 0,
                "unique_main_chapters": 0,
                "effective_chapters": 0,
                "matched_with_anchor": 0,
                "compatibility": 0.0,
                "skipped_reason": "empty chapter list",
            }
            continue

        # Index this source's chapters by extracted number.
        source_nums: Dict[float, Dict] = {}
        for ch in chapters:
            num = _extract_chapter_num(ch.get("chap"))
            if num is None:
                continue
            if num in source_nums:
                continue  # keep first occurrence
            source_nums[num] = ch

        # How many of this source's chapters have a number that exists in the
        # anchor? Determines whether the source's numbering system is
        # compatible enough for a full merge.
        if source_nums:
            overlap = len([n for n in source_nums if n in anchor_index])
            compatibility = overlap / len(source_nums)
        else:
            overlap = 0
            compatibility = 0.0

        # Phase 2/3 enrichment — see anchor block above for rationale.
        unique_main, effective = _classify_main_chapters(
            list(source_nums.keys()), collapse_splits=collapse_splits
        )
        diagnostics[site_name] = {
            "role": "alternative",
            "total_chapters": len(source_nums),
            "unique_main_chapters": unique_main,
            "effective_chapters": effective,
            "matched_with_anchor": overlap,
            "compatibility": round(compatibility, 3),
        }

        # Always merge the overlapping chapters — they're definitionally a
        # safe match per number. Non-overlapping chapters from this source are
        # added as orphan entries ONLY if compatibility is high enough,
        # signaling the source is using the same numbering scheme.
        merge_orphans = compatibility >= compatibility_threshold
        if not merge_orphans:
            diagnostics[site_name]["skipped_reason"] = (
                f"compatibility {compatibility:.0%} below {compatibility_threshold:.0%} "
                "threshold; only overlapping chapters merged"
            )

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
        Rule 3. Integer X + ≥2 decimals (e.g. {1, 1.1, 1.2, 1.3})
              → 1 group: ChapterGroup(label="X", parts=[X])
                (decimals are redundant duplicate uploads of X; dropped)
        Rule 4. No integer X, 1 decimal X.k
              → 1 group: ChapterGroup(label="X.k", parts=[X.k])
        Rule 5. No integer X, decimals form .1, .2, .3, ... starting at .1
              → 1 group: ChapterGroup(label="X", parts=[X.1, X.2, X.3, ...])
                (split-cluster: caller concatenates image_items)
        Rule 6. No integer X, decimals scattered or not starting at .1
                (e.g. {1.5, 1.6}, {1.5, 1.1}, {1.2, 1.3})
              → 1 group: ChapterGroup(label="X.k_min", parts=[lowest decimal])
                (treat as duplicate partial uploads; emit one. Label uses
                the actual decimal value rather than the integer floor so
                the on-disk tdir doesn't collide with a real Chapter X
                from another source on resume.)

    When `collapse_splits=False`, returns one group per chapter with its
    original label preserved (passthrough — no merging, no dropping).

    Unparseable chapters (label has no numeric token) are emitted unchanged
    at the end of the list as singleton groups — they can't be bucketed but
    shouldn't be silently dropped.

    Cross-file: caller is aio-dl.py's chapter download loop. For groups
    where `len(parts) > 1` (rule 5 only), the caller synthesizes a combined
    chapter dict by concatenating `get_chapter_images(part)` across parts
    and uses `group.label` for the output filename.
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
                # Rule 3: drop splits, emit only X.
                groups.append(ChapterGroup(label=str(floor), parts=[integer_ch]))
            elif len(decimal_entries) == 1:
                # Rule 2: emit both X and the partial.
                groups.append(ChapterGroup(label=str(floor), parts=[integer_ch]))
                num, ch = decimal_entries[0]
                groups.append(ChapterGroup(
                    label=_format_chapter_label(num), parts=[ch],
                ))
            else:
                # Rule 1: just X.
                groups.append(ChapterGroup(label=str(floor), parts=[integer_ch]))
        else:
            # No integer X.
            if len(decimal_entries) == 1:
                # Rule 4: singleton partial.
                num, ch = decimal_entries[0]
                groups.append(ChapterGroup(
                    label=_format_chapter_label(num), parts=[ch],
                ))
            else:
                # Rules 5 or 6 — distinguish by sub-index shape.
                # Round to 1 decimal place to handle float drift (0.1+0.2 cases).
                sorted_decimals = sorted(round(e[0] - floor, 1) for e in decimal_entries)
                expected = [round(0.1 * (k + 1), 1) for k in range(len(sorted_decimals))]
                if sorted_decimals == expected:
                    # Rule 5: sequential split-cluster — combine all parts.
                    groups.append(ChapterGroup(
                        label=str(floor),
                        parts=[e[1] for e in decimal_entries],
                    ))
                else:
                    # Rule 6: scattered decimals = duplicate partials.
                    # Keep only the lowest (first by sort). Label uses the
                    # actual decimal value (e.g. "1.5") rather than the
                    # integer floor — labelling as floor risks tdir collision
                    # on resume when another source provides a real Chapter
                    # X. With label="X", `main_tmp_dir/ch_X` would match
                    # both the rule-6 group and a real X from a separate
                    # source on subsequent runs, falsely treating one as
                    # the resume target for the other. Using the actual
                    # decimal keeps the on-disk identity unique.
                    lowest_num, lowest_ch = decimal_entries[0]  # already sorted ascending
                    groups.append(ChapterGroup(
                        label=_format_chapter_label(lowest_num),
                        parts=[lowest_ch],
                    ))

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
    "align_chapter_lists",
    "DEFAULT_COMPATIBILITY_THRESHOLD",
    "ChapterGroup",
    "group_chapters_for_download",
]
