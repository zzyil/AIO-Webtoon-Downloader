// ============================================================
// Settings → Sync — the category's sections
//
// syncSettingsSections(ctx) returns SettingsTab SECTIONS entries
// ({group:"sync", title, render}); SettingsTab spreads them into its
// registry, so the nav count and pane routing work as for every category.
//
// Two persistence models side by side (spec §4, the disabledSites precedent):
//   * DRAFT (the Save button): the eight global keys of src/lib/syncSettings.js
//     — enable, prompt-on-connect, removals for new targets, checking PC
//     files, error budget, size warning, chapter patterns, diagnostic log.
//     They live in SettingsTab's `local` and are written by Save.
//   * IMMEDIATE (config-ops): the adb choice and every target. Those
//     sections carry the "Changes here save immediately" note.
//
// Gating: only the enable switch shows until the service reports it is on
// (the hook's live `enabled`, not the draft), because adb and targets need a
// running service. Between toggling and saving, a callout says so.
//
// Depends on: useDeviceSync (App.jsx passes it as `sync`), ./TargetEditor,
// ./useConfigOps, ./copy. Approved design: spec §4 "Settings → Sync",
// mockup settingsOff / settingsOn / adbSection / settingsAdvanced.
// ============================================================

import React, { useEffect, useMemo, useRef, useState } from "react";
import { Badge, Button, Input, Select, Switch, SegmentedControl, Textarea } from "@/components/ui/primitives";
import { AlertCircle, CheckCircle2, ChevronDown, ChevronUp, CircleDashed, Clock, Folder, FolderOpen, FolderSync, Info, Loader2, Plus, RefreshCw, Tablet } from "lucide-react";
import { cn } from "@/lib/utils";
import { SYNC_PRESETS } from "@/lib/syncSettings";
import TargetEditor, { Field, PersistNote } from "./TargetEditor";
import RemoteBrowser from "./RemoteBrowser";
import { useConfigOps, refusalField } from "./useConfigOps";
import {
  ADB_SOURCE_COPY, DELETE_POLICY_COPY, DELETE_POLICY_ORDER, DEVICE_STATE_COPY, HASH_MODE_COPY, KIND_COPY, PROFILE_COPY, PROFILE_ORDER,
  adbTrackingCopy, configRefusalCopy, deviceLabel,
} from "./copy";

/** A settings row: title + sub on the left, the control on the right. */
function SRow({ title, sub, children }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2">
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-medium">{title}</div>
        {sub && <div className="mt-0.5 text-xs leading-snug text-muted-foreground">{sub}</div>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

const TONE_TEXT = { ok: "text-success", info: "text-info-fg", warn: "text-warn-fg", muted: "text-muted-foreground", bad: "text-destructive" };

function Callout({ tone = "info", icon: Icon = Info, children }) {
  const box = {
    info: "border-info/30 bg-info/[.08]",
    warn: "border-warning/35 bg-warning/[.10]",
    bad: "border-destructive/30 bg-destructive/[.08]",
    muted: "border-border bg-muted/50",
  }[tone];
  return (
    <div className={cn("my-1.5 flex items-start gap-2.5 rounded-md border px-3 py-2.5 text-[12.5px]", box)}>
      <Icon className={cn("mt-px h-4 w-4 shrink-0", TONE_TEXT[tone] || TONE_TEXT.muted, Icon === Loader2 && "animate-spin motion-reduce:animate-none")} />
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

// ── Device Sync (draft) ──
function DeviceSyncSection({ local, set, settings, sync }) {
  const live = !!sync.enabled;
  const want = !!local.syncEnabled;
  const savedOn = !!(settings && settings.syncEnabled);
  // Saved on but not running: starting takes a moment after Save (the
  // service starts, then pushes `settings`), so wait before calling it a
  // failure.
  const [late, setLate] = useState(false);
  const waiting = !live && want && savedOn && !!sync.loaded;
  useEffect(() => {
    setLate(false);
    if (!waiting) return undefined;
    const h = setTimeout(() => setLate(true), 5000);
    return () => clearTimeout(h);
  }, [waiting]);
  return (
    <>
      <SRow
        title="Turn on Device Sync"
        sub={live ? null : "Copies your library to an Android tablet over USB, or to any folder, chapter by chapter. Nothing on the tablet is removed until you review it."}
      >
        <Switch checked={want} onCheckedChange={(v) => set("syncEnabled", v)} />
      </SRow>
      {!live && want && !savedOn && (
        <Callout tone="info">Save to turn on Device Sync. The Sync tab appears in the left rail once it's on.</Callout>
      )}
      {waiting && !late && (
        <Callout tone="info" icon={Loader2}>
          Starting Device Sync…
        </Callout>
      )}
      {waiting && late && (
        <Callout tone="bad" icon={AlertCircle}>
          Device Sync is turned on but didn't start. Turn it off and on again, or check the diagnostic log in the app's data folder.
        </Callout>
      )}
      {!live && !want && (
        <Callout tone="muted" icon={FolderSync}>
          <b className="font-semibold">What you'll get</b>
          <div className="mt-0.5 text-muted-foreground">
            A Sync tab that shows, per series, which chapters are on your tablet and which will be copied. You pick the tablet or folder after turning it on.
          </div>
        </Callout>
      )}
      {live && !want && <Callout tone="warn">Save to turn off Device Sync. The Sync tab disappears and nothing is copied until you turn it on again.</Callout>}
      {live && (
        <SRow title="Ask when a target connects" sub="Shows what would change, with a Review button, when a tablet or folder appears.">
          <Switch checked={!!local.syncPromptOnConnect} onCheckedChange={(v) => set("syncPromptOnConnect", v)} />
        </SRow>
      )}
    </>
  );
}

// ── adb (immediate) ──
function AdbSection({ sync, ops, host }) {
  const adb = sync.adb || {};
  const t = adbTrackingCopy(adb);
  const Icon = { ok: CheckCircle2, spin: Loader2, clock: Clock, idle: CircleDashed, alert: AlertCircle }[t.icon] || CircleDashed;
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const cands = adb.candidates || [];
  const api = host || (typeof window !== "undefined" ? window.electronAPI : null);
  const configured = sync.config && sync.config.adbPath;

  const choose = async (p) => {
    setBusy(true);
    try {
      const r = await ops.run("set-adb-path", { path: p });
      if (r && r.ok === false) setErr(configRefusalCopy(r));
      else {
        setErr(null);
        await sync.actions.locateAdb({});
      }
    } finally {
      setBusy(false);
    }
  };
  const browse = async () => {
    if (!api || typeof api.pickFile !== "function") return;
    const isWin = typeof navigator !== "undefined" && /Windows/i.test(navigator.userAgent);
    const p = await api.pickFile(isWin ? [{ name: "adb", extensions: ["exe"] }] : []);
    if (p) choose(p);
  };
  // Detect once when the section first shows with no answer yet (R1):
  // candidates stay null until something asks, and the section read
  // "No adb found" on a PC that has one.
  // A failed ask is kept, so the line says why instead of "Looking…" for good.
  const asked = useRef(false);
  const [lookErr, setLookErr] = useState(null);
  const unknown = adb.candidates == null;
  useEffect(() => {
    if (!unknown || asked.current) return;
    asked.current = true;
    setBusy(true);
    sync.actions
      .locateAdb({})
      .then((r) => setLookErr(r && r.ok === false ? r.message || "it failed" : null))
      .finally(() => setBusy(false));
  }, [unknown, sync.actions]);
  const redetect = async () => {
    setBusy(true);
    try {
      await sync.actions.locateAdb({ rescan: true });
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PersistNote />
      <div className={cn("flex items-center gap-1.5 pb-1 pt-0.5 text-[12.5px] font-semibold", TONE_TEXT[t.tone])}>
        <Icon className={cn("h-3.5 w-3.5", t.icon === "spin" && "animate-spin motion-reduce:animate-none")} />
        {t.title}
      </div>
      <div className="mb-2 text-xs text-muted-foreground">{t.body}</div>
      {adb.trackingError && adb.tracking !== "tracking" && <div className="-mt-1 mb-2 font-mono text-[11px] text-muted-foreground">{adb.trackingError}</div>}
      <div className="overflow-hidden rounded-lg border" role="radiogroup" aria-label="adb">
        {cands.map((c) => {
          const on = c.path === adb.binary;
          return (
            <label key={c.path} className={cn("flex items-center gap-2 border-b px-2.5 py-2", c.ok ? "cursor-pointer hover:bg-accent/40" : "opacity-60")}>
              <input type="radio" className="accent-primary" checked={on} disabled={!c.ok || busy} onChange={() => choose(c.path)} />
              <span className="min-w-0 flex-1 truncate font-mono text-xs" title={c.path}>
                {c.path}
              </span>
              <span className="shrink-0 font-mono text-[11px] text-muted-foreground">
                {c.ok ? [c.version, c.build && c.build.split("-")[0]].filter(Boolean).join(" · ") : c.error || "doesn't run"}
              </span>
              <span className="w-[70px] shrink-0 text-right text-[11px] text-muted-foreground">{ADB_SOURCE_COPY[c.source] || c.source}</span>
            </label>
          );
        })}
        {!cands.length && <div className="border-b px-2.5 py-2 text-xs text-muted-foreground">{unknown && lookErr ? `Couldn't look for adb: ${lookErr}. Try Re-detect.` : unknown ? "Looking for adb…" : "No adb found on this PC."}</div>}
        <div className="flex items-center gap-1.5 px-2.5 py-2">
          <Button size="sm" variant="outline" className="h-7" disabled={busy} onClick={browse}>
            <FolderOpen className="h-3.5 w-3.5" />
            Browse…
          </Button>
          <Button size="sm" variant="ghost" className="h-7" disabled={busy} onClick={redetect}>
            <RefreshCw className={cn("h-3.5 w-3.5", busy && "animate-spin motion-reduce:animate-none")} />
            Re-detect
          </Button>
          {configured && (
            <Button size="sm" variant="ghost" className="h-7" disabled={busy} onClick={() => choose(null)} title={`Chosen: ${configured}`}>
              Detect automatically
            </Button>
          )}
          <span className="flex-1" />
          <span className="text-[11px] text-muted-foreground">{cands.filter((c) => c.ok).length} found</span>
        </div>
      </div>
      {err && <div className="mt-1 text-xs text-destructive">{err}</div>}
    </>
  );
}

// ── Sync defaults (draft) ──
function DefaultsSection({ local, set }) {
  return (
    <>
      <SRow
        title="Removals for new targets"
        sub={`${(DELETE_POLICY_COPY[local.syncDeletePolicy] || DELETE_POLICY_COPY.guarded).hint} Each target keeps its own setting after it's added.`}
      >
        <Select value={local.syncDeletePolicy} onChange={(e) => set("syncDeletePolicy", e.target.value)} className="h-8 w-[150px] text-[13px]">
          {DELETE_POLICY_ORDER.filter((k) => SYNC_PRESETS.syncDeletePolicy.includes(k)).map((k) => (
            <option key={k} value={k}>
              {DELETE_POLICY_COPY[k].label}
            </option>
          ))}
        </Select>
      </SRow>
      <SRow title="Checking PC files" sub={`${HASH_MODE_COPY.cached.label} reuses fingerprints from the last scan. Re-check can re-hash once.`}>
        <Select value={local.syncHashMode} onChange={(e) => set("syncHashMode", e.target.value)} className="h-8 w-[150px] text-[13px]">
          {SYNC_PRESETS.syncHashMode.map((k) => (
            <option key={k} value={k}>
              {HASH_MODE_COPY[k].label}
            </option>
          ))}
        </Select>
      </SRow>
    </>
  );
}

// ── Targets (immediate) ──
function isConnected(t, sync) {
  if (t.kind === "folder") return sync.folderPresence && sync.folderPresence[t.id] === true;
  const d = (sync.devices || []).find((x) => x.serial === t.serial);
  return !!d && d.stateKind === "device";
}

function TargetCard({ target: t, sync, ops, library, host, open, onToggle }) {
  const d = t.kind === "adb" ? (sync.devices || []).find((x) => x.serial === t.serial) : null;
  const connected = isConnected(t, sync);
  const state = d && d.stateKind !== "device" ? DEVICE_STATE_COPY[d.stateKind] : null;
  const KindIcon = t.kind === "adb" ? Tablet : Folder;
  return (
    <div className="rounded-lg border bg-card">
      <button type="button" onClick={onToggle} aria-expanded={open} className="flex w-full items-center gap-2.5 px-3 py-2.5 text-left">
        <KindIcon className={cn("h-4 w-4 shrink-0", connected ? "text-success" : "text-muted-foreground")} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 text-[13px] font-semibold">
            <span className="truncate">{t.name || "Unnamed target"}</span>
            {connected ? (
              <Badge variant="token-success">Connected</Badge>
            ) : state ? (
              <Badge variant={state.tone === "bad" ? "token-destructive" : "token-muted"}>{state.label}</Badge>
            ) : (
              <Badge variant="token-muted">Not connected</Badge>
            )}
          </div>
          <div className="truncate font-mono text-[11px] text-muted-foreground" title={t.root}>
            {t.kind === "adb" ? `${d ? deviceLabel(d) : "Tablet"} · ${t.serial}` : "Folder"} · {t.root}
          </div>
        </div>
        <span className="hidden shrink-0 text-[11px] text-muted-foreground sm:inline">{(PROFILE_COPY[t.profile] || PROFILE_COPY.everything).label}</span>
        {open ? <ChevronUp className="h-4 w-4 shrink-0 text-muted-foreground" /> : <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />}
      </button>
      {open && (
        <div className="border-t px-3 pb-3 pt-2">
          <TargetEditor target={t} sync={sync} ops={ops} library={library} host={host} />
        </div>
      )}
    </div>
  );
}

/** The draft card behind "Add target": nothing is saved until Add. */
function DraftTarget({ sync, ops, host, onAdded, onCancel }) {
  const devices = (sync.devices || []).filter((d) => d.targetable);
  const firstReady = devices.find((d) => d.stateKind === "device");
  const [kind, setKind] = useState(firstReady ? "adb" : "folder");
  const [serial, setSerial] = useState(firstReady ? firstReady.serial : "");
  const [root, setRoot] = useState("");
  const [name, setName] = useState("");
  const [profile, setProfile] = useState("everything");
  const [browsing, setBrowsing] = useState(false);
  const [err, setErr] = useState(null);
  const api = host || (typeof window !== "undefined" ? window.electronAPI : null);
  const dev = devices.find((d) => d.serial === serial);
  const ready = !!dev && dev.stateKind === "device";
  const autoName = kind === "adb" ? (dev ? deviceLabel(dev) : "") : root.split(/[\\/]/).filter(Boolean).slice(-2).join(" ");
  const valid = !!root && (kind !== "adb" || !!serial);

  // Device discovery while this card is open (R1): list-devices keeps
  // main's device tracker up for a couple of minutes even with no adb
  // target yet (service.js _discover), and its `device` events fill
  // sync.devices. Started on open, not on choosing "tablet", so a plugged-in
  // tablet is already listed when the user gets there; renewed every minute.
  const actions = sync.actions;
  const [listErr, setListErr] = useState(null);
  useEffect(() => {
    let alive = true;
    const ask = () =>
      actions.listDevices({}).then((r) => {
        if (alive) setListErr(r && r.ok === false ? r.message || "adb isn't running" : null);
      });
    ask();
    const h = setInterval(ask, 60 * 1000);
    return () => {
      alive = false;
      clearInterval(h);
    };
  }, [actions]);
  // The first tablet to become ready is picked for the user.
  useEffect(() => {
    if (kind === "adb" && !serial && firstReady) setSerial(firstReady.serial);
  }, [kind, serial, firstReady]);

  const add = async () => {
    const r = await ops.run("add-target", { target: { name: name.trim() || autoName || "Sync target", kind, serial: kind === "adb" ? serial : null, root, profile } });
    if (r && r.ok === false) setErr({ field: refusalField(r), text: configRefusalCopy(r) });
    else onAdded(r.targetId);
  };
  const errFor = (f) => (err && err.field === f ? err.text : null);

  return (
    <div className="rounded-lg border border-primary/40 bg-card px-3 pb-3 pt-2.5 shadow-[0_0_0_3px_hsl(var(--primary)/0.08)]">
      <div className="mb-1 text-[13px] font-semibold">New target</div>
      <div className="mb-1 text-[11.5px] text-muted-foreground">Nothing is saved until you click Add target.</div>
      <Field label="Kind">
        <SegmentedControl
          aria-label="Kind"
          value={kind}
          onChange={(v) => {
            setKind(v);
            setRoot("");
            setBrowsing(false);
          }}
          options={["adb", "folder"].map((k) => ({ value: k, label: KIND_COPY[k] }))}
        />
      </Field>
      {kind === "adb" && (
        <Field
          label="Tablet"
          error={errFor("serial") || (listErr && !devices.length ? `Can't list tablets: ${listErr}. Check the adb section above.` : null)}
          hint={!devices.length ? "Plug in a tablet with USB debugging on; it appears here." : null}
        >
          <Select value={serial} onChange={(e) => setSerial(e.target.value)} className="h-8 text-[13px]">
            {!serial && <option value="">Pick a tablet…</option>}
            {devices.map((d) => (
              <option key={d.serial} value={d.serial}>
                {deviceLabel(d)} · {d.serial}
                {d.stateKind !== "device" ? ` (${(DEVICE_STATE_COPY[d.stateKind] || {}).label || d.stateKind})` : ""}
              </option>
            ))}
          </Select>
        </Field>
      )}
      <Field label={kind === "adb" ? "Folder on the tablet" : "Folder"} error={errFor("root")}>
        <div className="flex items-center gap-1.5">
          <Input value={root} onChange={(e) => setRoot(e.target.value)} spellCheck={false} placeholder={kind === "adb" ? "/sdcard/Komikku/local" : "D:\\Tablet\\Komikku\\local"} className="h-8 min-w-0 flex-1 font-mono text-xs" />
          <Button
            size="sm"
            variant="outline"
            className="h-8 shrink-0"
            disabled={kind === "adb" && !ready}
            onClick={async () => {
              if (kind === "adb") return setBrowsing((v) => !v);
              if (api && typeof api.pickFolder === "function") {
                const p = await api.pickFolder();
                if (p) setRoot(p);
              }
            }}
          >
            <FolderOpen className="h-3.5 w-3.5" />
            Browse
          </Button>
        </div>
      </Field>
      {browsing && kind === "adb" && ready && (
        <div className="mb-2">
          <RemoteBrowser
            key={serial}
            sync={sync}
            serial={serial}
            startPath={root || "/sdcard"}
            profile={profile}
            onCancel={() => setBrowsing(false)}
            onUse={(p) => {
              setRoot(p);
              setBrowsing(false);
            }}
          />
        </div>
      )}
      <Field label="Reader" error={errFor("profile")} hint={PROFILE_COPY[profile].hint}>
        <SegmentedControl aria-label="Reader" value={profile} onChange={setProfile} options={PROFILE_ORDER.map((k) => ({ value: k, label: PROFILE_COPY[k].label }))} />
      </Field>
      <Field label="Name" error={errFor("name")}>
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={autoName || "Galaxy Tab"} className="h-8 text-[13px]" />
      </Field>
      {err && !err.field && <div className="mt-1 text-xs text-destructive">{err.text}</div>}
      <div className="mt-2 flex gap-2">
        <Button size="sm" disabled={!valid || ops.pending} onClick={add}>
          Add target
        </Button>
        <Button size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

function TargetsSection({ sync, ops, library, host }) {
  const targets = (sync.config && sync.config.targets) || [];
  const [open, setOpen] = useState(() => (targets.length === 1 ? targets[0].id : null));
  const [adding, setAdding] = useState(false);
  return (
    <>
      <PersistNote />
      <div className="flex flex-col gap-1.5">
        {targets.map((t) => (
          <TargetCard key={t.id} target={t} sync={sync} ops={ops} library={library} host={host} open={open === t.id} onToggle={() => setOpen((o) => (o === t.id ? null : t.id))} />
        ))}
        {!targets.length && !adding && <div className="py-1 text-xs text-muted-foreground">No targets yet. Add a tablet or a folder to sync to.</div>}
        {adding ? (
          <DraftTarget
            sync={sync}
            ops={ops}
            host={host}
            onCancel={() => setAdding(false)}
            onAdded={(id) => {
              setAdding(false);
              setOpen(id);
            }}
          />
        ) : (
          <div>
            <Button size="sm" variant="outline" onClick={() => setAdding(true)}>
              <Plus className="h-3.5 w-3.5" />
              Add target
            </Button>
          </div>
        )}
      </div>
    </>
  );
}

// ── Advanced (draft) ──
const SAMPLE_NAMES = ["Episode 42 - Eleceed.cbz", "Ch.118.5 Extra.cbz", "Vol.03 Ch.021.cbz", "Omake (bonus).cbz"];

function labelText(r) {
  // A refused or failed call (r.ok false) is not "Not a chapter".
  if (!r || r.error || r.ok === false) return null;
  if (r.unit === "chapter") return `Ch.${r.label}`;
  if (r.unit === "range") return `Ch.${r.label}`;
  if (r.unit === "volume") return `Vol.${r.label}`;
  if (r.unit === "whole") return "Whole series";
  return "Not a chapter";
}

function PatternPreview({ sync, patterns }) {
  // sync.actions, not sync (R11): `sync` is new every App render, which
  // restarted the 250 ms debounce on every log line.
  const actions = sync.actions;
  const [test, setTest] = useState("");
  const [rows, setRows] = useState([]);
  const [errors, setErrors] = useState([]);
  const seq = useRef(0);
  const names = useMemo(() => (test.trim() ? [test.trim(), ...SAMPLE_NAMES] : SAMPLE_NAMES), [test]);
  useEffect(() => {
    const my = ++seq.current;
    const h = setTimeout(async () => {
      const out = [];
      let errs = [];
      for (const name of names) {
        const custom = await actions.labelPreview({ name, folderName: "", patterns });
        if (custom && Array.isArray(custom.error)) {
          errs = custom.error;
          break;
        }
        const builtin = patterns.trim() ? await actions.labelPreview({ name, folderName: "", patterns: "" }) : custom;
        const a = labelText(custom);
        out.push({ name, label: a, source: a === "Not a chapter" ? "—" : patterns.trim() && a !== labelText(builtin) ? "custom" : "built-in" });
      }
      if (my !== seq.current) return;
      setErrors(errs);
      setRows(errs.length ? [] : out);
    }, 250);
    return () => clearTimeout(h);
  }, [actions, patterns, names]);
  return (
    <div className="overflow-hidden rounded-lg border">
      {errors.map((e) => (
        <div key={e.line} className="border-b px-2.5 py-1.5 text-xs text-destructive">
          Line {e.line}: {e.message}
        </div>
      ))}
      {rows.map((r, i) => (
        <div key={`${r.name}:${i}`} className={cn("flex items-center gap-2 px-2.5 py-1.5 text-xs", i > 0 && "border-t")}>
          <span className="min-w-0 flex-1 truncate font-mono">{r.name}</span>
          <span className={cn("font-mono", r.label === "Not a chapter" ? "text-warn-fg" : "text-primary")}>{r.label}</span>
          <span className="w-[52px] text-right text-[11px] text-muted-foreground">{r.source}</span>
        </div>
      ))}
      <div className="flex items-center gap-2 border-t px-2.5 py-1.5">
        <Input value={test} onChange={(e) => setTest(e.target.value)} placeholder="Test a file name" aria-label="Test a file name" className="h-7 min-w-0 flex-1 font-mono text-xs" />
        <span className="text-[11px] text-muted-foreground">Live test</span>
      </div>
    </div>
  );
}

function AdvancedSection({ local, set, sync, host }) {
  const api = host || (typeof window !== "undefined" ? window.electronAPI : null);
  return (
    <>
      <SRow title="Stop after this many failed files" sub="A sync stops once this many files fail to copy, so a loose cable doesn't keep retrying.">
        <SegmentedControl
          aria-label="Stop after this many failed files"
          value={local.syncErrorBudget}
          onChange={(v) => set("syncErrorBudget", v)}
          options={SYNC_PRESETS.syncErrorBudget.map((n) => ({ value: n, label: String(n) }))}
        />
      </SRow>
      <SRow title="Warn when a file shrinks by" sub="A PC file this much smaller than the copy on the target asks before replacing it. Off never asks.">
        <SegmentedControl
          aria-label="Warn when a file shrinks by"
          value={local.syncSizeWarnPct}
          onChange={(v) => set("syncSizeWarnPct", v)}
          options={SYNC_PRESETS.syncSizeWarnPct.map((n) => ({ value: n, label: n === 0 ? "Off" : `${n}%` }))}
        />
      </SRow>
      <div className="flex flex-col gap-1.5 py-2">
        <div className="text-[13px] font-medium">Chapter name patterns</div>
        <div className="text-xs text-muted-foreground">
          One regular expression per line, tried before the built-in patterns. Name the chapter number <span className="font-mono">(?&lt;ch&gt;…)</span>.
        </div>
        <Textarea
          value={local.syncChapterPatterns}
          onChange={(e) => set("syncChapterPatterns", e.target.value)}
          spellCheck={false}
          placeholder={"Episode\\s*(?<ch>\\d+(?:\\.\\d+)?)"}
          className="min-h-[56px] resize-y font-mono text-xs"
        />
        <PatternPreview sync={sync} patterns={local.syncChapterPatterns || ""} />
      </div>
      <SRow title="Keep a diagnostic log" sub="Writes sync activity to a log file you can attach to a bug report.">
        <Switch checked={!!local.syncDiagnosticLog} onCheckedChange={(v) => set("syncDiagnosticLog", v)} />
      </SRow>
      <div className="pb-2 pt-0.5">
        <Button size="sm" variant="outline" disabled={!sync.logDir || !api || typeof api.openFolder !== "function"} onClick={() => api.openFolder(sync.logDir)}>
          <FolderOpen className="h-3.5 w-3.5" />
          Open log folder
        </Button>
      </div>
    </>
  );
}

/**
 * SECTIONS entries for the Sync category. `ctx`: {local, set, settings, sync,
 * ops, library, host}. While the service is off only the first section
 * exists, so the nav count reads 1 until sync starts.
 */
export function syncSettingsSections(ctx) {
  const first = { group: "sync", title: "Device Sync", render: () => <DeviceSyncSection {...ctx} /> };
  if (!ctx.sync || !ctx.sync.enabled) return [first];
  return [
    first,
    { group: "sync", title: "adb", render: () => <AdbSection {...ctx} /> },
    { group: "sync", title: "Sync defaults", render: () => <DefaultsSection {...ctx} /> },
    { group: "sync", title: "Targets", render: () => <TargetsSection {...ctx} /> },
    { group: "sync", title: "Advanced", render: () => <AdvancedSection {...ctx} /> },
  ];
}

export { useConfigOps };
