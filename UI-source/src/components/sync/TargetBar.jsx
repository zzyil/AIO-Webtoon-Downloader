// ============================================================
// TargetBar — the Sync tab's top strip (approved spec §4 item 1; mockup
// targetBar / recheckMenu / targetMenu)
//
// Picker · status · capacity gauge · Re-check ▾ · settings ▾. Narrow (the
// 735 px window minimum) is two rows with the gauge under; wide is one row.
//
// Re-check ▾ (design D3/D4): two ONE-TIME choices, reset to Cached / Trust
// record every time the menu opens; the caller decides what they mean
// (SyncTab: Trust record → a plan, Read back → a verify job). Nothing here is
// stored.
// settings ▾ (design D5): Removals as a quick switch (a real target field,
// saved at once through useConfigOps) and "All <target> settings…", which
// the caller opens as the connected-target sheet.
//
// Consumer: SyncTab.jsx. Depends on CapacityGauge, copy.js, syncView.js.
// ============================================================

import React, { useState } from "react";
import { ChevronDown, HardDrive, RefreshCw, SlidersHorizontal, Smartphone, Tablet } from "lucide-react";
import { Button, Select, SegmentedControl } from "@/components/ui/primitives";
import { Popover } from "@/components/ui/overlay";
import { cn } from "@/lib/utils";
import CapacityGauge from "./CapacityGauge";
import { DELETE_POLICY_COPY, DELETE_POLICY_ORDER, HASH_MODE_COPY } from "./copy";
import { clock } from "./syncView";

const DOT = { ok: "bg-success", info: "bg-info", warn: "bg-warning", bad: "bg-destructive", muted: "bg-muted-foreground/60" };
const XS = "h-[26px] gap-1 px-2 text-[11.5px] [&_svg]:h-[13px] [&_svg]:w-[13px]";

// `stacked` puts the control under its label: Removals' three choices need
// ~235 px, more than the 300 px menu leaves beside a label.
function MenuRow({ label, sub, stacked, children }) {
  return (
    <div className="flex flex-col gap-1 px-1.5 py-1.5">
      <div className={cn("flex gap-2 text-xs", stacked ? "flex-col items-start gap-1.5" : "items-center")}>
        <span className="flex-1 font-semibold">{label}</span>
        {children}
      </div>
      <div className="text-[11px] text-muted-foreground">{sub}</div>
    </div>
  );
}

function RecheckMenu({ tn, disabled, onRun }) {
  const [open, setOpen] = useState(false);
  const [hash, setHash] = useState("cached");
  const [dev, setDev] = useState("trust");
  const toggle = () => {
    // One-time choices (D4): every opening starts from the defaults.
    if (!open) {
      setHash("cached");
      setDev("trust");
    }
    setOpen(!open);
  };
  return (
    <Popover
      open={open}
      onClose={() => setOpen(false)}
      width={310}
      trigger={
        <Button size="sm" variant="outline" className={cn(XS, open && "shadow-[inset_0_0_0_1px_hsl(var(--primary))]")} onClick={toggle} aria-expanded={open} aria-haspopup="menu">
          <RefreshCw aria-hidden="true" />
          Re-check
          <ChevronDown aria-hidden="true" />
        </Button>
      }
    >
      <MenuRow label="PC files" sub={`${HASH_MODE_COPY.cached.label} reuses fingerprints from the last scan. ${HASH_MODE_COPY.rehash.label} reads every PC chapter again.`}>
        <SegmentedControl size="xs" aria-label="PC files" value={hash} onChange={setHash} options={[{ value: "cached", label: "Cached" }, { value: "rehash", label: "Re-hash" }]} />
      </MenuRow>
      <MenuRow label={tn} sub={`Trust record uses the sync history. Read back reads every chapter on ${tn} once.`}>
        <SegmentedControl size="xs" aria-label={tn} value={dev} onChange={setDev} options={[{ value: "trust", label: "Trust record" }, { value: "read-back", label: "Read back" }]} />
      </MenuRow>
      <div className="my-1 h-px bg-border" />
      <div className="flex items-center px-1.5 pb-1">
        <span className="flex-1 text-[11px] text-muted-foreground">Applies to this Re-check only</span>
        <Button
          size="sm"
          className={XS}
          disabled={disabled}
          onClick={() => {
            setOpen(false);
            onRun({ hashMode: hash, readBack: dev === "read-back" });
          }}
        >
          <RefreshCw aria-hidden="true" />
          Re-check
        </Button>
      </div>
    </Popover>
  );
}

function SettingsMenu({ target, tn, ops, policyNow, onOpenAll }) {
  const [open, setOpen] = useState(false);
  const [err, setErr] = useState(null);
  // The target's own field, else what the last plan resolved it to (a null
  // field means "the global default for new targets", settings syncDeletePolicy).
  const policy = target.deletePolicy || policyNow || "guarded";
  const setPolicy = async (v) => {
    setErr(null);
    const r = await ops.run("update-target", { targetId: target.id, patch: { deletePolicy: v } });
    if (!r.ok) setErr(r.message || "That didn't save.");
  };
  return (
    <Popover
      open={open}
      onClose={() => setOpen(false)}
      width={300}
      trigger={
        <Button
          size="sm"
          variant="ghost"
          title={`${tn} settings`}
          aria-label={`${tn} settings`}
          aria-expanded={open}
          aria-haspopup="menu"
          className={cn(XS, "px-1.5", open && "shadow-[inset_0_0_0_1px_hsl(var(--primary))]")}
          onClick={() => setOpen(!open)}
        >
          <SlidersHorizontal aria-hidden="true" />
          <ChevronDown aria-hidden="true" />
        </Button>
      }
    >
      {/* The sub line explains the policy in force (three no longer fit one
          sentence each in the 300 px menu); the editor and Settings → Sync
          show the same hint. */}
      <MenuRow label="Removals" stacked sub={(DELETE_POLICY_COPY[policy] || DELETE_POLICY_COPY.guarded).hint}>
        <SegmentedControl
          size="xs"
          aria-label="Removals"
          value={policy}
          onChange={setPolicy}
          disabled={ops.pending}
          options={DELETE_POLICY_ORDER.map((k) => ({ value: k, label: DELETE_POLICY_COPY[k].label }))}
        />
      </MenuRow>
      {err && <div className="px-1.5 pb-1 text-[11px] text-destructive">{err}</div>}
      <div className="my-1 h-px bg-border" />
      <button
        type="button"
        role="menuitem"
        className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12.5px] hover:bg-accent"
        onClick={() => {
          setOpen(false);
          onOpenAll();
        }}
      >
        <SlidersHorizontal className="h-3.5 w-3.5" aria-hidden="true" />
        All {tn} settings…
      </button>
    </Popover>
  );
}

/**
 * @param {{targets:{target,status}[], target, status, summary, wide:boolean,
 *   busy:boolean, ops, onPick(id), onRecheck({hashMode, readBack}), onOpenAll()}} props
 */
export default function TargetBar({ targets, target, status, summary, wide, busy, ops, onPick, onRecheck, onOpenAll }) {
  const tn = (target && target.name) || "this target";
  const KindIcon = !target ? Tablet : target.kind === "folder" ? HardDrive : /phone|pixel/i.test(target.name || "") ? Smartphone : Tablet;
  const picker = (
    <div className="flex min-w-0 items-center gap-2">
      <KindIcon className={cn("h-4 w-4 shrink-0", status.connected ? "text-success" : "text-muted-foreground")} aria-hidden="true" />
      <Select
        aria-label="Sync target"
        value={target ? target.id : ""}
        onChange={(e) => onPick(e.target.value)}
        className="h-[30px] px-2 text-[12.5px] font-semibold"
        style={{ width: wide ? 210 : 176 }}
      >
        {targets.map(({ target: t, status: s }) => (
          <option key={t.id} value={t.id}>
            {t.name || t.id}
            {s.connected ? "" : " (not connected)"}
          </option>
        ))}
      </Select>
    </div>
  );
  const listed = summary && summary.asOf ? ` · listed ${clock(summary.asOf)}` : "";
  const statusLine = (
    <div className="flex items-center gap-1.5 whitespace-nowrap text-xs" title={status.label}>
      <span className={cn("h-[7px] w-[7px] shrink-0 rounded-full", DOT[status.tone] || DOT.muted)} />
      <span className="max-w-[220px] truncate">{status.label}</span>
      {status.connected && listed && <span className="text-muted-foreground">{listed}</span>}
    </div>
  );
  const gauge = summary && summary.capacity ? <CapacityGauge capacity={summary.capacity} targetName={tn} className="min-w-0 flex-1" /> : null;
  const actions = target && (
    <div className="flex shrink-0 items-center gap-1">
      <RecheckMenu tn={tn} disabled={busy || !status.connected} onRun={onRecheck} />
      <SettingsMenu target={target} tn={tn} ops={ops} policyNow={summary && summary.policy ? summary.policy.deletePolicy : null} onOpenAll={onOpenAll} />
    </div>
  );
  if (wide) {
    return (
      <div className="flex items-center gap-3.5 border-b px-4 py-2.5">
        {picker}
        {statusLine}
        {gauge || <span className="flex-1" />}
        {actions}
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-2 border-b px-4 py-2.5">
      <div className="flex items-center gap-3">
        {picker}
        {statusLine}
        <span className="flex-1" />
        {actions}
      </div>
      {gauge}
    </div>
  );
}
