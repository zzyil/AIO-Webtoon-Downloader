// ============================================================
// ChapterStrip — one series' chapters on a target, as coloured segments
//
// Renders planner.js chapterStrip() output verbatim: run-length segments
// [{state, count, from, to}] in label order, `other` (volumes, ranges,
// unnumbered files) last. Segment width is proportional to `count`. The
// renderer never recomputes states: a selection change arrives as a new
// strip in the pushed summary (service.js _summarize).
//
// Colours: globals.css .sync-seg-* (spec §2), named only through SEG_CLASS.
// Sizes: sm 8 px (ledger, narrow), md 12 px (ledger, wide), xl 16 px
// (series detail).
// `lane` = percent of this series' copy already done in the running apply
// (spec §6 transfer lane); it fills every push segment from the left.
// `animate` + `delay` play the plan-landed sweep (SyncTab decides when).
//
// Consumers: Ledger.jsx, SeriesSheet.jsx. Copy: copy.js STRIP_STATE_COPY.
// ============================================================

import React from "react";
import { cn } from "@/lib/utils";
import { STRIP_STATE_COPY } from "./copy";
import { chLabel } from "./syncView";

const HEIGHT = { sm: "h-2 rounded-[3px]", md: "h-3 rounded", xl: "h-4 rounded" };

/**
 * The ONLY way to name a segment colour class: `.sync-seg sync-seg-<state>`
 * spelled out in full. Tailwind keeps a globals.css @layer utilities class
 * only when it finds the literal name in src, so a built string
 * (a template literal joining the prefix and the state) silently drops whichever state no other file
 * happens to spell (it dropped `absent`: unticked chapters drew as nothing).
 * Consumers: this file, Ledger.jsx (tile bars), SeriesSheet.jsx and
 * ReviewDialog.jsx (group squares). tools/_test_device_sync_view.js checks
 * it covers every planner strip state.
 */
export const SEG_CLASS = Object.freeze({
  synced: "sync-seg sync-seg-synced",
  push: "sync-seg sync-seg-push",
  warn: "sync-seg sync-seg-warn",
  delete: "sync-seg sync-seg-delete",
  absent: "sync-seg sync-seg-absent",
  other: "sync-seg sync-seg-other",
});

function segTitle(s, targetName) {
  const range = s.from == null ? `${s.count} file${s.count === 1 ? "" : "s"}` : s.from === s.to ? chLabel(s.from) : `${chLabel(s.from)}–${chLabel(s.to)}`;
  const word = s.state === "synced" && targetName ? `on ${targetName}` : STRIP_STATE_COPY[s.state] || s.state;
  return `${range} · ${word} (${s.count})`;
}

export default function ChapterStrip({ segments, size = "sm", lane, targetName, animate, delay = 0, className }) {
  const segs = segments || [];
  if (!segs.length) return <div className={cn("w-full", HEIGHT[size], "bg-muted", className)} aria-hidden="true" />;
  return (
    <div
      role="img"
      aria-label={`Chapters: ${segs.map((s) => `${s.count} ${STRIP_STATE_COPY[s.state] || s.state}`).join(", ")}`}
      className={cn("flex w-full gap-px overflow-hidden", HEIGHT[size], animate && "sync-strip-in", className)}
      style={animate ? { "--d": `${delay}ms` } : undefined}
    >
      {segs.map((s, i) => (
        <span
          key={i}
          title={segTitle(s, targetName)}
          className={SEG_CLASS[s.state] || SEG_CLASS.other}
          style={{ flexGrow: s.count, flexBasis: 0, ...(s.state === "push" && lane ? { "--fill": `${Math.max(0, Math.min(100, lane))}%` } : null) }}
        />
      ))}
    </div>
  );
}

/** One swatch per state, for the ledger legend (labels only when wide). */
export function StripLegend({ compact }) {
  const items = [
    ["synced", "On target"],
    ["push", "Will copy"],
    ["warn", "Needs a decision"],
    ["delete", "Will remove"],
    ["absent", "Not copied"],
    ["other", "Volumes / other"],
  ];
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11.5px] text-muted-foreground">
      {items.map(([s, l]) => (
        <span key={s} className="inline-flex items-center gap-1.5" title={compact ? l : undefined}>
          <i className={cn("inline-block h-2 w-3.5 rounded-sm", SEG_CLASS[s])} />
          {!compact && l}
        </span>
      ))}
    </div>
  );
}
