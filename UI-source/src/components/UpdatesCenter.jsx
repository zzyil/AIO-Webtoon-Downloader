// ============================================================
// UPDATES CENTER — right-side slide-out panel for "Check All"
//
// Owns the visual presentation + per-row actions for the bulk
// update-check sweep that LibraryTab triggers. Reads progress
// from the parent's `seriesStates` Map and live-renders it.
//
// State flow:
//   hooks/useUpdateCheck.js (mounted for the app's lifetime via
//   useDownloader) subscribes to onUpdateCheckProgress and holds
//   the Map<folderPath, row>; LibraryTab forwards it here. We
//   never own the canonical state, and neither does LibraryTab
//   any more — it unmounts on every tab switch while the sweep
//   keeps running in the main process, which is exactly the bug
//   that moved this state up a level.
//
// Closing the panel mid-scan does NOT cancel the scan, and
// neither does leaving the Library tab. Events keep flowing into
// the hook, so reopening restores the live view; the header's
// Cancel button is the only thing that stops a sweep, and Rescan
// is the only thing that starts a second one.
//
// Aesthetic: operational dashboard. Distinct from the rest of
// the app via:
//   - 2px gradient stripe along the top edge (orange→emerald→slate)
//   - JetBrains Mono for counts/labels (matches LogPanel)
//   - Subtle background grid pattern on the panel body
//   - "Scanning" gradient sweep on running rows
//
// PER-CHAPTER CONTROLS: a found row expands into one chip per missing
// chapter, with two independent gestures (see ChapterChips.jsx). Ticking is
// transient and lives HERE — "queue all" has to read every row's ticks, so no
// row can own its own. Crossing out is persistent and lives in
// .aio_series.json, so it rides back on the row itself as `ignoredChapters`;
// this file only renders it and fires the write.
//
// Cross-file:
//   - electron/main.js:check-all-updates handler emits the events;
//     set-chapters-ignored persists a cross-out
//   - electron/preload.js exposes checkAllUpdates / cancelCheckAllUpdates /
//     setChaptersIgnored
//   - components/ChapterChips.jsx is the expanded row's chip list
//   - LibraryTab.jsx hosts this component + handles the queue actions
//   - useDownloader.js:queueDownload is reached via onStartDownload prop
// ============================================================

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import ChapterChips, { ChapterChipsLegend, selectedChapters } from "./ChapterChips";
import { KOMIKKU_VOLUMES_MESSAGE } from "@/lib/downloadArgs";

// Volume ticks share the panel's `deselections` Map under a second key per
// series. NUL can't occur in a path, so it can't collide with a folderPath.
function volumeKey(folderPath) {
  return `${folderPath}\u0000vol`;
}

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
// "found" | "uptodate" | "error").
//
// A row that reported any missing chapter — downloadable or crossed out — is
// EXPANDABLE, and that includes up-to-date ones: a series whose every missing
// chapter is crossed out is legitimately up to date, but the undo for those
// crosses lives nowhere else, so hiding the expander there would make them
// permanent. Rows still being checked, and ones already queued/dismissed
// (resolveRows strips their chapters), have nothing to show and stay flat.
function SeriesRow({
  row,
  onQueue,
  onDismiss,
  expanded,
  onToggleExpand,
  // Set of this row's newChapters the user unticked. Owned by the panel, not
  // here, because "Queue all" has to read every row's ticks (grep deselections).
  deselected,
  onToggleChapter,
  onSetIgnored,
  onSelectAll,
  busy,
  // Volume half (series that track MangaFire volumes): its own deselections,
  // tick, cross-out (kind "volume") and select-all. `komikku` blocks queueing
  // volumes and says why (lib/downloadArgs.js KOMIKKU_VOLUMES_MESSAGE).
  volumeDeselected,
  onToggleVolume,
  onSetVolumesIgnored,
  onSelectAllVolumes,
  komikku = false,
}) {
  const isRunning = row.state === "running";
  const isQueued = row.state === "queued";
  const isFound = row.state === "found";
  const isUpToDate = row.state === "uptodate";
  const isError = row.state === "error";

  const newChapters = row.newChapters || [];
  const ignoredChapters = row.ignoredChapters || [];
  const selected = selectedChapters(newChapters, deselected);
  const newVolumes = row.newVolumes || [];
  const ignoredVolumes = row.ignoredVolumes || [];
  // Volumes can't be queued under Komikku, so none count as selected then.
  const selectedVolumes = komikku ? [] : selectedChapters(newVolumes, volumeDeselected);
  const selectedTotal = selected.length + selectedVolumes.length;
  const crossedOut = ignoredChapters.length + ignoredVolumes.length;
  const expandable =
    (isFound || isUpToDate) && newChapters.length + newVolumes.length + crossedOut > 0;
  const isOpen = expandable && expanded;

  // "3 crossed out" tail, shared by the found and up-to-date sublines — it is
  // the only hint an otherwise-clean row has that something is being withheld.
  const crossedOutTail = crossedOut > 0 && (
    <>
      <span className="text-zinc-600">·</span>
      <span className="text-zinc-500 line-through decoration-zinc-600">
        {crossedOut} crossed out
      </span>
    </>
  );

  // Build the subline text — one of "site · 12 new (1-3, 5)" etc.
  let subline;
  if (isFound) {
    const range = chaptersToRangeString(newChapters);
    subline = (
      <>
        <span>{siteLabel(row.site)}</span>
        <span className="text-orange-400/80">·</span>
        {newChapters.length > 0 && (
          <span className="text-orange-300/90">
            {newChapters.length} new
          </span>
        )}
        {newVolumes.length > 0 && (
          <>
            {newChapters.length > 0 && <span className="text-zinc-600">·</span>}
            <span className="text-sky-300/90">
              {newVolumes.length} new vol
            </span>
          </>
        )}
        {range && (
          <>
            <span className="text-zinc-600">·</span>
            <span className="truncate text-zinc-500" title={range}>
              {range}
            </span>
          </>
        )}
        {crossedOutTail}
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
        {crossedOutTail}
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

  // The identity block. A <button> only when there is something to expand, so
  // a plain up-to-date row doesn't advertise a click that does nothing.
  const identity = (
    <>
      {expandable && (
        <span className="shrink-0 text-zinc-500 mt-0.5">
          {isOpen ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        </span>
      )}
      <span className="flex-1 min-w-0">
        <span className="block text-xs font-semibold leading-tight truncate" title={row.title}>
          {row.title}
        </span>
        <span className="flex items-center gap-1.5 mt-0.5 text-[10px] font-mono text-zinc-400/90 leading-tight min-w-0">
          {subline}
        </span>
      </span>
    </>
  );

  return (
    <div
      className={cn(
        "relative border-b border-white/[0.04] last:border-b-0",
        "transition-colors duration-150",
        isFound && "hover:bg-orange-500/[0.06]",
        isRunning && "bg-sky-500/[0.04]",
        isQueued && "opacity-60",
        isUpToDate && "hover:bg-emerald-500/[0.04]",
        isError && "hover:bg-red-500/[0.06]",
        isOpen && "bg-white/[0.02]"
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

      <div className="relative flex items-center gap-3 px-4 py-2.5">
        <RowCover title={row.title} cover={row.cover} />

        {expandable ? (
          <button
            type="button"
            onClick={onToggleExpand}
            aria-expanded={isOpen}
            title={isOpen ? "Hide chapters" : "Show individual chapters"}
            className="flex-1 min-w-0 flex items-start gap-1.5 text-left"
          >
            {identity}
          </button>
        ) : (
          <div className="flex-1 min-w-0 flex items-start gap-1.5">{identity}</div>
        )}

        {/* Right column: count + action button.
            Found rows get a pill counting what would actually be queued (ticked
            chapters, so it drops as the user unticks or crosses out) AND a
            single icon-button that queues just this row; running/queued get a
            spinner; up-to-date gets the check; error gets the alert icon
            (title tooltip = full message). */}
        <div className="flex items-center gap-2 shrink-0">
          {isFound && (
            <span
              className={cn(
                "text-[10px] font-bold font-mono px-1.5 py-0.5 rounded-sm border",
                selectedTotal > 0
                  ? "bg-orange-500/15 text-orange-300 border-orange-500/30"
                  : "bg-zinc-700/30 text-zinc-500 border-zinc-600/40"
              )}
              title={
                newVolumes.length > 0
                  ? `${selected.length} of ${newChapters.length} chapters, ${selectedVolumes.length} of ${newVolumes.length} volumes selected`
                  : selected.length === newChapters.length
                  ? `${newChapters.length} new chapter${newChapters.length === 1 ? "" : "s"}`
                  : `${selected.length} of ${newChapters.length} selected`
              }
            >
              +{selectedTotal}
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
              disabled={selectedTotal === 0}
              className="h-7 w-7 p-0 hover:bg-orange-500/15 hover:text-orange-300 disabled:opacity-40"
              title={
                selectedTotal === 0
                  ? "Nothing selected to queue"
                  : [
                      selected.length > 0 && `${selected.length} chapter${selected.length === 1 ? "" : "s"}`,
                      selectedVolumes.length > 0 && `${selectedVolumes.length} volume${selectedVolumes.length === 1 ? "" : "s"}`,
                    ].filter(Boolean).join(" + ").replace(/^/, "Queue ")
              }
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
              // Spelled out because an expanded row shows TWO kinds of ×, and
              // they differ in scope: this one clears the badge for now and
              // saves nothing, while a chapter's × is written to the series.
              title="Dismiss this series for now — clears the badge, saves nothing. The next check reports it again."
            >
              <X className="w-3 h-3" />
            </Button>
          )}
        </div>
      </div>

      {/* ── Expanded per-chapter controls ── */}
      {isOpen && (
        <div className="relative px-4 pb-3 pl-[58px] space-y-2">
          {/* Wording lives in ChapterChipsLegend so this and the detail view
              explain the same two buttons the same way. It drops the tick line
              by itself when every chapter is crossed out. */}
          <ChapterChipsLegend
            hasChapters={newChapters.length > 0}
            actions={
              newChapters.length > 1 && (
                <span className="flex items-center gap-1.5 font-mono shrink-0">
                  <button
                    type="button"
                    onClick={() => onSelectAll(true)}
                    className="hover:text-zinc-200 transition-colors"
                    title="Include every chapter in this download"
                  >
                    all
                  </button>
                  <span className="text-zinc-700">|</span>
                  <button
                    type="button"
                    onClick={() => onSelectAll(false)}
                    className="hover:text-zinc-200 transition-colors"
                    title="Skip every chapter in this download (nothing is crossed out)"
                  >
                    none
                  </button>
                </span>
              )
            }
          />
          <ChapterChips
            chapters={newChapters}
            ignored={ignoredChapters}
            deselected={deselected}
            onToggle={onToggleChapter}
            onSetIgnored={onSetIgnored}
            busy={busy}
          />
          {/* Volume half: same gestures, own namespace. */}
          {newVolumes.length + ignoredVolumes.length > 0 && (
            <div className="pt-1 space-y-1.5">
              <div className="flex items-center gap-2 text-[10px] font-mono uppercase tracking-[0.1em] text-sky-300/80">
                <span>Volumes</span>
                {newVolumes.length > 1 && !komikku && (
                  <span className="ml-auto flex items-center gap-1.5 normal-case tracking-normal text-zinc-400">
                    <button type="button" onClick={() => onSelectAllVolumes(true)}
                      className="hover:text-zinc-200 transition-colors"
                      title="Include every volume in this download">all</button>
                    <span className="text-zinc-700">|</span>
                    <button type="button" onClick={() => onSelectAllVolumes(false)}
                      className="hover:text-zinc-200 transition-colors"
                      title="Skip every volume in this download (nothing is crossed out)">none</button>
                  </span>
                )}
              </div>
              {komikku && newVolumes.length > 0 && (
                <p className="text-[10px] text-yellow-500/90 leading-snug">
                  {KOMIKKU_VOLUMES_MESSAGE}
                </p>
              )}
              <ChapterChips
                chapters={newVolumes}
                ignored={ignoredVolumes}
                deselected={komikku ? new Set(newVolumes) : volumeDeselected}
                onToggle={komikku ? undefined : onToggleVolume}
                onSetIgnored={onSetVolumesIgnored}
                busy={busy}
                noun="volume"
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── A collapsible section ──
// `chapterCtl` is the panel's per-chapter machinery, forwarded verbatim rather
// than unpacked into six props: BOTH the found and up-to-date sections need it
// (an all-crossed-out series lands in the latter and its undo lives there), and
// bundling keeps those two call sites from drifting apart.
function Section({
  theme,
  rows,
  defaultOpen = true,
  onQueue,
  onDismiss,
  chapterCtl,
  count, // override for the chip count when it differs from rows.length
  komikku = false,
}) {
  const [open, setOpen] = useState(defaultOpen);

  // Follow the row the user is working in. Crossing out a series' LAST
  // downloadable chapter re-files it from "updates found" into this section,
  // which starts collapsed — so without this the row the user was mid-way
  // through editing simply disappears, which reads as "gone for good" rather
  // than "reclassified", on the one action whose whole point is being
  // undoable. Keyed on the transition, so a section the user then collapses
  // by hand stays collapsed.
  const holdsExpandedRow = rows.some((r) => chapterCtl?.isExpanded(r.folderPath));
  useEffect(() => {
    if (holdsExpandedRow) setOpen(true);
  }, [holdsExpandedRow]);

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
              expanded={chapterCtl?.isExpanded(row.folderPath)}
              onToggleExpand={() => chapterCtl?.toggleExpand(row.folderPath)}
              deselected={chapterCtl?.deselectedFor(row.folderPath)}
              onToggleChapter={(ch) => chapterCtl?.toggleChapter(row.folderPath, ch)}
              onSetIgnored={(chs, ignored) =>
                chapterCtl?.setIgnored(row.folderPath, chs, ignored)
              }
              onSelectAll={(on) => chapterCtl?.selectAll(row, on)}
              busy={chapterCtl?.isBusy(row.folderPath)}
              volumeDeselected={chapterCtl?.deselectedFor(volumeKey(row.folderPath))}
              onToggleVolume={(v) => chapterCtl?.toggleChapter(volumeKey(row.folderPath), v)}
              onSetVolumesIgnored={(vs, ignored) =>
                chapterCtl?.setIgnored(row.folderPath, vs, ignored, "volume")
              }
              onSelectAllVolumes={(on) => chapterCtl?.selectAllVolumes(row, on)}
              komikku={komikku}
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
  scanStats, // { completed, total, durationMs, aborted }
  onRescan,
  onCancel,
  // (row, chapters) → start a single download via parent's onStartDownload.
  // `chapters` is the ticked subset, NOT row.newChapters — the panel owns the
  // ticks, so it has to say which ones it means.
  onQueueRow,
  // (Array<{ row, chapters }>) → queue every found row's ticked subset
  onQueueAll,
  // (row | "all") → clear new-chapter counts; passes folderPath array
  onDismiss,
  // (folderPath, chapters[], ignored) → persist a cross-out / undo. Resolves
  // once .aio_series.json is written; the row updates itself through the hook.
  onSetChaptersIgnored,
  // True iff there are checkable series (parent computes from libraryEntries)
  hasCheckableSeries,
  // settings.defaults.komikku: volumes are shown but can't be queued.
  komikku = false,
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

  // ── Per-chapter controls ──
  // Which rows are expanded, which chapters the user unticked, and which rows
  // have a cross-out write in flight. All three are TRANSIENT and live here on
  // purpose: a tick means "not in this queue click", so it should not outlive
  // the panel, whereas a cross-out is persisted by main and comes back on the
  // row itself. Keyed by folderPath, so a row replaced by a fresh progress
  // event keeps its expansion instead of snapping shut mid-scan.
  const [expandedRows, setExpandedRows] = useState(() => new Set());
  // folderPath → Set of unticked chapter labels. Stored as DEselections so a
  // row whose chapter list grows (a later sweep found more) has the new ones
  // ticked by default without needing to re-seed anything.
  const [deselections, setDeselections] = useState(() => new Map());
  const [busyRows, setBusyRows] = useState(() => new Set());

  const toggleExpand = useCallback((folderPath) => {
    setExpandedRows((prev) => {
      const next = new Set(prev);
      if (next.has(folderPath)) next.delete(folderPath);
      else next.add(folderPath);
      return next;
    });
  }, []);

  const toggleChapter = useCallback((folderPath, chapter) => {
    setDeselections((prev) => {
      const next = new Map(prev);
      const set = new Set(next.get(folderPath) || []);
      if (set.has(chapter)) set.delete(chapter);
      else set.add(chapter);
      if (set.size === 0) next.delete(folderPath);
      else next.set(folderPath, set);
      return next;
    });
  }, []);

  // "all" clears the row's deselections; "none" deselects every chapter the
  // row currently offers. Reads the row rather than the stored Set so "none"
  // can't leave a chapter ticked that arrived after the last interaction.
  const selectAll = useCallback((row, on) => {
    setDeselections((prev) => {
      const next = new Map(prev);
      if (on) next.delete(row.folderPath);
      else next.set(row.folderPath, new Set(row.newChapters || []));
      return next;
    });
  }, []);

  // Volume twin of selectAll; volume ticks live under volumeKey(folderPath).
  const selectAllVolumes = useCallback((row, on) => {
    setDeselections((prev) => {
      const next = new Map(prev);
      if (on) next.delete(volumeKey(row.folderPath));
      else next.set(volumeKey(row.folderPath), new Set(row.newVolumes || []));
      return next;
    });
  }, []);

  // Cross out / restore. The row itself is updated by the hook once the write
  // lands; all this owns is the in-flight flag that greys the chips, so a
  // double click can't fire two writes for the same series.
  // `kind` "volume" writes the volume namespace (useUpdateCheck setRowIgnored).
  const setIgnored = useCallback(
    async (folderPath, chapters, ignored, kind = "chapter") => {
      if (!onSetChaptersIgnored) return;
      setBusyRows((prev) => new Set(prev).add(folderPath));
      try {
        await onSetChaptersIgnored(folderPath, chapters, ignored, kind);
      } finally {
        setBusyRows((prev) => {
          const next = new Set(prev);
          next.delete(folderPath);
          return next;
        });
      }
      // A crossed-out chapter leaves newChapters, so any deselection of it is
      // now dead weight; dropping it keeps "all/none" honest if it comes back.
      if (ignored) {
        const key = kind === "volume" ? volumeKey(folderPath) : folderPath;
        setDeselections((prev) => {
          const set = prev.get(key);
          if (!set) return prev;
          const nextSet = new Set(set);
          for (const c of chapters) nextSet.delete(c);
          if (nextSet.size === set.size) return prev;
          const next = new Map(prev);
          if (nextSet.size === 0) next.delete(key);
          else next.set(key, nextSet);
          return next;
        });
      }
    },
    [onSetChaptersIgnored]
  );

  const chapterCtl = useMemo(
    () => ({
      isExpanded: (folderPath) => expandedRows.has(folderPath),
      toggleExpand,
      deselectedFor: (folderPath) => deselections.get(folderPath),
      toggleChapter,
      selectAll,
      selectAllVolumes,
      setIgnored,
      isBusy: (folderPath) => busyRows.has(folderPath),
    }),
    [expandedRows, deselections, busyRows, toggleExpand, toggleChapter, selectAll, selectAllVolumes, setIgnored]
  );

  // The row's ticked volumes ([] under Komikku: they can't be queued then).
  const volumesFor = useCallback(
    (row) => (komikku ? [] : selectedChapters(row.newVolumes || [], deselections.get(volumeKey(row.folderPath)))),
    [deselections, komikku]
  );

  // The ticks are panel state, so the panel — not LibraryTab — is what turns a
  // row into "these exact chapters". Both queue paths go through
  // selectedChapters so the button, the footer and the +N pill can't disagree
  // about what a click will download.
  const queueRow = useCallback(
    (row) => {
      const chapters = selectedChapters(row.newChapters || [], deselections.get(row.folderPath));
      const volumes = volumesFor(row);
      if (chapters.length === 0 && volumes.length === 0) return;
      onQueueRow(row, chapters, volumes);
    },
    [deselections, onQueueRow, volumesFor]
  );

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
    found.sort(
      (a, b) =>
        (b.newChapters?.length || 0) + (b.newVolumes?.length || 0)
        - ((a.newChapters?.length || 0) + (a.newVolumes?.length || 0))
    );
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

  // What "Queue all" would actually start: every found row paired with its
  // ticked chapters, rows with nothing ticked dropped entirely. Computed once
  // so the button's label, its tooltip and its disabled state all read the
  // same plan the click executes.
  const queueAllPlan = useMemo(
    () =>
      groups.found
        .map((row) => ({
          row,
          chapters: selectedChapters(row.newChapters || [], deselections.get(row.folderPath)),
          volumes: volumesFor(row),
        }))
        .filter((item) => item.chapters.length + item.volumes.length > 0),
    [groups.found, deselections, volumesFor]
  );
  const queueAllChapterCount = useMemo(
    () => queueAllPlan.reduce((n, item) => n + item.chapters.length, 0),
    [queueAllPlan]
  );
  const queueAllVolumeCount = useMemo(
    () => queueAllPlan.reduce((n, item) => n + item.volumes.length, 0),
    [queueAllPlan]
  );
  const queueAll = useCallback(() => {
    if (queueAllPlan.length > 0) onQueueAll(queueAllPlan);
  }, [queueAllPlan, onQueueAll]);

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
    // A cancelled sweep left most series unchecked, so "ALL CLEAR" would be
    // a lie — say CANCELLED unless it actually found something first.
    const cancelledClean = scanStats.aborted && totalFound === 0;
    statusLine = (
      <>
        <span className={cn(
          totalFound > 0
            ? "text-orange-300"
            : cancelledClean
            ? "text-zinc-400"
            : "text-emerald-300/90"
        )}>
          {totalFound > 0
            ? `${totalFound} UPDATES`
            : cancelledClean
            ? "CANCELLED"
            : "ALL CLEAR"}
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
                onQueue={queueRow}
                onDismiss={(row) => onDismiss([row.folderPath])}
                chapterCtl={chapterCtl}
                komikku={komikku}
              />
              <Section
                theme={SECTION_THEME.active}
                rows={groups.active}
                defaultOpen={isScanning}
                count={groups.active.length}
              />
              {/* Up-to-date rows get the chapter controls too: a series whose
                  every missing chapter is crossed out reports up to date, and
                  this is the only place those crosses can be undone. */}
              <Section
                theme={SECTION_THEME.uptodate}
                rows={groups.uptodate}
                defaultOpen={false}
                chapterCtl={chapterCtl}
                komikku={komikku}
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
                onClick={queueAll}
                disabled={queueAllPlan.length === 0}
                className={cn(
                  "flex-1 gap-1.5 text-xs font-semibold",
                  "bg-orange-500 hover:bg-orange-500/90 text-white",
                  "shadow-[0_0_20px_-6px_rgba(249,115,22,0.6)]",
                  "disabled:opacity-50 disabled:shadow-none"
                )}
                title={
                  queueAllChapterCount + queueAllVolumeCount === 0
                    ? "Every new chapter is unticked or crossed out"
                    : `${[
                        queueAllChapterCount > 0 && `${queueAllChapterCount} chapter${queueAllChapterCount === 1 ? "" : "s"}`,
                        queueAllVolumeCount > 0 && `${queueAllVolumeCount} volume${queueAllVolumeCount === 1 ? "" : "s"}`,
                      ].filter(Boolean).join(" + ")} across ${queueAllPlan.length} series`
                }
              >
                <Download className="w-3.5 h-3.5" />
                {/* Counts SERIES, as it always has — but only the ones that
                    still have something ticked, so the number matches what the
                    click actually queues. */}
                Queue all {queueAllPlan.length} update{queueAllPlan.length === 1 ? "" : "s"}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => onDismiss(groups.found.map((r) => r.folderPath))}
                className="text-xs text-zinc-500 hover:text-zinc-300"
                title="Clear every badge for now without downloading — saves nothing, so the next check reports them all again"
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
