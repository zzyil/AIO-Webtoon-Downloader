// ============================================================
// CapacityGauge — a target's storage, and what the selection would take
//
// Input is the summary's `capacity` (service.js _capacity): {free, total,
// needed, lowest, reserve, freeable, after, shortBy, shortAt, fits, asOf}.
// `after` is main's free-after-sync from the space model (space.js
// simulateRun: signed growth, only provable removals); `lowest` is free space
// at the run's tightest moment, which sits below `after` when a series writes
// its new copies before the old ones go. `total` comes from the transport's
// storage() and is null when the target can't report it. Without a total the
// bar is drawn against free space alone: the "used" part is unknown, so the
// bar shows the after-sync ghost and the reserve as shares of what is free.
//
// Spec §4: 8 px pill on --muted; used --foreground/.45; the after-sync ghost
// a primary hatch, the mid-run dip beyond it a lighter hatch (plan
// device-sync-auto-replace.md A2); the reserve a destructive hatch at the
// right end. Labels below in mono: "after sync X free", plus "dips to Y" when
// the dip is visible on the bar, or "short by Z" when the run doesn't fit
// (Start is refused; user decision Q9: no "sync what fits"). `free == null`
// (free space unreadable) renders a muted line instead of a bar, never a
// made-up number.
//
// Consumer: TargetBar.jsx. Depends on lib/utils formatBytes; the numbers
// are syncView.js gaugeView (tested in tools/_test_device_sync_view.js).
// ============================================================

import React from "react";
import { cn, formatBytes } from "@/lib/utils";
import { gaugeView } from "./syncView";

export default function CapacityGauge({ capacity, targetName, className }) {
  const g = gaugeView(capacity);
  if (!g) {
    return <div className={cn("text-[11px] text-muted-foreground", className)}>Free space on {targetName || "the target"} unknown</div>;
  }
  const pct = (x) => `${Math.max(0, Math.min(100, (x / g.total) * 100))}%`;
  const ghost = Math.min(g.net, g.free);
  const dip = Math.min(g.dip, Math.max(0, g.free - ghost));
  return (
    <div className={cn("flex min-w-0 flex-col gap-1", className)}>
      <div
        role="img"
        aria-label={`${formatBytes(g.free)} free, ${formatBytes(g.net)} needed${g.showDip ? `, dips to ${formatBytes(Math.max(0, g.lowest))} free mid-run` : ""}`}
        className="flex h-2 overflow-hidden rounded-full bg-muted shadow-[inset_0_0_0_1px_hsl(var(--border))]"
      >
        {g.used > 0 && <span className="bg-foreground/45" style={{ width: pct(g.used) }} />}
        {ghost > 0 && <span className="sync-gauge-ghost" style={{ width: pct(ghost) }} />}
        {g.showDip && dip > 0 && <span className="sync-gauge-dip" style={{ width: pct(dip) }} />}
        {g.reserve > 0 && <span className="sync-gauge-reserve ml-auto" style={{ width: pct(Math.min(g.reserve, g.total)) }} />}
      </div>
      <div className="flex gap-2.5 font-mono text-[11px] tabular-nums text-muted-foreground">
        {g.used > 0 && <span>{formatBytes(g.used)} used</span>}
        <span>{formatBytes(g.free)} free</span>
        <span className="flex-1" />
        {g.short ? (
          <span className="text-destructive">
            short by <b className="font-semibold">{formatBytes(g.shortBy)}</b>
          </span>
        ) : (
          <span>
            {g.showDip && <>dips to {formatBytes(Math.max(0, g.lowest))} · </>}
            after sync <b className="font-semibold text-foreground">{formatBytes(Math.max(0, g.after))}</b> free
          </span>
        )}
      </div>
    </div>
  );
}
