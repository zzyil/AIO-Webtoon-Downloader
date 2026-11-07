// ============================================================
// SyncCards — the Sync tab's callouts: refusal cards, the last-run card and
// warning callouts (approved design spec §4 slot 2 and §5 "Refusal panel" /
// "Last-run card"; mockup refusals(), LAST_RUNS).
//
// They sit between the target bar and the tiles. Every card only REPORTS and
// offers the recovery action for its code; the action itself is the
// caller's (`onAction(kind, arg)`), so the cards stay free of IPC:
//   review · recheck · edit-target · forget-record · set-verify-mode(mode) ·
//   open-settings(category) · open-log · dismiss
//
// Words: copy.js (refusalTitle, refusalCardCopy, blockingCopy, warningCopy);
// last-run content: syncView.js lastRunView. Consumer: SyncTab.jsx, and
// ReviewDialog.jsx for an apply refused from the dialog.
// ============================================================

import React, { useState } from "react";
import {
  AlertCircle, AlertTriangle, CheckCircle2, CircleDashed, FileWarning, FolderX, History, ListChecks,
  Loader2, RefreshCw, ShieldCheck, StopCircle, Unlink, X, XCircle,
} from "lucide-react";
import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { useArm } from "./TargetEditor";
import { blockingCopy, refusalCardCopy, refusalTitle, VERIFY_MODE_COPY, warningCopy } from "./copy";

const ICONS = {
  "shield-check": ShieldCheck,
  "list-checks": ListChecks,
  "alert-triangle": AlertTriangle,
  "file-warning": FileWarning,
  unlink: Unlink,
  loader: Loader2,
  "folder-x": FolderX,
  "refresh-cw": RefreshCw,
  "x-circle": XCircle,
  "circle-dashed": CircleDashed,
  "alert-circle": AlertCircle,
  check: CheckCircle2,
  stop: StopCircle,
  x: XCircle,
};

// tone → [callout surface, icon colour]. Tokens only; never orange (spec §2).
export const TONE = {
  info: ["bg-info/[.08] border-info/25", "text-info-fg"],
  warn: ["bg-warning/[.09] border-warning/30", "text-warn-fg"],
  bad: ["bg-destructive/[.07] border-destructive/30", "text-destructive"],
  muted: ["bg-muted border-border", "text-muted-foreground"],
  ok: ["bg-success/[.08] border-success/25", "text-success"],
};

export function Callout({ tone = "info", icon, children, className }) {
  const [surface, fg] = TONE[tone] || TONE.info;
  const Icon = typeof icon === "string" ? ICONS[icon] : icon;
  return (
    <div className={cn("flex gap-2.5 rounded-lg border px-3 py-2.5 text-[12.5px] leading-[1.45]", surface, className)}>
      {Icon && <Icon className={cn("mt-px h-4 w-4 shrink-0", fg, Icon === Loader2 && "animate-spin motion-reduce:animate-none")} aria-hidden="true" />}
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

/** The two-card chooser of a `needs-mode` refusal (stored as the target's verifyMode). */
function ModeChooser({ tn, modes, onPick }) {
  const list = (modes && modes.length ? modes : ["read-back", "adopt-size"]).filter((m) => VERIFY_MODE_COPY[m]);
  const [mode, setMode] = useState(list[0]);
  return (
    <>
      <div className="mt-2 grid grid-cols-2 gap-2" role="radiogroup" aria-label={`How to check ${tn}`}>
        {list.map((m) => (
          <button
            key={m}
            type="button"
            role="radio"
            aria-checked={mode === m}
            onClick={() => setMode(m)}
            className={cn(
              "rounded-lg border bg-card p-2.5 text-left transition-colors hover:bg-accent/40",
              mode === m && "shadow-[inset_0_0_0_1px_hsl(var(--primary)/.5)]"
            )}
          >
            <div className="flex items-center gap-2 text-[13px] font-semibold">
              <span className={cn("grid h-4 w-4 place-items-center rounded-full border border-primary", mode === m && "after:h-2 after:w-2 after:rounded-full after:bg-primary after:content-['']")} />
              {VERIFY_MODE_COPY[m].label}
            </div>
            <div className="mt-1 text-xs text-muted-foreground">{VERIFY_MODE_COPY[m].hint}</div>
          </button>
        ))}
      </div>
      <div className="mt-2">
        <Button size="sm" className="h-[26px] px-2.5 text-[11.5px]" onClick={() => onPick(mode)}>
          Use {VERIFY_MODE_COPY[mode].label.toLowerCase()} and plan
        </Button>
      </div>
    </>
  );
}

const XS = "h-[26px] gap-1.5 px-2.5 text-[11.5px] [&_svg]:h-[13px] [&_svg]:w-[13px]";

/** What a blocked-checklist item offers (refusal.blocking[].kind). */
function blockingAction(kind) {
  switch (kind) {
    case "plan-changed":
      return ["Review again", "review"];
    case "listing-sanity":
      return ["Re-check", "recheck"];
    case "loss":
    case "mass-delete":
    case "mass-repush":
      return ["Confirm in Review", "review"];
    case "free-space":
      return ["Review", "review"];
    default:
      return null;
  }
}

/**
 * One refusal (a plan's `refusal`, an action's reply, or an apply refused at
 * start). `onDismiss` adds a close button (transient replies only: a plan's
 * refusal clears when the next plan lands).
 */
export function RefusalCard({ refusal: r, tn, onAction, onDismiss, compact, className }) {
  const { armed, arm, disarm } = useArm();
  if (!r) return null;
  const c = refusalCardCopy(r, tn);
  const title = r.internal ? "Something went wrong inside sync" : r.code === "blocked" ? blockedTitle(r) : refusalTitle(r.code, tn, r) || "Sync stopped";
  const act = (kind, arg) => onAction && onAction(kind, arg);
  const btn = (label, kind, variant = "outline", Icon = null, arg) => (
    <Button key={kind + label} size="sm" variant={variant} className={XS} onClick={() => act(kind, arg)}>
      {Icon && <Icon aria-hidden="true" />}
      {label}
    </Button>
  );
  let actions = [];
  switch (r.internal ? "internal" : r.code) {
    case "record-mismatch":
      actions = [
        btn("Edit target", "edit-target"),
        armed === "forget" ? (
          <Button key="forget" size="sm" variant="destructive" className={XS} onClick={() => { disarm(); act("forget-record"); }}>
            <History aria-hidden="true" />
            Click again to forget history for {tn}
          </Button>
        ) : (
          <Button key="forget" size="sm" variant="outline" className={cn(XS, "border-destructive/45 text-destructive hover:bg-destructive/[.08] hover:text-destructive")} onClick={() => arm("forget")}>
            Start fresh…
          </Button>
        ),
      ];
      break;
    case "device-listing-suspect":
      actions = [btn("Re-check", "recheck", "default", RefreshCw), btn(`${tn} settings`, "edit-target", "ghost")];
      break;
    case "disconnected":
      actions = [btn("Re-check", "recheck", "outline", RefreshCw)];
      break;
    case "library-missing":
      actions = [btn("Open Settings", "open-settings", "outline", null, "general")];
      break;
    case "version-conflict":
      actions = [btn("Use current settings", "dismiss", "default")];
      break;
    case "disabled":
      actions = [btn("Open Settings", "open-settings", "outline", null, "sync")];
      break;
    case "internal":
      actions = [btn("Open log folder", "open-log")];
      break;
    case "invalid":
      actions = onDismiss ? [] : [btn("Re-check", "recheck", "outline", RefreshCw)];
      break;
    default:
      break;
  }
  return (
    <Callout tone={c.tone} icon={c.icon} className={className}>
      <div className="flex items-start gap-2">
        <b className="flex-1 font-semibold">{title}</b>
        {!compact && <span className="font-mono text-[11px] text-muted-foreground">{r.code}</span>}
        {onDismiss && (
          <button type="button" onClick={onDismiss} aria-label="Dismiss" className="-mr-1 rounded p-0.5 text-muted-foreground hover:bg-accent">
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
      {c.body && <div className="mt-0.5 text-muted-foreground">{c.body}</div>}
      {r.code === "needs-mode" && !r.internal && <ModeChooser tn={tn} modes={r.modes} onPick={(m) => act("set-verify-mode", m)} />}
      {r.code === "blocked" && Array.isArray(r.blocking) && (
        <div className="mt-2 flex flex-col gap-1.5">
          {r.blocking.map((b, i) => {
            const a = blockingAction(b.kind);
            return (
              <div key={i} className="flex items-center gap-2 text-[12.5px]">
                <CircleDashed className="h-3.5 w-3.5 shrink-0 text-warn-fg" aria-hidden="true" />
                <span className="flex-1">{blockingCopy(b, tn)}</span>
                {a && btn(a[0], a[1])}
              </div>
            );
          })}
        </div>
      )}
      {actions.length > 0 && <div className="mt-2 flex flex-wrap gap-2">{actions}</div>}
    </Callout>
  );
}

function blockedTitle(r) {
  const n = Array.isArray(r.blocking) ? r.blocking.length : 0;
  return n > 1 ? `Start is waiting on ${n} things` : "Start is waiting on you";
}

/** The last finished run on this target (syncView.lastRunView output). */
export function LastRunCard({ view, tn, onAction, onDismiss }) {
  if (!view) return null;
  const Icon = ICONS[view.icon] || CheckCircle2;
  const fg = { ok: "text-success", muted: "text-muted-foreground", warn: "text-warn-fg", bad: "text-destructive" }[view.tone];
  return (
    <div className="rounded-lg border bg-card px-3 py-2.5 shadow-sm">
      <div className="flex items-start gap-2">
        <Icon className={cn("mt-px h-4 w-4 shrink-0", fg)} aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <b className="flex-1 text-[13px] font-semibold">{view.title}</b>
          </div>
          <div className="mt-0.5 font-mono text-xs tabular-nums">{view.line}</div>
          {view.note && <div className="mt-0.5 text-xs text-muted-foreground">{view.note}</div>}
          {view.skipped.map((s, i) => (
            <div key={i} className="mt-0.5 text-xs text-muted-foreground">{s}</div>
          ))}
          {view.refusal && <RefusalCard refusal={view.refusal} tn={tn} onAction={onAction} compact className="mt-2" />}
          {view.action && (
            <div className="mt-1.5">
              <Button size="sm" variant="outline" className={XS} onClick={() => onAction && onAction("recheck")}>
                <RefreshCw aria-hidden="true" />
                {view.action === "recheck-space" ? "Free up space, then Re-check" : "Re-check"}
              </Button>
            </div>
          )}
        </div>
        {onDismiss && (
          <button type="button" onClick={onDismiss} aria-label="Dismiss" className="-mr-1 rounded p-0.5 text-muted-foreground hover:bg-accent">
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
    </div>
  );
}

/** Plan warnings as co-warn callouts (spec §5 "Not drawn but implied"). */
export function WarningCallouts({ warnings, tn }) {
  const [all, setAll] = useState(false);
  const list = (warnings || []).map((w) => warningCopy(w, tn) || w.message || w.kind).filter(Boolean);
  const uniq = [...new Set(list)];
  if (!uniq.length) return null;
  const shown = all ? uniq : uniq.slice(0, 2);
  return (
    <Callout tone="warn" icon="alert-triangle">
      {shown.map((t, i) => (
        <div key={i}>{t}</div>
      ))}
      {uniq.length > shown.length && (
        <button type="button" className="mt-0.5 text-xs font-medium text-primary hover:underline" onClick={() => setAll(true)}>
          {uniq.length - shown.length} more
        </button>
      )}
    </Callout>
  );
}

