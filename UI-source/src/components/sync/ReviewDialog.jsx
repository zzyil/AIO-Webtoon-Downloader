// ============================================================
// ReviewDialog — the one place Start lives (approved spec §4 "Review dialog";
// mockup reviewDialog)
//
// Data: sync:review (planner.js planReview + service gen/stale/capacity/
// policy/busy), re-read whenever the plan generation or the applied
// selection sequence moves. Start sends apply {gen} with the gen ON SCREEN
// (design D6: the review holds no planning controls, so whatever it shows
// is a plan that exists).
//
// Acknowledgements (UI decision 5, U2-2):
//   * one tick per series with a loss, stored as the sorted lossOpIds it was
//     ticked over (syncView lossSig); it counts, and sends its ids, only
//     while the series' current losses are exactly those (review R3);
//   * mass-delete / mass-repush ticks store the COUNT they were ticked at;
//     a tick only counts while it equals the review's current count, so a
//     selection that grows after the tick re-opens it (the service checks
//     the same and refuses `blocked` otherwise).
// The rebind name choice (previous / from the PC / don't copy back) sits in
// the mass-repush callout and goes through the selection client like any
// tick ({rebind}).
//
// A refusal to Start (blocked, plan-changed, busy…) shows inside the dialog;
// a started apply closes it (the progress dock takes over).
//
// Staleness (U2-11, review R6/R7): the review is dropped on every open, and
// Start needs the review's (gen, selSeq) to equal the pushed view's. A gen
// change the user didn't cause (an automatic re-plan; their own rebind is
// the one they did) that changes what the dialog shows (syncView.reviewSig)
// raises "The plan changed. Review it again." and clears the ticks; it is
// dismissed before Start enables, so a swapped plan is never started unread.
// A re-plan with identical content only moves the gen.
// Consumer: SyncTab.jsx. Gate logic: syncView.reviewGate / applyPayload.
// ============================================================

import React, { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight, HardDrive, Info, Play, AlertTriangle } from "lucide-react";
import { Button, Checkbox } from "@/components/ui/primitives";
import { Dialog } from "@/components/ui/overlay";
import { cn, formatBytes } from "@/lib/utils";
import { RefusalCard, TONE } from "./SyncCards";
import { SEG_CLASS } from "./ChapterStrip";
import { massDeleteCopy, opReasonCopy, reviewGroupTitle, seriesKeyName } from "./copy";
import { applyPayload, chLabel, clock, lossAcked, lossSig, plannedWithLine, reviewGate, reviewSig, workCount } from "./syncView";

const PAGE = 150;
const KIND_TONE = { push: "push", update: "push", replace: "warn", rename: "other", delete: "delete" };
const KIND_WORD = { push: "Copy", update: "Update", replace: "Replace", rename: "Rename", delete: "Remove" };

function groupTitle(g, tn) {
  const t = reviewGroupTitle(g.kind, g.reason, tn);
  if (t) return t;
  const why = opReasonCopy(g.kind, g.reason, tn);
  return why ? `${KIND_WORD[g.kind] || g.kind}: ${why[0].toLowerCase()}${why.slice(1)}` : KIND_WORD[g.kind] || g.kind;
}

function ReviewGroup({ g, tn, locked, pendingOps, onSelect }) {
  const [open, setOpen] = useState(false);
  const [shown, setShown] = useState(PAGE);
  const series = new Set(g.ops.map((o) => o.seriesKey)).size;
  const box = g.selectedCount === 0 ? false : g.selectedCount === g.count ? true : "mixed";
  const hasLoss = g.ops.some((o) => o.loss || o.lossIfSelected);
  const pending = g.ops.some((o) => pendingOps && pendingOps[o.opId]);
  const Chev = open ? ChevronDown : ChevronRight;
  return (
    <>
      <div className="flex items-center gap-2 border-b border-border/70 py-[7px]">
        <Checkbox
          checked={box}
          pending={pending}
          locked={locked}
          danger={g.kind === "delete"}
          aria-label={groupTitle(g, tn)}
          onCheckedChange={(v) => onSelect({ bulk: "group", group: g.kind, reason: g.reason, selected: v })}
        />
        <button type="button" className="flex min-w-0 flex-1 items-center gap-2 text-left" onClick={() => setOpen(!open)} aria-expanded={open}>
          <span className={cn("inline-block h-2.5 w-2.5 shrink-0 rounded-[3px]", SEG_CLASS[KIND_TONE[g.kind]] || SEG_CLASS.other)} />
          <span className="min-w-0 flex-1 truncate text-[13px]">
            {groupTitle(g, tn)}
            {hasLoss && <AlertTriangle className="ml-1 inline h-3 w-3 text-destructive" aria-label="Includes chapters that aren't on your PC" />}
          </span>
          <span className="shrink-0 font-mono text-xs text-muted-foreground">{series} series</span>
          <span className="w-[86px] shrink-0 whitespace-nowrap text-right font-mono text-xs tabular-nums">
            {g.selectedCount === g.count ? `${g.count} file${g.count === 1 ? "" : "s"}` : `${g.selectedCount} of ${g.count}`}
          </span>
          <span className="w-[60px] shrink-0 text-right font-mono text-xs tabular-nums">{g.kind === "delete" || !g.selectedBytes ? "—" : formatBytes(g.selectedBytes)}</span>
          <Chev className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
        </button>
      </div>
      {open &&
        g.ops.slice(0, shown).map((o) => (
          <div key={o.opId} className="flex items-center gap-2.5 border-b border-border/50 py-[5px] pl-6 text-[12.5px]">
            <Checkbox
              checked={!!o.selected}
              pending={!!(pendingOps && pendingOps[o.opId])}
              locked={locked}
              danger={o.kind === "delete"}
              aria-label={o.name}
              onCheckedChange={(v) => onSelect({ changes: [{ opId: o.opId, selected: v }], seriesKey: o.seriesKey })}
            />
            <span className="w-[140px] shrink-0 truncate">{o.series}</span>
            <span className="w-[62px] shrink-0 font-mono text-xs">{chLabel(o.label)}</span>
            <span className="min-w-0 flex-1 truncate text-muted-foreground" title={o.name}>{o.kind === "rename" ? `${o.from} → ${o.to}` : o.name}</span>
            <span className="w-[56px] shrink-0 text-right font-mono text-[11px]">{o.kind === "delete" || !o.size ? "" : formatBytes(o.size)}</span>
          </div>
        ))}
      {open && g.ops.length > shown && (
        <button type="button" className="w-full py-[7px] pl-6 text-left text-xs font-medium text-primary hover:underline" onClick={() => setShown(shown + PAGE)}>
          Show {Math.min(PAGE, g.ops.length - shown)} more
        </button>
      )}
    </>
  );
}

function rebindLabel(c) {
  return c.source === "previous" ? "previous name" : "from the PC";
}

const WHY = {
  stale: "The plan is being rebuilt",
  updating: "Updating the plan…",
  busy: "A sync job is writing to the target",
  saving: "Saving your last change…",
  changed: "The plan just changed; reloading",
  notice: "The plan changed. Review it again.",
  space: "Not enough free space",
  empty: "Nothing selected",
  loading: "Loading…",
};

/**
 * @param {{open, onClose, targetId, tn, wide, contained, sync, view, settled,
 *   locked, pendingOps, onSelect(req), onAction(kind, arg)}} props
 *   view = the target's pushed plans view {gen, selSeq, stale, …}.
 */
export default function ReviewDialog({ open, onClose, targetId, tn, wide, contained, sync, view, settled, locked, pendingOps, onSelect, onAction }) {
  const [review, setReview] = useState(null);
  const [acks, setAcks] = useState({ losses: new Map(), massDelete: null, massRepush: null });
  const [refusal, setRefusal] = useState(null);
  const [starting, setStarting] = useState(false);
  // R7: the gen this open first showed, whether a later gen came from the
  // user's own rebind, and the undismissed "plan changed" notice.
  // seenSig: reviewSig of what the user was last shown for that gen.
  const seenGen = useRef(null);
  const seenSig = useRef(null);
  // ownRebind: null, or {selSeq} = the review's selSeq when the user clicked
  // a rebind. Only a later review whose selSeq moved past it carries that
  // rebind (service.js set-selection advances selSeq only on success), so a
  // REFUSED rebind can't swallow the next automatic change's notice.
  const ownRebind = useRef(null);
  const [changedNotice, setChangedNotice] = useState(false);
  const gen = view ? view.gen : null;
  const selSeq = view ? view.selSeq : null;
  const blankAcks = () => ({ losses: new Map(), massDelete: null, massRepush: null });

  // Every open starts blank: the last open's review describes a selection
  // that may since have changed (R6), and its ticks belong to it. Cleared on
  // CLOSE as well as on open: an effect that clears on open runs in the same
  // commit as the "seen" effect below, which would still read the previous
  // open's review and record it as seen, so the reopen's first review
  // looked like a plan change (every reopen after a re-plan showed the
  // notice and dropped the ticks; found by the tab E2E).
  useEffect(() => {
    setReview(null);
    seenGen.current = null;
    seenSig.current = null;
    ownRebind.current = null;
    setChangedNotice(false);
    setAcks(blankAcks());
    setRefusal(null);
  }, [open, targetId]);
  useEffect(() => {
    if (!open) return undefined;
    let alive = true;
    sync.actions.review({ targetId }).then((r) => alive && setReview(r));
    return () => {
      alive = false;
    };
  }, [open, targetId, gen, selSeq, sync.actions]);
  // A new plan whose CONTENT differs (reviewSig), not caused by the user's
  // own rebind, raises the notice and asks for the ticks again: they were
  // given to what was shown. A new gen with identical content (a re-plan
  // that found nothing new) changes nothing the user sees. The user's own
  // selection changes keep the ticks: lossAcked and the mass counts already
  // re-check them against the current review (syncView.reviewGate).
  useEffect(() => {
    // A target switch while open has the same same-commit hazard: skip a
    // review answered for the previous target.
    if (!open || !review || !review.ok || review.targetId !== targetId) return;
    const sig = reviewSig(review);
    if (seenGen.current == null) {
      seenGen.current = review.gen;
    } else if (review.gen !== seenGen.current) {
      seenGen.current = review.gen;
      const mine = ownRebind.current && review.selSeq !== ownRebind.current.selSeq;
      if (mine) ownRebind.current = null;
      else if (sig !== seenSig.current) {
        setChangedNotice(true);
        setAcks(blankAcks());
        setRefusal(null);
      }
    }
    seenSig.current = sig;
  }, [open, review, targetId]);

  const gate = reviewGate(review, acks, { stale: view && view.stale, updating: !!(view && view.updating), settled, busy: locked, viewGen: gen, viewSelSeq: selSeq, changedNotice });
  const ok = review && review.ok;
  const files = ok ? workCount(review.totals) : 0;
  const cap = ok ? review.capacity : null;

  const start = async () => {
    setStarting(true);
    setRefusal(null);
    const r = await sync.actions.apply(applyPayload(targetId, review, acks));
    setStarting(false);
    if (r && r.ok) onClose();
    else setRefusal(r || { ok: false, code: "invalid", message: "no answer" });
  };
  const toggleLoss = (l, v) =>
    setAcks((a) => {
      const losses = new Map(a.losses);
      if (v) losses.set(l.seriesKey, lossSig(l.lossOpIds));
      else losses.delete(l.seriesKey);
      return { ...a, losses };
    });
  const select = (req) => {
    if (req && req.rebind && review) ownRebind.current = { selSeq: review.selSeq };
    return onSelect(req);
  };

  const title = `Review sync to ${tn}`;
  const head = (
    <span className="flex items-center gap-2">
      <span className="flex-1">{title}</span>
      {ok && <span className="font-mono text-[11px] font-normal text-muted-foreground">plan {review.gen} · {clock(review.asOf)}</span>}
    </span>
  );
  const desc = ok ? (
    <span className="flex items-start gap-1.5">
      <Info className="mt-px h-3 w-3 shrink-0" aria-hidden="true" />
      <span>{plannedWithLine(review.policy, tn)} Change these from Re-check or the settings menu; the plan rebuilds.</span>
    </span>
  ) : null;

  const footer = (
    <>
      <span className={cn("flex-1 text-xs", gate.why === "space" ? "text-destructive" : "text-muted-foreground")}>
        {gate.why === "acks" ? `${gate.left} confirmation${gate.left === 1 ? "" : "s"} left` : gate.why ? WHY[gate.why] : "Ready"}
      </span>
      <Button size="sm" variant="ghost" onClick={onClose}>
        Cancel
      </Button>
      <Button size="sm" onClick={start} disabled={!gate.canStart || starting} className="gap-1.5 [&_svg]:h-3.5 [&_svg]:w-3.5">
        <Play aria-hidden="true" />
        {starting ? "Starting…" : `Start sync · ${files} file${files === 1 ? "" : "s"}`}
      </Button>
    </>
  );

  return (
    <Dialog open={open} onClose={onClose} width={wide ? 720 : 660} contained={contained} title={head} aria-label={title} description={desc} footer={footer} bodyClassName="py-3">
      {changedNotice && (
        <div className="mb-2 flex items-center gap-2 rounded-lg border border-warning/40 bg-warning/[.10] px-3 py-2 text-[12.5px]">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-warn-fg" aria-hidden="true" />
          <span className="flex-1">
            <b className="font-semibold">The plan changed. Review it again.</b> Something on your PC or {tn} changed while this was open, and the list below is the new plan.
          </span>
          <Button size="sm" variant="outline" className="h-7" onClick={() => setChangedNotice(false)}>
            I've reviewed it
          </Button>
        </div>
      )}
      {review && !review.ok && <RefusalCard refusal={review} tn={tn} onAction={onAction} compact />}
      {refusal && <RefusalCard refusal={refusal} tn={tn} onAction={(k, a) => (k === "review" ? setRefusal(null) : onAction(k, a))} onDismiss={() => setRefusal(null)} compact className="mb-2" />}
      {ok && !review.groups.length && <div className="py-3 text-sm text-muted-foreground">Nothing to copy or remove on {tn}.</div>}
      {ok && review.groups.map((g) => <ReviewGroup key={`${g.kind}:${g.reason}`} g={g} tn={tn} locked={locked} pendingOps={pendingOps} onSelect={select} />)}
      {ok && (
        <div className="mt-3 flex flex-col gap-2">
          {review.losses.map((l) => (
            <div key={l.seriesKey} className="rounded-lg border border-destructive/40 bg-destructive/[.06] px-3 py-2.5 text-[12.5px]">
              <div className="flex items-start gap-2.5">
                <Checkbox danger checked={lossAcked(acks, l)} onCheckedChange={(v) => toggleLoss(l, v)} aria-label={`Confirm removing chapters of ${l.series}`} className="mt-px" />
                <div className="min-w-0 flex-1">
                  <b className="font-semibold">
                    {l.lossOpIds.length === 1 ? `Remove 1 chapter of ${l.series} that isn't on your PC` : `Remove ${l.lossOpIds.length} chapters of ${l.series} that aren't on your PC`}
                  </b>
                  <div className="mt-0.5 font-mono text-xs text-muted-foreground">
                    {l.labels.map(chLabel).join(" · ")}
                    {l.labels.length ? " — " : ""}after this {l.lossOpIds.length === 1 ? "it exists" : "they exist"} nowhere you sync from.
                  </div>
                </div>
              </div>
            </div>
          ))}
          {review.massRepush && review.massRepush.required && (
            <div className={cn("flex gap-2.5 rounded-lg border px-3 py-2.5 text-[12.5px]", TONE.warn[0])}>
              <Checkbox
                checked={acks.massRepush === review.massRepush.count}
                onCheckedChange={(v) => setAcks((a) => ({ ...a, massRepush: v ? review.massRepush.count : null }))}
                aria-label="Confirm copying back"
                className="mt-px"
              />
              <div className="min-w-0 flex-1">
                <b className="font-semibold">
                  {/* planner.js evaluateSelection: massRepush.count is gone SERIES with a copy selected, not files */}
                  Copy back {review.massRepush.count} folder{review.massRepush.count === 1 ? "" : "s"} to {tn}
                </b>
                <div className="mt-0.5 text-xs text-muted-foreground">These folders were removed on {tn} since the last sync. Each copies back under the name you pick:</div>
                {(review.rebinds || []).map((rb) => (
                  <div key={rb.seriesKey} className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
                    <span className="font-semibold">{rb.series}</span>
                    {(rb.rebind.choices || []).map((c) => (
                      <label key={c.source} className={cn("flex items-center gap-1.5", !c.available && "opacity-50")}>
                        <input
                          type="radio"
                          name={`rb-${rb.seriesKey}`}
                          disabled={!c.available || locked}
                          checked={!rb.rebind.declined && !!rb.rebind.chosen && rb.rebind.chosen.source === c.source}
                          onChange={() => select({ rebind: { shardId: rb.rebind.shardId, source: c.source, declined: false }, seriesKey: rb.seriesKey })}
                        />
                        <span className="font-mono">{c.name}</span>
                        <span className="text-muted-foreground">({rebindLabel(c)})</span>
                      </label>
                    ))}
                    <label className="flex items-center gap-1.5">
                      <input type="radio" name={`rb-${rb.seriesKey}`} disabled={locked} checked={!!rb.rebind.declined} onChange={() => select({ rebind: { shardId: rb.rebind.shardId, declined: true }, seriesKey: rb.seriesKey })} />
                      Don't copy it back
                    </label>
                  </div>
                ))}
              </div>
            </div>
          )}
          {review.massDelete && review.massDelete.required && (
            <div className="rounded-lg border border-destructive/40 bg-destructive/[.06] px-3 py-2.5 text-[12.5px]">
              <div className="flex items-center gap-2.5">
                <Checkbox
                  danger
                  checked={acks.massDelete === review.massDelete.count}
                  onCheckedChange={(v) => setAcks((a) => ({ ...a, massDelete: v ? review.massDelete.count : null }))}
                  aria-label="Confirm the mass removal"
                />
                <b className="font-semibold">{massDeleteCopy(review.massDelete, tn).title}</b>
              </div>
              <div className="ml-[26px] mt-0.5 text-xs text-muted-foreground">{massDeleteCopy(review.massDelete, tn).sub}</div>
            </div>
          )}
          {cap && cap.free != null && (
            <div className="flex items-center gap-2 px-0.5 py-1.5 text-[12.5px]">
              <HardDrive className={cn("h-3.5 w-3.5 shrink-0", cap.fits === false ? "text-destructive" : "text-success")} aria-hidden="true" />
              <span className={cn("flex-1", cap.fits === false && "text-destructive")}>
                {cap.fits === false ? "Doesn't fit" : "Fits"}: needs <span className="font-mono">{formatBytes(Math.max(0, cap.needed || 0))}</span>,{" "}
                <span className="font-mono">{formatBytes(cap.free)}</span> free, <span className="font-mono">{formatBytes(cap.reserve)}</span> kept in reserve
                {/* service.js _capacity (space.js simulateRun): needed = the run's deepest dip, shortBy = what it lacks above the reserve */}
                {cap.fits === false ? ` (short by ${formatBytes(cap.shortBy || 0)}${cap.shortAt ? `, runs out at ${seriesKeyName(cap.shortAt)}` : ""})` : ""}
              </span>
            </div>
          )}
        </div>
      )}
    </Dialog>
  );
}
