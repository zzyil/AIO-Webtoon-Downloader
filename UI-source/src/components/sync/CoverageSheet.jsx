// ============================================================
// CoverageSheet — "Coverage details" for one Find sources row (coverage plan
// C4 = mockup B, sync-temp/plans/find-sources-coverage.md decision 5)
//
// A second sheet over FindSourcesSheet with the Search tab's full coverage
// panel for the row's match: the map-wide counts, a "Download from" radio
// list (pick here too), the per-site summary + chips and the site × chapter
// grid (components/ChapterCoverage.jsx, the same pieces SearchChapterMap
// draws, so both tabs read one renderer).
//
// Rendered by FindSourcesSheet as a SIBLING of its <Sheet>, never inside it:
// this sheet's own scrim then catches an outside click (closing only this
// one), and overlay.jsx's STACK sends Escape and the Tab trap to whichever
// sheet opened last. Its lifecycle returns focus to what held it on open,
// the row's "Coverage details" link. Width = the parent's minus 48 px, so a
// sliver of Find sources stays visible behind it.
//
// The radios patch the same runner row as the site list (onPatch({pickUrl}),
// FindSourcesSheet's patch), under their own `name` so the two radio groups
// never share a group while both are mounted. The row comes from the
// sheet's live rows, so a pick made here shows in the list on close.
// Consumer: FindSourcesSheet.jsx. Depends on: findView.js, lib/chapterCoverage.js.
// ============================================================

import React, { useEffect, useId, useMemo } from "react";
import { Sheet } from "@/components/ui/overlay";
import { cn } from "@/lib/utils";
import { coverageModel } from "@/lib/chapterCoverage";
import { CoverageGrid, CoverageHeadline, CoverageSummary } from "@/components/ChapterCoverage";
import { ratingText, ratingTitle, rowCandidate, rowCoverage, rowSites, unmeasured } from "./findView";

/**
 * @param {object} p
 * @param {object|null} p.row   the findView row, or null = closed
 * @param {number} p.width      the parent sheet's width (this one is 48 px narrower)
 * @param {boolean} p.busy      a row patch or the queue is in flight
 * @param {(patch:object) => any} p.onPatch  FindSourcesSheet's patch for this row
 */
export default function CoverageSheet({ row, width, contained, busy, onClose, onPatch }) {
  const cov = row ? rowCoverage(row) : null;
  const map = cov && cov.kind === "map" ? cov.map : null;
  const model = useMemo(() => (map ? coverageModel(map) : null), [map]);
  // Drawn like the Search tab: only sites with a chapter in the aligned list
  // (an empty list, or a low match with no overlap, has nothing to draw).
  const drawn = useMemo(() => (model ? model.sites.filter((s) => s.ch > 0) : []), [model]);
  // A re-search that lands without a map has nothing to show: the sheet hides
  // at once and tells the parent, so a later map can't reopen it unasked. (A
  // row that disappears leaves the parent's state alone: the DEV gallery
  // opens this before its rows arrive.)
  const open = !!model;
  useEffect(() => {
    if (row && !model) onClose();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row, model]);
  const cand = row ? rowCandidate(row) : null;
  const title = (cand && cand.title) || row?.folder || "";
  const sites = row && model ? rowSites(row) : [];
  // Per instance (see FindSourcesSheet's panelId): never shares a group with
  // the row's list or with another copy of this sheet.
  const name = `coverage-sites-${useId()}`;
  return (
    <Sheet
      open={open}
      onClose={onClose}
      width={Math.max(320, width - 48)}
      contained={contained}
      aria-label={`Chapter coverage for ${title}`}
      title={
        <span className="flex min-w-0 flex-col">
          <span className="text-[11px] font-normal text-muted-foreground">Chapter coverage</span>
          <span className="truncate" title={title}>
            {title}
            {cand && cand.year ? <span className="font-normal text-muted-foreground"> ({cand.year})</span> : null}
          </span>
        </span>
      }
      description={
        model && (
          <span className="flex flex-wrap items-baseline gap-x-1">
            <span>Across {drawn.length} site{drawn.length === 1 ? "" : "s"}</span>
            <CoverageHeadline model={model} />
            {model.anchorSite && (
              <span title="The site listing the most chapters. Each site's match % is measured against its numbering.">
                · compared against <span className="font-mono">{model.anchorSite}</span>
              </span>
            )}
          </span>
        )
      }
    >
      {model && (
        <>
          <div className="mb-4">
            <div className="mb-1 text-[11px] font-medium text-muted-foreground" id={`${name}-label`}>
              Download from
            </div>
            <div role="radiogroup" aria-labelledby={`${name}-label`} className="flex flex-col rounded-md border border-border/70 bg-muted/30 p-1">
              {sites.map(({ source, picked, fallback }) => {
                const warn = unmeasured(source);
                return (
                  <label key={source.url} className={cn("flex min-w-0 cursor-pointer items-center gap-2 rounded px-2 py-1 text-xs hover:bg-accent", picked && "bg-primary/[.06]")} title={source.url}>
                    <input type="radio" name={name} className="accent-primary" checked={picked} disabled={busy} onChange={() => onPatch({ pickUrl: source.url })} />
                    <span className="w-28 shrink-0 truncate font-mono">{source.site}</span>
                    <span className={cn("w-20 shrink-0 truncate text-[11px] text-muted-foreground", warn ? "italic" : "font-mono")} title={ratingTitle(source)}>
                      {ratingText(source)}
                    </span>
                    <span className={cn("ml-auto text-[11px]", picked ? "font-medium text-primary" : "text-muted-foreground")}>{picked ? "downloads" : fallback ? `fallback ${fallback}` : ""}</span>
                  </label>
                );
              })}
            </div>
            {row.multiSource === false && sites.length > 1 && <div className="mt-1 text-[11px] text-muted-foreground">Fallback sites were off for this search, so only the picked site downloads.</div>}
          </div>
          <CoverageSummary model={model} sites={drawn} />
          <CoverageGrid model={model} sites={drawn} />
        </>
      )}
    </Sheet>
  );
}
