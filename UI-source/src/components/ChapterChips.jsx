// ============================================================
// CHAPTER CHIPS — per-chapter controls for the update-check surfaces
//
// One chip per chapter an update check reported as missing. A chip carries TWO
// separate gestures, and the difference between them is SCOPE:
//
//   TICK — click the chip body. Changes what THIS download queues, nothing
//     else. Lives in the calling panel's own state, is never written to disk,
//     and is gone the moment the panel closes or a new sweep starts. It exists
//     so "grab 1-3 now, the rest later" doesn't require crossing anything out.
//
//   CROSS OUT — click the ×. Writes .aio_series.json:chapters_ignored (via
//     useUpdateCheck.setRowIgnored → main's set-chapters-ignored IPC). Survives
//     app restarts. The chapter keeps being REPORTED by every future check —
//     struck through, with an undo arrow — but no download button will ever
//     queue it until it is restored.
//
// In one line: tick means "not in this batch", cross out means "not until I say
// otherwise". Both are reversible, which is why neither one HIDES the chapter.
//
// The glyphs carry that distinction, because two identical-looking buttons on
// one chip is exactly how a user ends up unsure which does what: the tick side
// shows a real checkbox (ticked / empty), and a crossed-out chip loses the
// checkbox entirely — it is no longer part of the selection at all, so offering
// it a tick would be a lie. ChapterChipsLegend below is the words for the same
// distinction, exported so both surfaces say it identically.
//
// Shared by the two places a check result is rendered, so they can't drift:
//   - components/UpdatesCenter.jsx  — the "Check All" panel's expanded row
//   - components/LibraryTab.jsx     — one series' detail-view UpdateSection
// Colors stick to the orange "new chapter" accent both surfaces already use
// plus the shared HSL tokens, so it reads native in either.
// ============================================================

import React from "react";
import { X, RotateCcw, CheckSquare, Square } from "lucide-react";
import { cn } from "@/lib/utils";

// One chapter. `state` is "on" (ticked) | "off" (unticked) | "ignored".
//
// The chip is a <button> for the tick with a SIBLING button for the cross-out —
// nested buttons are invalid HTML and swallow the inner click, so the two live
// side by side inside a bordered span instead.
// `noun` is "chapter" or "volume" (the volume rows of the update check reuse
// these chips); it only changes the words.
// No `onCrossOut` → no cross-out button: LibraryTab's "Get volumes" browse
// view passes none, because a cross-out there would write a volume block and
// so make a chapter-only series track volumes (series-ledger.js trackedKinds).
function ChapterChip({ chapter, state, onToggle, onCrossOut, onRestore, disabled, noun = "chapter" }) {
  const ignored = state === "ignored";
  const on = state === "on";
  const Noun = noun.charAt(0).toUpperCase() + noun.slice(1);
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md border overflow-hidden",
        "font-mono text-[10px] leading-none transition-colors",
        ignored
          ? "border-border/60 bg-muted/20 text-muted-foreground/60"
          : on
          ? "border-orange-500/40 bg-orange-500/15 text-orange-200"
          : "border-border bg-transparent text-muted-foreground"
      )}
    >
      <button
        type="button"
        disabled={disabled || ignored}
        onClick={onToggle}
        // aria-pressed only describes the tick, so an ignored chip (whose body
        // is inert) reports nothing rather than lying about a toggle state.
        aria-pressed={ignored ? undefined : on}
        // Tooltips name the SCOPE, not just the action — "won't download" is
        // the one thing both gestures have in common and so the one thing that
        // can't tell them apart.
        title={
          ignored
            ? `${Noun} ${chapter} is crossed out — left out of every check until you restore it`
            : on
            ? `${Noun} ${chapter} will download. Click to skip it in this download only.`
            : `${Noun} ${chapter} is skipped for this download. Click to put it back in.`
        }
        className={cn(
          "flex items-center gap-1 px-1.5 py-1 tabular-nums transition-colors",
          ignored && "line-through decoration-muted-foreground/50 cursor-default",
          !ignored && "hover:bg-white/[0.06]",
          disabled && "opacity-40"
        )}
      >
        {/* No checkbox on a crossed-out chip: it is out of the selection
            entirely, so a tick state would be meaningless there. */}
        {!ignored &&
          (on ? (
            <CheckSquare className="w-2.5 h-2.5 shrink-0 text-orange-400" aria-hidden />
          ) : (
            <Square className="w-2.5 h-2.5 shrink-0 opacity-60" aria-hidden />
          ))}
        {chapter}
      </button>
      {onCrossOut && (
      <button
        type="button"
        disabled={disabled}
        onClick={ignored ? onRestore : onCrossOut}
        title={
          ignored
            ? `Restore ${noun} ${chapter} — makes it downloadable again`
            : `Cross out ${noun} ${chapter} — saved to this series, so every future check leaves it out until you restore it`
        }
        aria-label={ignored ? `Restore ${noun} ${chapter}` : `Cross out ${noun} ${chapter}`}
        className={cn(
          "px-1 py-1 border-l transition-colors",
          ignored
            ? "border-border/60 text-muted-foreground/70 hover:text-emerald-400 hover:bg-emerald-500/10"
            : "border-orange-500/25 text-muted-foreground hover:text-red-400 hover:bg-red-500/10",
          disabled && "opacity-40"
        )}
      >
        {ignored ? <RotateCcw className="w-2.5 h-2.5" /> : <X className="w-2.5 h-2.5" />}
      </button>
      )}
    </span>
  );
}

// The words for the two gestures, glyph-led so each line maps onto the control
// it describes. Exported (rather than written inline at each surface) because
// the whole risk here is the two surfaces explaining the same two buttons
// differently.
//
// `hasChapters` false drops the tick line: a series whose every missing chapter
// is crossed out has nothing to tick, and explaining a control that isn't on
// screen is worse than saying nothing.
// `actions` is an optional node pinned to the right of the first line (the
// callers put their "all / none" shortcuts there).
// `crossOut` false drops the cross-out line, for chips rendered without one.
export function ChapterChipsLegend({ hasChapters = true, actions, className, noun = "chapter", crossOut = true }) {
  return (
    <div className={cn("space-y-1 text-[10px] leading-snug text-muted-foreground", className)}>
      {hasChapters ? (
        <div className="flex items-start gap-1.5">
          <CheckSquare className="w-3 h-3 mt-px shrink-0 text-orange-400/80" aria-hidden />
          <span className="flex-1">
            <span className="text-foreground/80">Click a {noun}</span> to include or skip it —{" "}
            <span className="text-foreground/80">this download only</span>.
          </span>
          {actions}
        </div>
      ) : (
        actions && <div className="flex items-start justify-end gap-1.5">{actions}</div>
      )}
      {crossOut && (
      <div className="flex items-start gap-1.5">
        <X className="w-3 h-3 mt-px shrink-0 text-red-400/80" aria-hidden />
        <span className="flex-1">
          <span className="text-foreground/80">Cross it out</span> to keep it out of{" "}
          <span className="text-foreground/80">every future check</span> — saved to the series,
          undo with{" "}
          <RotateCcw className="inline-block w-2.5 h-2.5 align-[-1px] text-emerald-400/80" aria-hidden />{" "}
          any time.
        </span>
      </div>
      )}
    </div>
  );
}

// The full chip list for one series.
//
// Props:
//   chapters   — downloadable chapter labels (the check's newChapters)
//   ignored    — crossed-out labels (the check's ignoredChapters)
//   deselected — Set of `chapters` entries the user has unticked. Owned by the
//                caller, because "queue all" has to read every row's ticks.
//   onToggle(chapter)      — flip one tick
//   onSetIgnored(chapters, ignored) — cross out / restore (array = one write);
//                omit it to render chips with no cross-out button
//   busy       — disables everything while a write is in flight
//   noun       — "chapter" (default) or "volume": wording only
//
// ORDER: crossed-out chapters are interleaved in numeric order rather than
// pushed to the end, so the list still reads as the series' chapter sequence
// and a cross-out doesn't make a chapter appear to move.
export default function ChapterChips({
  chapters = [],
  ignored = [],
  deselected,
  onToggle,
  onSetIgnored,
  busy = false,
  noun = "chapter",
}) {
  const ignoredSet = React.useMemo(() => new Set(ignored), [ignored]);
  const ordered = React.useMemo(() => {
    const all = [...chapters, ...ignored];
    return all.sort((a, b) => {
      const fa = parseFloat(a);
      const fb = parseFloat(b);
      if (Number.isNaN(fa) || Number.isNaN(fb)) return String(a).localeCompare(String(b));
      return fa - fb;
    });
  }, [chapters, ignored]);

  if (ordered.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-1">
      {ordered.map((ch) => {
        const isIgnored = ignoredSet.has(ch);
        return (
          <ChapterChip
            key={ch}
            chapter={ch}
            state={isIgnored ? "ignored" : deselected?.has(ch) ? "off" : "on"}
            disabled={busy}
            noun={noun}
            onToggle={() => onToggle?.(ch)}
            onCrossOut={onSetIgnored ? () => onSetIgnored([ch], true) : undefined}
            onRestore={onSetIgnored ? () => onSetIgnored([ch], false) : undefined}
          />
        );
      })}
    </div>
  );
}

// The chapters a row would actually queue: everything reported downloadable
// minus what the user unticked. Exported so UpdatesCenter's per-row button,
// its "Queue all" footer and LibraryTab's detail view all answer the question
// the same way — a second copy of this one-liner is how they would drift.
// Crossed-out chapters are already absent from `chapters` (main.js splits them
// off into ignoredChapters), so they need no filtering here.
export function selectedChapters(chapters, deselected) {
  if (!chapters || chapters.length === 0) return [];
  if (!deselected || deselected.size === 0) return [...chapters];
  return chapters.filter((c) => !deselected.has(c));
}
