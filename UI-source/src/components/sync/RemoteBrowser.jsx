// ============================================================
// RemoteBrowser — pick a folder on an adb tablet
//
// Drives sync:browse-remote {serial, path} → {path (canonical, realpath'd),
// parent, entries:[{name,isDir}], presets:[{label, path, exists}]}
// (service.js H["browse-remote"]). "Use this folder" hands back the
// CANONICAL path the service answered, which is what a target stores (plan
// decision 5: the root stored is browse-remote's canonical path).
//
// Used by TargetEditor.jsx (the editor and the draft add card). Approved
// design: spec §4 TargetEditor, mockup `remoteBrowser()`.
// ============================================================

import React, { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/primitives";
import { Folder, Loader2, Tablet, Undo2 } from "lucide-react";
import { cn } from "@/lib/utils";

export default function RemoteBrowser({ sync, serial, startPath, profile, onUse, onCancel }) {
  const [view, setView] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  // Depends on sync.actions (memoised for the hook's life), never on `sync`
  // itself: that object is new on every App render, and depending on it
  // snapped the browser back to startPath and re-listed over adb on every
  // log line (review R11). `seq` drops a listing a later click superseded.
  const actions = sync.actions;
  const seq = useRef(0);
  const go = useCallback(
    async (p) => {
      const my = ++seq.current;
      setLoading(true);
      setError(null);
      const r = await actions.browseRemote({ serial, path: p });
      if (my !== seq.current) return;
      setLoading(false);
      if (r && r.ok !== false) setView(r);
      else setError((r && r.message) || "Couldn't list that folder");
    },
    [actions, serial]
  );

  // The start folder is read once per mount: typing in the root field
  // behind an open browser must not move it. "Once per tablet" holds because
  // both callers key this component by serial (SyncSettings DraftTarget,
  // TargetEditor), so another tablet remounts it from its own start.
  const start = useRef(startPath);
  useEffect(() => {
    go(start.current || "/sdcard");
  }, [go]);

  const presets = (view && view.presets) || [];
  const here = view ? view.path : startPath || "/sdcard";
  const segs = here.split("/").filter(Boolean);
  // Komikku and Mihon read only the `local` folder inside their storage root.
  const notLocal = profile === "komikku" && view && !/\/local$/.test(view.path);

  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      <div className="flex flex-wrap gap-1 border-b px-2.5 py-2">
        {presets.map((p) => (
          <button
            key={p.path}
            type="button"
            disabled={!p.exists}
            title={p.exists ? p.path : `${p.path} doesn't exist on this tablet`}
            onClick={() => go(p.path)}
            className={cn(
              "rounded-full border px-2.5 py-0.5 text-[11.5px] transition-colors disabled:opacity-40",
              // Presets name /sdcard paths; the view is the canonical
              // realpath. Highlight only (cosmetic), assuming the usual
              // /sdcard → /storage/emulated/0 link; a miss just leaves the
              // chip unhighlighted.
              view && (view.path === p.path || view.path === p.path.replace(/^\/sdcard(?=\/|$)/, "/storage/emulated/0"))
                ? "border-primary/40 bg-primary/10 text-primary"
                : "hover:bg-accent"
            )}
          >
            {p.label}
          </button>
        ))}
      </div>
      <div className="flex items-center gap-1.5 border-b px-2.5 py-1.5 font-mono text-[11px] text-muted-foreground">
        <Tablet className="h-3 w-3 shrink-0" />
        <span className="min-w-0 truncate" title={here}>
          /{segs.slice(0, -1).join("/")}
          {segs.length > 1 ? "/" : ""}
          <b className="font-semibold text-foreground">{segs[segs.length - 1] || ""}</b>
        </span>
        {loading && <Loader2 className="ml-auto h-3 w-3 animate-spin motion-reduce:animate-none" />}
      </div>
      <div className="max-h-48 overflow-y-auto py-1">
        {error && <div className="px-2.5 py-1.5 text-xs text-destructive">{error}</div>}
        {view && view.parent && (
          <Row icon={Undo2} name=".." onClick={() => go(view.parent)} />
        )}
        {view && view.entries.map((e) => <Row key={e.name} icon={Folder} name={e.name} onClick={() => go(`${view.path.replace(/\/$/, "")}/${e.name}`)} />)}
        {view && !view.entries.length && <div className="px-2.5 py-1.5 text-xs text-muted-foreground">No folders here.</div>}
      </div>
      <div className="flex items-center gap-2 border-t px-2.5 py-2">
        <span className={cn("min-w-0 flex-1 text-[11px]", notLocal ? "text-warn-fg" : "text-muted-foreground")}>
          {view
            ? notLocal
              ? "Komikku reads only the local folder inside its storage folder."
              : `${view.entries.length} folder${view.entries.length === 1 ? "" : "s"}`
            : ""}
        </span>
        {onCancel && (
          <Button size="sm" variant="ghost" className="h-7" onClick={onCancel}>
            Cancel
          </Button>
        )}
        <Button size="sm" className="h-7" disabled={!view || loading} onClick={() => onUse(view.path)}>
          Use this folder
        </Button>
      </div>
    </div>
  );
}

function Row({ icon: Icon, name, onClick }) {
  return (
    <button type="button" onClick={onClick} className="flex w-full items-center gap-2 px-2.5 py-1 text-left text-xs hover:bg-accent/60">
      <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      <span className="truncate font-mono">{name}</span>
    </button>
  );
}
