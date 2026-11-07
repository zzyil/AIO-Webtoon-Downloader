// ============================================================
// SyncDock — the sticky bar under the ledger (approved spec §4 item 6 and §5
// "Progress"; mockup selectionBar)
//
// Two faces:
//   * selection: "9 series · 452 files · 6.8 GB · 4 removals", a status line
//     (plan id + build time, the settling countdown, or why Review waits),
//     and Review and start — the only way into the Review dialog.
//   * progress (a verify/apply/prune/rename job on this target): the current
//     file (or folder), throughput, ETA, files / series / folders and bytes
//     done, Cancel. A plan job keeps the selection face and says
//     "Planning…" in its status line: plans are short and frequent, and the
//     user's place in the totals shouldn't jump. A refresh (a review
//     decision's top-up) likewise stays on the selection face, as "Updating
//     the plan…" (syncView.dockStatus), so PHASE_VERB / UNIT have no entry.
//
// Throughput is computed by the caller from progress samples
// (syncView.throughput); this file only draws.
// Consumer: SyncTab.jsx.
// ============================================================

import React from "react";
import { AlertTriangle, Check, ListChecks, Loader2, Search, Square, Timer, Upload } from "lucide-react";
import { Button } from "@/components/ui/primitives";
import { cn, formatBytes, formatEta } from "@/lib/utils";

const PHASE_VERB = {
  apply: (tn) => `Copying to ${tn}`,
  prune: (tn) => `Removing folders from ${tn}`,
  rename: (tn) => `Renaming a folder on ${tn}`,
  verify: (tn) => `Reading back ${tn}`,
};
// What filesDone/filesTotal count per phase: files for an apply, series
// folders for a verify (executor.js verify), folders for a prune or rename
// (same split as service.js quitInfo).
const UNIT = { apply: ["file", "files"], verify: ["series", "series"], prune: ["folder", "folders"], rename: ["folder", "folders"] };
const STATUS_ICON = { ok: Check, settle: Timer, busy: Loader2, warn: AlertTriangle, search: Search };

export function ProgressDock({ job, tn, rate, eta, onCancel, cancelling }) {
  const p = job.progress || {};
  const verb = (PHASE_VERB[job.phase] || PHASE_VERB.apply)(tn);
  // A verify's current is a folder alone (device-hash names no file).
  const cur = p.current ? (p.current.name ? `${p.current.folder} / ${p.current.name}` : p.current.folder) : p.step ? `${p.step}…` : "Starting…";
  const [one, many] = UNIT[job.phase] || UNIT.apply;
  const unit = (n) => (n === 1 ? one : many);
  const pct = p.bytesTotal > 0 ? (p.bytesDone / p.bytesTotal) * 100 : p.filesTotal > 0 ? (p.filesDone / p.filesTotal) * 100 : 0;
  return (
    <div className="flex shrink-0 flex-col gap-1.5 border-t bg-card px-3.5 py-2.5" role="status" aria-live="polite">
      <div className="flex min-w-0 items-center gap-2 text-[12.5px]">
        <Upload className="h-3.5 w-3.5 shrink-0 text-primary" aria-hidden="true" />
        <b className="shrink-0 font-semibold">{verb}</b>
        <span className="min-w-0 flex-1 truncate font-mono text-muted-foreground" title={cur}>{cur}</span>
        {rate != null && <span className="shrink-0 font-mono tabular-nums">{formatBytes(rate)}/s</span>}
        {eta != null && eta > 0 && <span className="shrink-0 font-mono tabular-nums text-muted-foreground">· {formatEta(eta)} left</span>}
        <Button size="sm" variant="outline" className="h-[26px] gap-1 px-2 text-[11.5px] [&_svg]:h-[13px] [&_svg]:w-[13px]" onClick={onCancel} disabled={cancelling}>
          <Square aria-hidden="true" />
          {cancelling ? "Cancelling…" : "Cancel"}
        </Button>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-muted">
        <i className="block h-full rounded-full bg-primary transition-[width] duration-300 motion-reduce:transition-none" style={{ width: `${Math.max(0, Math.min(100, pct))}%` }} />
      </div>
      <div className="flex font-mono text-[11px] tabular-nums text-muted-foreground">
        <span>
          {p.filesTotal != null ? `${p.filesDone || 0} of ${p.filesTotal} ${unit(p.filesTotal)}` : `${p.filesDone || 0} ${unit(p.filesDone || 0)}`}
          {p.bytesTotal > 0 ? ` · ${formatBytes(p.bytesDone || 0)} of ${formatBytes(p.bytesTotal)}` : ""}
        </span>
      </div>
    </div>
  );
}

/**
 * @param {{totals:{series,files,bytes,removals}, status:{kind,text}|null,
 *   canReview:boolean, reviewTitle?:string, onReview()}} props
 */
export function SelectionDock({ totals, status, canReview, reviewTitle, onReview }) {
  const Icon = status ? STATUS_ICON[status.kind] || Check : null;
  return (
    <div className="flex shrink-0 items-center gap-2.5 border-t bg-card px-3.5 py-2.5">
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 text-[13px]">
          <b className="font-semibold tabular-nums">{totals.series} series</b>
          <span className="text-muted-foreground">·</span>
          <span className="font-mono tabular-nums">
            {totals.files} file{totals.files === 1 ? "" : "s"} · {formatBytes(totals.bytes)}
          </span>
          {totals.removals > 0 && (
            <>
              <span className="text-muted-foreground">·</span>
              <span className="font-mono tabular-nums text-destructive">
                {totals.removals} removal{totals.removals === 1 ? "" : "s"}
              </span>
            </>
          )}
        </div>
        {status && (
          <div className={cn("flex min-w-0 items-center gap-1.5 text-[11px]", status.kind === "warn" ? "text-warn-fg" : "text-muted-foreground")}>
            {Icon && <Icon className={cn("h-3 w-3 shrink-0", Icon === Loader2 && "animate-spin motion-reduce:animate-none")} aria-hidden="true" />}
            <span className="truncate">{status.text}</span>
          </div>
        )}
      </div>
      <Button size="sm" onClick={onReview} disabled={!canReview} title={reviewTitle} className="gap-1.5 [&_svg]:h-3.5 [&_svg]:w-3.5">
        <ListChecks aria-hidden="true" />
        Review and start
      </Button>
    </div>
  );
}
