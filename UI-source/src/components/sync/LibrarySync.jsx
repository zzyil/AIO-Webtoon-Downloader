// ============================================================
// LibrarySync — Device Sync's two Library surfaces (UI pass U4, design D9;
// mockup syncChip() / libraryDetail())
//
//   SyncChip   bottom-right on a cover (grid card and DetailView cover):
//              tablet + count / tablet + check in success / dim tablet
//   SyncLine   DetailView's per-series "Sync" facts row, under the stats,
//              one item per target, with Open in Sync
//
// Both draw libraryView.js over the hook's libraryStatus; neither asks main
// for anything. Consumers: LibraryTab.jsx (grep SyncChip), the DEV gallery.
// ============================================================

import React from "react";
import { Check, ExternalLink, Folder, FolderSync, Tablet } from "lucide-react";
import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

const CHIP = "absolute bottom-1.5 right-1.5 flex h-[18px] items-center gap-[3px] rounded-[5px] px-[5px] text-[10px] font-bold";

/** @param {{chip: {kind:'count'|'ok'|'dim', n?:number, tip:string}|null}} p */
export function SyncChip({ chip }) {
  if (!chip) return null;
  const icon = <Tablet className="h-2.5 w-2.5" aria-hidden="true" />;
  if (chip.kind === "count") {
    return (
      <span data-testid="sync-chip" data-kind="count" title={chip.tip} className={cn(CHIP, "bg-card/90 text-foreground shadow-[0_0_0_1px_hsl(var(--border))]")}>
        {icon}
        <span className="font-mono tabular-nums">{chip.n}</span>
      </span>
    );
  }
  if (chip.kind === "ok") {
    return (
      <span data-testid="sync-chip" data-kind="ok" title={chip.tip} className={cn(CHIP, "bg-card/90 text-success shadow-[0_0_0_1px_hsl(var(--border))]")}>
        {icon}
        <Check className="h-2.5 w-2.5" aria-hidden="true" />
      </span>
    );
  }
  return (
    <span data-testid="sync-chip" data-kind="dim" title={chip.tip} className={cn(CHIP, "bg-card/70 text-muted-foreground opacity-75")}>
      {icon}
    </span>
  );
}

/** @param {{lines: Array<{targetId,name,kind,text,tone}>, onOpen?: () => void}} p */
export function SyncLine({ lines, onOpen }) {
  if (!lines || !lines.length) return null;
  return (
    <div data-testid="sync-line" className="flex flex-wrap items-center gap-x-2.5 gap-y-1 rounded-lg bg-muted/70 px-2.5 py-1.5 text-xs">
      <span className="flex items-center gap-1 text-muted-foreground">
        <FolderSync className="h-3.5 w-3.5" aria-hidden="true" />
        Sync
      </span>
      {lines.map((l) => {
        const Icon = l.kind === "folder" ? Folder : Tablet;
        return (
          <span key={l.targetId} className="flex min-w-0 items-center gap-1.5">
            <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span className="truncate">{l.name}</span>
            {l.tone === "push" ? (
              <span className="rounded bg-primary/[.12] px-[5px] font-mono text-[11px] leading-[18px] tabular-nums text-primary">{l.text}</span>
            ) : l.tone === "ok" ? (
              <span className="flex items-center gap-0.5 text-success">
                <Check className="h-3 w-3" aria-hidden="true" />
                {l.text}
              </span>
            ) : (
              <span className="text-muted-foreground">{l.text}</span>
            )}
          </span>
        );
      })}
      <span className="flex-1" />
      {onOpen && (
        <Button size="sm" variant="ghost" className="h-6 gap-1 px-2 text-xs" onClick={onOpen}>
          <ExternalLink className="h-3 w-3" aria-hidden="true" />
          Open in Sync
        </Button>
      )}
    </div>
  );
}
