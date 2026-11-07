// ============================================================
// CHAPTER COVERAGE VIEWS
//
// The header counts and two bodies of the Search tab's "Chapter coverage"
// panel, split out so the Sync tab's Find sources coverage sheet (coverage
// plan C4, sync-temp/plans/find-sources-coverage.md, sync/CoverageSheet.jsx)
// draws the same thing:
//   CoverageHeadline "· 282 main +10 extras · −3 dropped" (map-wide)
//   CoverageSummary  per-site counts ("282 main +3", licensed, match %) +
//                    breakdown chips, then the chip legend
//   CoverageGrid     the site × chapter heatmap, then its legend
//   MainCount        the "282 +3" count alone, also used by the Find
//                    sources site list (sync/FindSourcesSheet.jsx)
// All take the model from lib/chapterCoverage.js; the bodies also take the `sites` to draw
// (the caller filters: the Search tab drops sites with no chapters).
// Cells: gold = is_official (licensed publisher), primary = available,
// muted = absent. Chip colors by bucket kind (grep buildBuckets in
// lib/chapterCoverage.js):
//   amber  = side stories       (X.5+ kept as content)
//   cyan   = prologue           (chapter 0 / negatives)
//   emerald = source-only latest (above peer max — fresh releases)
//   sky    = merged split parts (X.1/X.2/X.3 with no integer X → one chapter)
//   orange = source-only orphans (mid-range, suspicious but kept)
//   rose   = fragments dropped  (.1/.2/.3/.4 source-only duplicates)
// The markup is the pre-split SearchChapterMap's except the per-site count
// (main + extras, lib/chapterCoverage.js header); the oracle test
// tools/_test_chapter_coverage.js renders both and compares the HTML around it.
// ============================================================

import React from "react";
import { cn } from "@/lib/utils";
import { mainCountTitle } from "@/lib/chapterCoverage";

// Render-helper for a single bucket chip. Baseline-aligned dual-
// typography pattern: bright sign+count, half-opacity micro-label.
// Lower-case font-mono labels stay aligned with the row's existing
// "main / entries" text. The strikethrough on dropped chips uses a
// thin decoration so the count is still readable; the line-through
// is a semantic hint, not a censor.
export function BucketChip({ chip }) {
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

/**
 * A site's "282 +3": distinct main chapters, then the extras in the side-
 * story chip colour when there are any (amber-600 on light, where the
 * chips' amber-400 is too faint for a number read on its own). `label` adds
 * " main" after the number (the Search tab's summary row). Tooltip:
 * mainCountTitle.
 */
export function MainCount({ site, label, className }) {
  return (
    <span className={cn("tabular-nums", className)} title={mainCountTitle(site)}>
      <span className="text-foreground">{site.main}</span>
      {label && <span className="text-muted-foreground"> main</span>}
      {site.extras > 0 && <span className="font-mono text-amber-600 dark:text-amber-400/80"> +{site.extras}</span>}
    </span>
  );
}

/**
 * The map-wide counts, "· 282 main +10 extras · −3 dropped": the count rule
 * of lib/chapterCoverage.js (header) over every aligned number, the same one
 * each site's row uses. headlineDropped is what collapse removed across the
 * map, muted when collapse is off (informational: NOT removed from the
 * download). Inline spans for a header line: the Search tab panel's button
 * and the Find sources coverage sheet's description (sync/CoverageSheet.jsx).
 * The oracle test blanks the first span by its class list
 * (tools/_test_chapter_coverage.js, grep HEADCOUNT): keep it.
 */
export function CoverageHeadline({ model }) {
  const { totalAligned, mainAligned, extrasAligned, collapseApplied, headlineDropped } = model;
  return (
    <>
      <span
        className="text-xs text-muted-foreground tabular-nums"
        title={`${mainAligned} distinct main chapters (34 and 34.5 count once) and ${extrasAligned} side stor${extrasAligned === 1 ? "y" : "ies"} kept, out of ${totalAligned} chapter numbers any source lists.`}
      >
        · {mainAligned} main
        {extrasAligned > 0 && <span className="text-amber-600 dark:text-amber-400/80"> +{extrasAligned} extras</span>}
      </span>
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
    </>
  );
}

/**
 * Per-site summary rows + the breakdown legend. The count is main + extras
 * (MainCount), the number to compare across sources for parity.
 */
export function CoverageSummary({ model, sites }) {
  const { collapseApplied } = model;
  return (
    <>
      <div className="grid gap-1.5 mb-3" style={{ gridTemplateColumns: "120px 1fr" }}>
        {sites.map((s) => {
          const { site, off, compatibility, buckets } = s;
          return (
            <React.Fragment key={site}>
              <div className="flex items-center gap-1.5 min-w-0">
                <span className="font-mono text-[11px] truncate">{site}</span>
              </div>
              <div className="flex items-center gap-2 text-[10px] tabular-nums flex-wrap">
                <MainCount site={s} label />
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
                {/* Breakdown chips: only when the backend supplied a populated
                    ChapterBreakdown for this source. The vertical separator
                    uses border + height instead of a "·" character so the
                    break between the counts and the chips is deliberate. */}
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

      {/* Bucket legend — only when any drawn source has chips: one place to
          learn the chip colors without hovering every chip. */}
      {sites.some((s) => s.buckets.length > 0) && (
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
            <span className="inline-block w-1.5 h-1.5 rounded-sm bg-sky-400/70" />
            <span>merged splits</span>
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
    </>
  );
}

/**
 * Heatmap grid — site rows × chapter columns, then the cell legend. Each
 * cell ~6px wide so a 1200-chapter series fits in ~7000px (horizontal
 * scroll in a contained div); typical 100-300 chapter series fit as is.
 */
export function CoverageGrid({ model, sites }) {
  const { chapterNums } = model;
  return (
    <>
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
            {sites.map(({ site, present, official }) => (
              <tr key={site} className="border-b border-border/40 last:border-0">
                <td className="sticky left-0 z-10 bg-background px-2 py-1.5 font-mono text-foreground">
                  {site}
                </td>
                {chapterNums.map((n) => {
                  const has = present.has(n);
                  const isOfficial = official.has(n);
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
    </>
  );
}
