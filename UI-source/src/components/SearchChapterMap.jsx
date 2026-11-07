// ============================================================
// SEARCH CHAPTER MAP
//
// The Search tab's collapsible "Chapter coverage" panel for a search's
// winner_chapter_map (only present when --multi-source is on): a header
// with the aligned main/extras/dropped counts, and, expanded, the per-site
// summary + chips and the site × chapter heatmap. Lets the user spot DMCA
// gaps + licensed-translation coverage at a glance. Power feature —
// collapsed by default in SearchTab.
//
// The map is read by lib/chapterCoverage.js (coverageModel, buildBuckets)
// and the header counts and bodies are components/ChapterCoverage.jsx
// (CoverageHeadline, CoverageSummary, CoverageGrid), shared with the Sync tab's Find sources coverage sheet
// (coverage plan, sync-temp/plans/find-sources-coverage.md). Sites with no
// chapter in chapters[] (an empty list, or a low match with no overlap)
// exist only in merge_diagnostics and are not drawn here, as before the split.
// ============================================================

import React, { useMemo, useState } from "react";
import { cn } from "@/lib/utils";
import { ChevronDown, Info } from "lucide-react";
import { coverageModel } from "@/lib/chapterCoverage";
import { CoverageHeadline, CoverageSummary, CoverageGrid } from "@/components/ChapterCoverage";

export default function SearchChapterMap({ chapterMap }) {
  const [expanded, setExpanded] = useState(false);

  // One walk of the JSON per map; cheap for series with <2000 chapters
  // (One Piece is the worst case at ~1250).
  const model = useMemo(() => coverageModel(chapterMap), [chapterMap]);
  const sites = useMemo(() => (model ? model.sites.filter((s) => s.ch > 0) : []), [model]);

  if (!model || sites.length === 0) return null;

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
          <CoverageHeadline model={model} />
        </div>
        <ChevronDown
          className={cn("w-4 h-4 transition-transform", expanded && "rotate-180")}
        />
      </button>

      {expanded && (
        <div className="px-4 pb-4 pt-2 border-t bg-background/50 animate-slide-up">
          <CoverageSummary model={model} sites={sites} />
          <CoverageGrid model={model} sites={sites} />
        </div>
      )}
    </div>
  );
}
