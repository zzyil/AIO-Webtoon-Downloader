// ============================================================
// SyncTab — the Sync rail tab (UI pass U2; approved design "Transfer Desk",
// sync-temp/design/approved/spec.md §4, mockup overview())
//
// Top to bottom: TargetBar · last-run / refusal / warning cards · tiles ·
// ledger toolbar · ledger (rows, "Not synced to X", "Only on X") · dock.
// Overlays: SeriesSheet, ReviewDialog, PruneDialog, the connected-target
// sheet (TargetEditor), FindSourcesSheet (U4).
//
// U4 entry points: view.focus {seriesKey} (App's Open in Sync from the
// Library) opens that series' detail once the plan holds its row, then is
// cleared; a device-only row's "Find source" calls onFindSource (App opens
// Search with the banner); "Find sources…" opens the batch sheet, which
// queues through onQueueDownload.
//
// WHO OWNS WHAT
//   * main owns every plan, selection and job; this renders the hook's
//     mirror (useDeviceSync: plans[targetId] = {summary, refusal, stale,
//     updating, links, lastRun, gen, selSeq, lastPlan}) and sends intents.
//   * a review decision (Link, Ignore, Acknowledge, an exclude…) re-derives
//     the plan in main without a full re-plan (plan sync-temp/plans/
//     device-sync-cheap-replans.md): rows say "Linking…" / "Verifying…" /
//     "Connect to verify" while it lands (syncView.liveState), and the dock
//     says "Updating the plan…" (syncView.dockStatus).
//   * the selection client (queue, pending marks) lives in the hook
//     (actions.select), because App unmounts this tab on every tab switch.
//   * per-visit view state (picked target, tile filter, query, sort,
//     dismissed last-run cards) lives in App (UI-pass U2-14) and comes in as
//     `view`/`onView`; without them (the DEV gallery) it is local state.
//   * planning on open: `plan {ifNeeded:true}` on mount, on a target switch
//     and when the target connects. The SERVICE decides whether that plans
//     (service.js _planIfNeeded), so a remount can never loop or re-plan a
//     fresh plan.
//
// Motion (spec §6): rows/strips animate only when a NEW plan generation
// arrives while the tab is open; a remount, filter or selection never
// replays it.
//
// Consumer: App.jsx (and src/dev/SyncGallery.jsx). Pure view logic:
// ./syncView.js (tested by tools/_test_device_sync_view.js).
// ============================================================

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FolderSync, Loader2, Unlink } from "lucide-react";
import { Button } from "@/components/ui/primitives";
import { Sheet } from "@/components/ui/overlay";
import { formatEta } from "@/lib/utils";
import * as SQ from "@/hooks/selectionQueue";
import TargetBar from "./TargetBar";
import TargetEditor from "./TargetEditor";
import { DeviceOnlySection, HeldHeader, LedgerRow, LedgerToolbar, SummaryTiles } from "./Ledger";
import { ProgressDock, SelectionDock } from "./SyncDock";
import SeriesSheet from "./SeriesSheet";
import ReviewDialog from "./ReviewDialog";
import PruneDialog from "./PruneDialog";
import FindSourcesSheet from "./FindSourcesSheet";
import { aliasFor } from "./findView";
import { workLine } from "./connectView";
import { LastRunCard, RefusalCard, WarningCallouts } from "./SyncCards";
import { useConfigOps } from "./useConfigOps";
import {
  averageEta, coverIndex, dockStatus, dockTotals, etaMs, jobOn, laneFills, lastRunView, ledgerRows, linkIndex, liveState, pcPathIndex, pickTargets, rowToggle,
  settleInfo, throughput, tileCounts,
} from "./syncView";

const WIDE_PX = 1000;
const ANIM_MS = 1000;
const EMPTY_VIEW = Object.freeze({ targetId: null, tile: null, query: "", sort: "copy", dismissed: {}, focus: null });

/**
 * [wide, ref]: a CALLBACK ref, because the tab swaps its root element (the
 * no-target screen vs the desk) and an effect keyed on a ref object would
 * keep observing the first, detached one.
 */
function useWide() {
  const [wide, setWide] = useState(false);
  const roRef = useRef(null);
  const ref = useCallback((el) => {
    if (roRef.current) roRef.current.disconnect();
    roRef.current = null;
    if (!el || typeof ResizeObserver === "undefined") return;
    // Measure now: the observer's first callback lands a frame later, which
    // painted a wide window in the narrow layout first.
    setWide(el.getBoundingClientRect().width >= WIDE_PX);
    const ro = new ResizeObserver(([e]) => setWide(e.contentRect.width >= WIDE_PX));
    ro.observe(el);
    roRef.current = ro;
  }, []);
  return [wide, ref];
}

/** 1 s clock while `active` (the settling countdown), else frozen. */
function useNow(active) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return undefined;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active]);
  return now;
}

/**
 * @param {{sync, onOpenSettings(category?), libraryEntries?, loadLibrary?, view?,
 *   onView?(patch), contained?:boolean}} props
 *   contained = overlays stay inside the tab (gallery). libraryEntries feed the
 *   covers (useDownloader's library scan; loaded here once if still empty).
 *   initial = {detail?:seriesKey, review?, prune?, edit?, find?, findOpen?:folder[], findCoverage?:folder}: overlays
 *   open at mount (the DEV gallery's seam; App never passes it).
 *   onFindSource({targetId, targetName, folder, query}) = single find (U4-6);
 *   onQueueDownload(url, args) = App's queueDownload; the download defaults
 *   go under `args` when the download starts ("defaultsPlus" recipe,
 *   lib/queueRecipe.js); searchSettings = the saved {seededOnly, collapseSplits} the
 *   find-sources sheet names (and links to via onOpenSettings("search")).
 */
export default function SyncTab({ sync, onOpenSettings, libraryEntries, loadLibrary, view: viewProp, onView, contained, initial, onFindSource, onQueueDownload, searchSettings }) {
  const [wide, rootRef] = useWide();
  const [localView, setLocalView] = useState(EMPTY_VIEW);
  const view = viewProp || localView;
  const setView = useCallback((patch) => (onView ? onView(patch) : setLocalView((v) => ({ ...v, ...patch }))), [onView]);
  const ops = useConfigOps(sync);
  useEffect(() => {
    if (loadLibrary && !(libraryEntries && libraryEntries.length)) loadLibrary();
    // Once per mount: covers are a nicety, not worth a rescan per render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const targets = useMemo(() => pickTargets((sync.config && sync.config.targets) || [], sync.devices, sync.folderPresence), [sync.config, sync.devices, sync.folderPresence]);
  const picked = targets.find((x) => x.target.id === view.targetId) || targets[0] || null;
  // The first target shown becomes the picked one (review R8): pickTargets
  // re-sorts on every connect, so falling back to targets[0] each render
  // switched targets (and closed open overlays) without the user choosing.
  const fallbackId = picked ? picked.target.id : null;
  useEffect(() => {
    if (fallbackId && fallbackId !== view.targetId && !targets.some((x) => x.target.id === view.targetId)) setView({ targetId: fallbackId });
  }, [fallbackId, view.targetId, targets, setView]);
  const target = picked ? picked.target : null;
  const status = picked ? picked.status : { connected: false, tone: "muted", label: "No target" };
  const tid = target ? target.id : null;
  const tn = (target && target.name) || "this target";
  const pv = (tid && sync.plans[tid]) || null;
  const summary = pv ? pv.summary : null;
  const sel = (tid && sync.selection && sync.selection[tid]) || SQ.EMPTY;
  const run = jobOn(sync.job, tid);
  const locked = SQ.selectionLocked(sync.job, tid);

  // Transient UI.
  const init = initial || {};
  const [detailKey, setDetailKey] = useState(init.detail || null);
  const [reviewOpen, setReviewOpen] = useState(!!init.review);
  const [pruneOpen, setPruneOpen] = useState(!!init.prune);
  const [editOpen, setEditOpen] = useState(!!init.edit);
  const [findOpen, setFindOpen] = useState(!!init.find);
  const [actionRefusal, setActionRefusal] = useState(null);
  const [cancelling, setCancelling] = useState(false);
  // A real switch only: the first target to appear (the hook hydrates after
  // mount) keeps whatever `initial` opened.
  const firstTid = useRef(tid);
  useEffect(() => {
    const prev = firstTid.current;
    firstTid.current = tid;
    if (prev == null || prev === tid) return;
    setDetailKey(null);
    setReviewOpen(false);
    setPruneOpen(false);
    setFindOpen(false);
    // The settings sheet too (R9): its editor's draft belongs to the old target.
    setEditOpen(false);
    setActionRefusal(null);
  }, [tid]);

  // Open in Sync (U4-5): wait for a summary, open the row if the plan has
  // it, and clear the focus either way so a later visit doesn't reopen it.
  // A disconnected target with no summary will not get one by waiting, so
  // the focus clears too; otherwise the sheet would pop up whenever that
  // target next connects.
  const focusKey = view.focus && view.focus.seriesKey;
  const focusSummary = (tid && sync.plans[tid] && sync.plans[tid].summary) || null;
  const focusDead = !focusSummary && !status.connected;
  useEffect(() => {
    if (!focusKey) return;
    if (!focusSummary) {
      if (focusDead) setView({ focus: null });
      return;
    }
    if (focusSummary.series.some((r) => r.seriesKey === focusKey)) setDetailKey(focusKey);
    setView({ focus: null });
  }, [focusKey, focusSummary, focusDead, setView]);

  // Plan on open / switch / connect; the service decides (U2-1).
  useEffect(() => {
    if (!tid || !status.connected) return;
    sync.actions.plan({ targetId: tid, ifNeeded: true }).then((r) => {
      if (r && r.ok === false) setActionRefusal(r);
    });
  }, [tid, status.connected, sync.actions]);

  // Motion: only a gen that changes while mounted.
  const gen = pv ? pv.gen : null;
  const genRef = useRef(gen);
  const [animating, setAnimating] = useState(false);
  useEffect(() => {
    if (gen == null || gen === genRef.current) return undefined;
    genRef.current = gen;
    setAnimating(true);
    const t = setTimeout(() => setAnimating(false), ANIM_MS);
    return () => clearTimeout(t);
  }, [gen]);

  // Throughput samples per run and step (progress `at` from service.js
  // _progressOf). Per step because a plan job's bytes restart at each one
  // (PC hashing, then the read-back), which a run-wide list would read as
  // a negative rate.
  const samplesRef = useRef({ key: null, list: [] });
  const job = run ? run.job : null;
  if (job && job.progress) {
    const key = `${job.runId}:${job.progress.step || ""}`;
    const s = samplesRef.current.key === key ? samplesRef.current : (samplesRef.current = { key, list: [] });
    const at = job.progress.at || Date.now();
    const last = s.list[s.list.length - 1];
    if (!last || last.at !== at) s.list = [...s.list.slice(-39), { at, bytes: job.progress.bytesDone || 0 }];
  }
  const rate = job ? throughput(samplesRef.current.list) : null;
  // The loading screen's line while a plan builds (the first read-back of a
  // big tablet runs for many minutes). A read-back advances once per series,
  // too seldom for throughput's 8 s window, so its time left is the step's
  // average rate.
  const work = summary ? null : workLine(sync.job, tid);
  const workEta = work && work.pct != null && job ? averageEta(samplesRef.current.list, job.progress) : null;
  useEffect(() => {
    if (!job) setCancelling(false);
  }, [job]);

  const laneRef = useRef(null);
  const fills = useMemo(() => {
    const r = laneFills(sync.job && sync.job.targetId === tid ? sync.job : null, laneRef.current);
    laneRef.current = r.memo;
    return r.fills;
  }, [sync.job, tid]);

  const settle = settleInfo(summary, Date.now());
  const now = useNow(!!settle);
  const settleNow = settleInfo(summary, now);

  const covers = useMemo(() => coverIndex(libraryEntries), [libraryEntries]);
  const pcPaths = useMemo(() => pcPathIndex(libraryEntries), [libraryEntries]);
  const pendingRows = useMemo(() => SQ.pendingRows(sel), [sel]);
  const counts = useMemo(() => tileCounts(summary), [summary]);
  const links = useMemo(() => linkIndex(pv), [pv]);
  const liveCtx = { links, updating: !!(pv && pv.updating), refreshing: !!(run && run.refreshing), connected: !!status.connected };
  const { active, held } = useMemo(() => ledgerRows(summary, view), [summary, view]);
  const totals = useMemo(() => dockTotals(summary), [summary]);
  const titleOf = useCallback((k) => {
    const r = summary && summary.series.find((x) => x.seriesKey === k);
    return r ? r.title || r.folder : String(k || "").replace(/^pc:/, "");
  }, [summary]);

  const select = useCallback((req) => tid && sync.actions.select(tid, req), [tid, sync.actions]);
  const onToggle = useCallback(
    (row) => {
      const t = rowToggle(row);
      if (!t) return;
      if (t.openDetail) setDetailKey(row.seriesKey);
      else select(t);
    },
    [select]
  );
  const onOpen = useCallback((k) => setDetailKey(k), []);
  const reply = (r) => {
    if (r && r.ok === false && !r.cancelled) setActionRefusal(r);
    return r;
  };
  // ops.run is stable (useConfigOps), so LedgerRow's memo holds across renders.
  const opsRun = ops.run;
  const onInclude = useCallback((k) => opsRun("include-series", { targetId: tid, seriesKey: k }).then(reply), [opsRun, tid]); // eslint-disable-line react-hooks/exhaustive-deps

  const recheck = ({ hashMode, readBack }) => {
    setActionRefusal(null);
    // Read back is always mode read-back (U2-9): with no mode the service
    // falls back to the target's stored verifyMode, which for adopt-size
    // reads nothing back, and on a non-hashing tablet with none refuses
    // needs-mode (review R2).
    const p = readBack ? sync.actions.verify({ targetId: tid, hashMode, mode: "read-back" }) : sync.actions.plan({ targetId: tid, overrides: { hashMode } });
    return p.then(reply);
  };
  const onAction = (kind, arg) => {
    switch (kind) {
      case "review":
        setReviewOpen(true);
        break;
      case "recheck":
        recheck({ hashMode: "cached", readBack: false });
        break;
      case "edit-target":
        setEditOpen(true);
        break;
      case "forget-record":
        ops.run("forget-record", { targetId: tid }).then(reply);
        break;
      case "set-verify-mode":
        ops.run("update-target", { targetId: tid, patch: { verifyMode: arg } }).then(reply);
        break;
      case "open-settings":
        if (onOpenSettings) onOpenSettings(arg);
        break;
      case "open-log": {
        const api = typeof window !== "undefined" ? window.electronAPI : null;
        if (api && sync.logDir && typeof api.openFolder === "function") api.openFolder(sync.logDir);
        break;
      }
      case "dismiss":
        setActionRefusal(null);
        break;
      default:
        break;
    }
  };

  // ── No targets at all ──
  if (!target) {
    return (
      <div ref={rootRef} className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
        <FolderSync className="h-8 w-8 text-muted-foreground" aria-hidden="true" />
        <div className="text-sm font-medium">No sync targets yet</div>
        <div className="max-w-sm text-xs text-muted-foreground">Add a tablet or a folder in Settings → Sync, then come back here to review and sync.</div>
        <Button size="sm" variant="outline" onClick={() => onOpenSettings && onOpenSettings("sync")}>
          Open Settings → Sync
        </Button>
      </div>
    );
  }

  // The previous run's card hides while a new job writes here: the dock is
  // the live result then, and the old card would read as this run's.
  const lastRun0 = pv && pv.lastRun && view.dismissed[tid] !== pv.lastRun.runId && !(run && run.writing) ? lastRunView(pv.lastRun, tn, titleOf) : null;
  // A refused apply leaves the same refusal as the plan view's and the run's
  // (service.js _refused): draw it once, from the plan view (U2-13).
  const lastRun =
    lastRun0 && lastRun0.refusal && pv.refusal && lastRun0.refusal.code === pv.refusal.code && lastRun0.refusal.message === pv.refusal.message ? { ...lastRun0, refusal: null } : lastRun0;
  const selError = sel.error && sel.error.ok === false ? sel.error : null;
  const deviceOnly = (summary && summary.deviceOnly) || [];
  const showLedger = view.tile !== "only";
  const detailRow = detailKey
    ? detailKey.startsWith("dev:")
      ? deviceOnly.find((d) => d.key === detailKey)
      : summary && summary.series.find((r) => r.seriesKey === detailKey)
    : null;

  const dockLine = dockStatus({ run, pv, settle: settleNow, otherRunning: !!(sync.job && sync.job.state === "running" && sync.job.targetId !== tid), summary, tn });
  const anything = totals.files + totals.removals + totals.renames > 0 || !!(summary && summary.massRepush && summary.massRepush.required);
  const canReview = !!summary && status.connected && !(run && run.writing) && anything;

  const body = !summary ? (
    <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center text-xs text-muted-foreground">
      {!status.connected ? (
        <>
          <Unlink className="h-6 w-6" aria-hidden="true" />
          <div className="text-sm font-medium text-foreground">{tn} isn't connected</div>
          <div>{target.kind === "adb" ? "Plug it in and unlock it. The plan builds when it appears." : "The folder isn't reachable. Check the drive, or change the folder in its settings."}</div>
        </>
      ) : pv && pv.refusal ? null : (
        <>
          <Loader2 className="h-5 w-5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
          <div>Comparing your library with {tn}…</div>
          {work && (
            <div className="flex w-full max-w-sm flex-col items-center gap-1.5" role="status" aria-live="polite">
              <div className="text-foreground">{work.text}</div>
              {work.pct != null && (
                <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
                  <i className="block h-full rounded-full bg-primary transition-[width] duration-300 motion-reduce:transition-none" style={{ width: `${work.pct}%` }} />
                </div>
              )}
              {(work.current || workEta != null) && (
                <div className="flex w-full min-w-0 justify-between gap-3 font-mono text-[11px] tabular-nums">
                  <span className="min-w-0 truncate" title={work.current || ""}>{work.current || ""}</span>
                  {workEta != null && workEta > 0 && <span className="shrink-0">about {formatEta(workEta)} left</span>}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  ) : (
    <>
      <SummaryTiles counts={counts} tile={view.tile} onTile={(t) => setView({ tile: t })} tn={tn} />
      <LedgerToolbar
        query={view.query}
        onQuery={(q) => setView({ query: q })}
        sort={view.sort}
        onSort={(s) => setView({ sort: s })}
        wide={wide}
        onExport={() => sync.actions.exportReport({ targetId: tid }).then(reply)}
      />
      <div className="min-h-0 flex-1 overflow-auto border-t" data-testid="sync-ledger">
        {showLedger &&
          active.map((r, i) => (
            <LedgerRow
              key={r.seriesKey}
              row={r}
              index={i}
              wide={wide}
              tn={tn}
              cover={covers.get(r.folder) || null}
              pending={pendingRows.has(r.seriesKey)}
              locked={locked}
              live={liveState(r, liveCtx)}
              lane={fills[r.seriesKey]}
              active={detailKey === r.seriesKey}
              animate={animating}
              onToggle={onToggle}
              onOpen={onOpen}
              onInclude={onInclude}
            />
          ))}
        {showLedger && held.length > 0 && <HeldHeader n={held.length} tn={tn} />}
        {showLedger &&
          held.map((r, i) => (
            <LedgerRow
              key={r.seriesKey}
              row={r}
              index={active.length + i}
              wide={wide}
              tn={tn}
              cover={covers.get(r.folder) || null}
              pending={pendingRows.has(r.seriesKey)}
              locked={locked}
              live={liveState(r, liveCtx)}
              lane={fills[r.seriesKey]}
              active={detailKey === r.seriesKey}
              animate={animating}
              onToggle={onToggle}
              onOpen={onOpen}
              onInclude={onInclude}
            />
          ))}
        {(view.tile == null || view.tile === "only") && !view.query && (
          <DeviceOnlySection
            items={deviceOnly}
            suggestions={summary.suggestionList}
            titleOf={titleOf}
            tn={tn}
            linkLocked={locked}
            pruneLocked={locked || !!(sync.job && sync.job.state === "running")}
            linking={links.folders}
            active={detailKey}
            onOpen={onOpen}
            onLink={(g) => sync.actions.link({ targetId: tid, suggestionIds: [g.id] }).then(reply)}
            onIgnore={(g) => ops.run("ignore-suggestion", { targetId: tid, suggestionId: g.id }).then(reply)}
            onPrune={() => setPruneOpen(true)}
            onFindAll={onQueueDownload ? () => setFindOpen(true) : undefined}
            onFindOne={onFindSource ? (d) => onFindSource({ targetId: tid, targetName: tn, folder: d.name, query: d.query || d.name }) : undefined}
            isLinked={(name) => !!aliasFor(target, name)}
          />
        )}
        {showLedger && !active.length && !held.length && (
          <div className="px-4 py-6 text-center text-xs text-muted-foreground">{view.query ? "No series match the filter." : "Nothing in this view."}</div>
        )}
      </div>
    </>
  );

  return (
    <div ref={rootRef} className="relative flex h-full min-h-0 flex-col">
      <TargetBar
        targets={targets}
        target={target}
        status={status}
        summary={summary}
        wide={wide}
        busy={!!(run && run.writing)}
        ops={ops}
        onPick={(id) => setView({ targetId: id, tile: null })}
        onRecheck={recheck}
        onOpenAll={() => setEditOpen(true)}
      />
      {(lastRun || (pv && pv.refusal) || actionRefusal || selError || (summary && summary.warnings && summary.warnings.length > 0)) && (
        <div className="flex max-h-[40%] flex-col gap-2 overflow-auto px-4 pt-2.5">
          {lastRun && (
            <LastRunCard view={lastRun} tn={tn} onAction={onAction} onDismiss={() => setView({ dismissed: { ...view.dismissed, [tid]: pv.lastRun.runId } })} />
          )}
          {pv && pv.refusal && <RefusalCard refusal={pv.refusal} tn={tn} onAction={onAction} />}
          {actionRefusal && <RefusalCard refusal={actionRefusal} tn={tn} onAction={onAction} onDismiss={() => setActionRefusal(null)} />}
          {selError && <RefusalCard refusal={selError} tn={tn} onAction={onAction} onDismiss={() => sync.actions.clearSelectionError(tid)} />}
          {summary && <WarningCallouts warnings={summary.warnings} tn={tn} />}
        </div>
      )}
      {body}
      {run && (run.writing || run.job.phase === "verify") ? (
        <ProgressDock
          job={run.job}
          tn={tn}
          rate={rate}
          // A verify job advances per series, too seldom for throughput's
          // window: fall back to the step's average.
          eta={etaMs(run.job.progress, rate) ?? (run.job.phase === "verify" ? averageEta(samplesRef.current.list, run.job.progress) : null)}
          cancelling={cancelling}
          onCancel={() => {
            setCancelling(true);
            sync.actions.cancel({ runId: run.job.runId }).then(reply);
          }}
        />
      ) : summary ? (
        <SelectionDock totals={totals} status={dockLine} canReview={canReview} onReview={() => setReviewOpen(true)} />
      ) : null}

      <SeriesSheet
        open={!!detailKey}
        onClose={() => setDetailKey(null)}
        targetId={tid}
        seriesKey={detailKey}
        row={detailRow}
        tn={tn}
        wide={wide}
        contained={contained}
        sync={sync}
        ops={ops}
        cover={detailRow && detailRow.folder ? covers.get(detailRow.folder) : null}
        pcPath={detailRow && detailRow.folder ? pcPaths.get(detailRow.folder) : null}
        root={target.root || null}
        locked={locked}
        live={detailRow && !detailKey.startsWith("dev:") ? liveState(detailRow, liveCtx) : null}
        linking={links.folders}
        pendingOps={sel.ops}
        gen={gen}
        selSeq={pv ? pv.selSeq : null}
        onSelect={select}
        onError={reply}
      />
      <ReviewDialog
        open={reviewOpen}
        onClose={() => setReviewOpen(false)}
        targetId={tid}
        tn={tn}
        wide={wide}
        contained={contained}
        sync={sync}
        view={pv}
        settled={SQ.isSettled(sel)}
        locked={locked}
        pendingOps={sel.ops}
        onSelect={select}
        onAction={onAction}
      />
      <FindSourcesSheet
        open={findOpen}
        onClose={() => setFindOpen(false)}
        target={target}
        tn={tn}
        items={deviceOnly}
        wide={wide}
        contained={contained}
        sync={sync}
        ops={ops}
        searchSettings={searchSettings}
        onOpenSearchSettings={onOpenSettings ? () => onOpenSettings("search") : undefined}
        onQueueDownload={onQueueDownload || (() => Promise.reject(new Error("downloads aren't available here")))}
        openSites={init.findOpen}
        openCoverage={init.findCoverage}
      />
      <PruneDialog open={pruneOpen} onClose={() => setPruneOpen(false)} targetId={tid} tn={tn} items={deviceOnly} contained={contained} sync={sync} onAction={onAction} />
      <Sheet
        open={editOpen}
        onClose={() => setEditOpen(false)}
        width={wide ? 560 : 470}
        contained={contained}
        title={`${tn} settings`}
        description="Only the target you're looking at appears here. Every target is in Settings → Sync."
      >
        <TargetEditor key={target.id} target={target} sync={sync} ops={ops} library={{ entries: libraryEntries || [], loading: false, load: loadLibrary || null }} />
      </Sheet>
    </div>
  );
}
