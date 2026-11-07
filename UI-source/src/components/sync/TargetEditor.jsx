// ============================================================
// TargetEditor — one sync target's settings, saved as you change them
//
// The SHARED editor (plan decision 5, user decision 4): Settings → Sync →
// Targets lists every target in one of these, and the Sync tab's connected-
// target sheet (U2) shows the same component for what is plugged in now.
//
// Persistence: every change is an immediate sync:config-op through
// useConfigOps (serialized, newest expectVersion). Text fields save on blur
// or Enter. Nothing here touches the Settings draft / Save button; the
// "Changes here save immediately" note says so.
//
// Identity fields (kind, tablet, folder) forget the target's sync history
// when changed (profiles.js recordIdentityChanged; the reader is NOT one,
// user decision 2026-10-16). They edit a local draft and commit only through
// the inline confirm strip naming the target (spec §4).
//
// Reader presets fix the file options; only Custom shows them (user
// decision 2026-10-16: file types, extra files, .nomedia, folder names,
// rename-to-match and clean-up are Custom-only). Switching to Custom seeds
// the old preset's values server-side (service.js update-target), so the
// switch changes no file.
//
// Copy: ./copy.js. Approved design: spec §4 TargetEditor, mockup
// `targetEditor()` in sync-temp/design/approved/device-sync-ui.html.
// ============================================================

import React, { useEffect, useMemo, useRef, useState } from "react";
import { Button, Input, Select, Switch, SegmentedControl } from "@/components/ui/primitives";
import { Popover } from "@/components/ui/overlay";
import { AlertTriangle, Check, FolderOpen, History, Plus, Trash2, X, Zap } from "lucide-react";
import { cn } from "@/lib/utils";
import RemoteBrowser from "./RemoteBrowser";
import { refusalField } from "./useConfigOps";
import {
  PROFILE_COPY, PROFILE_ORDER, DELETE_POLICY_COPY, DELETE_POLICY_ORDER, NAMING_POLICY_COPY, SELECTION_MODE_COPY,
  CHAPTER_FORMATS, SIDECAR_NAMES, CUSTOM_DEFAULTS, KIND_COPY, configRefusalCopy, deviceLabel,
} from "./copy";

const ARM_MS = 4000;

/** Width-driven layout: fields stack below ~560 px (spec §4). */
function useNarrow(ref, limit = 560) {
  const [narrow, setNarrow] = useState(true);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(([e]) => setNarrow(e.contentRect.width < limit));
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref, limit]);
  return narrow;
}

/** Two-click arming for destructive buttons; disarms itself after ARM_MS. */
export function useArm() {
  const [armed, setArmed] = useState(null);
  const timer = useRef(null);
  useEffect(() => () => clearTimeout(timer.current), []);
  const arm = (key) => {
    clearTimeout(timer.current);
    setArmed(key);
    timer.current = setTimeout(() => setArmed(null), ARM_MS);
  };
  return { armed, arm, disarm: () => setArmed(null) };
}

export function PersistNote({ children }) {
  return (
    <div className="mb-2 flex items-center gap-1.5 text-[11.5px] text-muted-foreground">
      <Zap className="h-3 w-3 shrink-0 text-primary" />
      <span>{children || "Changes here save immediately."}</span>
    </div>
  );
}

export function Field({ label, identity, targetName, hint, error, children, className }) {
  return (
    <div className={cn("flex flex-col gap-1 py-1.5", className)}>
      <div className="flex items-center gap-1 text-xs font-semibold">
        {label}
        {identity && (
          <span title={`Changing this forgets sync history for ${targetName}`} className="text-muted-foreground">
            <History className="h-3 w-3" />
          </span>
        )}
      </div>
      {children}
      {error ? (
        <div className="flex items-start gap-1 text-[11px] leading-snug text-destructive">
          <AlertTriangle className="mt-px h-3 w-3 shrink-0" />
          <span>{error}</span>
        </div>
      ) : hint ? (
        <div className="text-[11px] leading-snug text-muted-foreground">{hint}</div>
      ) : null}
    </div>
  );
}

function Chip({ on, children, onClick, onRemove, mono, title, disabled, tone }) {
  const Tag = onClick ? "button" : "span";
  return (
    <Tag
      type={onClick ? "button" : undefined}
      onClick={onClick}
      disabled={onClick ? disabled : undefined}
      title={title}
      aria-pressed={onClick ? !!on : undefined}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11.5px] transition-colors",
        mono && "font-mono",
        on ? "border-primary/40 bg-primary/10 text-primary" : "text-foreground",
        onClick && !disabled && "hover:bg-accent",
        disabled && "cursor-not-allowed opacity-60",
        tone === "warn" && "border-warning/40 text-warn-fg"
      )}
    >
      {on && onClick && <Check className="h-2.5 w-2.5" />}
      {children}
      {onRemove && (
        <span
          role="button"
          tabIndex={0}
          aria-label="Remove"
          onClick={(e) => {
            e.stopPropagation();
            onRemove();
          }}
          onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onRemove()}
          className="-mr-0.5 rounded-full p-px text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          <X className="h-2.5 w-2.5" />
        </span>
      )}
    </Tag>
  );
}

/** A library entry as a stored series reference (service.js _seriesRef). */
export function entryRef(e) {
  const ref = {};
  if (e.seriesKey) ref.identityKey = e.seriesKey;
  if (e.seriesMeta && e.seriesMeta.url) ref.url = e.seriesMeta.url;
  if (e.title) ref.pcFolder = e.title;
  return ref;
}

function refMatches(ref, e) {
  if (!ref || !e) return false;
  if (ref.identityKey && e.seriesKey && ref.identityKey === e.seriesKey) return true;
  if (ref.url && e.seriesMeta && e.seriesMeta.url === ref.url) return true;
  return !!ref.pcFolder && ref.pcFolder === e.title;
}

const refLabel = (ref) => (ref && (ref.pcFolder || ref.url || ref.identityKey)) || "?";

/** Library series picker in a popover (exclude / only lists). */
function SeriesPicker({ label, library, taken, onPick }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const entries = library.entries || [];
  useEffect(() => {
    if (open && !entries.length && library.load) library.load();
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return entries
      .filter((e) => !taken.some((r) => refMatches(r, e)))
      .filter((e) => !needle || String(e.title).toLowerCase().includes(needle))
      .slice(0, 50);
  }, [entries, taken, q]);
  return (
    <Popover
      open={open}
      onClose={() => setOpen(false)}
      align="start"
      width={280}
      trigger={
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="inline-flex items-center gap-1 rounded-full border border-dashed px-2 py-0.5 text-[11.5px] text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          <Plus className="h-2.5 w-2.5" />
          {label}
        </button>
      }
    >
      <Input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter your library" className="mb-1 h-7 text-xs" />
      <div className="max-h-56 overflow-y-auto">
        {library.loading && !entries.length && <div className="px-2 py-1.5 text-xs text-muted-foreground">Loading your library…</div>}
        {!library.loading && !shown.length && <div className="px-2 py-1.5 text-xs text-muted-foreground">No matching series.</div>}
        {shown.map((e) => (
          <button
            key={e.folderPath || e.title}
            type="button"
            onClick={() => {
              onPick(entryRef(e));
              setOpen(false);
              setQ("");
            }}
            className="block w-full truncate rounded px-2 py-1 text-left text-xs hover:bg-accent"
          >
            {e.title}
          </button>
        ))}
      </div>
    </Popover>
  );
}

/**
 * @param {object} props
 * @param {object} props.target   The stored target (sync.config.targets[i]).
 * @param {object} props.sync     useDeviceSync() result.
 * @param {object} props.ops      useConfigOps(sync) result (shared per surface).
 * @param {{entries:Array, loading?:boolean, load?:Function}} props.library
 * @param {{pickFolder?:Function}} [props.host]  window.electronAPI by default.
 */
export default function TargetEditor({ target: t, sync, ops, library, host }) {
  const rootRef = useRef(null);
  const narrow = useNarrow(rootRef);
  const tn = t.name || "this target";
  const [err, setErr] = useState(null); // {field, text}
  const [name, setName] = useState(t.name || "");
  const [idDraft, setIdDraft] = useState(null); // {kind, serial, root}
  const [browsing, setBrowsing] = useState(false);
  const [cleanupInput, setCleanupInput] = useState("");
  const { armed, arm, disarm } = useArm();
  const api = host || (typeof window !== "undefined" ? window.electronAPI : null);

  useEffect(() => setName(t.name || ""), [t.name]);

  const send = async (op, payload, field) => {
    const r = await ops.run(op, { targetId: t.id, ...(payload || {}) });
    if (r && r.ok === false) setErr({ field: refusalField(r) || field || null, text: configRefusalCopy(r, tn) });
    else setErr(null);
    return r;
  };
  const patch = (p, field) => send("update-target", { patch: p }, field);
  // List fields change by ONE add or remove (service.js edit-list / the
  // entry-named removes), never by a whole array or an index taken from
  // this render: a second click queued behind the first would otherwise
  // undo it or hit the wrong entry (review R12).
  const toggle = (field, value, on) => send("edit-list", { field, [on ? "remove" : "add"]: value }, field);
  const errFor = (f) => (err && err.field === f ? err.text : null);

  // ── identity draft ──
  const id = idDraft || { kind: t.kind, serial: t.serial || null, root: t.root || "" };
  const idChanged = !!idDraft && (id.kind !== t.kind || (id.serial || null) !== (t.serial || null) || id.root !== t.root);
  const idComplete = !!id.root && (id.kind !== "adb" || !!id.serial);
  const editId = (k, v) =>
    setIdDraft((d) => {
      const cur = d || { kind: t.kind, serial: t.serial || null, root: t.root || "" };
      if (k === "kind") return v === cur.kind ? cur : { kind: v, serial: v === "adb" ? cur.serial || null : null, root: "" };
      return { ...cur, [k]: v };
    });
  const commitId = async () => {
    const r = await patch({ kind: id.kind, serial: id.kind === "adb" ? id.serial : null, root: id.root }, "root");
    if (r && r.ok !== false) {
      setIdDraft(null);
      setBrowsing(false);
    }
  };

  const devices = (sync.devices || []).filter((d) => d.targetable);
  const serialOpts = useMemo(() => {
    const list = devices.map((d) => ({ serial: d.serial, label: `${deviceLabel(d)} · ${d.serial}`, ready: d.stateKind === "device" }));
    if (id.serial && !list.some((x) => x.serial === id.serial)) list.unshift({ serial: id.serial, label: `${id.serial} (not connected)`, ready: false });
    return list;
  }, [devices, id.serial]);
  const deviceReady = serialOpts.some((x) => x.serial === id.serial && x.ready);

  const profile = PROFILE_COPY[t.profile] ? t.profile : "everything";
  const custom = profile === "custom";
  const val = (k) => (t[k] != null ? t[k] : CUSTOM_DEFAULTS[k]);
  const formats = val("formats");
  const sidecars = val("sidecars");
  const cleanup = val("cleanupNames");
  const deletePolicy = t.deletePolicy || (sync.settings && sync.settings.syncDeletePolicy) || "guarded";
  const selection = t.selection === "only" ? "only" : "all-except";
  const excludes = t.excludes || [];
  const only = t.only || [];
  const aliases = t.aliases || [];
  const libEntries = library.entries || [];

  const grid = (a, b) => (narrow ? <>{a}{b}</> : <div className="grid grid-cols-2 gap-x-3.5">{a}{b}</div>);

  const pickLocalFolder = async () => {
    if (!api || typeof api.pickFolder !== "function") return;
    const p = await api.pickFolder();
    if (p) editId("root", p);
  };

  return (
    <div ref={rootRef} className="flex flex-col">
      <PersistNote>
        Changes here save immediately. Fields marked <History className="inline h-3 w-3 align-[-2px]" /> forget sync history for {tn}.
      </PersistNote>
      {err && !err.field && <div className="mb-1 rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-1.5 text-xs text-destructive">{err.text}</div>}

      {grid(
        <Field label="Name" error={errFor("name")}>
          <Input
            value={name}
            aria-label="Name"
            className="h-8 text-[13px]"
            onChange={(e) => setName(e.target.value)}
            onBlur={() => name.trim() && name.trim() !== t.name && patch({ name: name.trim() }, "name")}
            onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
          />
        </Field>,
        <Field label="Kind" identity targetName={tn} error={errFor("kind")}>
          <SegmentedControl
            aria-label="Kind"
            value={id.kind}
            onChange={(v) => editId("kind", v)}
            options={["adb", "folder"].map((k) => ({ value: k, label: KIND_COPY[k] }))}
          />
        </Field>
      )}

      {id.kind === "adb" && (
        <Field label="Tablet" identity targetName={tn} error={errFor("serial")}>
          <Select value={id.serial || ""} onChange={(e) => editId("serial", e.target.value || null)} className="h-8 text-[13px]">
            {!id.serial && <option value="">Pick a tablet…</option>}
            {serialOpts.map((o) => (
              <option key={o.serial} value={o.serial}>
                {o.label}
              </option>
            ))}
          </Select>
        </Field>
      )}

      <Field
        label={id.kind === "adb" ? `Folder on ${tn}` : "Folder"}
        identity
        targetName={tn}
        error={errFor("root")}
        hint={profile === "komikku" && id.root && !/[\\/]local[\\/]?$/.test(id.root) ? "Komikku reads only the local folder inside its storage folder. Pick that local folder." : null}
      >
        <div className="flex items-center gap-1.5">
          <Input
            value={id.root}
            aria-label="Folder"
            spellCheck={false}
            className="h-8 min-w-0 flex-1 font-mono text-xs"
            onChange={(e) => editId("root", e.target.value)}
          />
          <Button
            size="sm"
            variant="outline"
            className="h-8 shrink-0"
            disabled={id.kind === "adb" && !deviceReady}
            title={id.kind === "adb" && !deviceReady ? `Connect ${tn} to browse it` : undefined}
            onClick={() => (id.kind === "adb" ? setBrowsing((v) => !v) : pickLocalFolder())}
          >
            <FolderOpen className="h-3.5 w-3.5" />
            Browse
          </Button>
        </div>
      </Field>
      {browsing && id.kind === "adb" && deviceReady && (
        <div className="mb-2">
          <RemoteBrowser
            key={id.serial}
            sync={sync}
            serial={id.serial}
            startPath={id.root || "/sdcard"}
            profile={profile}
            onCancel={() => setBrowsing(false)}
            onUse={(p) => {
              editId("root", p);
              setBrowsing(false);
            }}
          />
        </div>
      )}
      {idChanged && (
        <div className="my-1 flex flex-col gap-2 rounded-md border border-destructive/35 bg-destructive/[.07] px-3 py-2.5 text-[12.5px]">
          <div>
            <b className="font-semibold">This forgets sync history for {tn}.</b>{" "}
            {idComplete ? (
              <>
                The next sync re-checks every file in <span className="font-mono">{id.root}</span> before copying anything.
              </>
            ) : (
              <span className="text-muted-foreground">{id.kind === "adb" && !id.serial ? "Pick a tablet and a folder first." : "Pick a folder first."}</span>
            )}
          </div>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="destructive" className="h-7" disabled={!idComplete || ops.pending} onClick={commitId}>
              Change and forget history
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="h-7"
              onClick={() => {
                setIdDraft(null);
                setBrowsing(false);
              }}
            >
              Keep current settings
            </Button>
          </div>
        </div>
      )}

      <Field label="Reader" error={errFor("profile")} hint={PROFILE_COPY[profile].hint}>
        <SegmentedControl
          aria-label="Reader"
          value={profile}
          onChange={(v) => patch({ profile: v }, "profile")}
          options={PROFILE_ORDER.map((k) => ({ value: k, label: PROFILE_COPY[k].label }))}
        />
      </Field>

      <Field label="Removals" error={errFor("deletePolicy")} hint={DELETE_POLICY_COPY[deletePolicy].hint}>
        <SegmentedControl
          aria-label="Removals"
          value={deletePolicy}
          onChange={(v) => patch({ deletePolicy: v }, "deletePolicy")}
          options={DELETE_POLICY_ORDER.map((k) => ({ value: k, label: DELETE_POLICY_COPY[k].label }))}
        />
      </Field>

      {custom && (
        <div className="mt-1 rounded-md border bg-muted/30 px-3 py-1">
          {grid(
            <Field label="File types" error={errFor("formats")}>
              <div className="flex flex-wrap gap-1">
                {CHAPTER_FORMATS.map((f) => {
                  const on = formats.includes(f);
                  return (
                    <Chip
                      key={f}
                      mono
                      on={on}
                      disabled={on && formats.length === 1}
                      title={on && formats.length === 1 ? "A Custom reader needs at least one file type" : undefined}
                      onClick={() => toggle("formats", f, on)}
                    >
                      {f}
                    </Chip>
                  );
                })}
              </div>
            </Field>,
            <Field label="Also copy" error={errFor("sidecars")}>
              <div className="flex flex-wrap gap-1">
                {SIDECAR_NAMES.map((s) => {
                  const on = sidecars.includes(s);
                  return (
                    <Chip key={s} mono on={on} onClick={() => toggle("sidecars", s, on)}>
                      {s}
                    </Chip>
                  );
                })}
              </div>
            </Field>
          )}
          <div className="flex items-center justify-between gap-3 py-1.5">
            <div>
              <div className="text-xs font-semibold">Hide covers from the gallery</div>
              <div className="text-[11px] text-muted-foreground">
                Writes a <span className="font-mono">.nomedia</span> file in the folder.
              </div>
            </div>
            <Switch checked={val("nomedia")} onCheckedChange={(v) => patch({ nomedia: v }, "nomedia")} />
          </div>
          {grid(
            <Field label="Folder names" error={errFor("namingPolicy")} hint={NAMING_POLICY_COPY[val("namingPolicy")]?.example}>
              <Select value={val("namingPolicy")} onChange={(e) => patch({ namingPolicy: e.target.value }, "namingPolicy")} className="h-8 text-[13px]">
                {Object.entries(NAMING_POLICY_COPY).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v.label}
                  </option>
                ))}
              </Select>
            </Field>,
            <Field label={`Rename files on ${tn} to match PC`} error={errFor("renameToMatch")}>
              <div className="flex h-8 items-center gap-2">
                <Switch checked={val("renameToMatch")} onCheckedChange={(v) => patch({ renameToMatch: v }, "renameToMatch")} />
                <span className="text-xs text-muted-foreground">{val("renameToMatch") ? "On" : "Off"}</span>
              </div>
            </Field>
          )}
          <Field label="Clean-up names" error={errFor("cleanupNames")} hint={`Removed from each synced folder on ${tn} before copying. Dot-files only.`}>
            <div className="flex flex-wrap items-center gap-1">
              {cleanup.map((n) => (
                <Chip key={n} mono onRemove={() => toggle("cleanupNames", n, true)}>
                  {n}
                </Chip>
              ))}
              <form
                className="inline-flex"
                onSubmit={(e) => {
                  e.preventDefault();
                  const n = cleanupInput.trim();
                  if (!n || cleanup.includes(n)) return;
                  toggle("cleanupNames", n, false).then((r) => r && r.ok !== false && setCleanupInput(""));
                }}
              >
                <Input value={cleanupInput} onChange={(e) => setCleanupInput(e.target.value)} placeholder=".name" aria-label="Add a clean-up name" className="h-6 w-28 px-2 font-mono text-[11.5px]" />
              </form>
            </div>
          </Field>
        </div>
      )}

      <Field label="Series" error={errFor("excludes") || errFor("selection")}>
        <div className="flex flex-col gap-1.5">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[12.5px]" role="radiogroup" aria-label="Series">
            {["all-except", "only"].map((m) => (
              <label key={m} className="inline-flex cursor-pointer items-center gap-1.5">
                <input type="radio" className="accent-primary" checked={selection === m} onChange={() => send("set-selection-mode", { mode: m }, "selection")} />
                {SELECTION_MODE_COPY[m]}
              </label>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-1">
            {selection === "all-except"
              ? excludes.map((e, i) => (
                  <Chip key={`${refLabel(e)}:${i}`} title={e.removeFiles ? `Its files are being removed from ${tn}` : undefined} onRemove={() => send("remove-exclude", { entry: e }, "excludes")}>
                    {refLabel(e)}
                  </Chip>
                ))
              : only.map((e, i) => (
                  <Chip key={`${refLabel(e)}:${i}`} onRemove={() => toggle("only", e, true)}>
                    {refLabel(e)}
                  </Chip>
                ))}
            {selection === "all-except" ? (
              <SeriesPicker label="Exclude a series" library={library} taken={excludes} onPick={(ref) => send("add-exclude", { entry: ref }, "excludes")} />
            ) : (
              <SeriesPicker label="Add a series" library={library} taken={only} onPick={(ref) => toggle("only", ref, false)} />
            )}
          </div>
          {selection === "only" && !only.length && <div className="text-[11px] text-warn-fg">Nothing is synced to {tn} until you add a series.</div>}
        </div>
      </Field>

      {aliases.length > 0 && (
        <Field label="Linked folders" hint={`A folder on ${tn} that holds a series under another name.`} error={errFor("aliases")}>
          <div className="overflow-hidden rounded-md border">
            {aliases.map((a, i) => {
              const dangling = libEntries.length > 0 && !libEntries.some((e) => refMatches(a, e));
              return (
                <div key={`${a.deviceFolder}:${i}`} className={cn("flex items-center gap-2 px-2.5 py-1.5 text-xs", i > 0 && "border-t")}>
                  <span className="min-w-0 flex-1 truncate font-mono" title={a.deviceFolder}>
                    {a.deviceFolder}
                  </span>
                  <span className="text-muted-foreground">is</span>
                  <span className={cn("min-w-0 flex-1 truncate", dangling && "text-warn-fg")} title={dangling ? "This series is no longer in your library" : refLabel(a)}>
                    {dangling && <AlertTriangle className="mr-1 inline h-3 w-3 align-[-2px]" />}
                    {dangling ? `${refLabel(a)} (not in your library)` : refLabel(a)}
                  </span>
                  <button type="button" aria-label="Remove link" onClick={() => send("remove-alias", { entry: a }, "aliases")} className="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground">
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              );
            })}
          </div>
        </Field>
      )}

      <div className="mt-3 flex items-center gap-3 pb-2">
        <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Danger zone</span>
        <div className="h-px flex-1 bg-border" />
      </div>
      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          variant={armed === "forget" ? "destructive" : "outline"}
          className={cn("h-7", armed !== "forget" && "border-destructive/50 text-destructive hover:bg-destructive/10 hover:text-destructive")}
          disabled={ops.pending}
          onClick={() => {
            if (armed !== "forget") return arm("forget");
            disarm();
            send("forget-record");
          }}
        >
          <History className="h-3.5 w-3.5" />
          {armed === "forget" ? `Click again to forget history for ${tn}` : "Forget sync history"}
        </Button>
        <Button
          size="sm"
          variant={armed === "remove" ? "destructive" : "outline"}
          className={cn("h-7", armed !== "remove" && "border-destructive/50 text-destructive hover:bg-destructive/10 hover:text-destructive")}
          disabled={ops.pending}
          onClick={() => {
            if (armed !== "remove") return arm("remove");
            disarm();
            send("remove-target");
          }}
        >
          <Trash2 className="h-3.5 w-3.5" />
          {armed === "remove" ? `Click again to remove ${tn}` : "Remove target"}
        </Button>
      </div>
      <div className="mt-1.5 text-[11px] text-muted-foreground">
        Forgetting history keeps every file. The next sync re-checks what's on {tn} first. Removing the target keeps its files too.
      </div>
    </div>
  );
}
