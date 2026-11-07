// ============================================================
// PruneDialog — "Remove folders from <target>" (approved spec §4 surfaces;
// mockup pruneDialog)
//
// Prune is its own action, never part of Review (UI-pass decision 3): the
// user ticks folders one by one and sync:prune gets exactly that list
// (service.js H.prune refuses an empty one). Only `orphaned` folders (the app
// synced them, the series left the library) can be ticked; `preserved`
// folders (never synced by this app) are listed locked, so it's clear they
// were seen and kept. Nothing starts ticked.
// Consumer: SyncTab.jsx.
// ============================================================

import React, { useEffect, useState } from "react";
import { Trash2 } from "lucide-react";
import { Button, Checkbox } from "@/components/ui/primitives";
import { Dialog } from "@/components/ui/overlay";
import { formatBytes } from "@/lib/utils";
import { RefusalCard } from "./SyncCards";

export default function PruneDialog({ open, onClose, targetId, tn, items, contained, sync, onAction }) {
  const [picked, setPicked] = useState(() => new Set());
  const [refusal, setRefusal] = useState(null);
  const [starting, setStarting] = useState(false);
  useEffect(() => {
    if (open) {
      setPicked(new Set());
      setRefusal(null);
    }
  }, [open]);
  const list = items || [];
  const preserved = list.filter((d) => d.state !== "orphaned").map((d) => d.name);
  const chosen = list.filter((d) => picked.has(d.name));
  const bytes = chosen.reduce((n, d) => n + (d.bytes || 0), 0);
  const start = async () => {
    setStarting(true);
    const r = await sync.actions.prune({ targetId, folders: chosen.map((d) => d.name) });
    setStarting(false);
    if (r && r.ok) onClose();
    else setRefusal(r);
  };
  return (
    <Dialog
      open={open}
      onClose={onClose}
      width={520}
      contained={contained}
      title={`Remove folders from ${tn}`}
      description={`Only the folders you tick are removed.${preserved.length ? ` ${preserved.length === 1 ? preserved[0] + " can't be removed here because this app never synced it." : `${preserved.length} folders this app never synced can't be removed here.`}` : ""}`}
      bodyClassName="py-2"
      footer={
        <>
          <Button size="sm" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button size="sm" variant="destructive" disabled={!chosen.length || starting} onClick={start} className="gap-1.5 [&_svg]:h-3.5 [&_svg]:w-3.5">
            <Trash2 aria-hidden="true" />
            {chosen.length ? `Remove ${chosen.length} folder${chosen.length === 1 ? "" : "s"} (${formatBytes(bytes)})` : "Remove folders"}
          </Button>
        </>
      }
    >
      {refusal && <RefusalCard refusal={refusal} tn={tn} onAction={onAction} onDismiss={() => setRefusal(null)} compact className="mb-2" />}
      {list.map((d) => {
        const ok = d.state === "orphaned" && !!d.shardId;
        return (
          <div key={d.key} className="flex items-center gap-2.5 border-b border-border/70 py-[7px]">
            <Checkbox
              checked={picked.has(d.name)}
              locked={!ok}
              danger={ok}
              aria-label={`Remove ${d.name}`}
              title={ok ? undefined : "Never synced by this app; always kept"}
              onCheckedChange={(v) =>
                setPicked((s) => {
                  const n = new Set(s);
                  if (v) n.add(d.name);
                  else n.delete(d.name);
                  return n;
                })
              }
            />
            <span className="min-w-0 flex-1 truncate font-mono text-xs">{d.name}</span>
            <span className="shrink-0 font-mono text-[11px] text-muted-foreground">
              {d.files} file{d.files === 1 ? "" : "s"} · {formatBytes(d.bytes)}
            </span>
          </div>
        );
      })}
    </Dialog>
  );
}
