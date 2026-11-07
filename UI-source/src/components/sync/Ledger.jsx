// ============================================================
// Ledger — the Sync tab's tiles, toolbar and series rows (approved spec §4
// items 3-5; mockup tiles / ledgerToolbar / ledgerRow / deviceOnlySection)
//
// Renders planner.js planSummary rows as they come; every number and state
// is syncView.js over that summary. Held series (design D8) always sit last
// under "Not synced to <target>"; folders only on the target close the list.
//
// Rows are plain DOM with content-visibility:auto (spec §4) and memoised on
// their props, so a selection change re-renders only the rows whose summary
// row object changed (main sends a new summary; unchanged rows still differ
// by identity, so the memo compares the fields the row draws: grep rowSig).
//
// The ROW checkbox sends bulk 'series' (syncView.rowToggle): off = untick
// everything; on = back to the plan's own pre-selection; a row with nothing
// pre-selectable opens the detail sheet instead.
//
// Consumer: SyncTab.jsx. Words: copy.js. Strip: ChapterStrip.jsx.
// ============================================================

import React, { memo, useState } from "react";
import { AlertTriangle, Check, ChevronRight, EyeOff, Folder, Link, Loader2, MoreHorizontal, Search, Tablet, Trash2 } from "lucide-react";
import { Badge, Button, Checkbox, Input, Select } from "@/components/ui/primitives";
import { Popover } from "@/components/ui/overlay";
import { cn, formatBytes } from "@/lib/utils";
import ChapterStrip, { SEG_CLASS, StripLegend } from "./ChapterStrip";
import { attentionCopy, DEVICE_ONLY_COPY, heldReasonCopy, liveStateCopy, SUGGESTION_TIER_COPY, seriesStateCopy, TAG_COPY } from "./copy";
import { rowView, SORTS } from "./syncView";

const XS = "h-[26px] gap-1 px-2 text-[11.5px] [&_svg]:h-[13px] [&_svg]:w-[13px]";
const GROUP_H = "flex items-center gap-2 border-b bg-muted/70 px-3 py-2 text-[12.5px] font-semibold";

// ── Tiles ──
const TILE_BAR = { push: SEG_CLASS.push, delete: SEG_CLASS.delete, synced: SEG_CLASS.synced, only: SEG_CLASS.other, attn: SEG_CLASS.warn };

export function SummaryTiles({ counts, tile, onTile, tn }) {
  const items = [
    ["push", counts.push.n, `To copy · ${counts.push.series} series`],
    ["delete", counts.delete.n, "Removals to review"],
    ["synced", counts.synced.n, "In sync"],
    ["only", counts.only.n, `Only on ${tn}`],
    ["attn", counts.attn.n, "Needs attention"],
  ];
  return (
    <div className="grid grid-cols-5 gap-2 px-4 pb-1.5 pt-2.5">
      {items.map(([id, n, label]) => {
        const on = tile === id;
        return (
          <button
            key={id}
            type="button"
            aria-pressed={on}
            onClick={() => onTile(on ? null : id)}
            className={cn(
              "flex min-w-0 flex-col gap-0.5 rounded-lg border bg-card px-2.5 py-2 text-left transition-colors hover:bg-accent/40",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              on && "border-primary/55 bg-primary/[.06] shadow-[inset_0_0_0_1px_hsl(var(--primary)/.35)]"
            )}
          >
            <span className="font-mono text-lg font-semibold leading-tight tabular-nums">{n}</span>
            <span className="truncate text-[11.5px] text-muted-foreground" title={label}>{label}</span>
            <span className={cn("mt-1 block h-[3px] rounded-sm", TILE_BAR[id])} />
          </button>
        );
      })}
    </div>
  );
}

// ── Toolbar ──
export function LedgerToolbar({ query, onQuery, sort, onSort, wide, onExport }) {
  const [menu, setMenu] = useState(false);
  return (
    <div className="flex items-center gap-2 px-4 pb-2 pt-1.5">
      {/* A set width that may shrink: flex-1 here split the row with the
          spacer below and squeezed the box to a truncated placeholder. */}
      <div className="relative min-w-0 shrink" style={{ width: wide ? 300 : 200 }}>
        <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
        <Input value={query} onChange={(e) => onQuery(e.target.value)} placeholder="Filter series" aria-label="Filter series" className="h-[30px] pl-7 text-[12.5px]" />
      </div>
      <Select aria-label="Sort" value={sort} onChange={(e) => onSort(e.target.value)} className="h-[30px] w-[150px] px-2 text-[12.5px]">
        {SORTS.map((s) => (
          <option key={s.value} value={s.value}>{s.label}</option>
        ))}
      </Select>
      <span className="flex-1" />
      <StripLegend compact={!wide} />
      <Popover
        open={menu}
        onClose={() => setMenu(false)}
        width={210}
        trigger={
          <Button size="sm" variant="ghost" className="h-[26px] w-[26px] p-0" aria-label="More" title="More" onClick={() => setMenu(!menu)}>
            <MoreHorizontal className="h-[13px] w-[13px]" />
          </Button>
        }
      >
        <button
          type="button"
          role="menuitem"
          className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12.5px] hover:bg-accent"
          onClick={() => {
            setMenu(false);
            onExport();
          }}
        >
          Export report (JSON)
        </button>
      </Popover>
    </div>
  );
}

// ── Rows ──
function hue(s) {
  let h = 0;
  for (const c of String(s || "")) h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}
export function fileUrl(p) {
  return p ? "localfile:///" + encodeURI(String(p).replace(/\\/g, "/")) : null;
}
export function Cover({ path, title, className }) {
  const [bad, setBad] = useState(false);
  if (path && !bad) {
    return <img src={fileUrl(path)} alt="" loading="lazy" onError={() => setBad(true)} className={cn("shrink-0 rounded-[3px] object-cover shadow-[inset_0_0_0_1px_rgb(0_0_0/.1)]", className)} />;
  }
  const h = hue(title);
  return <span aria-hidden="true" className={cn("shrink-0 rounded-[3px]", className)} style={{ background: `linear-gradient(160deg, hsl(${h} 35% 55%), hsl(${h} 40% 30%))` }} />;
}

const STATE_BADGE = { new: "default", gone: "token-warning", "needs-verify": "token-info", held: "token-muted" };

/** `live` (syncView.liveState) outranks the planner state while a decision lands. */
function RowLabel({ row, v, wide, tn, live }) {
  const liveText = live ? liveStateCopy(live, tn) : null;
  if (liveText) {
    return (
      <Badge variant="token-info">
        {live !== "verify-offline" && <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />}
        {liveText}
      </Badge>
    );
  }
  const stateText = row.state === "held" ? heldReasonCopy(row.heldReason, tn) : seriesStateCopy(row.state, tn);
  if (stateText) return <Badge variant={STATE_BADGE[row.state] || "token-muted"}>{stateText}</Badge>;
  const tags = (row.tags || []).slice(0, wide ? 2 : 1).filter((t) => TAG_COPY[t]);
  if (tags.length) {
    return tags.map((t) => (
      <span key={t} className="whitespace-nowrap rounded bg-secondary px-1.5 text-[10.5px] leading-[17px] text-secondary-foreground">{TAG_COPY[t]}</span>
    ));
  }
  const on = (row.counts && (row.counts.inSync || 0) + (row.counts.kept || 0)) || 0;
  return on ? <span className="font-mono text-[11px] text-muted-foreground">{on} on {tn}</span> : null;
}

function Pills({ v, row }) {
  return (
    <>
      {v.writes > 0 && <span className="rounded bg-primary/[.12] px-[5px] font-mono text-[11px] leading-[18px] tabular-nums text-primary">+{v.writes}</span>}
      {v.deletes > 0 && <span className="rounded bg-destructive/10 px-[5px] font-mono text-[11px] leading-[18px] tabular-nums text-destructive">−{v.deletes}</span>}
      {v.warn > 0 && <span className="rounded bg-warning/[.14] px-[5px] font-mono text-[11px] leading-[18px] tabular-nums text-warn-fg">?{v.warn}</span>}
      {v.inSync && <Check className="h-3.5 w-3.5 text-success" aria-label="In sync" />}
    </>
  );
}

/** The fields a row draws; LedgerRow re-renders only when one changes. */
function rowSig(p) {
  const r = p.row;
  return [
    r.seriesKey, r.title, r.state, r.heldReason, r.removing, r.hasLoss, JSON.stringify(r.counts), JSON.stringify(r.strip), (r.tags || []).join(),
    (r.attention || []).join(), p.wide, p.tn, p.cover, p.pending, p.locked, p.lane, p.active, p.animate, p.index, p.live,
  ].join("\u0001");
}

export const LedgerRow = memo(
  function LedgerRow({ row, index, wide, tn, cover, pending, locked, live, lane, active, animate, onToggle, onOpen, onInclude }) {
    const v = rowView(row);
    const attnText = v.attention.filter((a) => a !== "loss").map((a) => attentionCopy(a, tn)).filter(Boolean);
    const d = `${Math.min(index, 12) * 28}ms`;
    return (
      <div
        onClick={() => onOpen(row.seriesKey)}
        className={cn(
          "flex cursor-pointer items-center gap-2.5 border-b border-border/70 px-3 py-2 hover:bg-accent/45",
          active && "bg-primary/[.07] shadow-[inset_3px_0_0_hsl(var(--primary))]",
          v.held && !row.removing && "opacity-60",
          animate && "sync-row-in"
        )}
        style={{ contentVisibility: "auto", containIntrinsicSize: "auto 44px", ...(animate ? { "--d": d } : null) }}
      >
        <span onClick={(e) => e.stopPropagation()} className="flex w-4 shrink-0">
          {v.box != null && (
            <Checkbox
              checked={v.box}
              pending={pending}
              locked={locked}
              danger={v.writes === 0 && v.deletes > 0}
              aria-label={`Sync ${row.title || row.folder}`}
              title={locked ? "Locked while the sync runs" : undefined}
              onCheckedChange={() => onToggle(row)}
            />
          )}
        </span>
        <Cover path={cover} title={row.title} className="h-[30px] w-[22px]" />
        <div className="flex min-w-0 shrink-0 flex-col" style={{ width: wide ? 300 : 178 }}>
          <div className="flex min-w-0 items-center gap-1.5 text-[13px] font-semibold">
            <span className="truncate">{row.title || row.folder}</span>
            {row.hasLoss && (
              <span className="shrink-0 text-destructive" title={attentionCopy("loss", tn)}>
                <AlertTriangle className="h-3 w-3" aria-label={attentionCopy("loss", tn)} />
              </span>
            )}
            {!row.hasLoss && attnText.length > 0 && (
              <span className="shrink-0 text-warn-fg" title={attnText.join("\n")}>
                <AlertTriangle className="h-3 w-3" aria-label={attnText.join(". ")} />
              </span>
            )}
          </div>
          <div className="mt-0.5 flex min-w-0 items-center gap-1 overflow-hidden">
            <RowLabel row={row} v={v} wide={wide} tn={tn} live={live} />
            {pending && (
              <span className="flex items-center gap-[3px] text-[11px] text-muted-foreground">
                <Loader2 className="h-3 w-3 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                saving
              </span>
            )}
          </div>
        </div>
        <div className="min-w-0 flex-1">
          <ChapterStrip segments={row.strip} size={wide ? "md" : "sm"} lane={lane} targetName={tn} animate={animate} delay={Math.min(index, 12) * 28} />
        </div>
        <div className="flex shrink-0 items-center justify-end gap-1" style={{ width: wide ? 120 : 76 }}>
          {v.held ? (
            <>
              {row.removing && <Pills v={v} row={row} />}
              {/* Include only undoes an exclude (service.js include-series edits
                  the exclude / only lists); other held reasons (image-only, a
                  name problem, fork…) aren't the user's choice to undo here. */}
              {!row.removing && row.heldReason === "excluded" && (
                <Button size="sm" variant="ghost" className={XS} onClick={(e) => { e.stopPropagation(); onInclude(row.seriesKey); }} disabled={locked}>
                  Include
                </Button>
              )}
            </>
          ) : (
            <Pills v={v} row={row} />
          )}
        </div>
        {wide && <span className="w-[58px] shrink-0 text-right font-mono text-[11px] tabular-nums text-muted-foreground">{row.counts && row.counts.bytes ? formatBytes(row.counts.bytes) : ""}</span>}
        {/* The keyboard way into the detail (the row's own click is mouse-only:
            a role=button row can't hold the checkbox button). */}
        <button
          type="button"
          aria-label={`Open ${row.title || row.folder}`}
          className="-mr-1 flex h-6 w-5 shrink-0 items-center justify-center rounded text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          onClick={(e) => {
            e.stopPropagation();
            onOpen(row.seriesKey);
          }}
        >
          <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      </div>
    );
  },
  (a, b) => rowSig(a) === rowSig(b) && a.onToggle === b.onToggle && a.onOpen === b.onOpen && a.onInclude === b.onInclude
);

// ── Device-only folders ──
/**
 * onFindAll opens the batch Find sources sheet; onFindOne(d) searches one
 * folder in the Search tab (UI pass U4). Either may be absent (no button).
 * isLinked(name): the target already holds an alias for that folder (a
 * download was queued for it), so it shows that instead of Find source; a
 * second find would add a second alias to the same folder (add-alias
 * accepts duplicates; the planner then warns alias-conflict).
 *
 * Two locks (plan sync-temp/plans/device-sync-cheap-replans.md §7):
 * linkLocked is the selection lock (an apply / prune / rename of this target,
 * the only jobs service.js H.link refuses during); pruneLocked is any job
 * running, since a prune is a job of its own. linking = device folders with a
 * Link in flight (syncView.linkIndex): they show "Linking…" instead of the
 * buttons, so a second click can't send the same Link twice.
 */
export function DeviceOnlySection({ items, suggestions, titleOf, tn, linkLocked, pruneLocked, linking, active, onOpen, onLink, onIgnore, onPrune, onFindAll, onFindOne, isLinked }) {
  if (!items || !items.length) return null;
  const orphaned = items.filter((d) => d.state === "orphaned");
  const byFolder = new Map();
  for (const g of suggestions || []) if (!byFolder.has(g.deviceFolder)) byFolder.set(g.deviceFolder, g);
  return (
    <>
      <div className={GROUP_H}>
        <Tablet className="h-3.5 w-3.5" aria-hidden="true" />
        Only on {tn}
        <span className="font-mono font-normal text-muted-foreground">{items.length}</span>
        <span className="flex-1" />
        {onFindAll && (
          <Button size="sm" variant="ghost" onClick={onFindAll} className={XS}>
            <Search aria-hidden="true" />
            Find sources…
          </Button>
        )}
        {orphaned.length > 0 && (
          <Button size="sm" variant="outline" disabled={pruneLocked} onClick={onPrune} className={cn(XS, "border-destructive/45 text-destructive hover:bg-destructive/[.08] hover:text-destructive")}>
            <Trash2 aria-hidden="true" />
            Remove folders…
          </Button>
        )}
      </div>
      {items.map((d) => {
        const g = byFolder.get(d.name);
        const isLinking = !!(linking && linking.has(d.name));
        return (
          <div
            key={d.key}
            onClick={() => onOpen(d.key)}
            className={cn("flex cursor-pointer items-center gap-2.5 border-b border-border/70 px-3 py-2 hover:bg-accent/45", active === d.key && "bg-primary/[.07] shadow-[inset_3px_0_0_hsl(var(--primary))]")}
            style={{ contentVisibility: "auto", containIntrinsicSize: "auto 44px" }}
          >
            <Folder className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
            <div className="flex min-w-0 flex-1 flex-col">
              <span className="truncate font-mono text-xs">{d.name}</span>
              <span className="truncate text-[11px] text-muted-foreground">
                {d.listed === false ? `Couldn't list it on ${tn}` : `${d.files} file${d.files === 1 ? "" : "s"} · ${formatBytes(d.bytes)}`} · {DEVICE_ONLY_COPY[d.state] || d.state}
              </span>
            </div>
            {isLinking && (
              <span className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground" role="status">
                <Loader2 className="h-3 w-3 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                Linking…
              </span>
            )}
            {!isLinking && !g && isLinked && isLinked(d.name) && (
              <span className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground" title="It syncs into this folder once its download lands">
                <Link className="h-3 w-3 text-primary" aria-hidden="true" />
                Linked, waiting for its download
              </span>
            )}
            {!isLinking && !g && onFindOne && !(isLinked && isLinked(d.name)) && (
              <Button
                size="sm"
                variant="ghost"
                className={XS}
                onClick={(e) => {
                  e.stopPropagation();
                  onFindOne(d);
                }}
              >
                <Search aria-hidden="true" />
                Find source
              </Button>
            )}
            {!isLinking && g && (
              <span onClick={(e) => e.stopPropagation()} className="flex shrink-0 items-center gap-1.5">
                <span className="flex items-center gap-1 text-xs" title={SUGGESTION_TIER_COPY[g.tier]}>
                  <Link className="h-3 w-3 text-primary" aria-hidden="true" />
                  Looks like <b className="font-semibold">{titleOf(g.seriesKey)}</b>
                </span>
                <Button size="sm" variant="outline" className={XS} disabled={linkLocked} onClick={() => onLink(g)}>
                  Link
                </Button>
                <Button size="sm" variant="ghost" className={XS} disabled={linkLocked} onClick={() => onIgnore(g)}>
                  Ignore
                </Button>
              </span>
            )}
            <button
              type="button"
              aria-label={`Open ${d.name}`}
              className="-mr-1 flex h-6 w-5 shrink-0 items-center justify-center rounded text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onClick={(e) => {
                e.stopPropagation();
                onOpen(d.key);
              }}
            >
              <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </div>
        );
      })}
    </>
  );
}

export function HeldHeader({ n, tn }) {
  return (
    <div className={GROUP_H}>
      <EyeOff className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
      Not synced to {tn}
      <span className="font-mono font-normal text-muted-foreground">{n}</span>
    </div>
  );
}
