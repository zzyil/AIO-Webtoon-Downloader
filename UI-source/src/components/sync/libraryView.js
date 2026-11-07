// ============================================================
// libraryView — Device Sync's pure view model for the Library tab (UI pass
// U4, approved design D9: a cover chip on each card + a "Sync" line in
// DetailView with Open in Sync)
//
// Input is one series' entry of the hook's libraryStatus.bySeries, keyed by
// the series' folderPath (service.js _computeLibraryStatus, planner
// libraryStatus): [{targetId, state, pending, asOf?}], one per target that
// mirrors the series. state: synced | pending | absent | removed. The status
// is built from each target's RECORD, never a device listing (rule 13), so it
// is valid for a target that isn't connected. A series that a target doesn't
// mirror (excluded, image-only, nothing mirrored) has no entry for it; a
// series no target mirrors has no entry at all, and gets no chip.
//
// Never orange: orange is the Library "N new" badge (spec §2).
//
// Consumers: components/LibraryTab.jsx (SyncChip, SyncLine), App.jsx (Open
// in Sync), src/dev/SyncGallery.jsx. Tested by
// tools/_test_device_sync_view.js.
// ============================================================

/** The planner's seriesKey for a library folder (planner.js pcSeriesKey: `pc:` + folder name). */
export function librarySeriesKey(folderPath) {
  const parts = String(folderPath || "").split(/[\\/]+/).filter(Boolean);
  return parts.length ? `pc:${parts[parts.length - 1]}` : null;
}

function _named(rows, targets) {
  const byId = new Map((targets || []).map((t) => [t.id, t]));
  return (rows || []).filter((r) => r && byId.has(r.targetId)).map((r) => ({ ...r, target: byId.get(r.targetId) }));
}

const _present = (r) => r.state === "pending" || r.state === "removed";

/**
 * The cover chip: {kind:'count', n, tip} (the most chapters missing on any
 * target that holds the series), {kind:'ok', tip} (nothing missing anywhere
 * it is, and on at least one target), {kind:'dim', tip} (on no target yet),
 * or null (not mirrored anywhere).
 */
export function seriesSyncChip(rows, targets) {
  const list = _named(rows, targets);
  if (!list.length) return null;
  const tip = list.map((r) => `${r.target.name || "Target"}: ${lineText(r)}`).join("\n");
  const present = list.filter(_present);
  if (present.length) return { kind: "count", n: Math.max(...present.map((r) => r.pending || 0)), tip };
  if (list.some((r) => r.state === "synced")) return { kind: "ok", tip };
  return { kind: "dim", tip };
}

function lineText(r) {
  const n = r.pending || 0;
  switch (r.state) {
    case "synced":
      return "in sync";
    case "pending":
      return `${n} to copy`;
    case "removed":
      return `removed there · ${n} to copy`;
    default:
      return "not on it yet";
  }
}

/**
 * DetailView's Sync line: one entry per target, in config order.
 * tone: push (blue count pill) | ok (success) | muted.
 */
export function seriesSyncLines(rows, targets) {
  const list = _named(rows, targets);
  const order = new Map((targets || []).map((t, i) => [t.id, i]));
  list.sort((a, b) => order.get(a.targetId) - order.get(b.targetId));
  return list.map((r) => ({
    targetId: r.targetId,
    name: r.target.name || "Target",
    kind: r.target.kind === "folder" ? "folder" : "tablet",
    text: lineText(r),
    tone: _present(r) ? "push" : r.state === "synced" ? "ok" : "muted",
  }));
}

/** Where Open in Sync lands: the first target (config order) with chapters to copy, else the first. */
export function openInSyncTarget(rows, targets) {
  const lines = seriesSyncLines(rows, targets);
  const hit = lines.find((l) => l.tone === "push") || lines[0];
  return hit ? hit.targetId : null;
}
