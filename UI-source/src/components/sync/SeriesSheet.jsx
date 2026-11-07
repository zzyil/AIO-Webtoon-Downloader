// ============================================================
// SeriesSheet — one series (or one folder only on the target) in a right
// side sheet (approved spec §4 "Series detail sheet"; mockup detailSheet /
// detailGroups)
//
// Data: sync:series-detail (planner.js seriesDetail), re-read whenever the
// plan generation or the applied selection sequence moves, so ticks made
// here come back as the service's truth. Ticks go through the hook's
// selection client (actions.select → selectionQueue): per-op `changes`
// carrying this row's seriesKey, so the ledger row shows them pending too.
//
// Footer actions (design D7/D8, U2-8):
//   Exclude from <target>   config-op remove-series {removeFiles:false}
//   Rename on <target>…     sync:rename-device-folder (a job)
//   Remove from <target>    config-op remove-series (two-click; its deletes
//                           then go through Review like any removal)
//   Include                 config-op include-series (held/excluded series)
// A held series being removed shows no Exclude/Remove (it already is).
//
// Groups open by default when they need a decision (replace, removals,
// losses); an open group renders PAGE rows, then "Show N more".
// Consumer: SyncTab.jsx. Words: copy.js. Grouping: syncView.detailGroups.
// ============================================================

import React, { useEffect, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronRight, EyeOff, Info, Link, Loader2, Pencil, Trash2 } from "lucide-react";
import { Badge, Button, Checkbox, Input } from "@/components/ui/primitives";
import { Sheet } from "@/components/ui/overlay";
import { cn, formatBytes } from "@/lib/utils";
import ChapterStrip, { SEG_CLASS } from "./ChapterStrip";
import { Cover } from "./Ledger";
import { useArm } from "./TargetEditor";
import {
  anomalyCopy, autoHeldCopy, DEVICE_ONLY_COPY, heldReasonCopy, liveStateCopy, opReasonCopy, READER_ISSUE_COPY, seriesStateCopy, SUGGESTION_TIER_COPY, TAG_COPY,
} from "./copy";
import { chLabel, detailGroups, groupChanges, seriesPathLine } from "./syncView";

const PAGE = 150;
const XS = "h-[26px] gap-1 px-2 text-[11.5px] [&_svg]:h-[13px] [&_svg]:w-[13px]";
const DANGER_OUTLINE = "border border-destructive/45 bg-transparent text-destructive hover:bg-destructive/[.08] hover:text-destructive";

function groupTitle(id, tn) {
  return {
    push: "Copy",
    replace: "Replace",
    rename: `Rename on ${tn}`,
    delete: `Remove from ${tn}`,
    settling: "Still downloading",
    "in-sync": `Already on ${tn}`,
    "device-only": `On ${tn}`,
  }[id];
}
function groupHint(g, tn) {
  const reasons = [...new Set(g.rows.map((r) => r.reason).filter(Boolean))];
  if (reasons.length === 1) return opReasonCopy(g.rows[0].kind, reasons[0], tn) || "";
  if (g.id === "settling") return "Joins the plan once the download settles";
  return "";
}

function FileRow({ r, tn, locked, pending, onTick }) {
  const isDel = r.kind === "delete";
  const loss = r.loss || (r.lossIfSelected && !r.selected);
  const why = r.blockedBy && r.blockedBy.length ? "Waiting on a link suggestion" : opReasonCopy(r.kind, r.reason, tn);
  return (
    <div className="flex items-center gap-2.5 border-b border-border/50 py-[5px] pl-[34px] pr-3 text-[12.5px]">
      {r.opId ? (
        <Checkbox checked={!!r.selected} pending={pending} locked={locked} danger={isDel} aria-label={`${r.selected ? "Untick" : "Tick"} ${r.name}`} onCheckedChange={(v) => onTick(r, v)} />
      ) : (
        <span className="w-4 shrink-0" />
      )}
      <span className="w-[62px] shrink-0 font-mono text-xs tabular-nums">{chLabel(r.label) || "—"}</span>
      <span className="min-w-0 flex-1 truncate text-muted-foreground" title={r.name}>
        {r.kind === "rename" && r.deviceName ? `${r.deviceName} → ${r.pcName}` : r.name}
        {why ? ` · ${why}` : ""}
      </span>
      {loss && (
        <span className="flex shrink-0 items-center gap-1 text-[11px] text-destructive" title="Not on your PC: after this it exists nowhere you sync from">
          <AlertTriangle className="h-3 w-3" aria-hidden="true" />
          not on PC
        </span>
      )}
      <span className="w-[56px] shrink-0 text-right font-mono text-[11px] tabular-nums">{r.size ? formatBytes(r.size) : ""}</span>
    </div>
  );
}

function Group({ g, tn, locked, pendingOps, open, onToggleOpen, onTickGroup, onTick }) {
  const [shown, setShown] = useState(PAGE);
  const Chev = open ? ChevronDown : ChevronRight;
  const anyPending = g.opIds.some((id) => pendingOps && pendingOps[id]);
  return (
    <>
      <div className="flex items-center gap-2 border-b px-3 py-2 text-[12.5px] font-semibold">
        {g.box == null ? (
          <span className="w-4 shrink-0" />
        ) : (
          <Checkbox checked={g.box} pending={anyPending} locked={locked} danger={g.id === "delete"} aria-label={`All of ${groupTitle(g.id, tn)}`} onCheckedChange={(v) => onTickGroup(g, v)} />
        )}
        <button type="button" onClick={onToggleOpen} aria-expanded={open} className="flex min-w-0 flex-1 items-center gap-2 text-left">
          <Chev className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span className={cn("inline-block h-2.5 w-2.5 shrink-0 rounded-[3px]", SEG_CLASS[g.tone] || SEG_CLASS.other)} />
          <span className="truncate">{groupTitle(g.id, tn)}</span>
          <span className="font-mono text-xs font-normal text-muted-foreground">{g.count}</span>
          <span className="min-w-0 flex-1 truncate text-right text-[11px] font-normal text-muted-foreground">{groupHint(g, tn)}</span>
        </button>
        <span className="w-[56px] shrink-0 text-right font-mono text-[11px] font-normal tabular-nums">{g.bytes ? formatBytes(g.bytes) : ""}</span>
      </div>
      {open &&
        g.rows.slice(0, shown).map((r, i) => (
          <FileRow key={r.opId || `${r.name}\0${i}`} r={r} tn={tn} locked={locked} pending={!!(r.opId && pendingOps && pendingOps[r.opId])} onTick={onTick} />
        ))}
      {open && g.rows.length > shown && (
        <button type="button" className="w-full px-[34px] py-[7px] text-left text-xs font-medium text-primary hover:underline" onClick={() => setShown(shown + PAGE)}>
          Show {Math.min(PAGE, g.rows.length - shown)} more
        </button>
      )}
    </>
  );
}

function anomalyText(a, tn) {
  const base = anomalyCopy(a.kind, tn) || a.kind;
  if (a.kind === "reader-compat" && a.data && Array.isArray(a.data.issues)) {
    const issues = a.data.issues.map((i) => READER_ISSUE_COPY[i]).filter(Boolean);
    if (issues.length) return `${base}: ${issues.join("; ")}`;
  }
  return base;
}

/**
 * @param {{open, onClose, targetId, seriesKey, row, tn, wide, sync, ops,
 *   cover, pcPath, root, locked, live, linking, pendingOps, gen, selSeq, contained, onSelect(req), onError(reply)}} props
 *   live = syncView.liveState of the row (its badge while a decision lands);
 *   linking = device folders with a Link in flight (syncView.linkIndex).
 *   pcPath = the series' PC folder (library scan); root = the target's root.
 *   row = the summary row (pc:) or the deviceOnly entry (dev:).
 */
export default function SeriesSheet({ open, onClose, targetId, seriesKey, row, tn, wide, contained, sync, ops, cover, pcPath, root, locked, live, linking, pendingOps, gen, selSeq, onSelect, onError }) {
  const [detail, setDetail] = useState(null);
  const [openGroups, setOpenGroups] = useState({});
  const [renaming, setRenaming] = useState(null);
  const { armed, arm, disarm } = useArm();
  const isDev = typeof seriesKey === "string" && seriesKey.startsWith("dev:");

  useEffect(() => {
    if (!open || !seriesKey) return undefined;
    let alive = true;
    sync.actions.seriesDetail({ targetId, seriesKey }).then((d) => {
      if (alive) setDetail(d && d.ok ? d : { ok: false, ...(d || {}) });
    });
    return () => {
      alive = false;
    };
  }, [open, targetId, seriesKey, gen, selSeq, sync.actions]);
  useEffect(() => {
    setOpenGroups({});
    setRenaming(null);
    disarm();
    // A different series: start from its own default open groups.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seriesKey]);

  const title = (row && (row.title || row.name || row.folder)) || (detail && detail.folder) || "";
  const groups = detail && detail.ok ? detailGroups(detail.rows) : [];
  const isOpen = (g) => (g.id in openGroups ? openGroups[g.id] : g.open);
  const tick = (r, v) => onSelect({ changes: [{ opId: r.opId, selected: v }], seriesKey });
  const tickGroup = (g, v) => {
    const changes = groupChanges(g, v);
    if (changes.length) onSelect({ changes, seriesKey });
  };
  const run = async (op, payload) => {
    const r = await ops.run(op, { targetId, seriesKey, ...payload });
    if (!r.ok) onError && onError(r);
    return r;
  };
  const selected = (detail && detail.ok ? detail.rows : []).filter((r) => r.opId && r.selected);
  const selBytes = selected.reduce((n, r) => n + (r.kind === "delete" ? 0 : r.size || 0), 0);
  const onTarget = (detail && detail.ok ? detail.rows : []).filter((r) => r.deviceName).length;
  const held = row && row.state === "held";
  const removing = row && row.removing;
  const anomalies = (detail && detail.anomalies) || [];
  const suggestions = ((detail && detail.suggestions) || []).filter((g) => !g.ignored);
  const rebind = detail && detail.rebind;

  // Spans, not divs: Sheet puts the title inside its <h2>.
  const header = (
    <span className="flex items-start gap-3">
      <Cover path={cover} title={title} className="h-11 w-8 rounded" />
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="truncate">{title}</span>
        <span className="truncate font-mono text-[11px] font-normal text-muted-foreground">
          {isDev
            ? `${tn}: ${row ? row.name : ""}`
            : seriesPathLine({
                folder: (detail && detail.folder) || (row && row.folder),
                deviceFolder: (detail && detail.deviceFolder) || (row && row.deviceFolder),
                pcPath,
                root,
                tn,
              })}
        </span>
      </span>
    </span>
  );
  const liveText = !isDev && live ? liveStateCopy(live, tn) : null;
  const badges = (
    <div className="flex flex-wrap gap-1">
      {liveText && (
        <Badge variant="token-info">
          {live !== "verify-offline" && <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" />}
          {liveText}
        </Badge>
      )}
      {!liveText && !isDev && row && (row.state === "held" ? heldReasonCopy(row.heldReason, tn) : seriesStateCopy(row.state, tn)) && (
        <Badge variant="token-muted">{row.state === "held" ? heldReasonCopy(row.heldReason, tn) : seriesStateCopy(row.state, tn)}</Badge>
      )}
      {isDev && row && <Badge variant="token-muted">{DEVICE_ONLY_COPY[row.state] || row.state}</Badge>}
      {((detail && detail.tags) || []).map((t) => TAG_COPY[t] && <Badge key={t} variant="token-muted">{TAG_COPY[t]}</Badge>)}
    </div>
  );

  const footer = isDev ? null : (
    <div className="flex w-full flex-wrap items-center gap-1.5">
      {held && !removing ? (
        // Include undoes an exclude only (see Ledger.jsx); other holds say why.
        row && row.heldReason === "excluded" ? (
          <Button size="sm" variant="outline" className={XS} disabled={locked} onClick={() => run("include-series").then((r) => r.ok && onClose())}>
            Include on {tn}
          </Button>
        ) : null
      ) : !removing ? (
        <>
          <Button size="sm" variant="ghost" className={XS} disabled={locked} onClick={() => run("remove-series", { removeFiles: false }).then((r) => r.ok && onClose())}>
            <EyeOff aria-hidden="true" />
            Exclude from {tn}
          </Button>
          {row && row.state === "bound" && (detail && detail.deviceFolder) && (
            <Button size="sm" variant="ghost" className={XS} disabled={locked} onClick={() => setRenaming(detail.deviceFolder)}>
              <Pencil aria-hidden="true" />
              Rename on {tn}…
            </Button>
          )}
          {onTarget > 0 &&
            (armed === "remove" ? (
              <Button size="sm" variant="destructive" className={XS} onClick={() => { disarm(); run("remove-series").then((r) => r.ok && onClose()); }}>
                <Trash2 aria-hidden="true" />
                Click again: remove {onTarget} file{onTarget === 1 ? "" : "s"} and stop syncing {title}
              </Button>
            ) : (
              <Button size="sm" variant="outline" className={cn(XS, DANGER_OUTLINE)} disabled={locked} onClick={() => arm("remove")}>
                <Trash2 aria-hidden="true" />
                Remove from {tn}
              </Button>
            ))}
        </>
      ) : (
        <Button size="sm" variant="outline" className={XS} disabled={locked} onClick={() => run("include-series").then((r) => r.ok && onClose())}>
          Keep it on {tn}
        </Button>
      )}
      <span className="flex-1" />
      {armed !== "remove" && selected.length > 0 && (
        <span className="font-mono text-xs tabular-nums">
          {selected.length} file{selected.length === 1 ? "" : "s"}
          {selBytes ? ` · ${formatBytes(selBytes)}` : ""}
        </span>
      )}
    </div>
  );

  return (
    <Sheet open={open} onClose={onClose} width={wide ? 520 : 440} accent title={header} description={badges} aria-label={title} footer={footer} contained={contained} bodyClassName="px-0 py-0">
      <div className="border-b px-4 py-3">
        <ChapterStrip segments={(detail && detail.strip) || (row && row.strip) || []} size="xl" targetName={tn} />
        {row && row.strip && row.strip.length > 0 && (
          <div className="mt-1 flex font-mono text-[11px] text-muted-foreground">
            <span>{chLabel(row.strip.find((s) => s.from != null)?.from)}</span>
            <span className="flex-1" />
            <span>{chLabel([...row.strip].reverse().find((s) => s.to != null)?.to)}</span>
          </div>
        )}
      </div>
      {detail && autoHeldCopy(detail.autoHeld, tn) && (
        // Auto-replace only (planner.js sp.autoHeld, plan R9): the tags that
        // kept its swaps unticked here, so the leftover copies aren't a mystery.
        <div className="flex items-start gap-2 border-b px-4 py-2.5 text-[12.5px] text-muted-foreground">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-info" aria-hidden="true" />
          <span>{autoHeldCopy(detail.autoHeld, tn)}</span>
        </div>
      )}
      {renaming != null && (
        <form
          className="flex items-center gap-2 border-b px-4 py-2.5"
          onSubmit={async (e) => {
            e.preventDefault();
            const to = renaming.trim();
            if (!to || to === detail.deviceFolder) return setRenaming(null);
            const r = await sync.actions.renameDeviceFolder({ targetId, seriesKey, to });
            if (!r.ok) onError && onError(r);
            setRenaming(null);
          }}
        >
          <span className="shrink-0 text-xs text-muted-foreground">New name on {tn}</span>
          <Input
            autoFocus
            value={renaming}
            onChange={(e) => setRenaming(e.target.value)}
            // Escape cancels the rename only; preventDefault tells the Sheet's
            // Escape handler (overlay.jsx) it was handled.
            onKeyDown={(e) => {
              if (e.key !== "Escape") return;
              e.preventDefault();
              setRenaming(null);
            }}
            className="h-7 font-mono text-xs"
            aria-label={`New folder name on ${tn}`}
          />
          <Button size="sm" type="submit" className={XS}>Rename</Button>
          <Button size="sm" type="button" variant="ghost" className={XS} onClick={() => setRenaming(null)}>Cancel</Button>
        </form>
      )}
      {rebind && row && row.state === "gone" && (
        <div className="border-b px-4 py-2.5 text-[12.5px]">
          <div className="font-semibold">This folder was removed on {tn} since the last sync. Copy it back under:</div>
          <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
            {(rebind.choices || []).map((c) => (
              <label key={c.source} className={cn("flex items-center gap-1.5", !c.available && "opacity-50")}>
                <input type="radio" name="rebind" disabled={!c.available || locked} checked={!rebind.declined && rebind.chosen && rebind.chosen.source === c.source} onChange={() => onSelect({ rebind: { shardId: rebind.shardId, source: c.source, declined: false }, seriesKey })} />
                <span className="font-mono">{c.name}</span>
                <span className="text-muted-foreground">({c.source === "previous" ? "previous name" : "from the PC"})</span>
              </label>
            ))}
            <label className="flex items-center gap-1.5">
              <input type="radio" name="rebind" disabled={locked} checked={!!rebind.declined} onChange={() => onSelect({ rebind: { shardId: rebind.shardId, declined: true }, seriesKey })} />
              Don't copy it back
            </label>
          </div>
        </div>
      )}
      {detail && !detail.ok && <div className="px-4 py-3 text-xs text-destructive">{detail.message || "Couldn't load this series."}</div>}
      {!detail && <div className="px-4 py-3 text-xs text-muted-foreground">Loading…</div>}
      {groups.map((g) => (
        <Group
          key={g.id}
          g={g}
          tn={tn}
          locked={locked || isDev}
          pendingOps={pendingOps}
          open={isOpen(g)}
          onToggleOpen={() => setOpenGroups((o) => ({ ...o, [g.id]: !isOpen(g) }))}
          onTickGroup={tickGroup}
          onTick={tick}
        />
      ))}
      {suggestions.length > 0 && (
        <div className="border-b px-3 py-2">
          {suggestions.map((g) => (
            <div key={g.id} className="flex items-center gap-2 py-1 text-[12.5px]">
              <Link className="h-3.5 w-3.5 shrink-0 text-primary" aria-hidden="true" />
              <span className="min-w-0 flex-1">
                {isDev ? (
                  <>Looks like <b className="font-semibold">{g.seriesKey.replace(/^pc:/, "")}</b> in your library</>
                ) : (
                  <>
                    <span className="font-mono">{g.deviceFolder}</span> on {tn} looks like this series
                  </>
                )}
                <span className="text-muted-foreground"> · {SUGGESTION_TIER_COPY[g.tier] || g.tier}</span>
              </span>
              {linking && linking.has(g.deviceFolder) ? (
                <span className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground" role="status">
                  <Loader2 className="h-3 w-3 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                  Linking…
                </span>
              ) : (
                <>
                  <Button size="sm" variant="outline" className={XS} disabled={locked} onClick={async () => { const r = await sync.actions.link({ targetId, suggestionIds: [g.id] }); if (!r.ok) onError && onError(r); }}>
                    Link
                  </Button>
                  <Button size="sm" variant="ghost" className={XS} disabled={locked} onClick={() => run("ignore-suggestion", { suggestionId: g.id })}>
                    Ignore
                  </Button>
                </>
              )}
            </div>
          ))}
        </div>
      )}
      {anomalies.length > 0 && (
        <>
          <div className="flex items-center gap-2 border-b px-3 py-2 text-[12.5px] font-semibold">
            <span className="w-4" />
            <AlertTriangle className="h-3.5 w-3.5 text-warn-fg" aria-hidden="true" />
            Anomalies
            <span className="font-mono text-xs font-normal text-muted-foreground">{anomalies.length}</span>
          </div>
          {anomalies.map((a) => (
            <div key={a.id} className={cn("flex items-center gap-2.5 border-b border-border/50 py-[5px] pl-10 pr-3 text-[12.5px]", a.acknowledged && "opacity-60")}>
              <span className="min-w-0 flex-1 text-muted-foreground">{anomalyText(a, tn)}</span>
              {a.acknowledged ? (
                <Button size="sm" variant="ghost" className={XS} onClick={() => run("unack-anomaly", { anomalyId: a.id })}>
                  Undo
                </Button>
              ) : (
                <Button size="sm" variant="ghost" className={XS} onClick={() => run("ack-anomaly", { anomalyId: a.id })}>
                  Acknowledge
                </Button>
              )}
            </div>
          ))}
        </>
      )}
    </Sheet>
  );
}
