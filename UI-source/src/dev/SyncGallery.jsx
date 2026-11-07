// ============================================================
// DEV ONLY — ?gallery=sync, every Device Sync UI state on one page
//
// Renders the REAL components (SettingsTab at its Sync category, the quit
// dialog's sync line, the overlay primitives) against fixture-backed fake
// sync backends (./syncFixtureSource.js), each case in a light and a `.dark`
// frame at a fixed width. Phase U1 of sync-temp/plans/device-sync-ui-pass.md
// added the Settings cases; U2 adds the Sync tab ("tab-*", the fake source's
// TAB MODE) and the card sheet ("cards"); U3 the connect cards ("connect-*")
// and the rail badge ("rail"); U4 the Library chip / Sync line / Search
// banner ("library") and the Find sources sheet ("tab-find*").
//
// URL: ?gallery=sync                      index of cases
//      ?gallery=sync&case=<id>            one case, light above dark
//      &w=wide                            1180 px frames instead of 735 px
//      &theme=light|dark                  one frame only
// 735 px is the content width at the window minimum (800 px minus the rail).
//
// Every case mounts its own useDeviceSync({source}) (the hook's DEV seam), so
// cases never share state and window.electronAPI stays undefined: the other
// hooks' IPC never starts (useDownloader is not mounted here at all).
// Loaded only by main.jsx under import.meta.env.DEV.
// ============================================================

import React, { useState } from "react";
import SettingsTab, { DEFAULT_SETTINGS } from "@/components/SettingsTab";
import { Dialog, Sheet } from "@/components/ui/overlay";
import { Button } from "@/components/ui/primitives";
import { useDeviceSync } from "@/hooks/useDeviceSync";
import SyncTab from "@/components/sync/SyncTab";
import { LastRunCard, RefusalCard } from "@/components/sync/SyncCards";
import { lastRunView } from "@/components/sync/syncView";
import ConnectPrompt, { ConnectCard, RailBadge } from "@/components/sync/ConnectPrompt";
import { connectCards } from "@/components/sync/connectView";
import { SyncChip, SyncLine } from "@/components/sync/LibrarySync";
import FindBanner from "@/components/sync/FindBanner";
import { seriesSyncChip, seriesSyncLines } from "@/components/sync/libraryView";
import { FolderSync } from "lucide-react";
import { FIXTURES, makeSyncSource } from "./syncFixtureSource";

const savedFrom = (snap, over) => {
  const { chapterPatternErrors, ...s } = snap.settings || {};
  return { ...structuredClone(DEFAULT_SETTINGS), ...s, ...(over || {}) };
};

// id → {title, note, source spec, app settings (what Save last wrote)}
const CASES = [
  { id: "off", title: "Sync off", note: "Only the switch and the explainer.", spec: { base: "off" }, saved: savedFrom(FIXTURES.stateOff) },
  {
    id: "starting",
    title: "Saved on, service not up yet",
    note: "'Starting…' for 5 s, then the didn't-start callout.",
    spec: { base: "off" },
    saved: savedFrom(FIXTURES.stateOff, { syncEnabled: true }),
  },
  ...Object.keys(FIXTURES.adbStates).map((k) => ({
    id: `on-${k}`,
    title: `Sync on, adb ${k}`,
    note: k === "tracking" ? "Three targets: adb tablet (Komikku), folder (Everything), offline tablet (Custom)." : null,
    spec: { base: "on", adb: k },
    saved: savedFrom(FIXTURES.stateOn),
  })),
  {
    id: "on-empty",
    title: "Sync on, no targets yet",
    note: "The add card is the first thing to do.",
    spec: { base: "on", mutate: (s) => { s.config.targets = []; } },
    saved: savedFrom(FIXTURES.stateOn),
  },
  {
    id: "on-bad-pattern",
    title: "Sync on, a chapter pattern that won't compile",
    note: "Open Advanced; the preview shows the line error.",
    spec: { base: "on" },
    saved: savedFrom(FIXTURES.stateOn, { syncChapterPatterns: "Episode (\\d+" }),
  },
];

// The fixture library (tools/_gen_sync_fixtures.js): titles only, no cover
// files, so rows show the placeholder cover.
const TAB_LIBRARY = ["Blue Lock", "Dungeon Meshi", "Eleceed", "Frieren", "Kagurabachi", "Solo Leveling", "Tower of God"].map((n) => ({
  folderName: n,
  title: n,
  folderPath: "D:/Sync-fixture/manga/" + n,
}));
const tabPlan = (s) => Object.values(s.plans)[0];
const TAB_CASES = [
  { id: "tab-planned", title: "Tab: planned", note: "Pre-selected changes, mass-delete pending, one held series, device-only section.", spec: { tab: "planned" } },
  { id: "tab-loss", title: "Tab: a loss ticked", note: "Dungeon Meshi ch.3 removal ticked (lives on neither side after).", spec: { tab: "withLoss" } },
  {
    id: "tab-settling",
    title: "Tab: settling",
    note: "settleAt moved to now + 23 s: the dock counts down.",
    spec: { tab: "planned", mutate: (s) => { tabPlan(s).summary.settleAt = Date.now() + 23000; } },
  },
  { id: "tab-pending", title: "Tab: selection pending", note: "Every set-selection is held: click a row box to see the pending state.", spec: { tab: "planned", hold: true } },
  { id: "tab-detail", title: "Tab: series detail (Eleceed)", spec: { tab: "planned" }, initial: { detail: "pc:Eleceed" } },
  { id: "tab-detail-loss", title: "Tab: series detail with a loss", spec: { tab: "withLoss" }, initial: { detail: "pc:Dungeon Meshi" } },
  { id: "tab-detail-device", title: "Tab: device-only folder detail", spec: { tab: "planned" }, initial: { detail: "dev:Kagurabachi_" } },
  { id: "tab-review", title: "Tab: review", spec: { tab: "planned" }, initial: { review: true } },
  { id: "tab-review-loss", title: "Tab: review with a loss to confirm", spec: { tab: "withLoss" }, initial: { review: true } },
  { id: "tab-prune", title: "Tab: remove folders", spec: { tab: "planned" }, initial: { prune: true } },
  { id: "tab-edit", title: "Tab: connected target settings", spec: { tab: "planned" }, initial: { edit: true } },
  { id: "tab-applying", title: "Tab: applying", note: "The recorded mid-apply snapshot.", spec: { tab: "applying" } },
  { id: "tab-after", title: "Tab: after apply", note: "Last-run card (evicted Tower of God).", spec: { tab: "afterApply" } },
  { id: "tab-first", title: "Tab: first sync", spec: { tab: "firstSync" } },
  { id: "tab-mismatch", title: "Tab: record mismatch", spec: { tab: "recordMismatch" } },
  { id: "tab-suspect", title: "Tab: device listing suspect", spec: { tab: "listingSuspect" } },
  { id: "tab-disconnected", title: "Tab: disconnected", spec: { tab: "disconnected" } },
  { id: "tab-needs-mode", title: "Tab: needs a verify mode (adb)", spec: { tab: "needsMode" } },
  { id: "tab-linking", title: "Tab: a Link queued behind a plan", note: "Hajime no Ippo linked while a plan job runs: Linking… on its row and on the folder only on the target (recorded).", spec: { tab: "linking" } },
  { id: "tab-verifying", title: "Tab: a Link verifying its folder", note: "Mob Psycho 100 linked with no job running: the interim plan, needs-verify while its refresh is to come; the dock reads Updating the plan (recorded).", spec: { tab: "verifying" } },
  {
    id: "tab-verify-offline",
    title: "Tab: a Link verifying, target away",
    note: "The verifying snapshot with the folder marked absent (synthetic): Connect to verify.",
    spec: { tab: "verifying", mutate: (s) => { for (const t of s.config.targets) s.folderPresence[t.id] = false; } },
  },
  { id: "tab-none", title: "Tab: no targets", spec: { tab: "planned", mutate: (s) => { s.config.targets = []; } } },
  { id: "tab-find", title: "Tab: find sources", note: "Recorded rows (synthetic search results): an error, a Ready match on six sites, nothing found, and a close call that reads Check match. A source chip opens every site of the series; Search replays them; Queue links then queues (a no-op here).", spec: { tab: "planned" }, initial: { find: true } },
  {
    id: "tab-find-linked",
    title: "Tab: find sources, one folder linked",
    note: "The recorded config after Queue linked Chainsaw Man.",
    spec: { tab: "planned", mutate: (s) => { s.config = JSON.parse(JSON.stringify(FIXTURES.tab.findConfig)); } },
    initial: { find: true },
  },
  {
    id: "tab-find-coverage",
    title: "Tab: find sources, chapter coverage",
    note: "Both site lists open (synthetic maps). Chainsaw Man: weebcentral anchors with a side story, mangadex has gaps and 3 licensed chapters, mangakatana's numbering differs, mangapill has scattered gaps, mangahub was not compared. The princess series: atsumaru listed no chapters.",
    spec: { tab: "planned" },
    initial: { find: true, findOpen: ["Chainsaw Man", "Tis Time for Torture Princess"] },
  },
  {
    id: "tab-find-coverage-details",
    title: "Tab: find sources, coverage details",
    note: "Chainsaw Man's Coverage details sheet over Find sources (synthetic map): the counts, Download from radios, the per-site summary and the grid. Picking a site here moves the list's pick too.",
    spec: { tab: "planned" },
    initial: { find: true, findOpen: ["Chainsaw Man"], findCoverage: "Chainsaw Man" },
  },
  {
    id: "tab-find-coverage-nomap",
    title: "Tab: find sources, rows without coverage",
    note: "Chainsaw Man searched before the map was kept (Search again); the princess folder picked its other match (top match only); Kagurabachi_ given Chainsaw Man's sites searched with fallback sites off.",
    spec: {
      tab: "planned",
      findRows: (rows) => {
        const by = Object.fromEntries(rows.map((r) => [r.folder, r]));
        const csm = by["Chainsaw Man"];
        const noMap = csm.candidates.map(({ chapterMap, ...c }) => c);
        csm.candidates = noMap;
        const tis = by["Tis Time for Torture Princess"];
        tis.pickUrl = tis.candidates[1].best.url;
        Object.assign(by["Kagurabachi_"], { candidates: JSON.parse(JSON.stringify(noMap)), multiSource: false });
        return rows;
      },
    },
    initial: { find: true, findOpen: ["Chainsaw Man", "Kagurabachi_", "Tis Time for Torture Princess"] },
  },
];

function TabCase({ c }) {
  const sync = useDeviceSync({ source: makeSyncSource(c.spec) });
  return <SyncTab sync={sync} contained initial={c.initial} libraryEntries={TAB_LIBRARY} onOpenSettings={() => {}} onQueueDownload={() => Promise.resolve()} onFindSource={() => {}} searchSettings={{ seededOnly: true, collapseSplits: false, multiSource: true }} />;
}

// Library surfaces (U4): the recorded library-status over the recorded
// target, plus a second, synthetic folder target so the multi-target chip and
// line show. Covers are plain gradients (the Library's own art isn't here).
function LibraryCase() {
  const T = FIXTURES.tab;
  const by = T.libraryStatus.bySeries;
  const targets = T.snapshots.planned.config.targets;
  const second = { id: "tsecond", name: "Komikku folder", kind: "folder" };
  const keys = Object.keys(by);
  const extra = { [keys[0]]: [...by[keys[0]], { targetId: "tsecond", state: "pending", pending: 7 }], [keys[1]]: [...by[keys[1]], { targetId: "tsecond", state: "synced", pending: 0 }] };
  const all = [...targets, second];
  const cards = [...keys.map((k) => [k, extra[k] || by[k]]), ["D:\\Sync-fixture\\tab-manga\\Not mirrored", undefined]];
  return (
    <div className="flex h-full flex-col gap-4 overflow-auto p-4">
      <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(120px, 1fr))" }}>
        {cards.map(([k, rows], i) => {
          const name = k.split(/[\\/]/).pop();
          return (
            <div key={k} className="overflow-hidden rounded-lg border border-border/50 bg-card/60">
              <div className="relative aspect-[3/4]" style={{ background: `linear-gradient(160deg, hsl(${i * 67} 35% 55%), hsl(${i * 67} 40% 28%))` }}>
                <SyncChip chip={seriesSyncChip(rows, all)} />
              </div>
              <div className="p-2 text-xs font-semibold">{name}</div>
            </div>
          );
        })}
      </div>
      {cards.slice(0, 5).map(([k, rows]) => (
        <SyncLine key={k} lines={seriesSyncLines(rows, all)} onOpen={() => {}} />
      ))}
      <FindBanner findFor={{ folder: "Chainsaw Man", targetName: "Galaxy Tab" }} onDismiss={() => {}} />
      <FindBanner findFor={{ folder: "Chainsaw Man", targetName: "Galaxy Tab", error: "The sync settings changed. Try again." }} onDismiss={() => {}} />
    </div>
  );
}

// Every refusal code and last-run outcome as a card. Synthetic objects in the
// service's shapes (service.js refuse(), _runSummary) except where a recorded
// one exists (tab.json refusals, the recorded snapshots' lastRun).
const T = FIXTURES.tab;
const REFUSALS = [
  ...Object.values(T.refusals),
  tabPlan(T.snapshots.recordMismatch).refusal,
  tabPlan(T.snapshots.listingSuspect).refusal,
  tabPlan(T.snapshots.needsMode).refusal,
  { ok: false, code: "device-listing-suspect", message: "far fewer files than recorded" },
  { ok: false, code: "blocked", message: "the selection needs acknowledgments first", blocking: [{ kind: "loss", seriesKey: "pc:Dungeon Meshi", opIds: ["x"] }, { kind: "mass-delete", count: 12 }, { kind: "free-space", needed: 9.1e9, free: 4.2e9 }] },
  FIXTURES.refusals.versionConflict,
  FIXTURES.refusals.invalidField,
  { ok: false, code: "disabled", message: "Device Sync is off" },
  { ok: false, code: "internal", internal: true, message: "TypeError: x is undefined" },
];
const run = (status, summary, phase = "apply") => ({ runId: 1, phase, status, at: Date.now() - 60000, summary: { filesDone: 0, filesTotal: 0, bytesDone: 0, ...summary } });
const RUNS = [
  tabPlan(T.snapshots.afterApply).lastRun,
  run("completed", { filesDone: 4, bytesDone: 1.2e8, skippedSeries: [{ seriesKey: "pc:Frieren", reason: "folder-reappeared" }], skippedReasons: { downloading: 2 } }),
  run("cancelled", { filesDone: 12, filesTotal: 40, bytesDone: 3.4e8 }),
  run("disconnected", { filesDone: 7, filesTotal: 40, bytesDone: 2.1e8 }),
  run("failed", { reason: "no-space", filesDone: 3, failed: [{ folder: "Eleceed", name: "Ch.210 - Eleceed.cbz" }] }),
  run("failed", { reason: "error-budget", filesDone: 20, failed: [{ folder: "Solo Leveling", name: "Ch.012.cbz" }] }),
  run("failed", { refusal: { ok: false, code: "blocked", blocking: [{ kind: "plan-changed", gen: 6, reviewedGen: 5 }] } }),
  run("completed", { prune: { removed: 2 } }, "prune"),
  run("completed", {}, "rename"),
  run("completed", { filesDone: 212 }, "verify"),
];
function CardsCase() {
  const titleOf = (k) => String(k || "").replace(/^pc:/, "");
  return (
    <div className="flex h-full flex-col gap-2 overflow-auto p-4">
      {REFUSALS.map((r, i) => (
        <RefusalCard key={`r${i}`} refusal={r} tn="Galaxy Tab" onAction={() => {}} onDismiss={() => {}} />
      ))}
      {RUNS.map((r, i) => (
        <LastRunCard key={`l${i}`} view={lastRunView(r, "Galaxy Tab", titleOf)} tn="Galaxy Tab" onAction={() => {}} onDismiss={() => {}} />
      ))}
    </div>
  );
}

function Case({ c }) {
  const sync = useDeviceSync({ source: makeSyncSource(c.spec) });
  const [saved, setSaved] = useState(c.saved);
  // Save in the gallery only updates the app settings this frame sees; the
  // fake service does not start or stop (that is what the "starting" case
  // shows).
  return (
    <SettingsTab
      settings={saved}
      onSave={async (s) => {
        setSaved(s);
        return { ok: true };
      }}
      initialCategory="sync"
      sync={sync}
      libraryEntries={[
        { folderName: "Eleceed", title: "Eleceed", path: "D:\\Sync-fixture\\manga\\Eleceed" },
        { folderName: "Tonikaku Kawaii", title: "Tonikaku Kawaii", path: "D:\\Sync-fixture\\manga\\Tonikaku Kawaii" },
        { folderName: "Blue Lock", title: "Blue Lock", path: "D:\\Sync-fixture\\manga\\Blue Lock" },
      ]}
      libraryLoading={false}
      loadLibrary={() => {}}
    />
  );
}

function OverlaysCase() {
  const [dlg, setDlg] = useState(true);
  const [sheet, setSheet] = useState(true);
  return (
    <div className="flex h-full items-start gap-2 p-4">
      <Button size="sm" onClick={() => setDlg(true)}>Dialog</Button>
      <Button size="sm" variant="outline" onClick={() => setSheet(true)}>Sheet</Button>
      <Sheet contained accent open={sheet} onClose={() => setSheet(false)} title="Eleceed" description="Galaxy Tab S9 · 212 chapters">
        <div className="p-4 text-sm text-muted-foreground">Sheet body. Escape or the scrim closes it.</div>
      </Sheet>
      <Dialog
        contained
        open={dlg}
        onClose={() => setDlg(false)}
        title="Turn off Device Sync now?"
        description="A copy to Galaxy Tab S9 is running."
        footer={
          <>
            <Button size="sm" variant="ghost" onClick={() => setDlg(false)}>Keep syncing</Button>
            <Button size="sm" variant="destructive" onClick={() => setDlg(false)}>Turn off</Button>
          </>
        }
      >
        <div className="text-sm text-muted-foreground">Dialog body.</div>
      </Dialog>
    </div>
  );
}

// ── Connect cards + rail badge (phase U3) ──
// Every card variant through the REAL connectCards() + ConnectCard, from the
// recorded prompts.json (ready / needs-mode, get-state at the card). What is
// synthetic, and why: the Preparing card's job progress (the recorded plan
// finished before reporting a file count), the read-back progress, the
// second target of the two-target card, the refusal entries, and every
// tablet state but `device` (one fake server can't be all of them at once).
const P = FIXTURES.prompts;
const RS = P.ready.state;
const NS = P.needsMode.state;
const readyP = RS.prompts[0];
const readyTid = readyP.targetIds[0];
const needP = NS.prompts[0];
const needTid = needP.targetIds[0];
const NOW = readyP.createdAt + 60000;
const withCard = (p, card, ids) => ({ ...p, targetIds: ids || p.targetIds, card: { ...p.card, ...card } });
const devRow = (stateKind) => ({ ...(NS.devices[0] || { serial: "R5CW71ABCDE", model: "SM-T290" }), state: stateKind === "no-permissions" ? "unauthorized" : stateKind, stateKind });
const devCtx = (stateKind) => ({ targets: NS.config.targets, devices: [devRow(stateKind)], deviceSince: { [`dev:${devRow(stateKind).serial}:${stateKind}`]: NOW - 10000 } });
const secondTarget = { id: "tsecond", name: "Komikku folder", kind: "folder", root: "D:/Komikku" };
const CONNECT_VARIANTS = [
  ["Ready (recorded)", { targets: RS.config.targets, prompts: RS.prompts, plans: RS.plans }],
  ["Ready with removals (frozen card, before the plan push)", { targets: RS.config.targets, prompts: [withCard(readyP, { targets: [{ ...readyP.card.targets[0], deletions: 4, losses: 1 }] })], plans: {} }],
  ["First sync: needs a read-back (recorded)", { targets: NS.config.targets, prompts: NS.prompts, plans: NS.plans, devices: NS.devices }],
  [
    "Needs a check, not a first sync",
    { targets: NS.config.targets, prompts: [withCard(needP, { attention: [{ ...needP.card.attention[0], firstSync: false }] })], plans: { [needTid]: { ...NS.plans[needTid], refusal: { ...NS.plans[needTid].refusal, firstSync: false } } }, devices: NS.devices },
  ],
  [
    "After Set up: reading back",
    { targets: NS.config.targets, prompts: NS.prompts, plans: {}, devices: NS.devices, job: { state: "running", runId: 9, phase: "plan", auto: true, targetId: needTid, progress: { step: "verify", filesDone: 31, filesTotal: 50 } } },
  ],
  [
    "Preparing (after the 2 s grace)",
    { targets: RS.config.targets, prompts: [], pending: P.ready.pending.pending, plans: {}, now: P.ready.pending.pending[0].since + 3000, job: { ...(P.ready.job || {}), state: "running", targetId: readyTid, progress: { step: "hashing", filesDone: 62, filesTotal: 100 } } },
  ],
  ["Needs attention: a refusal", { targets: RS.config.targets, prompts: [withCard(readyP, { targets: [], attention: [{ targetId: readyTid, code: "device-listing-suspect" }] })], plans: {} }],
  ["Couldn't check: the plan failed", { targets: RS.config.targets, prompts: [withCard(readyP, { targets: [], attention: [{ targetId: readyTid, code: "failed", reason: "internal" }] })], plans: {} }],
  [
    "Two targets, one needing attention",
    { targets: [...RS.config.targets, secondTarget], prompts: [withCard(readyP, { attention: [{ targetId: "tsecond", code: "record-mismatch" }] }, [readyTid, "tsecond"])], plans: RS.plans },
  ],
  ["Tablet: unauthorized", devCtx("unauthorized")],
  ["Tablet: no permissions", devCtx("no-permissions")],
  ["Tablet: authorizing", devCtx("authorizing")],
  ["Tablet: connecting", devCtx("connecting")],
];
function ConnectCardsCase() {
  return (
    <div className="grid h-full content-start gap-3 overflow-auto p-4" style={{ gridTemplateColumns: "repeat(auto-fill, 340px)" }}>
      {CONNECT_VARIANTS.map(([label, ctx]) => {
        const { cards } = connectCards({ devices: [], pending: [], prompts: [], plans: {}, job: null, now: NOW, ...ctx });
        return (
          <div key={label} className="flex flex-col gap-1">
            <span className="text-[11px] text-muted-foreground">{label}</span>
            {cards.length ? (
              cards.map((card) => (
                <div key={card.key} className="overflow-hidden rounded-xl border bg-card shadow-[0_16px_40px_rgb(0_0_0/0.22)]">
                  <ConnectCard card={card} onAction={() => {}} onClose={() => {}} />
                </div>
              ))
            ) : (
              <span className="text-xs text-destructive">no card</span>
            )}
          </div>
        );
      })}
    </div>
  );
}

// The live corner card over a placeholder tab: Review and Not now answer
// through the fake source (the card closes; nothing re-plans).
function ConnectLiveCase({ spec }) {
  const sync = useDeviceSync({ source: makeSyncSource(spec) });
  const [said, setSaid] = useState(null);
  return (
    <div className="relative h-full">
      <div className="p-4 text-sm text-muted-foreground">Any tab. {said ? `Review → open Sync on ${said}.` : "The card sits bottom-right, 16 px in."}</div>
      <ConnectPrompt sync={sync} hiddenTargetId={null} onReview={(t) => setSaid(t)} />
    </div>
  );
}

const RAIL_STATES = [
  [{ count: 9, dot: null }, "9 series to sync"],
  [{ count: 9, dot: "pulse" }, "Copying"],
  [{ count: 0, dot: "red" }, "Needs attention"],
  [{ count: 3, dot: "red" }, "Count + attention"],
  [{ count: 140, dot: null }, "99+"],
  [{ count: 0, dot: null }, "Nothing to do"],
];
function RailCase() {
  return (
    <div className="flex items-start gap-5 p-4">
      {RAIL_STATES.map(([b, label]) => (
        <div key={label} className="flex flex-col items-center gap-1.5">
          <div className="flex w-16 justify-center border-r bg-card/50 py-2">
            <div className="relative flex h-12 w-12 flex-col items-center justify-center rounded-lg text-muted-foreground">
              <FolderSync className="h-[18px] w-[18px]" />
              <span className="mt-0.5 text-[9px] font-medium">Sync</span>
              <RailBadge badge={b} />
            </div>
          </div>
          <span className="text-[11px] text-muted-foreground">{label}</span>
        </div>
      ))}
    </div>
  );
}

const ALL = [
  ...CASES,
  { id: "overlays", title: "Overlay primitives", note: "Dialog over a Sheet, both contained.", render: () => <OverlaysCase /> },
  ...TAB_CASES.map((c) => ({ ...c, render: () => <TabCase c={c} /> })),
  { id: "cards", title: "Tab: every refusal and last-run card", height: 2600, render: () => <CardsCase /> },
  { id: "connect-cards", title: "Connect prompt: every card variant", note: "Recorded ready / needs-mode cards plus the synthetic states (see the gallery source).", height: 1100, render: () => <ConnectCardsCase /> },
  { id: "connect-ready", title: "Connect prompt: live corner card (ready)", note: "Review / Not now answer through the fake source.", render: () => <ConnectLiveCase spec={{ prompt: "ready" }} /> },
  { id: "connect-needs-mode", title: "Connect prompt: live corner card (first sync)", note: "Set up stores read-back (the fake source only bumps the config).", render: () => <ConnectLiveCase spec={{ prompt: "needsMode" }} /> },
  { id: "library", title: "Library: cover chip, Sync line, Search banner", note: "Recorded library-status (pending / synced / absent / removed), a synthetic second target, a series no target mirrors (no chip).", height: 900, render: () => <LibraryCase /> },
  { id: "rail", title: "Rail badge", note: "Count · pulse while a job runs · red on a problem · 99+ · none.", height: 160, render: () => <RailCase /> },
];

function Frame({ theme, width, height = 640, children }) {
  return (
    <div className={theme === "dark" ? "dark" : ""}>
      <div className="relative overflow-hidden border bg-background text-foreground" style={{ width, height }}>
        {children}
      </div>
    </div>
  );
}

export default function SyncGallery() {
  const q = new URLSearchParams(window.location.search);
  const id = q.get("case");
  const width = q.get("w") === "wide" ? 1180 : 735;
  const themes = q.get("theme") ? [q.get("theme")] : ["light", "dark"];
  const c = ALL.find((x) => x.id === id);
  if (!c) {
    return (
      <div className="min-h-screen bg-background p-6 text-foreground">
        <h1 className="mb-3 text-lg font-semibold">Device Sync gallery</h1>
        <ul className="space-y-1 text-sm">
          {ALL.map((x) => (
            <li key={x.id}>
              <a className="text-primary underline" href={`?gallery=sync&case=${x.id}`}>{x.title}</a>{" "}
              <a className="text-xs text-muted-foreground underline" href={`?gallery=sync&case=${x.id}&w=wide`}>wide</a>
              {x.note && <span className="ml-2 text-xs text-muted-foreground">{x.note}</span>}
            </li>
          ))}
        </ul>
      </div>
    );
  }
  return (
    <div className="min-h-screen space-y-3 bg-muted p-3 text-foreground">
      <div className="text-xs text-muted-foreground">
        <b className="text-foreground">{c.title}</b> · {width}px {c.note ? `· ${c.note}` : ""} · <a className="underline" href="?gallery=sync">all cases</a>
      </div>
      {themes.map((t) => (
        <Frame key={t} theme={t} width={width} height={c.height}>
          {c.render ? c.render() : <Case c={c} />}
        </Frame>
      ))}
    </div>
  );
}
