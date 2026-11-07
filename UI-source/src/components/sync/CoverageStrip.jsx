// ============================================================
// CoverageStrip — one site's chapter coverage as a thin bar (coverage plan
// decision 6, sync-temp/plans/find-sources-coverage.md)
//
// Drawn in the Find sources site list (FindSourcesSheet.jsx) for each site
// the search compared. Its x axis is the whole series (model.chapterNums of
// lib/chapterCoverage.js, the same axis as the Search tab's grid):
//   filled  = the site has the chapter
//   yellow  = it has it as a licensed (official) release
//   notch   = missing; drawn on top with a minimum width, since one missing
//             chapter in 300 is under a pixel at this size (coverageGaps)
// Colours match CoverageGrid's legend (bg-primary/60, bg-yellow-500), so the
// strip and the details grid read the same; not --warning, which means
// "needs a decision" in the Sync tab (.sync-seg-warn).
// Positions are percentages of the axis, so the strip fits any width.
// ============================================================

import React from "react";
import { cn } from "@/lib/utils";
import { coverageGaps, coverageLabel, coverageRuns } from "@/lib/chapterCoverage";

const pct = (i, n) => `${(i / n) * 100}%`;

export default function CoverageStrip({ model, site, className }) {
  const n = model.chapterNums.length;
  const label = coverageLabel(model, site);
  if (!n) return null;
  return (
    <div role="img" aria-label={label} title={label} className={cn("relative h-2 overflow-hidden rounded-[3px] bg-muted", className)}>
      {coverageRuns(model, site).map((r) => (
        <span key={`r${r.from}`} className={cn("absolute inset-y-0", r.official ? "bg-yellow-500" : "bg-primary/60")} style={{ left: pct(r.from, n), width: pct(r.to - r.from + 1, n) }} />
      ))}
      {coverageGaps(model, site).map((g) => (
        <span key={`g${g.from}`} className="absolute inset-y-0 bg-muted" style={{ left: pct(g.from, n), width: `max(2px, ${pct(g.to - g.from + 1, n)})` }} />
      ))}
    </div>
  );
}
