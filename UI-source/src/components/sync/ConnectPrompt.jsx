// ============================================================
// ConnectPrompt — the bottom-right connect cards (UI pass U3, spec §4
// "Connect prompt": a 340 px corner card, non-blocking, over any tab)
//
// Draws connectView.connectCards() over the hook's live state; it decides
// nothing about WHEN a card is open (main's PromptMachine does: pending /
// prompts / withdraw) and never answers a prompt on the user's behalf.
// Buttons:
//   Review     prompt-response review, then App opens the Sync tab on that
//              target's series list (U3-2, onReview)
//   Set up     config-op update-target {verifyMode:'read-back'}: the edit's
//              own re-plan reads the tablet back; the card stays open and
//              follows the job ("Reading back N%"), then turns ready (U3-10)
//   Not now/✕  prompt-response not-now (a Preparing card too: U3-3)
//   I've allowed it / Re-check   list-devices, which main feeds into its
//              device diff (U3-7); ✕ on a tablet card hides it until its
//              state changes
// Mounted once by App.jsx, outside the tab area (like ConfirmQuitDialog).
// Depends on: hooks/useDeviceSync.js (the `sync` prop), connectView.js,
// useConfigOps.js. Design record: sync-temp/plans/device-sync-ui-pass.md
// "Phase U3 design".
// ============================================================

import React, { useEffect, useMemo, useRef, useState } from "react";
import { Tablet, AlertCircle, AlertTriangle, X, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { badgeText, connectCards } from "./connectView";
import { configRefusalCopy } from "./copy";
import { useConfigOps } from "./useConfigOps";

const TICK_MS = 500;

/**
 * The Sync rail button's badge (connectView.railBadge's {count, dot}): the
 * count pill top-right, and the dot top-right alone or top-left beside a
 * count, so the two never stack. Consumers: App.jsx (the rail), the gallery.
 */
export function RailBadge({ badge }) {
  if (!badge) return null;
  return (
    <>
      {badge.count > 0 && (
        <div data-testid="sync-rail-count" className="absolute -top-0.5 -right-0.5 flex h-4 min-w-[16px] items-center justify-center rounded-full bg-primary px-1 text-[9px] font-bold tabular-nums text-primary-foreground">
          {badgeText(badge.count)}
        </div>
      )}
      {badge.dot && (
        <div
          data-testid="sync-rail-dot"
          data-dot={badge.dot}
          className={cn("absolute top-1 h-2 w-2 rounded-full", badge.count > 0 ? "left-1" : "right-1", badge.dot === "red" ? "bg-destructive" : "sync-pulse bg-primary")}
        />
      )}
    </>
  );
}

/** One card's body. Exported for the DEV gallery (src/dev/SyncGallery.jsx). */
export function ConnectCard({ card, busy, error, onAction, onClose }) {
  const bad = card.tone === "bad";
  const multi = card.kind === "prompt" && card.lines && card.lines.length > 1;
  return (
    <div className="px-3.5 py-3" data-testid="sync-connect-card" data-kind={card.kind}>
      <div className="mb-1.5 flex items-center gap-2">
        {bad ? <AlertCircle className="h-4 w-4 shrink-0 text-destructive" /> : <Tablet className="h-4 w-4 shrink-0 text-primary" />}
        <b className="min-w-0 flex-1 truncate text-[13px] font-semibold">{card.title}</b>
        {onClose && (
          <button type="button" onClick={onClose} title="Dismiss" aria-label="Dismiss" className="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground">
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
      {card.spinner && (
        <div className="flex items-center gap-2 text-[12.5px] text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin motion-reduce:animate-none" />
          {card.spinner}
        </div>
      )}
      {card.rows && card.rows.length > 0 && (
        <div className="mb-1.5 flex flex-col gap-[3px]">
          {card.rows.map((r, i) => (
            <div key={i} className="flex items-center gap-2 text-[12.5px]">
              <span className={cn("shrink-0 truncate text-muted-foreground", multi ? "w-[104px]" : "w-14")} title={r.k}>
                {r.k}
              </span>
              <span className="min-w-0 flex-1 truncate" title={r.tip || r.v}>
                {r.v}
              </span>
              {r.warn ? <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-warn-fg" /> : null}
              {r.s ? <span className="shrink-0 font-mono text-xs">{r.s}</span> : null}
            </div>
          ))}
        </div>
      )}
      {card.body && <div className="mb-2 text-[12.5px] leading-[1.45] text-muted-foreground">{card.body}</div>}
      {card.progress != null && (
        <div className="mb-2 h-1.5 overflow-hidden rounded-full bg-muted" role="progressbar" aria-valuenow={card.progress} aria-valuemin={0} aria-valuemax={100}>
          <i className="block h-full bg-primary" style={{ width: `${card.progress}%` }} />
        </div>
      )}
      {card.note && <div className="mb-2 text-[11px] text-muted-foreground">{card.note}</div>}
      {error && <div className="mb-2 text-[11.5px] text-destructive">{error}</div>}
      {(card.primary || card.kind === "prompt" || card.kind === "preparing") && (
        <div className="flex items-center gap-2">
          {card.primary && (
            <Button size="sm" variant={card.kind === "device" ? "outline" : "default"} className="h-7 px-2.5 text-xs" disabled={busy} onClick={() => onAction(card.primary)}>
              {card.primary.label}
            </Button>
          )}
          {(card.kind === "prompt" || card.kind === "preparing") && (
            <Button size="sm" variant="ghost" className="h-7 px-2.5 text-xs" disabled={busy} onClick={onClose}>
              Not now
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * @param {object} p
 * @param {object} p.sync            useDeviceSync()
 * @param {string|null} p.hiddenTargetId  the target the Sync tab shows now (U3-13)
 * @param {(targetId:string) => void} p.onReview  open the Sync tab on it
 */
export default function ConnectPrompt({ sync, hiddenTargetId, onReview }) {
  const ops = useConfigOps(sync);
  const [now, setNow] = useState(() => Date.now());
  const [busy, setBusy] = useState({});
  const [errors, setErrors] = useState({});
  const [dismissedDevices, setDismissedDevices] = useState(() => new Set());
  // When each tablet entered its current state: the spinner cards wait out
  // PREPARE_GRACE_MS, so a tablet passing through "connecting" never flashes one.
  const sinceRef = useRef({});
  const devices = sync.devices || [];
  const deviceKeys = devices.map((d) => `dev:${d.serial}:${d.stateKind || d.state}`);
  const keySig = deviceKeys.join("|");
  const deviceSince = useMemo(() => {
    const next = {};
    for (const k of keySig ? keySig.split("|") : []) next[k] = sinceRef.current[k] != null ? sinceRef.current[k] : Date.now();
    sinceRef.current = next;
    return next;
  }, [keySig]);
  // A tablet card dismissed with ✕ stays hidden until that state ends.
  useEffect(() => {
    setDismissedDevices((s) => {
      const live = new Set(keySig ? keySig.split("|") : []);
      const kept = [...s].filter((k) => live.has(k));
      return kept.length === s.size ? s : new Set(kept);
    });
  }, [keySig]);

  // Re-render while something waits out its grace.
  const waiting = (sync.pending || []).length > 0 || deviceKeys.some((k) => /:(authorizing|connecting)$/.test(k));
  useEffect(() => {
    if (!waiting) return undefined;
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(t);
  }, [waiting]);

  const settings = sync.settings || {};
  const { cards, more } = connectCards({
    prompts: sync.prompts,
    pending: sync.pending,
    plans: sync.plans,
    job: sync.job,
    devices,
    targets: (sync.config && sync.config.targets) || [],
    promptOnConnect: settings.syncPromptOnConnect !== false,
    now,
    deviceSince,
    dismissedDevices,
    hiddenTargetId,
  });
  if (!sync.enabled || !cards.length) return null;

  // The NAME refusal copy interpolates: the action's target, or the card's
  // only target, else a neutral word. card.title is a sentence ("X
  // connected"), so it read "X connected isn't connected" (review low).
  const configTargets = (sync.config && sync.config.targets) || [];
  const nameFor = (card, a) => {
    const id = (a && a.targetId) || ((card.targetIds || []).length === 1 ? card.targetIds[0] : null);
    const t = id ? configTargets.find((x) => x.id === id) : null;
    return (t && t.name) || "the tablet";
  };
  const run = async (card, fn, a) => {
    setBusy((b) => ({ ...b, [card.key]: true }));
    setErrors((e) => ({ ...e, [card.key]: null }));
    try {
      const r = await fn();
      if (r && r.ok === false) setErrors((e) => ({ ...e, [card.key]: configRefusalCopy(r, nameFor(card, a)) }));
    } finally {
      setBusy((b) => ({ ...b, [card.key]: false }));
    }
  };

  const onAction = (card, a) => {
    if (a.action === "review") {
      return run(
        card,
        async () => {
          // Open the tab either way (the user asked to look); a refused
          // answer (the prompt already closed) still shows on the card.
          const r = await sync.actions.promptResponse({ promptId: card.promptId, action: "review" });
          onReview(a.targetId);
          return r;
        },
        a,
      );
    }
    if (a.action === "setup") return run(card, () => ops.run("update-target", { targetId: a.targetId, patch: { verifyMode: "read-back" } }), a);
    if (a.action === "recheck") return run(card, () => sync.actions.listDevices(), a);
    return null;
  };

  const onClose = (card) => {
    if (card.kind === "device") {
      setDismissedDevices((s) => new Set([...s, card.key]));
      return null;
    }
    return run(card, () => sync.actions.promptResponse({ promptId: card.promptId, action: "not-now" }));
  };

  return (
    <div className="pointer-events-none absolute bottom-4 right-4 z-[26] flex w-[340px] flex-col gap-2" role="status" aria-live="polite">
      {more > 0 && <div className="pointer-events-auto self-end rounded-md border bg-card px-2 py-0.5 text-[11px] text-muted-foreground shadow-sm">+{more} more</div>}
      {cards.map((card) => (
        <div key={card.key} className="pointer-events-auto overflow-hidden rounded-xl border bg-card shadow-[0_16px_40px_rgb(0_0_0/0.22)]">
          <ConnectCard card={card} busy={!!busy[card.key]} error={errors[card.key]} onAction={(a) => onAction(card, a)} onClose={() => onClose(card)} />
        </div>
      ))}
    </div>
  );
}

