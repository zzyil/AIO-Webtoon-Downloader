// ============================================================
// CHAPTER COVERAGE MODEL
//
// One read of a search's `winner_chapter_map` (aio_search_cli.py, built only
// with --multi-source, for the winning series alone) into what the coverage
// views draw: the chapter axis, per-site presence + licensed sets, per-site
// counts and breakdown chips, and run/gap helpers for compact strips.
//
// Read by: components/ChapterCoverage.jsx (CoverageSummary, CoverageGrid),
// components/SearchChapterMap.jsx (Search tab panel) and, from coverage
// plan C3/C4, the Sync tab's Find sources sheet, which stores the same map
// per row (electron/sync/find-sources.js, grep chapterMap). Plan:
// sync-temp/plans/find-sources-coverage.md. Pure; no React.
//
// Map facts the model leans on (sites/chapter_merger.py):
//   - chapters[].sources lists the sites carrying each chapter; a site whose
//     list is EMPTY never appears there, only in merge_diagnostics
//     (skipped_reason "empty chapter list").
//   - a site whose numbering overlaps the anchor < 50 % merges only its
//     overlapping chapters (skipped_reason set): its chapters[] presence is
//     PARTIAL while total/effective count its whole list, so its "missing"
//     cells are not real gaps. `lowMatch` flags it.
//   - the anchor (role "anchor") is the site with the most chapters, not the
//     user's pick; compatibility is measured against it.
//
// THE COUNT BOTH TABS SHOW (user call 2026-10-17, coverage plan User call 4):
// `main` = distinct main chapters (unique_main_chapters: distinct int()
// floors, so 34 and 34.5 are one), `extras` = what the merger's headline
// `effective_chapters` keeps above one entry per main chapter (side stories).
// "282 +3" instead of the old "285 main / 286 entries": every site carrying
// the whole series reads the same main number, and only extras differ. The
// breakdown chips classify the site's RAW list, so a chip can differ from
// `extras` by an entry the aligner folded (weebcentral: "+4 side", extras 3).
// ============================================================

/**
 * One source's breakdown dict → ordered chip descriptors {key, count, sign,
 * label, color, tooltip, strikethrough?}. Buckets are SEMANTIC, not raw —
 * consensus_side_stories and safe_decimals merge into one "side" chip
 * because the user thinks of them as one thing ("extras kept"); the tooltip
 * preserves the confirmation-status distinction. Empty/zero buckets are
 * omitted so clean sources render no chips at all. Order is intentional:
 * kept content first (positive sign), suspicious in middle (?), dropped last
 * (− with strikethrough) — mirrors the "additive vs removed" mental model.
 * Classifier source: sites/chapter_merger.py:_classify_chapter_breakdown.
 */
export function buildBuckets(breakdown, collapseApplied) {
  if (!breakdown) return [];
  const sideConfirmed = breakdown.consensus_side_stories || 0;
  const sideSourceOnly = breakdown.safe_decimals || 0;
  const sideTotal = sideConfirmed + sideSourceOnly;
  const out = [];
  if (sideTotal > 0) {
    let tip;
    if (sideConfirmed === sideTotal) {
      tip = `${sideConfirmed} side stor${sideConfirmed === 1 ? "y" : "ies"} confirmed by peer sources (X.5-style sub-chapters). Kept as content.`;
    } else if (sideSourceOnly === sideTotal) {
      tip = `${sideSourceOnly} side stor${sideSourceOnly === 1 ? "y" : "ies"} this source carries alone (X.5 or higher). Kept — too risky to call duplicates.`;
    } else {
      tip = `${sideConfirmed} peer-confirmed + ${sideSourceOnly} source-only side stor${sideTotal === 1 ? "y" : "ies"}. All kept as content.`;
    }
    out.push({ key: "side", count: sideTotal, sign: "+", label: "side", color: "text-amber-400/80", tooltip: tip });
  }
  if (breakdown.prologue_count > 0) {
    out.push({
      key: "pro",
      count: breakdown.prologue_count,
      sign: "+",
      label: "pro",
      color: "text-cyan-400/80",
      tooltip: `${breakdown.prologue_count} chapter 0 / prologue entr${breakdown.prologue_count === 1 ? "y" : "ies"}. Kept as content.`,
    });
  }
  if (breakdown.source_only_latest > 0) {
    out.push({
      key: "new",
      count: breakdown.source_only_latest,
      sign: "+",
      label: "new",
      color: "text-emerald-400/80",
      tooltip: `${breakdown.source_only_latest} chapter${breakdown.source_only_latest === 1 ? "" : "s"} past the peer-confirmed range (fresh release${breakdown.source_only_latest === 1 ? "" : "s"} other sources haven't caught up to). Kept.`,
    });
  }
  if (breakdown.source_only_orphans > 0) {
    out.push({
      key: "orph",
      count: breakdown.source_only_orphans,
      sign: "?",
      label: "orph",
      color: "text-orange-400/80",
      tooltip: `${breakdown.source_only_orphans} mid-range chapter${breakdown.source_only_orphans === 1 ? "" : "s"} peers don't have. Suspicious (possible re-released duplicates) but kept — could also be legit content.`,
    });
  }
  if (breakdown.merged_split_fragments > 0) {
    out.push({
      key: "merged",
      count: breakdown.merged_split_fragments,
      sign: "+",
      label: "merged",
      color: "text-sky-400/75",
      // Distinct from "drop": these are the extra rows of a no-integer
      // split-cluster (X.1/X.2/X.3 → one chapter). The download path
      // concatenates them into the parent — kept content, not duplicates.
      // Sits right before the drop chip so "+N merged −M drop" reads as
      // the two possible fates of fractional rows.
      tooltip: `${breakdown.merged_split_fragments} split-upload part${breakdown.merged_split_fragments === 1 ? "" : "s"} concatenated into their parent chapter (an X.1/X.2/X.3 cluster with no plain integer X — joined into one downloaded chapter). Kept, not dropped.`,
    });
  }
  if (breakdown.fragments_dropped > 0) {
    out.push({
      key: "drop",
      count: breakdown.fragments_dropped,
      sign: collapseApplied ? "−" : "·",
      label: collapseApplied ? "drop" : "frag",
      // Color tracks collapse state: bright rose when actually dropped,
      // muted rose when surfaced-but-not-dropped (collapse OFF). The
      // user immediately sees whether the count represents a real
      // download-time action or just an advisory tally.
      color: collapseApplied ? "text-rose-400/75" : "text-rose-400/40",
      strikethrough: collapseApplied,
      tooltip: collapseApplied
        ? `${breakdown.fragments_dropped} source-only .1/.2/.3/.4 fragment${breakdown.fragments_dropped === 1 ? "" : "s"} dropped — peers don't carry them, treated as duplicate uploads of the integer parent. Toggle collapse off in Settings to keep them.`
        : `${breakdown.fragments_dropped} source-only .1/.2/.3/.4 fragment${breakdown.fragments_dropped === 1 ? "" : "s"} (collapse is OFF — these are KEPT in the download). Toggle collapse on to drop them as duplicates.`,
    });
  }
  return out;
}

const EMPTY_LIST = "empty chapter list";

// Distinct main chapters of a number list the way the merger counts them
// (_classify_main_chapters: Python int(), which truncates toward zero).
function distinctMain(nums) {
  const floors = new Set();
  for (const n of nums) floors.add(Math.trunc(n));
  return floors.size;
}

/**
 * winner_chapter_map → the coverage model, or null when there is no map
 * (absent, or no `chapters` array).
 *
 * {chapterNums, totalAligned, effectiveAligned, mainAligned, extrasAligned,
 *  collapseApplied, hasInflation, headlineDropped, anchorSite, sites}
 *   chapterNums: every aligned chapter number, ascending (the X axis).
 *   mainAligned / extrasAligned: the header's "282 main +10 extras" (the
 *     header comment's count rule over every aligned number).
 *   sites: alphabetical, every site in chapters[] OR merge_diagnostics:
 *     {site, present:Set, official:Set, ch, off, total, effective, main,
 *      extras, compatibility, buckets, role, lowMatch, empty, skippedReason}
 *     ch/off = present/official sizes; total/effective fall back the way the
 *     Search tab always did (diag total → ch; diag effective → total); main
 *     falls back to the distinct floors of `present`.
 *     A view that must match the Search tab's old list shows only sites with
 *     ch > 0 (empty-list sites were never drawn there).
 */
export function coverageModel(chapterMap) {
  if (!chapterMap || !Array.isArray(chapterMap.chapters)) return null;
  const presence = new Map();
  const officialAt = new Map();
  const numSet = new Set();
  const ensure = (site) => {
    if (!presence.has(site)) {
      presence.set(site, new Set());
      officialAt.set(site, new Set());
    }
  };
  for (const entry of chapterMap.chapters) {
    numSet.add(entry.chapter_num);
    for (const s of entry.sources || []) {
      ensure(s.site);
      presence.get(s.site).add(entry.chapter_num);
      if (s.is_official) officialAt.get(s.site).add(entry.chapter_num);
    }
  }
  const diags = chapterMap.merge_diagnostics || {};
  for (const site of Object.keys(diags)) ensure(site);

  const totalAligned = chapterMap.total_chapters_aligned || chapterMap.chapters.length;
  const collapseApplied = !!chapterMap.collapse_splits_applied;
  const effectiveAligned = chapterMap.effective_chapters_aligned ?? totalAligned;
  let anchorSite = null;
  const sites = [...presence.keys()].sort().map((site) => {
    const present = presence.get(site);
    const official = officialAt.get(site);
    const diag = diags[site] || {};
    const total = diag.total_chapters ?? present.size;
    const skippedReason = diag.skipped_reason || null;
    const empty = skippedReason === EMPTY_LIST || (diag.total_chapters === 0 && present.size === 0);
    if (diag.role === "anchor") anchorSite = site;
    const effective = diag.effective_chapters ?? total;
    const main = diag.unique_main_chapters ?? distinctMain(present);
    return {
      site,
      present,
      official,
      ch: present.size,
      off: official.size,
      total,
      effective,
      main,
      extras: Math.max(0, effective - main),
      compatibility: diag.compatibility,
      buckets: buildBuckets(diag.breakdown, collapseApplied),
      role: diag.role || null,
      lowMatch: !!skippedReason && !empty,
      empty,
      skippedReason,
    };
  });
  const chapterNums = [...numSet].sort((a, b) => a - b);
  const mainAligned = distinctMain(chapterNums);
  return {
    chapterNums,
    totalAligned,
    effectiveAligned,
    mainAligned,
    extrasAligned: Math.max(0, effectiveAligned - mainAligned),
    collapseApplied,
    hasInflation: effectiveAligned !== totalAligned,
    // Aligned entries minus post-refinement effective: what collapse removed
    // (or, with collapse off, would remove) across the whole map.
    headlineDropped: Math.max(0, totalAligned - effectiveAligned),
    anchorSite,
    sites,
  };
}

/** The model's entry for `site`, or null. */
export function siteCoverage(model, site) {
  return (model && model.sites.find((s) => s.site === site)) || null;
}

/**
 * Index runs over model.chapterNums for one site, for a strip drawn as
 * absolutely-positioned segments: [{from, to, official}] with from/to
 * inclusive indices, consecutive present chapters merged while their
 * licensed flag stays the same. Missing chapters are the space between runs.
 */
export function coverageRuns(model, site) {
  const out = [];
  if (!model || !site) return out;
  let cur = null;
  model.chapterNums.forEach((n, i) => {
    if (!site.present.has(n)) {
      cur = null;
      return;
    }
    const official = site.official.has(n);
    if (cur && cur.official === official && cur.to === i - 1) cur.to = i;
    else out.push((cur = { from: i, to: i, official }));
  });
  return out;
}

/**
 * Index runs over model.chapterNums where the site is MISSING: [{from, to}],
 * inclusive. A strip draws these over its "present" fill with a minimum
 * width, because one missing chapter in 300 is under a pixel wide at strip
 * size and would vanish if gaps were only the space between present runs.
 */
export function coverageGaps(model, site) {
  const out = [];
  if (!model || !site) return out;
  let cur = null;
  model.chapterNums.forEach((n, i) => {
    if (site.present.has(n)) {
      cur = null;
      return;
    }
    if (cur && cur.to === i - 1) cur.to = i;
    else out.push((cur = { from: i, to: i }));
  });
  return out;
}

/**
 * One sentence for a site's coverage, the strip's aria-label and tooltip:
 * "285 of 295 chapters, missing 37, 60-61, 81 and 1 more" (no "missing"
 * part when nothing is), "+ 3 licensed" when any are.
 */
export function coverageLabel(model, site) {
  if (!model || !site) return "";
  const n = model.chapterNums.length;
  let out = `${site.ch} of ${n} chapter${n === 1 ? "" : "s"}`;
  const { labels, more } = gapLabels(model, site);
  if (labels.length) out += `, missing ${labels.join(", ")}${more ? ` and ${more} more` : ""}`;
  if (site.off > 0) out += `, ${site.off} licensed`;
  return out;
}

/** The main + extras count's tooltip: "282 main chapters + 3 extras (side stories) · 286 entries listed". */
export function mainCountTitle(site) {
  if (!site) return "";
  let out = `${site.main} main chapter${site.main === 1 ? "" : "s"}`;
  if (site.extras > 0) out += ` + ${site.extras} extra${site.extras === 1 ? "" : "s"} (side stories kept beyond one entry per chapter)`;
  return `${out} · ${site.total} entr${site.total === 1 ? "y" : "ies"} listed`;
}

/**
 * The site's missing chapters as range labels ("37", "60-61"), at most `max`
 * of them, plus how many more ranges there are: {labels, more}. Consecutive
 * missing positions on the chapter axis form one range, so a missing 97 and
 * 97.5 read "97-97.5". Not meaningful for a lowMatch site (its presence is
 * partial by construction); callers show its match % instead.
 */
export function gapLabels(model, site, max = 3) {
  const ranges = [];
  if (!model || !site) return { labels: [], more: 0 };
  let start = null;
  let prev = null;
  for (const n of model.chapterNums) {
    if (site.present.has(n)) {
      if (start != null) ranges.push(start === prev ? String(start) : `${start}-${prev}`);
      start = null;
    } else if (start == null) start = n;
    prev = n;
  }
  if (start != null) ranges.push(start === prev ? String(start) : `${start}-${prev}`);
  return { labels: ranges.slice(0, max), more: Math.max(0, ranges.length - max) };
}
