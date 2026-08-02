// ============================================================
// UPDATES CENTER — right-side slide-out panel for "Check All"
//
// Owns the visual presentation + per-row actions for the bulk
// update-check sweep that LibraryTab triggers. Reads progress
// from the parent's `seriesStates` Map and live-renders it.
//
// State flow:
//   LibraryTab subscribes to onUpdateCheckProgress, builds a
//   Map<folderPath, SeriesState>, and passes it here. We never
//   own the canonical state — LibraryTab is the source so the
//   toolbar badge + grid card "new" counts stay in lockstep.
//
// Closing the panel mid-scan does NOT cancel the scan. The
// background events keep flowing into LibraryTab's state and
// reopening restores the live view. Explicit Cancel button is
// available in the header during a scan.
//
// Aesthetic: operational dashboard. Distinct from the rest of
// the app via:
//   - 2px gradient stripe along the top edge (orange→emerald→slate)
//   - JetBrains Mono for counts/labels (matches LogPanel)
//   - Subtle background grid pattern on the panel body
//   - "Scanning" gradient sweep on running rows
//
// Cross-file:
//   - electron/main.js:check-all-updates handler emits the events
//   - electron/preload.js exposes checkAllUpdates / cancelCheckAllUpdates
//   - LibraryTab.jsx hosts this component + handles the queue actions
//   - useDownloader.js:queueDownload is reached via onStartDownload prop
// ============================================================

import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  X,
  RefreshCw,
  Download,
  AlertCircle,
  Loader2,
  Check,
  ChevronDown,
  ChevronRight,
  Bell,
  Globe,
  Clock,
  StopCircle,
} from "lucide-react";
import { Button } from "@/components/ui/primitives";
import { cn, chaptersToRangeString, getInitials, naturalCompare } from "@/lib/utils";

// ── Section state → presentational metadata ──
// Single source for the per-section accent so the chip, the section header,
// and the row border stay in lockstep when we add/remove sections later.
const SECTION_THEME = {
  found: {
    label: "UPDATES FOUND",
    icon: Bell,
    accent: "text-orange-400",
    rule: "bg-gradient-to-r from-orange-500/40 to-orange-500/0",
    chip: "bg-orange-500/15 text-orange-300 border-orange-500/30",
  },
  active: {
    label: "CHECKING",
    icon: Loader2,
    accent: "text-sky-400",
    rule: "bg-gradient-to-r from-sky-500/40 to-sky-500/0",
    chip: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  },
  uptodate: {
    label: "UP TO DATE",
    icon: Check,
    accent: "text-emerald-400/80",
    rule: "bg-gradient-to-r from-emerald-500/30 to-emerald-500/0",
    chip: "bg-emerald-500/10 text-emerald-300/90 border-emerald-500/25",
  },
  errors: {
    label: "ERRORS",
    icon: AlertCircle,
    accent: "text-red-400",
    rule: "bg-gradient-to-r from-red-500/40 to-red-500/0",
    chip: "bg-red-500/15 text-red-300 border-red-500/30",
  },
};

// ── Helpers ──
// chaptersToRangeString + getInitials are shared from @/lib/utils (imported
// above). formatDuration below is LOCAL and intentionally distinct from the
// utils one — it renders the scan duration as "Nms"/"N.Ns"/"MmSSs", which is
// a different format than utils.formatDuration ("Ns"/"Mm Ss").

function formatDuration(ms) {
  if (!ms || ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const rem = Math.floor(s % 60);
  return `${m}m${rem.toString().padStart(2, "0")}s`;
}

function siteLabel(site) {
  if (!site) return "unknown";
  return site;
}

// ── Cover thumbnail ──
// Series covers come from the .aio_series.json `cover` URL. We pass through
// the local cover-cache via the same `localfile://` shim LibraryTab uses for
// PDF thumbs but only when the cached file is on disk (`webCoverCached`).
// Otherwise the remote URL works in Electron as long as it's https. Falls
// back to a two-letter monogram block on missing/erroring image.
function RowCover({ title, cover }) {
  const [errored, setErrored] = useState(false);
  const initials = useMemo(
    () => getInitials(title, { splitUnderscore: true, filterEmpty: true, fallback: "??" }),
    [title]
  );

  if (cover && !errored) {
    return (
      <img
        src={cover}
        alt=""
        loading="lazy"
        onError={() => setErrored(true)}
        className="w-[30px] h-[42px] object-cover rounded-sm border border-white/[0.08] shrink-0 bg-muted/30"
      />
    );
  }
  return (
    <div
      className={cn(
        "w-[30px] h-[42px] rounded-sm border border-white/[0.08] shrink-0",
        "flex items-center justify-center",
        "bg-gradient-to-br from-zinc-700/60 to-zinc-900/80",
        "text-[10px] font-bold tracking-wider text-zinc-400 font-mono"
      )}
    >
      {initials}
    </div>
  );
}

// ── A single series row ──
// Visual state derives entirely from `row.state` ("queued" | "running" |
// "found" | "uptodate" | "error"). All other props are display-only.
function SeriesRow({
  row,
  onQueue,
  onDismiss,
}) {
  const isRunning = row.state === "running";
  const isQueued = row.state === "queued";
  const isFound = row.state === "found";
  const isUpToDate = row.state === "uptodate";
  const isError = row.state === "error";

  // Build the subline text — one of "site · 12 new (1-3, 5)" etc.
  let subline;
  if (isFound) {
    const range = chaptersToRangeString(row.newChapters || []);
    subline = (
      <>
        <span>{siteLabel(row.site)}</span>
        <span className="text-orange-400/80">·</span>
        <span className="text-orange-300/90">
          {row.newChapters.length} new
        </span>
        {range && (
          <>
            <span className="text-zinc-600">·</span>
            <span className="truncate text-zinc-500" title={range}>
              {range}
            </span>
          </>
        )}
      </>
    );
  } else if (isRunning) {
    subline = (
      <>
        <span>{siteLabel(row.site)}</span>
        <span className="text-sky-500/60">·</span>
        <span className="text-sky-300/90">checking…</span>
      </>
    );
  } else if (isQueued) {
    subline = (
      <>
        <span>{siteLabel(row.site)}</span>
        <span className="text-zinc-600">·</span>
        <span className="text-zinc-500">queued</span>
      </>
    );
  } else if (isUpToDate) {
    subline = (
      <>
        <span>{siteLabel(row.site)}</span>
        <span className="text-emerald-500/60">·</span>
        <span className="text-emerald-300/80">
          {row.total ? `${row.total} on site` : "up to date"}
        </span>
      </>
    );
  } else if (isError) {
    const message = row.error === "aborted" ? "cancelled" : (row.errorMessage || row.error || "check failed");
    subline = (
      <>
        <span>{siteLabel(row.site)}</span>
        <span className="text-red-500/60">·</span>
        <span className="text-red-300/85 truncate" title={message}>
          {message}
        </span>
      </>
    );
  }

  return (
    <div
      className={cn(
        "relative flex items-center gap-3 px-4 py-2.5",
        "border-b border-white/[0.04] last:border-b-0",
        "transition-colors duration-150",
        isFound && "hover:bg-orange-500/[0.06]",
        isRunning && "bg-sky-500/[0.04]",
        isQueued && "opacity-60",
        isUpToDate && "hover:bg-emerald-500/[0.04]",
        isError && "hover:bg-red-500/[0.06]"
      )}
    >
      {/* Running row scanning sweep — pure CSS gradient with animated bg-position.
          Uses the `bg-[length:200%_100%]` + custom keyframes class below. */}
      {isRunning && (
        <span
          aria-hidden
          className="absolute inset-0 pointer-events-none updates-scan-sweep"
        />
      )}

      <RowCover title={row.title} cover={row.cover} />

      <div className="flex-1 min-w-0 relative">
        <div className="text-xs font-semibold leading-tight truncate" title={row.title}>
          {row.title}
        </div>
        <div className="flex items-center gap-1.5 mt-0.5 text-[10px] font-mono text-zinc-400/90 leading-tight min-w-0">
          {subline}
        </div>
      </div>

      {/* Right column: count + action button.
          Found rows get a +N pill AND a single icon-button that queues just
          this row; running/queued get a spinner; up-to-date gets the check;
          error gets the alert icon (title tooltip = full message). */}
      <div className="flex items-center gap-2 shrink-0">
        {isFound && (
          <span
            className={cn(
              "text-[10px] font-bold font-mono px-1.5 py-0.5 rounded-sm border",
              "bg-orange-500/15 text-orange-300 border-orange-500/30"
            )}
          >
            +{row.newChapters.length}
          </span>
        )}
        {isRunning && (
          <Loader2 className="w-3.5 h-3.5 animate-spin text-sky-400" />
        )}
        {isQueued && (
          <Clock className="w-3.5 h-3.5 text-zinc-500" />
        )}
        {isUpToDate && (
          <Check className="w-3.5 h-3.5 text-emerald-400" />
        )}
        {isError && (
          <AlertCircle
            className="w-3.5 h-3.5 text-red-400"
            title={row.errorMessage || row.error}
          />
        )}

        {isFound && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onQueue(row)}
            className="h-7 w-7 p-0 hover:bg-orange-500/15 hover:text-orange-300"
            title={`Queue ${row.newChapters.length} new chapter${row.newChapters.length === 1 ? "" : "s"}`}
          >
            <Download className="w-3.5 h-3.5" />
          </Button>
        )}
        {isFound && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onDismiss(row)}
            className="h-7 w-7 p-0 text-zinc-500 hover:text-zinc-300"
            title="Dismiss (clear the new-chapter badge)"
          >
            <X className="w-3 h-3" />
          </Button>
        )}
      </div>
    </div>
  );
}

// ── A collapsible section ──
function Section({
  theme,
  rows,
  defaultOpen = true,
  onQueue,
  onDismiss,
  count, // override for the chip count when it differs from rows.length
}) {
  const [open, setOpen] = useState(defaultOpen);
  if (rows.length === 0) return null;
  const Icon = theme.icon;
  const showCount = count != null ? count : rows.length;
  return (
    <div>
      {/* Section header — clickable to collapse */}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "w-full flex items-center gap-2 px-4 py-1.5",
          "text-[10px] font-mono uppercase tracking-[0.12em]",
          "hover:bg-white/[0.02] transition-colors",
          theme.accent
        )}
      >
        {open ? (
          <ChevronDown className="w-3 h-3" />
        ) : (
          <ChevronRight className="w-3 h-3" />
        )}
        <Icon className={cn("w-3 h-3", theme.label === "CHECKING" && "animate-spin")} />
        <span>{theme.label}</span>
        <span
          className={cn(
            "ml-auto text-[10px] font-bold px-1.5 py-0.5 rounded-sm border",
            theme.chip
          )}
        >
          {showCount}
        </span>
      </button>

      {/* Decorative gradient rule — fades right; gives the header weight */}
      <div className={cn("h-px mx-4 -mt-0.5 mb-1", theme.rule)} />

      {/* Rows */}
      {open && (
        <div>
          {rows.map((row) => (
            <SeriesRow
              key={row.folderPath}
              row={row}
              onQueue={onQueue}
              onDismiss={onDismiss}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ── Progress bar (head of the panel) ──
// Renders a "filled bar + dotted remainder" line. Filled portion is solid
// primary; dotted portion is a repeating-linear-gradient. Width animates.
function HeaderProgress({ scanState, completed, total }) {
  const pct = total > 0 ? (completed / total) * 100 : 0;
  const isActive = scanState === "running";
  const isDone = scanState === "done";
  return (
    <div className="relative h-[3px] mt-3 mx-4 rounded-full overflow-hidden bg-zinc-800/60">
      {/* Filled portion */}
      <div
        className={cn(
          "absolute inset-y-0 left-0 transition-[width] duration-300 ease-out rounded-full",
          isActive && "bg-gradient-to-r from-sky-500 via-sky-400 to-orange-400",
          isDone && "bg-gradient-to-r from-emerald-500 to-emerald-400",
          !isActive && !isDone && "bg-zinc-700"
        )}
        style={{ width: `${pct}%` }}
      />
      {/* Glow on the running edge */}
      {isActive && pct < 100 && (
        <div
          className="absolute inset-y-0 w-2 bg-sky-300/50 blur-[2px] transition-[left] duration-300 ease-out"
          style={{ left: `calc(${pct}% - 4px)` }}
          aria-hidden
        />
      )}
    </div>
  );
}

// ── Main panel ──
export default function UpdatesCenter({
  open,
  onClose,
  // Map<folderPath, SeriesState>
  seriesStates,
  scanState, // "idle" | "running" | "done"
  scanStats, // { completed, total, durationMs }
  onRescan,
  onCancel,
  // (row) → start a single download via parent's onStartDownload
  onQueueRow,
  // () → queue every found row
  onQueueAll,
  // (row | "all") → clear new-chapter counts; passes folderPath array
  onDismiss,
  // True iff there are checkable series (parent computes from libraryEntries)
  hasCheckableSeries,
}) {
  // The panel keeps a tiny render-only flag for the slide-in animation; the
  // mount itself is gated by `open` from the parent. When `open` flips to
  // true we delay setting `mounted` so the entrance animation runs.
  const [animateIn, setAnimateIn] = useState(false);
  const panelRef = useRef(null);

  useEffect(() => {
    if (open) {
      // Defer one paint so the initial translate-x-full state is committed
      // before we flip to translate-x-0 (avoids the panel "jumping in").
      const id = requestAnimationFrame(() => setAnimateIn(true));
      return () => cancelAnimationFrame(id);
    }
    setAnimateIn(false);
  }, [open]);

  // Escape key closes the panel.
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // Group rows by state for the section rendering. We sort each bucket so
  // the visual order is stable across re-renders (latest-completed at top
  // of "found", queued in original FIFO).
  const groups = useMemo(() => {
    const found = [];
    const active = []; // running + queued
    const uptodate = [];
    const errors = [];
    for (const row of seriesStates.values()) {
      if (row.state === "found") found.push(row);
      else if (row.state === "running" || row.state === "queued") active.push(row);
      else if (row.state === "uptodate") uptodate.push(row);
      else if (row.state === "error") errors.push(row);
    }
    // Found: most chapters first (most "noticeable")
    found.sort((a, b) => (b.newChapters?.length || 0) - (a.newChapters?.length || 0));
    // Active: running before queued, then enqueue order
    active.sort((a, b) => {
      if (a.state === b.state) return (a.enqueuedAt || 0) - (b.enqueuedAt || 0);
      return a.state === "running" ? -1 : 1;
    });
    // Up to date: alphabetical, numeric-aware ("Vol 2" before "Vol 10")
    uptodate.sort((a, b) => naturalCompare(a.title || "", b.title || ""));
    errors.sort((a, b) => naturalCompare(a.title || "", b.title || ""));
    return { found, active, uptodate, errors };
  }, [seriesStates]);

  const totalFound = groups.found.length;
  const isScanning = scanState === "running";
  const isDone = scanState === "done";

  // Header status line: changes between scanning / idle / done
  let statusLine;
  if (isScanning) {
    statusLine = (
      <>
        <span className="text-sky-300/90">SCANNING</span>
        <span className="text-zinc-600">·</span>
        <span className="text-zinc-300 tabular-nums">
          {scanStats.completed}/{scanStats.total}
        </span>
      </>
    );
  } else if (isDone) {
    statusLine = (
      <>
        <span className={cn(
          totalFound > 0 ? "text-orange-300" : "text-emerald-300/90"
        )}>
          {totalFound > 0 ? `${totalFound} UPDATES` : "ALL CLEAR"}
        </span>
        {scanStats.durationMs > 0 && (
          <>
            <span className="text-zinc-600">·</span>
            <span className="text-zinc-500">{formatDuration(scanStats.durationMs)}</span>
          </>
        )}
      </>
    );
  } else {
    statusLine = (
      <>
        <span className="text-zinc-400">UPDATES</span>
        {totalFound > 0 && (
          <>
            <span className="text-zinc-600">·</span>
            <span className="text-orange-300 tabular-nums">{totalFound} found</span>
          </>
        )}
      </>
    );
  }

  // Don't even render the DOM when closed — avoids backdrop blur cost.
  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex"
      role="dialog"
      aria-label="Updates Center"
    >
      {/* ── Backdrop ── */}
      <button
        type="button"
        aria-label="Close updates panel"
        onClick={onClose}
        className={cn(
          "absolute inset-0 bg-black transition-opacity duration-200",
          animateIn ? "opacity-40" : "opacity-0"
        )}
      />

      {/* ── Side sheet ── */}
      <div
        ref={panelRef}
        className={cn(
          "absolute top-0 right-0 h-full w-full max-w-[480px]",
          "bg-[hsl(var(--card))] border-l border-white/[0.06]",
          "shadow-[0_0_60px_-12px_rgba(0,0,0,0.6)]",
          "flex flex-col",
          "transition-transform duration-[220ms] ease-[cubic-bezier(0.16,1,0.3,1)]",
          animateIn ? "translate-x-0" : "translate-x-full"
        )}
        style={{
          // Subtle dotted grid overlay so the panel reads as its own surface.
          backgroundImage:
            "radial-gradient(rgba(255,255,255,0.025) 1px, transparent 1px)",
          backgroundSize: "16px 16px",
        }}
      >
        {/* Top edge gradient stripe — orange/emerald/slate.
            Pure decoration, signals "this surface is about state colors". */}
        <div
          aria-hidden
          className="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-orange-500 via-emerald-500 to-slate-500/40"
        />

        {/* ── Header ── */}
        <div className="relative pt-5 pb-3 border-b border-white/[0.06] bg-[hsl(var(--card))]/95 backdrop-blur-sm">
          <div className="flex items-start justify-between px-4 gap-3">
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 text-[10px] font-mono tracking-[0.18em] leading-tight">
                {statusLine}
              </div>
              <div className="mt-0.5 text-[10px] text-zinc-500 font-mono">
                {hasCheckableSeries
                  ? `${scanStats.total || groups.uptodate.length + groups.found.length + groups.errors.length + groups.active.length} ongoing series`
                  : "no ongoing series to check"}
              </div>
            </div>

            <div className="flex items-center gap-1">
              {isScanning && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={onCancel}
                  className="gap-1.5 text-xs text-red-400 hover:text-red-300 hover:bg-red-500/10"
                  title="Cancel the in-flight scan"
                >
                  <StopCircle className="w-3.5 h-3.5" />
                  Cancel
                </Button>
              )}
              {!isScanning && hasCheckableSeries && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={onRescan}
                  className="gap-1.5 text-xs text-zinc-400 hover:text-zinc-200"
                  title="Run another scan now"
                >
                  <RefreshCw className="w-3.5 h-3.5" />
                  Rescan
                </Button>
              )}
              <Button
                variant="ghost"
                size="sm"
                onClick={onClose}
                className="h-8 w-8 p-0"
                title="Close (scan continues in background)"
              >
                <X className="w-4 h-4" />
              </Button>
            </div>
          </div>

          <HeaderProgress
            scanState={scanState}
            completed={scanStats.completed}
            total={scanStats.total}
          />
        </div>

        {/* ── Body ── */}
        <div className="flex-1 overflow-y-auto">
          {!hasCheckableSeries ? (
            <div className="flex flex-col items-center justify-center text-center h-full py-16 px-8 gap-3">
              <div className="w-12 h-12 rounded-full bg-zinc-800/60 flex items-center justify-center">
                <Globe className="w-5 h-5 text-zinc-500" />
              </div>
              <p className="text-xs text-zinc-400 max-w-[280px] leading-relaxed">
                None of your series have a source URL saved. Open a series'
                detail view and paste its URL to enable update checks.
              </p>
            </div>
          ) : seriesStates.size === 0 ? (
            <div className="flex flex-col items-center justify-center text-center h-full py-16 px-8 gap-3">
              <Loader2 className="w-6 h-6 text-zinc-500 animate-spin" />
              <p className="text-xs text-zinc-500 font-mono">preparing scan…</p>
            </div>
          ) : (
            <div className="py-2">
              <Section
                theme={SECTION_THEME.found}
                rows={groups.found}
                onQueue={onQueueRow}
                onDismiss={(row) => onDismiss([row.folderPath])}
              />
              <Section
                theme={SECTION_THEME.active}
                rows={groups.active}
                defaultOpen={isScanning}
                count={groups.active.length}
              />
              <Section
                theme={SECTION_THEME.uptodate}
                rows={groups.uptodate}
                defaultOpen={false}
              />
              <Section
                theme={SECTION_THEME.errors}
                rows={groups.errors}
                defaultOpen={groups.errors.length > 0 && !isScanning}
              />
            </div>
          )}
        </div>

        {/* ── Sticky footer (visible when ≥1 update found) ── */}
        {totalFound > 0 && (
          <div className="border-t border-white/[0.06] bg-[hsl(var(--card))]/95 backdrop-blur-sm px-4 py-3">
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                onClick={onQueueAll}
                className={cn(
                  "flex-1 gap-1.5 text-xs font-semibold",
                  "bg-orange-500 hover:bg-orange-500/90 text-white",
                  "shadow-[0_0_20px_-6px_rgba(249,115,22,0.6)]"
                )}
              >
                <Download className="w-3.5 h-3.5" />
                Queue all {totalFound} update{totalFound === 1 ? "" : "s"}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => onDismiss(groups.found.map((r) => r.folderPath))}
                className="text-xs text-zinc-500 hover:text-zinc-300"
                title="Clear all new-chapter badges without downloading"
              >
                Dismiss all
              </Button>
            </div>
          </div>
        )}
      </div>

      {/* ── Scoped CSS for the scan-sweep effect ──
          Declared inline so this component is self-contained (no globals.css
          edit needed). The keyframe animates background-position across a
          200% gradient, producing a subtle moving sheen on running rows. */}
      <style>{`
        .updates-scan-sweep {
          background-image: linear-gradient(
            90deg,
            transparent 0%,
            rgba(56, 189, 248, 0.08) 50%,
            transparent 100%
          );
          background-size: 200% 100%;
          background-repeat: no-repeat;
          animation: updates-sweep 1.6s linear infinite;
        }
        @keyframes updates-sweep {
          0%   { background-position: 200% 0; }
          100% { background-position: -200% 0; }
        }
      `}</style>
    </div>
  );
}
