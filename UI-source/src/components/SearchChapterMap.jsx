// ============================================================
// SEARCH CHAPTER MAP
//
// Visualizes winner_chapter_map (only present when --multi-source
// is on). Shows a thin grid: chapter numbers across X, sources
// down Y. Cells colored by status:
//   gold    = is_official (licensed publisher)
//   primary = available
//   muted   = absent
//
// Lets the user spot DMCA gaps + licensed-translation coverage
// at a glance. Power feature — collapsed by default in SearchTab.
//
// Per-source breakdown chips (2026-05-27): when the backend supplies
// merge_diagnostics[site].breakdown (sites/chapter_merger.py:
// ChapterBreakdown dataclass), each row renders a strip of micro-chips
// after the main/entries summary, color-coded by bucket kind:
//   amber  = side stories       (X.5+ kept as content)
//   cyan   = prologue           (chapter 0 / negatives)
//   emerald = source-only latest (above peer max — fresh releases)
//   orange = source-only orphans (mid-range, suspicious but kept)
//   rose   = fragments dropped  (.1/.2/.3/.4 source-only duplicates)
// Grep target: buildBuckets in this file; classifier source in
// sites/chapter_merger.py:_classify_chapter_breakdown.
// ============================================================

import React, { useMemo, useState } from "react";
import { cn } from "@/lib/utils";
import { ChevronDown, Info } from "lucide-react";

// Convert one source's breakdown dict into an ordered list of chip
// descriptors. Buckets are SEMANTIC, not raw — consensus_side_stories
// and safe_decimals merge into one "side" chip because the user thinks
// of them as one thing ("extras kept"); the tooltip preserves the
// confirmation-status distinction. Empty/zero buckets are omitted so
// clean sources render no chips at all. Order is intentional: kept
// content first (positive sign), suspicious in middle (?), dropped
// last (− with strikethrough) — mirrors the "additive vs removed"
// mental model.
function buildBuckets(breakdown, collapseApplied) {
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

// Render-helper for a single bucket chip. Baseline-aligned dual-
// typography pattern: bright sign+count, half-opacity micro-label.
// Lower-case font-mono labels stay aligned with the row's existing
// "main / entries" text. The strikethrough on dropped chips uses a
// thin decoration so the count is still readable; the line-through
// is a semantic hint, not a censor.
function BucketChip({ chip }) {
  return (
    <span
      title={chip.tooltip}
      className={cn(
        "tabular-nums font-mono inline-flex items-baseline gap-0.5 cursor-help",
        chip.color,
        chip.strikethrough && "line-through decoration-1 decoration-rose-400/40",
      )}
    >
      <span>{chip.sign}{chip.count}</span>
      <span className="opacity-50 text-[8.5px] not-italic tracking-tight">{chip.label}</span>
    </span>
  );
}

export default function SearchChapterMap({ chapterMap }) {
  const [expanded, setExpanded] = useState(false);

  // Pre-compute the matrix: site -> Set<chapter_num>, plus per-chapter
  // is_official site mapping. This walks the JSON once on mount; cheap
  // for series with <2000 chapters (One Piece is the worst case at ~1250).
  const { sites, chapterNums, presence, officialAt, totalAligned } = useMemo(() => {
    if (!chapterMap?.chapters) {
      return { sites: [], chapterNums: [], presence: {}, officialAt: {}, totalAligned: 0 };
    }
    const siteSet = new Set();
    const presence = {};   // site -> Set<chapter_num>
    const officialAt = {}; // site -> Set<chapter_num where is_official=true>
    const numSet = new Set();
    for (const entry of chapterMap.chapters) {
      numSet.add(entry.chapter_num);
      for (const s of entry.sources || []) {
        siteSet.add(s.site);
        if (!presence[s.site]) presence[s.site] = new Set();
        presence[s.site].add(entry.chapter_num);
        if (s.is_official) {
          if (!officialAt[s.site]) officialAt[s.site] = new Set();
          officialAt[s.site].add(entry.chapter_num);
        }
      }
    }
    return {
      sites: [...siteSet].sort(),
      chapterNums: [...numSet].sort((a, b) => a - b),
      presence,
      officialAt,
      totalAligned: chapterMap.total_chapters_aligned || chapterMap.chapters.length,
    };
  }, [chapterMap]);

  if (!chapterMap || sites.length === 0) return null;

  // Phase 3: Aggregate "main chapters" count surfaced when collapse is on
  // AND the count differs from raw aligned entries — visually flags the
  // inflation. Backend computes this at chapter_map build time via
  // _classify_main_chapters; we just read the field. Falls back to
  // totalAligned when the field is absent (older payloads / collapse off
  // / no inflation detected).
  const collapseApplied = !!chapterMap.collapse_splits_applied;
  const effectiveAligned = chapterMap.effective_chapters_aligned ?? totalAligned;
  const hasInflation = effectiveAligned !== totalAligned;

  // Per-site stats for the row label. Helps the user see which source has
  // the deepest catalog at a glance without expanding. Phase 3 enriches
  // this with effective_chapters from merge_diagnostics so MangaFire-style
  // 362-vs-119 inflation is visible without reading the heatmap. 2026-05-27
  // additionally extracts the per-source ChapterBreakdown buckets (built by
  // sites/chapter_merger.py:_classify_chapter_breakdown) into chip
  // descriptors via buildBuckets(). Sources without a breakdown (older
  // payloads, single-source runs with no consensus) yield buckets=[] which
  // renders nothing — no visual difference from the previous behavior.
  const siteStats = sites.map((site) => {
    const ch = presence[site]?.size || 0;
    const off = officialAt[site]?.size || 0;
    const diag = chapterMap.merge_diagnostics?.[site] || {};
    const totalChapters = diag.total_chapters ?? ch;
    const effective = diag.effective_chapters ?? totalChapters;
    const compatibility = diag.compatibility;
    const buckets = buildBuckets(diag.breakdown, collapseApplied);
    return { site, ch, totalChapters, effective, off, compatibility, buckets };
  });

  // Headline-level dropped tally: difference between aligned-entries and
  // post-refinement-effective. Surfaces what's removed at the aggregate
  // level so the user sees "22 dropped" at the header without expanding.
  // When collapse is off, the same count is shown muted (informational —
  // these chapters are NOT actually removed from the download).
  const headlineDropped = Math.max(0, totalAligned - effectiveAligned);

  return (
    <div className="rounded-lg border bg-card/50 overflow-hidden">
      <button
        onClick={() => setExpanded((v) => !v)}
        className={cn(
          "flex items-center justify-between w-full px-4 py-2.5 text-left",
          "hover:bg-accent/30 transition-colors",
        )}
      >
        <div className="flex items-center gap-2">
          <Info className="w-4 h-4 text-muted-foreground" />
          <span className="text-sm font-medium">
            Chapter coverage across {sites.length} sources
          </span>
          {hasInflation ? (
            <span
              className="text-xs text-muted-foreground tabular-nums"
              title={
                collapseApplied
                  ? "Aggregator catalogs include split-chapter rows (e.g. 1.1, 1.2) and source-only fragment uploads. The 'main' count post-collapses both; 'entries' is the raw row count from the deepest source."
                  : "Series uses decimal numbering — collapse is off, so main and entries diverge naturally."
              }
            >
              · {effectiveAligned} main / {totalAligned} entries
            </span>
          ) : (
            <span className="text-xs text-muted-foreground tabular-nums">
              · {totalAligned} chapters aligned
            </span>
          )}
          {headlineDropped > 0 && (
            <span
              className={cn(
                "text-[10px] font-mono tabular-nums tracking-tight",
                collapseApplied ? "text-rose-400/70" : "text-rose-400/35",
              )}
              title={
                collapseApplied
                  ? `${headlineDropped} duplicate fragment${headlineDropped === 1 ? "" : "s"} removed across all sources (source-only .1/.2/.3/.4 uploads). Expand the panel to see which source each came from.`
                  : `${headlineDropped} duplicate fragment${headlineDropped === 1 ? "" : "s"} detected but KEPT (collapse is off). Turn collapse on in Settings to drop them.`
              }
            >
              · <span className={collapseApplied ? "line-through decoration-1 decoration-rose-400/40" : ""}>−{headlineDropped}</span>{" "}
              <span className="opacity-60 text-[9px]">{collapseApplied ? "dropped" : "frag"}</span>
            </span>
          )}
        </div>
        <ChevronDown
          className={cn("w-4 h-4 transition-transform", expanded && "rotate-180")}
        />
      </button>

      {expanded && (
        <div className="px-4 pb-4 pt-2 border-t bg-background/50 animate-slide-up">
          {/* Per-site summary row.
              When the source has split-chapter inflation (effective < totalChapters
              under collapse, OR raw entries > effective in any case), show
              "X main / Y entries"; otherwise just "X chapters". The "main"
              count is what the user should compare across sources for parity. */}
          <div className="grid gap-1.5 mb-3" style={{ gridTemplateColumns: "120px 1fr" }}>
            {siteStats.map(({ site, ch, totalChapters, effective, off, compatibility, buckets }) => {
              const showInflation = effective !== totalChapters;
              return (
                <React.Fragment key={site}>
                  <div className="flex items-center gap-1.5 min-w-0">
                    <span className="font-mono text-[11px] truncate">{site}</span>
                  </div>
                  <div className="flex items-center gap-2 text-[10px] tabular-nums flex-wrap">
                    {showInflation ? (
                      <span
                        className="text-muted-foreground"
                        title={`Source returned ${totalChapters} entries; collapsed to ${effective} main chapters for parity with other sources.`}
                      >
                        <span className="text-foreground">{effective}</span>
                        <span className="text-muted-foreground"> main / {totalChapters} entries</span>
                      </span>
                    ) : (
                      <span className="text-muted-foreground">
                        {ch}/{totalAligned} chapters
                      </span>
                    )}
                    {off > 0 && (
                      <span className="text-yellow-600 dark:text-yellow-400">
                        · {off} licensed
                      </span>
                    )}
                    {compatibility != null && compatibility < 1 && (
                      <span className="text-muted-foreground">
                        · {Math.round(compatibility * 100)}% match
                      </span>
                    )}
                    {/* Breakdown chips: only rendered when the backend
                        supplied a populated ChapterBreakdown for this source.
                        The vertical separator uses border + height instead
                        of a "·" character so the visual break between
                        existing diag and the new chips is more deliberate.
                        Each chip carries a tooltip that explains its bucket;
                        the dropped-chip color/strikethrough additionally
                        encodes the collapse state. */}
                    {buckets.length > 0 && (
                      <>
                        <span
                          aria-hidden="true"
                          className="inline-block w-px h-2.5 bg-border/60 mx-0.5"
                        />
                        {buckets.map((chip) => (
                          <BucketChip key={chip.key} chip={chip} />
                        ))}
                      </>
                    )}
                  </div>
                </React.Fragment>
              );
            })}
          </div>

          {/* Bucket legend — only render when any source has chips. The
              legend is a thin row of muted swatches that gives the user
              one place to learn what the chip colors mean without having
              to hover every chip individually. Mirrors the heatmap
              legend below so visually they read as one cohesive
              "what you're looking at" footer. */}
          {siteStats.some((s) => s.buckets.length > 0) && (
            <div className="flex items-center flex-wrap gap-x-3 gap-y-1 mb-3 px-0.5 text-[9.5px] font-mono text-muted-foreground/60">
              <span className="opacity-60">breakdown:</span>
              <span className="inline-flex items-center gap-1">
                <span className="inline-block w-1.5 h-1.5 rounded-sm bg-amber-400/70" />
                <span>side stories</span>
              </span>
              <span className="inline-flex items-center gap-1">
                <span className="inline-block w-1.5 h-1.5 rounded-sm bg-cyan-400/70" />
                <span>prologue</span>
              </span>
              <span className="inline-flex items-center gap-1">
                <span className="inline-block w-1.5 h-1.5 rounded-sm bg-emerald-400/70" />
                <span>ahead of peers</span>
              </span>
              <span className="inline-flex items-center gap-1">
                <span className="inline-block w-1.5 h-1.5 rounded-sm bg-orange-400/70" />
                <span>orphans</span>
              </span>
              <span className="inline-flex items-center gap-1">
                <span className="inline-block w-1.5 h-1.5 rounded-sm bg-rose-400/70" />
                <span>
                  {collapseApplied ? "duplicates dropped" : "duplicates (kept)"}
                </span>
              </span>
            </div>
          )}

          {/* Heatmap grid — site rows × chapter columns. Each cell ~6px
              wide so a 1200-chapter series fits in ~7000px (horizontal
              scroll in a contained div). For typical 100-300 chapter
              series, fits the viewport easily. */}
          <div className="rounded border bg-background overflow-x-auto">
            <table className="text-[9px] tabular-nums">
              <thead>
                <tr className="border-b">
                  <th className="sticky left-0 z-10 bg-background px-2 py-1 text-left font-mono text-muted-foreground">
                    site
                  </th>
                  {chapterNums.map((n) => (
                    <th key={n} className="px-0 py-0 w-[6px] font-normal text-muted-foreground" />
                  ))}
                </tr>
              </thead>
              <tbody>
                {sites.map((site) => (
                  <tr key={site} className="border-b border-border/40 last:border-0">
                    <td className="sticky left-0 z-10 bg-background px-2 py-1.5 font-mono text-foreground">
                      {site}
                    </td>
                    {chapterNums.map((n) => {
                      const has = presence[site]?.has(n);
                      const isOfficial = officialAt[site]?.has(n);
                      const cell = isOfficial
                        ? "bg-yellow-500"
                        : has
                          ? "bg-primary/60"
                          : "bg-muted/30";
                      return (
                        <td
                          key={n}
                          className={cn("h-3 w-[6px]", cell)}
                          title={
                            `${site} · Ch ${n}` +
                            (isOfficial ? " · official" : has ? " · available" : " · missing")
                          }
                        />
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Legend */}
          <div className="flex items-center gap-4 mt-3 text-[10px] text-muted-foreground">
            <div className="flex items-center gap-1.5">
              <div className="w-3 h-3 rounded-sm bg-yellow-500" />
              <span>Licensed (official translation)</span>
            </div>
            <div className="flex items-center gap-1.5">
              <div className="w-3 h-3 rounded-sm bg-primary/60" />
              <span>Available</span>
            </div>
            <div className="flex items-center gap-1.5">
              <div className="w-3 h-3 rounded-sm bg-muted/30 border border-border" />
              <span>Missing</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
