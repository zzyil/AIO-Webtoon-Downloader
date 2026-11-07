// ============================================================
// DEVICE SYNC — PLANNER (pure)
//
// Owns WHAT a sync would do: buildPlan() turns the PC inventory, the device
// listing and the record into per-series ops with stable ids, and the
// selection helpers turn a stored selection into totals, losses and the
// acknowledgments Start needs. No I/O: after an inline edit, service.js
// re-plans from its cached listing and inventory.
//
// Parent plan rules implemented here (add-this-script-s-features-linked-
// pumpkin.md): 1 names and binding, 2 adoption, 4 per-slot planning,
// 5 guarded delete preselection and losses, 6's re-plan reconcile, 10's
// prompt summary (its one-click Sync now was removed by approved design D2),
// 13's library status. Non-UI plan additions:
// `settling` files, `adopted-size` never pre-selecting or covering a delete,
// and the gone-folder re-push (decision 10, "Provenance" section).
//
// TWO-PASS INPUTS. Two facts need device I/O that depends on this module's
// own decisions, so prepare.js calls buildPlan, does the I/O, and calls it
// again:
//   * plan.needsVerify — managed folders without a verified shard (rule 3:
//     verified before their ops are planned). Until then their files plan
//     as unverified, which is safe: nothing unverified pre-selects a delete.
//   * plan.needsProbe — candidate names for gone folders. A name is only
//     `available` once a STA2 returned ENOENT (opts.nameProbe), because the
//     storage's own case and normalization rules decide, not slotKey.
// Gone detection itself runs before planning: goneCandidates() names the
// shards to STA2, and the confirmed set comes back as opts.gone.
//
// AUTO-REPLACE (deletePolicy "auto-replace", plan sync-temp/plans/device-
// sync-auto-replace.md). Everything guarded pre-selects, plus a foreign
// file (Verify hashed it, it matches no PC file, unchanged since) that the
// PC's own copy of the same chapter replaces: its `replace` (same slot) or
// its "replacement" delete (other name, a pre-selected push brings the
// label). Why: after a Link every device copy verifies foreign, and leaving
// the old copies unticked makes a sync duplicate the library. Held back by
// a delete-blocking tag or `mixed`, and when the device name isn't one AIO
// writes (labelFile appLabel). `ch0-asymmetry` does NOT hold it back: on the
// user's tablet no Ch.000-asymmetric series was shifted by a number
// (content check 2026-10-27, plan "Measured"). Still never unverified, so
// the two-pass rule above holds. The executor's gate (executor.js _deletes)
// still runs every replacement delete only after the push landed.
//
// DELETE GATES AND THEIR CREDIT (plan A1b). A gated delete runs only once
// the post-series listing shows its label(s) in a file that IS the PC's copy
// (executor.js _deletes). Gated: "replacement" deletes, and a range extra
// ("Ch 1-10") whose chapters the PC covers, since its "not a loss" rests on
// those copies landing. Each gated delete carries `gateCredit`
// (_gateCredits): how the plan can prove the gate passes, so space.js
// credits the freed bytes only for removals that will happen.
//
// INPUT SHAPES (P3's pc-inventory.js and transports.js produce them):
//   pc     {ok, series:[{folder, folderPath, title, identityKey, url,
//           anilistId, synonyms[], imageOnly, malformedJson[],
//           files:[{name, size, mtimeMs, sha256, path}]}]}
//   device {trusted, dirs:[{name}], rootFiles:[{name,size,devMtime}],
//           folders: Map name→{ok, files:[{name,size,devMtime}], error?},
//           meta: Map name→{details?, aio?, malformed?}}
//   view   provenance.loadRecordView() after plan preparation.
//
// OP IDS: `<kind>:<folder>/<name>`, rename `rename:<folder>/<from>-><to>`,
// with the device folder name. The selection is keyed by (id, sha), so a
// newer PC version of a file is proposed again (rule 7).
//
// Read by: prepare.js, service.js (P4). Depends on: naming, chapter-labels,
// profiles, provenance, analysis, contract (refusals), library.js (identity,
// URLs).
// ============================================================

const { normalizeSeriesUrl, seriesIdentityKey } = require("../library");
const { refuse } = require("./contract");
const naming = require("./naming");
const { labelFile, compareLabels, extOf } = require("./chapter-labels");
const { isChapterFormat, isSidecar, isMirrored, SIDECAR_NAMES } = require("./profiles");
const { classifyDeviceFile, adoptMissingDevMtime, isForeignCurrent, ORIGINS_OURS } = require("./provenance");
const { deltaTags, deleteBlockingTags, contentAnomalies, makeAnomaly, ROOT_SERIES_KEY } = require("./analysis");
const { gateLabels, vouchedLabel, isWrite } = require("./space");

const { slotKey, compactKey, stripHidSuffix, segmentError } = naming;

// Parent plan / non-UI plan constants, each with its reason.
// Files younger than this plan as `settling` (deviation 9): apply's re-stat
// refuses anything this young, so offering it would plan a no-op.
const SETTLE_MS = 30 * 1000;
// Adoption: share of D's chapter files (content) or of the gone shard's
// entries (record) found by exact (name, size). 135/135, 233/233 and
// 153/153 on the old journal (parent "What we keep").
const ADOPT_RATIO = 0.9;
// Token-set similarity for the weak `similar` tier.
const SIMILAR_RATIO = 0.6;
// Mass-delete and mass-repush acknowledgments (parent rule 5, decision 10).
const MASS_RATIO = 0.5;
const MASS_MIN = 10;
const MASS_TOTAL_DELETES = 200;
// A range's integers are expanded for loss checks only up to this span;
// a wider one is never "covered" (it is a loss when deleted).
const RANGE_EXPAND_MAX = 5000;
// The delete reason of "Remove from <target>" (approved design D7; grep
// _planEvict). executor.js lifts the PC-slot guard for it alone.
const EVICT_REASON = "removed-from-target";

const STRONG_TIERS = Object.freeze(new Set(["identity", "content", "record"]));
const TIER_ORDER = Object.freeze(["identity", "content", "record", "name", "similar"]);

const SERIES_STATES = Object.freeze(["bound", "needs-verify", "new", "gone", "held"]);
const HELD_REASONS = Object.freeze([
  "excluded",
  "fork",
  "empty-pc-folder",
  "image-only",
  "folder-unreadable",
  "name-taken",
  "name-invalid",
]);

function pcSeriesKey(folder) {
  return `pc:${folder}`;
}

function deviceOnlyKey(name) {
  return `dev:${name}`;
}

function opId(kind, folder, name) {
  return `${kind}:${folder}/${name}`;
}

function renameOpId(folder, from, to) {
  return `rename:${folder}/${from}->${to}`;
}

// ------------------------------------------------------------------
// Binding (non-UI plan, "Persistent files": identityKey, then urlKey,
// then pcFolder, so a hid change keeps the binding through the URL and a
// URL change keeps it through the folder name).
// ------------------------------------------------------------------

function _richness(s) {
  return (s.files || []).filter((f) => isChapterFormat(f.name)).length;
}

function _byRichness(a, b) {
  return _richness(b) - _richness(a) || (a.folder < b.folder ? -1 : a.folder > b.folder ? 1 : 0);
}

/**
 * Bind live shards to PC series. Each shard binds at most one series and
 * each series at most one shard. Within one precedence pass, several
 * series matching one shard (a fork) resolve to the shard's own pcFolder
 * when present (the sticky choice), else the richest.
 *
 * @param {Array<object>} shards   view.shards
 * @param {Array<object>} series   normalized PC series ({seriesKey, folder, identityKey, urlKey, files})
 * @returns {{byShard: Map<string, object>, bySeries: Map<string, object>}}
 */
function bindShards(shards, series) {
  const byShard = new Map();
  const bySeries = new Map();
  const passes = [
    (sh, s) => !!sh.identityKey && sh.identityKey === s.identityKey,
    (sh, s) => !!sh.urlKey && sh.urlKey === s.urlKey,
    (sh, s) => !!sh.pcFolder && sh.pcFolder === s.folder,
  ];
  for (const match of passes) {
    for (const sh of shards) {
      if (byShard.has(sh.shardId)) continue;
      const cands = series.filter((s) => !bySeries.has(s.seriesKey) && match(sh, s));
      if (!cands.length) continue;
      const sticky = cands.find((s) => s.folder === sh.pcFolder);
      const win = sticky || cands.slice().sort(_byRichness)[0];
      byShard.set(sh.shardId, win);
      bySeries.set(win.seriesKey, sh);
    }
  }
  return { byShard, bySeries };
}

// ------------------------------------------------------------------
// Input normalization
// ------------------------------------------------------------------

function _normalizeSeries(raw, cfg, patterns) {
  const folder = String(raw.folder);
  const files = (raw.files || []).filter((f) => f && f.name && !String(f.name).startsWith("."));
  const s = {
    seriesKey: pcSeriesKey(folder),
    folder,
    folderPath: raw.folderPath || null,
    title: raw.title || null,
    identityKey: raw.identityKey || null,
    url: raw.url || null,
    urlKey: normalizeSeriesUrl(raw.url) || null,
    anilistId: raw.anilistId == null || raw.anilistId === "" ? null : String(raw.anilistId),
    synonyms: Array.isArray(raw.synonyms) ? raw.synonyms : [],
    imageOnly: !!raw.imageOnly,
    malformedJson: Array.isArray(raw.malformedJson) ? raw.malformedJson : [],
    files,
  };
  s.mirrored = files.filter((f) => isMirrored(f.name, cfg));
  s.chapterMirrored = s.mirrored.filter((f) => isChapterFormat(f.name));
  s.labeled = files
    .filter((f) => isChapterFormat(f.name))
    .map((f) => ({ ...labelFile(f.name, folder, patterns), name: f.name, size: f.size, ext: extOf(f.name), mirrored: isMirrored(f.name, cfg) }));
  return s;
}

function _isIncluded(s, cfg) {
  const ref = { identityKey: s.identityKey, url: s.url, folder: s.folder };
  if (cfg.selection === "only") return !!naming.findSeriesEntry(ref, cfg.only);
  return !naming.findSeriesEntry(ref, cfg.excludes);
}

function _deviceFiles(device, name) {
  const f = device.folders instanceof Map ? device.folders.get(name) : null;
  if (!f) return { ok: false, files: [], error: "not-listed" };
  if (f.ok === false) return { ok: false, files: [], error: f.error || "unreadable" };
  return { ok: true, files: (f.files || []).filter((x) => x && x.name && !String(x.name).startsWith(".")) };
}

function _tokens(s) {
  // Apostrophes fold away ("Hell's" → "hells"), as in compactKey.
  return new Set(
    stripHidSuffix(s)
      .replace(/['’]/g, "")
      .normalize("NFKD")
      .replace(/\p{M}+/gu, "")
      .toLowerCase()
      .split(/[^\p{L}\p{N}]+/u)
      .filter(Boolean),
  );
}

function _jaccard(a, b) {
  if (!a.size || !b.size) return 0;
  let both = 0;
  for (const t of a) if (b.has(t)) both += 1;
  return both / (a.size + b.size - both);
}

// ------------------------------------------------------------------
// Gone detection (decision 10): which shards prepare.js must STA2.
// ------------------------------------------------------------------

/**
 * Shards whose folder may be gone: bound to an included PC series, and the
 * shard's name plus any leftover renameIntent names all absent from the
 * root listing. prepare.js STA2s every returned name (all must answer
 * ENOENT), re-lists the root through the rc-checked shell `find` on adb,
 * and passes the confirmed shardIds to buildPlan as opts.gone. A shard whose
 * series is absent from the PC is never returned (it plans nothing).
 *
 * @returns {Array<{shardId:string, names:string[]}>}
 */
function goneCandidates({ view, pc, target, device }) {
  const cfg = target;
  const ci = !!view.caseInsensitive;
  const present = new Set((device.dirs || []).map((d) => slotKey(d.name, ci)));
  const series = (pc.series || []).map((r) => _normalizeSeries(r, cfg, []));
  const { byShard } = bindShards(view.shards, series);
  const out = [];
  for (const sh of view.shards) {
    const s = byShard.get(sh.shardId);
    if (!s || !_isIncluded(s, cfg)) continue;
    const names = [sh.name];
    // A folder rename's intent names folders; a file rename's (scope 'file',
    // executor.js) names files inside this folder and says nothing here.
    const ri = sh.renameIntent;
    if (ri && ri.scope !== "file") for (const n of [ri.from, ri.via, ri.to]) if (n && !names.includes(n)) names.push(n);
    if (names.some((n) => present.has(slotKey(n, ci)))) continue;
    out.push({ shardId: sh.shardId, names });
  }
  return out;
}

// ------------------------------------------------------------------
// buildPlan
// ------------------------------------------------------------------

/**
 * The refusals that need no device listing. prepare.js asks first, so a
 * refused plan makes no device or record write; buildPlan asks again.
 * @returns {object|null} a refusal, or null
 */
function inputRefusal(pc, view) {
  if (!pc || pc.ok === false) {
    return refuse("library-missing", "the library folder can't be read", { detail: pc && pc.error });
  }
  if (!(pc.series || []).length && view.shards.length) {
    // D: is removable: an empty walk while the record manages folders would
    // otherwise orphan every one of them.
    return refuse("library-missing", "the library looks empty while this target has synced series");
  }
  if (view.mismatch && view.mismatch.length) {
    return refuse("record-mismatch", "the sync record describes a different place", { fields: view.mismatch.slice() });
  }
  return null;
}

/**
 * @param {object} input
 * @param {object} input.target    resolveTargetConfig() result.
 * @param {object} input.settings  resolveSyncSettings() result.
 * @param {object} input.pc
 * @param {object} input.device
 * @param {object} input.view      loadRecordView() result.
 * @param {object} [input.opts]    {now, gone:Set<shardId>,
 *   nameProbe:Map<name, boolean absent>}. The removal policy is the
 *   target's own (cfg.deletePolicy, profiles.js DELETE_POLICIES), else the
 *   global syncDeletePolicy for a target saved before that field existed.
 * @returns {object} the plan, or a refusal ({ok:false, code, ...}).
 */
function buildPlan(input) {
  const cfg = input.target;
  const settings = input.settings || {};
  const pc = input.pc;
  const device = input.device || {};
  const view = input.view;
  const opts = input.opts || {};
  const now = Number.isFinite(opts.now) ? opts.now : Date.now();
  const policy = cfg.deletePolicy || settings.syncDeletePolicy || "guarded";
  const patterns = settings.chapterPatterns || [];
  const gone = opts.gone instanceof Set ? opts.gone : new Set(opts.gone || []);
  const nameProbe = opts.nameProbe instanceof Map ? opts.nameProbe : new Map();

  const early = inputRefusal(pc, view);
  if (early) return early;
  if (device.trusted === false) {
    return refuse("device-listing-suspect", "the device listing looks incomplete", { cause: "few-files" });
  }

  const ci = !!view.caseInsensitive;
  const ack = new Set(cfg.acknowledged || []);
  const ignored = new Set(cfg.ignoredSuggestions || []);
  const plan = {
    ok: true,
    targetId: cfg.id,
    targetName: cfg.name,
    asOf: now,
    firstSync: !!view.firstSync,
    caseInsensitive: ci,
    deletePolicy: policy,
    series: [],
    bySeriesKey: new Map(),
    deviceOnly: [],
    deviceOnlyRows: new Map(),
    ops: new Map(),
    suggestions: [],
    anomalies: [],
    needsVerify: [],
    needsProbe: [],
    gone: [],
    goneTotal: { bound: 0, gone: 0 },
    sanity: [],
    entryFixes: [],
    shardNameFixes: [],
    settleAt: null,
    warnings: [],
  };

  // Device folders by slot key, exact names kept.
  const dirSlots = new Map();
  for (const d of device.dirs || []) {
    if (d && d.name && !String(d.name).startsWith(".")) dirSlots.set(slotKey(d.name, ci), d.name);
  }

  const allSeries = (pc.series || [])
    .map((r) => _normalizeSeries(r, cfg, patterns))
    .sort((a, b) => (a.folder < b.folder ? -1 : a.folder > b.folder ? 1 : 0));
  const { byShard, bySeries } = bindShards(view.shards, allSeries);

  // Every live shard's name stays reserved until Forget, present or not: a
  // new series taking a gone or orphaned folder's name would leave two
  // shards naming one folder once its own shard is written.
  const claims = new Map(); // slot → {owner, name}
  const claim = (name, owner) => claims.set(slotKey(name, ci), { owner, name });
  const claimedByOther = (name, owner) => {
    const c = claims.get(slotKey(name, ci));
    return !!c && c.owner !== owner;
  };
  for (const sh of view.shards) claim(sh.name, `shard:${sh.shardId}`);

  const sps = [];
  for (const s of allSeries) {
    const shard = bySeries.get(s.seriesKey) || null;
    const sp = {
      seriesKey: s.seriesKey,
      folder: s.folder,
      folderPath: s.folderPath,
      title: s.title,
      identityKey: s.identityKey,
      urlKey: s.urlKey,
      state: null,
      heldReason: null,
      deviceFolder: null,
      heldFolder: null,
      shardId: shard ? shard.shardId : null,
      nameSource: shard ? "bound" : null,
      verifyMode: shard ? shard.verifyMode : null,
      binding: { identityKey: s.identityKey, urlKey: s.urlKey, pcFolder: s.folder },
      tags: [],
      blockingTags: [],
      // auto-replace only: the tags that kept a swap it would have ticked
      // unticked (_planExisting; the series detail's note, SeriesSheet.jsx).
      autoHeld: [],
      anomalyIds: [],
      opIds: [],
      rows: [],
      cover: new Map(),
      devFiles: [],
      brings: [],
      blockedBy: [],
      rebind: null,
      counts: { inSync: 0, kept: 0, settling: 0, unhashed: 0, unmanaged: 0 },
      deviceChapterFiles: 0,
      _s: s,
      _shard: shard,
    };
    sps.push(sp);
    plan.series.push(sp);
    plan.bySeriesKey.set(sp.seriesKey, sp);
  }

  const hold = (sp, reason, anomaly) => {
    sp.state = "held";
    sp.heldReason = reason;
    if (anomaly) _addAnomaly(plan, sp, anomaly);
  };

  // 1. Holds that need no name: excluded, image-only, nothing to mirror.
  for (const sp of sps) {
    const s = sp._s;
    if (!_isIncluded(s, cfg)) hold(sp, "excluded");
    else if (s.imageOnly) hold(sp, "image-only", makeAnomaly("image-only", sp.seriesKey, {}, ack));
    else if (!s.chapterMirrored.length) {
      // Sidecars alone don't make a folder (rule 1: no folder for a series
      // with zero mirrorable files; a cover-only folder shows as an empty
      // series in Komikku).
      hold(sp, "empty-pc-folder", makeAnomaly("empty-pc-folder", sp.seriesKey, { files: s.files.length }, ack));
    }
  }

  // Mass-repush denominator (decision 10): bound folders of included series.
  plan.goneTotal.bound = sps.filter((sp) => sp._shard && sp.heldReason !== "excluded").length;

  // 2. Forks: same identity, several PC folders. A bound member keeps its
  // folder; unbound members lose to it, or to the richest (rule 1).
  const byIdentity = new Map();
  for (const sp of sps) {
    if (sp.state === "held" || !sp.identityKey) continue;
    if (!byIdentity.has(sp.identityKey)) byIdentity.set(sp.identityKey, []);
    byIdentity.get(sp.identityKey).push(sp);
  }
  for (const group of byIdentity.values()) {
    if (group.length < 2) continue;
    const bound = group.filter((sp) => sp._shard);
    const unbound = group.filter((sp) => !sp._shard).sort((a, b) => _byRichness(a._s, b._s));
    const winner = bound.length ? bound[0] : unbound[0];
    const losers = unbound.filter((sp) => sp !== winner);
    if (!losers.length) continue;
    _addAnomaly(plan, winner, makeAnomaly("fork-collision", winner.seriesKey, { folders: group.map((g) => g.folder) }, ack));
    for (const l of losers) {
      hold(l, "fork", makeAnomaly("merge-in-library", l.seriesKey, { winner: winner.folder }, ack));
    }
  }

  // 3. Bound series: present → its folder (byte-exact; the listing's exact
  // bytes when only case or normalization drifted); absent and confirmed
  // gone → decision 10; absent and not confirmed → held, never guessed.
  for (const sp of sps) {
    const sh = sp._shard;
    if (!sh) continue;
    if (sp.state === "held") continue;
    const exact = dirSlots.get(slotKey(sh.name, ci));
    if (exact != null) {
      sp.deviceFolder = exact;
      sp.state = sh.verifiedAt ? "bound" : "needs-verify";
      // Same slot, other bytes (a case edit on the tablet): the device's
      // name is the folder's name now; prepare.js persists it.
      if (exact !== sh.name) plan.shardNameFixes.push({ shardId: sh.shardId, name: exact });
      claim(exact, sp.seriesKey);
    } else if (gone.has(sh.shardId)) {
      sp.state = "gone";
      plan.goneTotal.gone += 1;
    } else {
      hold(sp, "folder-unreadable", makeAnomaly("folder-unreadable", sp.seriesKey, { folder: sh.name, reason: "not-listed" }, ack));
    }
  }

  // 4. Gone series choose a name before any new series resolves (decision
  // 10): previous (byte-exact), then derived, then verbatim.
  const available = (name, owner) => {
    if (!name || segmentError(name)) return false;
    if (dirSlots.has(slotKey(name, ci))) return false;
    if (claimedByOther(name, owner)) return false;
    return nameProbe.get(name) === true;
  };
  const needProbe = (name) => {
    if (name && !segmentError(name) && !dirSlots.has(slotKey(name, ci)) && !nameProbe.has(name)) {
      if (!plan.needsProbe.includes(name)) plan.needsProbe.push(name);
    }
  };
  const rebinds = (view.selection && view.selection.rebinds) || {};
  for (const sp of sps) {
    if (sp.state !== "gone") continue;
    const sh = sp._shard;
    const owner = `shard:${sh.shardId}`;
    const candidates = [{ source: "previous", name: sh.name }];
    const derived = naming.resolveTargetName({ identityKey: sp.identityKey, url: sp._s.url, folder: sp.folder, title: sp.title }, cfg);
    if (!derived.error && slotKey(derived.name, ci) !== slotKey(sh.name, ci)) {
      candidates.push({ source: "derived", name: derived.name });
    }
    const verbatim = naming.verbatimName(sp._s);
    const choices = candidates.map((c) => ({ ...c, available: available(c.name, owner) }));
    for (const c of candidates) needProbe(c.name);
    const stored = rebinds[sh.shardId] || null;
    const declined = !!(stored && stored.declined);
    // A stored name that no longer equals its source's current candidate
    // (an alias or naming-policy edit since) counts as unset.
    const storedChoice = stored && choices.find((c) => c.source === stored.source && c.name === stored.name);
    const wanted = storedChoice || choices[0];
    let chosen = wanted.available ? wanted : choices.find((c) => c.available) || null;
    if (!chosen && !verbatim.error && !choices.some((c) => slotKey(c.name, ci) === slotKey(verbatim.name, ci))) {
      needProbe(verbatim.name);
      if (available(verbatim.name, owner)) chosen = { source: "verbatim", name: verbatim.name, available: true };
    }
    sp.rebind = {
      shardId: sh.shardId,
      choices,
      chosen: chosen ? { source: chosen.source, name: chosen.name } : null,
      fellBack: !!chosen && chosen !== wanted,
      declined,
    };
    if (!chosen) {
      hold(sp, "name-taken", makeAnomaly("name-taken", sp.seriesKey, { previous: sh.name }, ack));
      continue;
    }
    sp.deviceFolder = chosen.name;
    sp.nameSource = chosen.source;
    claim(chosen.name, owner);
    plan.gone.push({
      shardId: sh.shardId,
      seriesKey: sp.seriesKey,
      previous: sh.name,
      chosen: chosen.name,
      source: chosen.source,
      fellBack: sp.rebind.fellBack,
      declined,
    });
  }

  // Held series without a shard (excluded, image-only, empty) keep an
  // existing device folder of their resolved name. Unclaimed, it would go to
  // another series resolving to the same name, which would verify it and
  // push its own chapters into it. Fork losers share their winner's name by
  // definition, so they reserve nothing.
  for (const sp of sps) {
    if (sp.state !== "held" || sp._shard || sp.heldReason === "fork") continue;
    const r = naming.resolveTargetName({ identityKey: sp.identityKey, url: sp._s.url, folder: sp.folder, title: sp.title }, cfg);
    const exact = r.error ? null : dirSlots.get(slotKey(r.name, ci));
    if (exact == null || claimedByOther(exact, sp.seriesKey)) continue;
    sp.heldFolder = exact;
    claim(exact, sp.seriesKey);
  }

  // 5. Unbound series: alias, else naming policy; two different series on
  // one name each fall back to their PC folder name verbatim (rule 1).
  const unbound = sps.filter((sp) => !sp._shard && sp.state == null);
  const resolved = new Map();
  for (const sp of unbound) {
    const r = naming.resolveTargetName({ identityKey: sp.identityKey, url: sp._s.url, folder: sp.folder, title: sp.title }, cfg);
    resolved.set(sp.seriesKey, r);
  }
  const bySlot = new Map();
  for (const sp of unbound) {
    const r = resolved.get(sp.seriesKey);
    if (r.error) continue;
    const k = slotKey(r.name, ci);
    if (!bySlot.has(k)) bySlot.set(k, []);
    bySlot.get(k).push(sp);
  }
  for (const sp of unbound) {
    const owner = sp.seriesKey;
    let r = resolved.get(sp.seriesKey);
    let source = r.source;
    const shared = !r.error && bySlot.get(slotKey(r.name, ci)).length > 1;
    const taken = !r.error && claimedByOther(r.name, owner);
    if (taken && r.source === "alias") {
      plan.warnings.push({ kind: "alias-conflict", seriesKey: sp.seriesKey, deviceFolder: r.name });
    }
    if (r.error || shared || taken) {
      const v = naming.verbatimName(sp._s);
      if (v.error) {
        const kind = v.error === "too-long" ? "name-too-long" : "name-invalid";
        hold(sp, "name-invalid", makeAnomaly(kind, sp.seriesKey, { name: v.name || sp.folder }, ack));
        continue;
      }
      if (claimedByOther(v.name, owner)) {
        hold(sp, "name-taken", makeAnomaly("name-taken", sp.seriesKey, { name: v.name }, ack));
        continue;
      }
      r = v;
      source = "verbatim";
    }
    const exact = dirSlots.get(slotKey(r.name, ci));
    sp.nameSource = source;
    if (exact != null) {
      // An unbound device folder of that name: the series' folder, verified
      // before its ops are planned (rule 3, first sync).
      sp.deviceFolder = exact;
      sp.state = "needs-verify";
    } else {
      sp.deviceFolder = r.name;
      sp.state = "new";
    }
    claim(sp.deviceFolder, owner);
  }

  // 6. Device-only folders: listed, not managed by any planned series.
  const managed = new Set();
  for (const sp of sps) {
    if (sp.deviceFolder && sp.state !== "new" && sp.state !== "gone") managed.add(slotKey(sp.deviceFolder, ci));
    // A held series' own folder is held with it, never device-only.
    if (sp.state === "held" && sp._shard) managed.add(slotKey(sp._shard.name, ci));
    if (sp.state === "held" && sp.heldFolder) managed.add(slotKey(sp.heldFolder, ci));
  }
  const shardByName = new Map(view.shards.map((sh) => [slotKey(sh.name, ci), sh]));
  const unmatched = [];
  for (const [slot, name] of dirSlots) {
    if (managed.has(slot)) continue;
    const listing = _deviceFiles(device, name);
    const sh = shardByName.get(slot) || null;
    unmatched.push({
      name,
      slot,
      listing,
      shard: sh && !byShard.has(sh.shardId) ? sh : null,
      meta: device.meta instanceof Map ? device.meta.get(name) || null : null,
    });
  }

  // 7. Adoption ladder (rule 2) for series that would create a folder.
  const creators = sps.filter((sp) => sp.state === "new" || sp.state === "gone");
  for (const sp of creators) _matchAdoption(plan, sp, unmatched, ignored);
  for (const sp of creators) {
    const mine = plan.suggestions.filter((g) => g.seriesKey === sp.seriesKey && !g.ignored);
    const strong = mine.filter((g) => g.strong);
    const blocking = strong.length ? strong : plan.firstSync ? mine : [];
    sp.blockedBy = blocking.map((g) => g.id);
  }

  // 8. Per-series ops.
  for (const sp of sps) {
    if (sp.state === "held" || sp.state == null) continue;
    if (sp.state === "new" || sp.state === "gone") _planCreate(plan, sp, cfg, now, ack, settings);
    else _planExisting(plan, sp, cfg, device, now, ack, settings);
    if (sp.state === "needs-verify") {
      plan.needsVerify.push({
        folder: sp.deviceFolder,
        seriesKey: sp.seriesKey,
        shardId: sp.shardId,
        binding: { ...sp.binding },
      });
    }
  }

  // 8b. "Remove from <target>" (approved design D7): an exclude entry with
  // removeFiles keeps offering its series' files there as deletes, plan after
  // plan, until they are gone or unticked (the mark is saved, so an automatic
  // re-plan can't drop them). Only an excluded series, so nothing pushes back.
  for (const sp of sps) {
    if (sp.heldReason !== "excluded") continue;
    const s = sp._s;
    const entry = naming.findSeriesEntry({ identityKey: s.identityKey, url: s.url, folder: s.folder }, cfg.excludes);
    if (!entry || entry.removeFiles !== true) continue;
    const folder = sp._shard ? dirSlots.get(slotKey(sp._shard.name, ci)) : sp.heldFolder;
    if (folder != null) _planEvict(plan, sp, cfg, device, folder, settings);
  }

  // 9. Device-only rows and their suggestions.
  for (const u of unmatched) {
    const files = u.listing.files;
    const chapterFiles = files.filter((f) => isChapterFormat(f.name)).length;
    let ours = 0;
    if (u.shard) for (const f of files) if (classifyDeviceFile(u.shard.files.get(slotKey(f.name, ci)), f) === "ours") ours += 1;
    plan.deviceOnlyRows.set(
      deviceOnlyKey(u.name),
      files.map((f) => {
        const lab = labelFile(f.name, u.name, patterns);
        const e = u.shard ? u.shard.files.get(slotKey(f.name, ci)) : null;
        return { kind: "device-only", name: f.name, size: f.size, unit: lab.unit, label: lab.label, origin: e ? e.origin : null };
      }),
    );
    plan.deviceOnly.push({
      key: deviceOnlyKey(u.name),
      name: u.name,
      state: u.shard ? "orphaned" : "preserved",
      shardId: u.shard ? u.shard.shardId : null,
      listed: u.listing.ok,
      files: files.length,
      bytes: files.reduce((n, f) => n + (f.size || 0), 0),
      chapterFiles,
      ours,
      suggestions: plan.suggestions.filter((g) => g.deviceFolder === u.name).map((g) => g.id),
      // The single "Find source" search (UI-pass U4-1): the same query the
      // batch runner seeds its rows with (find-sources.js _newRow).
      query: naming.deriveSearchQuery(u.name),
    });
  }

  const loose = (device.rootFiles || []).filter((f) => f && f.name && !String(f.name).startsWith("."));
  if (loose.length) {
    plan.anomalies.push(
      makeAnomaly("loose-root-files", ROOT_SERIES_KEY, { count: loose.length, examples: loose.slice(0, 5).map((f) => f.name) }, ack),
    );
  }

  for (const sp of sps) {
    delete sp._s;
    delete sp._shard;
  }
  return plan;
}

function _addAnomaly(plan, sp, a) {
  if (sp.anomalyIds.includes(a.id)) return;
  plan.anomalies.push(a);
  sp.anomalyIds.push(a.id);
}

function _matchAdoption(plan, sp, unmatched, ignored) {
  const s = sp._s;
  const pcFiles = new Set(s.files.filter((f) => isChapterFormat(f.name)).map((f) => `${f.name}\0${f.size}`));
  const nameKeys = new Set(
    [stripHidSuffix(s.folder), s.title, ...(s.synonyms || [])].filter(Boolean).map((n) => compactKey(n)).filter(Boolean),
  );
  const tokenSets = [stripHidSuffix(s.folder), s.title].filter(Boolean).map(_tokens);
  const goneEntries = sp.state === "gone" && sp._shard ? [...sp._shard.files.values()] : null;
  for (const u of unmatched) {
    let tier = null;
    const meta = u.meta || {};
    const details = meta.details && typeof meta.details === "object" ? meta.details : null;
    const aio = meta.aio && typeof meta.aio === "object" ? meta.aio : null;
    if (
      (details && s.urlKey && normalizeSeriesUrl(details.source_url) === s.urlKey) ||
      (details && s.anilistId && details.anilist_id != null && String(details.anilist_id) === s.anilistId) ||
      (aio && s.identityKey && seriesIdentityKey(aio) === s.identityKey) ||
      (aio && s.urlKey && normalizeSeriesUrl(aio.url) === s.urlKey)
    ) {
      tier = "identity";
    }
    if (!tier) {
      const dch = u.listing.files.filter((f) => isChapterFormat(f.name));
      if (dch.length) {
        let hit = 0;
        for (const f of dch) if (pcFiles.has(`${f.name}\0${f.size}`)) hit += 1;
        if (hit / dch.length >= ADOPT_RATIO) tier = "content";
      }
    }
    if (!tier && goneEntries && goneEntries.length) {
      // The tablet-side rename of a gone folder: its kept names are in D.
      const have = new Set(u.listing.files.map((f) => `${f.name}\0${f.size}`));
      let hit = 0;
      for (const e of goneEntries) if (have.has(`${e.name}\0${e.size}`)) hit += 1;
      if (hit / goneEntries.length >= ADOPT_RATIO) tier = "record";
    }
    if (!tier && nameKeys.has(compactKey(stripHidSuffix(u.name)))) tier = "name";
    if (!tier) {
      const dt = _tokens(u.name);
      if (tokenSets.some((t) => _jaccard(t, dt) >= SIMILAR_RATIO)) tier = "similar";
    }
    if (!tier) continue;
    const id = `link:${sp.seriesKey}=>${u.name}`;
    plan.suggestions.push({
      id,
      kind: "link",
      tier,
      strong: STRONG_TIERS.has(tier),
      seriesKey: sp.seriesKey,
      deviceFolder: u.name,
      ignored: ignored.has(id),
    });
  }
  plan.suggestions.sort(
    (a, b) => (a.seriesKey < b.seriesKey ? -1 : a.seriesKey > b.seriesKey ? 1 : 0) || TIER_ORDER.indexOf(a.tier) - TIER_ORDER.indexOf(b.tier),
  );
}

function _addOp(plan, sp, op) {
  const full = {
    seriesKey: sp.seriesKey,
    folder: sp.deviceFolder,
    from: null,
    to: null,
    viaTemp: false,
    sidecar: false,
    size: 0,
    sha: null,
    pcName: null,
    pcPath: null,
    devFile: null,
    unit: null,
    label: null,
    gate: null,
    preselected: false,
    blockedBy: [],
    ...op,
  };
  if (plan.ops.has(full.id)) {
    plan.warnings.push({ kind: "duplicate-op", opId: full.id });
    return null;
  }
  plan.ops.set(full.id, full);
  sp.opIds.push(full.id);
  return full;
}

function _labelInfo(name, folder, patterns) {
  return labelFile(name, folder, patterns);
}

// Push/update/replace ops record the chapter label they bring, for losses.
function _brings(sp, op, pcLabel) {
  if (pcLabel && (pcLabel.unit === "chapter" || pcLabel.unit === "range")) {
    sp.brings.push({ opId: op.id, unit: pcLabel.unit, label: pcLabel.label, start: pcLabel.start, end: pcLabel.end });
  }
}

function _addCover(sp, label, opIdOrNull, strong) {
  if (!label) return;
  if (!sp.cover.has(label)) sp.cover.set(label, []);
  sp.cover.get(label).push({ opId: opIdOrNull, strong });
}

function _settling(f, now) {
  return Number.isFinite(f.mtimeMs) && isSettling(f.mtimeMs, now);
}

/**
 * A file is settling while its mtime is within SETTLE_MS of now, on EITHER
 * side. A future mtime farther out is a bad clock or an archive's stamp,
 * not a write in progress: counting it as settling held the file until that
 * date, and past ~24.8 days the re-plan timer overflowed (review R4).
 * Shared with executor.js _guard, so plan and apply agree.
 */
function isSettling(mtimeMs, now) {
  return Math.abs(now - mtimeMs) < SETTLE_MS;
}

function _noteSettle(plan, f) {
  const at = f.mtimeMs + SETTLE_MS;
  if (plan.settleAt == null || at > plan.settleAt) plan.settleAt = at;
}

// Two PC names on one slot key (rule 11): the first by name wins, the
// other is reported and planned nowhere.
function _pcSlots(plan, sp, files, ci) {
  const out = new Map();
  for (const f of files.slice().sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))) {
    const k = slotKey(f.name, ci);
    if (out.has(k)) {
      plan.warnings.push({ kind: "pc-name-collision", seriesKey: sp.seriesKey, names: [out.get(k).name, f.name] });
      continue;
    }
    out.set(k, f);
  }
  return out;
}

/**
 * New folder or gone folder: every mirrored PC file is a push. A gone
 * series reuses the old shard's kept names where the sha matches, so
 * Komikku's per-chapter progress (keyed by file name) survives.
 */
function _planCreate(plan, sp, cfg, now, ack, settings) {
  const s = sp._s;
  const ci = plan.caseInsensitive;
  const patterns = settings.chapterPatterns || [];
  for (const a of contentAnomalies({
    seriesKey: sp.seriesKey,
    pcLabeled: s.labeled,
    devLabeled: [],
    pcSidecars: s.mirrored.filter((f) => isSidecar(f.name, cfg)).map((f) => f.name),
    devNames: [],
    malformedJson: s.malformedJson,
    cfg,
    sizeWarnPct: 0,
    acknowledged: ack,
  })) {
    _addAnomaly(plan, sp, a);
  }
  const pcBySlot = _pcSlots(plan, sp, s.mirrored, ci);
  const keptBySha = new Map();
  if (sp.state === "gone") {
    for (const e of sp._shard.files.values()) {
      if (e && e.sha256 && e.origin !== "foreign" && !segmentError(e.name) && !keptBySha.has(e.sha256)) keptBySha.set(e.sha256, e.name);
    }
  }
  const usedSlots = new Set();
  const blocked = sp.blockedBy.length > 0 || (sp.rebind && sp.rebind.declined);
  for (const [slot, f] of pcBySlot) {
    const lab = _labelInfo(f.name, s.folder, patterns);
    if (_settling(f, now)) {
      sp.counts.settling += 1;
      _noteSettle(plan, f);
      sp.rows.push(_row("settling", { pcName: f.name, name: f.name, size: f.size, lab }));
      continue;
    }
    if (!f.sha256) {
      sp.counts.unhashed += 1;
      sp.rows.push(_row("unhashed", { pcName: f.name, name: f.name, size: f.size, lab }));
      continue;
    }
    let name = f.name;
    const kept = keptBySha.get(f.sha256);
    if (kept && kept !== f.name) {
      const k = slotKey(kept, ci);
      if (!usedSlots.has(k) && (k === slot || !pcBySlot.has(k))) name = kept;
    }
    usedSlots.add(slotKey(name, ci));
    const op = _addOp(plan, sp, {
      id: opId("push", sp.deviceFolder, name),
      kind: "push",
      name,
      size: f.size,
      sha: f.sha256,
      pcName: f.name,
      pcPath: f.path || null,
      unit: lab.unit,
      label: lab.label,
      reason: sp.state === "gone" ? "removed-on-device" : "new",
      sidecar: isSidecar(f.name, cfg),
      preselected: !blocked,
      blockedBy: sp.blockedBy.slice(),
    });
    if (!op) continue;
    _brings(sp, op, lab);
    sp.rows.push(_row("push", { opId: op.id, pcName: f.name, name, size: f.size, lab, reason: op.reason }));
  }
}

function _row(kind, x) {
  return {
    kind,
    opId: x.opId || null,
    name: x.name,
    pcName: x.pcName || null,
    deviceName: x.deviceName || null,
    unit: x.lab ? x.lab.unit : null,
    label: x.lab ? x.lab.label : null,
    size: x.size == null ? null : x.size,
    origin: x.origin || null,
    reason: x.reason || null,
  };
}

/**
 * An existing device folder (bound, or managed by name and awaiting its
 * Verify): rule 4 per slot, then rule 5 for the unclaimed device files.
 */
function _planExisting(plan, sp, cfg, device, now, ack, settings) {
  const s = sp._s;
  const sh = sp._shard;
  const ci = plan.caseInsensitive;
  const patterns = settings.chapterPatterns || [];
  const policy = plan.deletePolicy;
  const listing = _deviceFiles(device, sp.deviceFolder);
  if (!listing.ok) {
    sp.state = "held";
    sp.heldReason = "folder-unreadable";
    _addAnomaly(plan, sp, makeAnomaly("folder-unreadable", sp.seriesKey, { folder: sp.deviceFolder, reason: listing.error }, ack));
    return;
  }
  const entries = sh ? sh.files : new Map();
  if (sh && entries.size && !listing.files.length) {
    // Rule 7's sanity check: blocks apply for the target (service.js).
    plan.sanity.push({ folder: sp.deviceFolder, seriesKey: sp.seriesKey });
  }

  // Device files by slot, entry fixes applied (a pushed entry still without
  // devMtime adopts the listing's at an equal size).
  const dev = new Map();
  for (const d of listing.files) {
    const slot = slotKey(d.name, ci);
    let e = entries.get(slot);
    const fixed = adoptMissingDevMtime(e, d);
    if (fixed) {
      plan.entryFixes.push({ shardId: sh.shardId, slot, entry: fixed });
      e = fixed;
    }
    const lab = _labelInfo(d.name, sp.deviceFolder, patterns);
    dev.set(slot, { d, e, cls: classifyDeviceFile(e, d), lab, claimed: false, deleteOpId: null });
  }

  const pcAll = _pcSlots(plan, sp, s.files, ci);
  const pcMirrored = new Map([...pcAll].filter(([, f]) => isMirrored(f.name, cfg)));

  // Labels and tags first: tags decide delete preselection.
  const devLabeled = [...dev.values()].filter((x) => isChapterFormat(x.d.name)).map((x) => ({ ...x.lab, name: x.d.name, size: x.d.size, ext: extOf(x.d.name) }));
  sp.tags = deltaTags(s.labeled.filter((f) => f.mirrored), devLabeled);
  sp.blockingTags = deleteBlockingTags(sp.tags);
  sp.deviceChapterFiles = devLabeled.length;
  const devMeta = device.meta instanceof Map ? device.meta.get(sp.deviceFolder) : null;
  const malformed = s.malformedJson.slice();
  if (devMeta && devMeta.malformed) malformed.push(`device:${sp.deviceFolder}/details.json`);
  for (const a of contentAnomalies({
    seriesKey: sp.seriesKey,
    pcLabeled: s.labeled,
    devLabeled,
    pcSidecars: s.mirrored.filter((f) => isSidecar(f.name, cfg)).map((f) => f.name),
    devNames: listing.files.map((f) => f.name),
    malformedJson: malformed,
    cfg,
    sizeWarnPct: settings.syncSizeWarnPct || 0,
    acknowledged: ack,
  })) {
    _addAnomaly(plan, sp, a);
  }
  // Kept names: unclaimed files of ours whose content a PC file already
  // has. Only hash-verified origins: an adopted-size sha is an assumption.
  const keptPool = new Map();
  for (const [slot, x] of dev) {
    if (pcAll.has(slot)) continue;
    if (x.cls !== "ours" || (x.e.origin !== "pushed" && x.e.origin !== "adopted")) continue;
    const k = `${x.e.sha256}\0${x.d.size}`;
    if (!keptPool.has(k)) keptPool.set(k, slot);
  }

  // auto-replace is guarded plus one rule (header "AUTO-REPLACE"): a
  // hash-verified foreign copy the PC's own file replaces is pre-selected.
  const autoReplace = policy === "auto-replace";
  const guarded = policy === "guarded" || autoReplace;
  const renamePre = guarded && cfg.renameToMatch;
  const autoOk = autoReplace && !sp.blockingTags.length && !sp.tags.includes("mixed");
  const foreignCurrent = (x) => isForeignCurrent(x.e, x.d) && x.lab.unit === "chapter" && !!x.lab.appLabel;
  // A swap auto-replace would tick but for the series' tags (plan R9): the
  // detail names those tags so an unticked swap isn't a silent surprise.
  const heldBack = () => {
    if (autoReplace && !autoOk && !sp.autoHeld.length) sp.autoHeld = [...sp.blockingTags, ...(sp.tags.includes("mixed") ? ["mixed"] : [])];
  };
  for (const [slot, f] of pcMirrored) {
    const lab = _labelInfo(f.name, s.folder, patterns);
    const x = dev.get(slot);
    if (x) x.claimed = true;
    if (_settling(f, now)) {
      sp.counts.settling += 1;
      _noteSettle(plan, f);
      sp.rows.push(_row("settling", { pcName: f.name, name: x ? x.d.name : f.name, deviceName: x ? x.d.name : null, size: f.size, lab }));
      continue;
    }
    if (!f.sha256) {
      sp.counts.unhashed += 1;
      sp.rows.push(_row("unhashed", { pcName: f.name, name: x ? x.d.name : f.name, size: f.size, lab }));
      continue;
    }
    if (x) {
      const nameDiffers = x.d.name !== f.name;
      if (x.cls === "ours" && x.e.sha256 === f.sha256 && x.d.size === f.size) {
        sp.counts.inSync += 1;
        _addCover(sp, lab.label, null, x.e.origin !== "adopted-size");
        sp.rows.push(_row("in-sync", { pcName: f.name, name: x.d.name, deviceName: x.d.name, size: f.size, lab, origin: x.e.origin }));
      } else {
        let kind;
        let reason;
        let pre;
        if (x.cls === "ours") {
          kind = "update";
          reason = x.e.origin === "partial" ? "repair" : "changed-on-pc";
          pre = true;
        } else if (x.cls === "pending") {
          // The app was writing this slot and the RECV that would decide it
          // failed; the PC has the file (provenance.js resolvePending).
          kind = "update";
          reason = "unresolved-pending";
          pre = true;
        } else {
          kind = "replace";
          reason = x.cls === "changed" ? "changed-on-device" : x.cls;
          // Same slot, so the PC file brings this very label. Only a foreign
          // copy: a changed file of ours is the user's edit on the device.
          pre = autoOk && x.cls === "foreign" && foreignCurrent(x);
          if (x.cls === "foreign" && foreignCurrent(x)) heldBack();
        }
        const op = _addOp(plan, sp, {
          id: opId(kind, sp.deviceFolder, x.d.name),
          kind,
          name: x.d.name,
          size: f.size,
          sha: f.sha256,
          pcName: f.name,
          pcPath: f.path || null,
          devFile: { size: x.d.size, devMtime: x.d.devMtime },
          unit: lab.unit,
          label: lab.label,
          reason,
          sidecar: isSidecar(f.name, cfg),
          preselected: pre,
        });
        if (op) {
          _brings(sp, op, lab);
          _addCover(sp, lab.label, op.id, true);
          sp.rows.push(_row(kind, { opId: op.id, pcName: f.name, name: x.d.name, deviceName: x.d.name, size: f.size, lab, origin: x.e ? x.e.origin : null, reason }));
        }
      }
      if (nameDiffers && cfg.renameToMatch) {
        // Case- or normalization-only difference (same slot): through a temp
        // name (viaTemp), which the executor records in the rename intent.
        _renameOp(plan, sp, x, f, lab, renamePre, true);
      }
      continue;
    }
    const keptSlot = keptPool.get(`${f.sha256}\0${f.size}`);
    if (keptSlot != null) {
      keptPool.delete(`${f.sha256}\0${f.size}`);
      const k = dev.get(keptSlot);
      k.claimed = true;
      sp.counts.kept += 1;
      _addCover(sp, lab.label, null, true);
      sp.rows.push(_row("kept", { pcName: f.name, name: k.d.name, deviceName: k.d.name, size: f.size, lab, origin: k.e.origin }));
      if (cfg.renameToMatch) _renameOp(plan, sp, k, f, lab, renamePre, false);
      continue;
    }
    const op = _addOp(plan, sp, {
      id: opId("push", sp.deviceFolder, f.name),
      kind: "push",
      name: f.name,
      size: f.size,
      sha: f.sha256,
      pcName: f.name,
      pcPath: f.path || null,
      unit: lab.unit,
      label: lab.label,
      reason: "missing",
      sidecar: isSidecar(f.name, cfg),
      preselected: true,
    });
    if (op) {
      _brings(sp, op, lab);
      _addCover(sp, lab.label, op.id, true);
      sp.rows.push(_row("push", { opId: op.id, pcName: f.name, name: f.name, size: f.size, lab, reason: "missing" }));
    }
  }

  // PC chapter files by label, for the replacement deletes' gate.
  const pcByLabel = new Map();
  for (const f of s.labeled) {
    if (!f.mirrored || f.unit !== "chapter") continue;
    if (!pcByLabel.has(f.label)) pcByLabel.set(f.label, []);
    pcByLabel.get(f.label).push({ name: f.name, size: f.size });
  }

  // Unclaimed device files (rule 4's tail, rule 5).
  for (const [slot, x] of dev) {
    if (x.claimed) {
      if (isChapterFormat(x.d.name)) sp.devFiles.push(_devFile(x));
      continue;
    }
    const chapterFmt = isChapterFormat(x.d.name);
    const sidecar = isSidecar(x.d.name, cfg);
    // The slot guard (rule 6) in plan form: a slot any PC file of the series
    // holds is never offered, mirrored or not.
    if (pcAll.has(slot) || (!chapterFmt && !sidecar)) {
      sp.counts.unmanaged += 1;
      sp.rows.push(_row("unmanaged", { name: x.d.name, deviceName: x.d.name, size: x.d.size, lab: x.lab, origin: x.e ? x.e.origin : null, reason: pcAll.has(slot) ? "pc-slot" : "type" }));
      if (chapterFmt) sp.devFiles.push(_devFile(x));
      continue;
    }
    const lab = x.lab;
    let reason;
    if (sidecar) reason = "sidecar";
    else if (lab.unit === "chapter" && sp.cover.has(lab.label)) reason = "replacement";
    else reason = "extra";
    const strongCover =
      lab.unit === "chapter" && (sp.cover.get(lab.label) || []).some((c) => (c.opId == null ? c.strong : plan.ops.get(c.opId).preselected));
    const pre =
      (guarded &&
        x.cls === "ours" &&
        x.e.origin !== "adopted-size" &&
        !sp.blockingTags.length &&
        (sidecar || strongCover)) ||
      (autoOk && reason === "replacement" && x.cls === "foreign" && foreignCurrent(x) && strongCover);
    if (reason === "replacement" && x.cls === "foreign" && foreignCurrent(x) && strongCover) heldBack();
    const coverFiles = lab.unit === "chapter" ? (pcByLabel.get(lab.label) || []).slice() : [];
    // A range extra whose chapters the PC covers rests on them landing
    // (seriesLosses counts a brought label as carried), so it waits for them
    // like a replacement; labels the PC lacks are a loss, acked or blocking.
    const rangeCovered = !sidecar && lab.unit === "range" ? (_rangeInts(lab.start, lab.end) || []).filter((l) => sp.cover.has(l)) : [];
    let gate = null;
    if (reason === "replacement") gate = { label: lab.label, files: coverFiles };
    else if (rangeCovered.length) {
      gate = { labels: rangeCovered, files: rangeCovered.flatMap((l) => (pcByLabel.get(l) || []).map((f) => ({ ...f, label: l }))) };
    }
    const op = _addOp(plan, sp, {
      id: opId("delete", sp.deviceFolder, x.d.name),
      kind: "delete",
      name: x.d.name,
      sha: x.e && x.e.sha256 ? x.e.sha256 : `${x.d.size}@${x.d.devMtime}`,
      devFile: { size: x.d.size, devMtime: x.d.devMtime },
      unit: sidecar ? "sidecar" : lab.unit,
      label: lab.label,
      reason,
      sidecar,
      origin: x.e ? x.e.origin : null,
      ours: x.cls === "ours",
      // Rule 6's delete gate: runs only after the post-push listing shows
      // its label(s) at an exact PC size (header DELETE GATES).
      gate,
      preselected: pre,
    });
    if (op) {
      x.deleteOpId = op.id;
      sp.rows.push(_row("delete", { opId: op.id, name: x.d.name, deviceName: x.d.name, size: x.d.size, lab, origin: x.e ? x.e.origin : null, reason }));
    }
    if (chapterFmt) sp.devFiles.push(_devFile(x));
  }
  _gateCredits(plan, sp, dev, patterns);
}

/**
 * `gateCredit` on every gated delete of a series (header DELETE GATES): per
 * required label, the ways the executor's gate can find it landed. It mirrors
 * executor.js _deletes: a voucher is a chapter file labelled (with the DEVICE
 * folder, as the executor labels) at a gate size that is either written this
 * run ({op}: a push/update/replace, or a rename onto such a name) or already
 * ours from a push or hash adoption ({unless}: the ops that would take it
 * away: its delete, a rename off the label, an overwrite of its slot).
 * Optimistic only where the executor can still fail at run time (a push that
 * fails, a file the guard skips); space.js treats the rest as kept.
 */
function _gateCredits(plan, sp, dev, patterns) {
  const labelOf = (name) => _labelInfo(name, sp.deviceFolder, patterns);
  const ops = sp.opIds.map((id) => plan.ops.get(id));
  const writeBySlot = new Map();
  for (const op of ops) if (isWrite(op)) writeBySlot.set(slotKey(op.name, plan.caseInsensitive), op);
  const renameOf = new Map();
  for (const op of ops) if (op.kind === "rename") renameOf.set(slotKey(op.from, plan.caseInsensitive), op);
  for (const op of ops) {
    if (op.kind !== "delete" || !op.gate) continue;
    const required = gateLabels(op.gate);
    const ways = new Map(required.map((r) => [r.label, []]));
    const add = (label, w) => label != null && ways.has(label) && ways.get(label).push(w);
    for (const w of ops) if (isWrite(w)) add(vouchedLabel(required, w.name, w.size, labelOf), { op: w.id });
    for (const [slot, x] of dev) {
      if (x.cls !== "ours" || (x.e.origin !== "pushed" && x.e.origin !== "adopted")) continue;
      if (x.deleteOpId === op.id) continue;
      const write = writeBySlot.get(slot);
      const ren = renameOf.get(slot);
      const unless = [x.deleteOpId, write && write.id].filter(Boolean);
      const here = vouchedLabel(required, x.d.name, x.d.size, labelOf);
      if (here != null) add(here, { unless: ren ? [...unless, ren.id] : unless });
      if (ren) add(vouchedLabel(required, ren.to, x.d.size, labelOf), { op: ren.id, unless });
    }
    op.gateCredit = required.map((r) => ways.get(r.label));
  }
}

/**
 * Delete ops for every chapter file and sidecar of an excluded series' folder
 * on the target (8b). Files the record proves ours are pre-selected; anything
 * else (a file the app never wrote) is offered unticked, the same caution as
 * an extra. Other file types stay, as everywhere (unmanaged). A delete is a
 * LOSS only when the PC lacks that chapter too (sp.evict, read by
 * seriesLosses): removing what the PC keeps loses nothing. sp.deviceFolder
 * is set so the executor finds the folder; the state stays `held`.
 */
function _planEvict(plan, sp, cfg, device, folder, settings) {
  const listing = _deviceFiles(device, folder);
  if (!listing.ok) return;
  const ci = plan.caseInsensitive;
  const patterns = settings.chapterPatterns || [];
  const entries = sp._shard ? sp._shard.files : new Map();
  const pcLabels = new Set();
  for (const f of sp._s.labeled) {
    if (f.unit === "chapter") pcLabels.add(f.label);
    else if (f.unit === "range") for (const l of _rangeInts(f.start, f.end) || []) pcLabels.add(l);
  }
  sp.deviceFolder = folder;
  // pcNames covers what labels can't compare (volumes, whole files): the
  // same file name on the PC.
  sp.evict = { folder, pcLabels, pcNames: new Set(sp._s.files.map((f) => slotKey(f.name, ci))) };
  for (const d of listing.files) {
    const lab = _labelInfo(d.name, folder, patterns);
    const chapterFmt = isChapterFormat(d.name);
    const sidecar = isSidecar(d.name, cfg);
    const e = entries.get(slotKey(d.name, ci)) || null;
    if (!chapterFmt && !sidecar) {
      sp.counts.unmanaged += 1;
      sp.rows.push(_row("unmanaged", { name: d.name, deviceName: d.name, size: d.size, lab, origin: e ? e.origin : null, reason: "type" }));
      continue;
    }
    const ours = classifyDeviceFile(e, d) === "ours";
    const op = _addOp(plan, sp, {
      id: opId("delete", folder, d.name),
      kind: "delete",
      name: d.name,
      sha: e && e.sha256 ? e.sha256 : `${d.size}@${d.devMtime}`,
      devFile: { size: d.size, devMtime: d.devMtime },
      unit: sidecar ? "sidecar" : lab.unit,
      label: lab.label,
      reason: EVICT_REASON,
      sidecar,
      origin: e ? e.origin : null,
      ours,
      preselected: ours,
    });
    if (!op) continue;
    sp.rows.push(_row("delete", { opId: op.id, name: d.name, deviceName: d.name, size: d.size, lab, origin: e ? e.origin : null, reason: EVICT_REASON }));
    if (chapterFmt) {
      sp.deviceChapterFiles += 1;
      sp.devFiles.push({ name: d.name, unit: lab.unit, label: lab.label, start: lab.start, end: lab.end, deleteOpId: op.id });
    }
  }
}

function _devFile(x) {
  return {
    name: x.d.name,
    unit: x.lab.unit,
    label: x.lab.label,
    start: x.lab.start,
    end: x.lab.end,
    deleteOpId: x.deleteOpId,
  };
}

function _renameOp(plan, sp, x, f, lab, pre, viaTemp) {
  const op = _addOp(plan, sp, {
    id: renameOpId(sp.deviceFolder, x.d.name, f.name),
    kind: "rename",
    name: x.d.name,
    from: x.d.name,
    to: f.name,
    viaTemp,
    sha: f.sha256,
    devFile: { size: x.d.size, devMtime: x.d.devMtime },
    unit: lab.unit,
    label: lab.label,
    reason: "rename-to-match",
    preselected: pre,
  });
  if (op) sp.rows.push(_row("rename", { opId: op.id, pcName: f.name, name: x.d.name, deviceName: x.d.name, size: x.d.size, lab, reason: "rename-to-match" }));
}

// ------------------------------------------------------------------
// Selection: effective state, totals, losses, acknowledgments.
// ------------------------------------------------------------------

/**
 * The effective selection: a stored choice applies only while its sha
 * still equals the op's (rule 7); otherwise the op's preselection.
 * @param {object} plan
 * @param {Object<string,{sha:string, selected:boolean}>} storedOps  selection.json `ops`
 * @returns {Map<string, boolean>}
 */
function effectiveSelection(plan, storedOps) {
  const stored = storedOps || {};
  const out = new Map();
  for (const op of plan.ops.values()) {
    const s = Object.prototype.hasOwnProperty.call(stored, op.id) ? stored[op.id] : null;
    out.set(op.id, s && s.sha === op.sha ? !!s.selected : !!op.preselected);
  }
  return out;
}

function _rangeInts(start, end) {
  const a = Number(start);
  const b = Number(end);
  if (!Number.isInteger(a) || !Number.isInteger(b) || b < a || b - a > RANGE_EXPAND_MAX) return null;
  const out = [];
  for (let i = a; i <= b; i += 1) out.push(String(i));
  return out;
}

/**
 * Losses for one series under a selection (parent rule 5): a chapter label
 * on the device now that, after the selected ops, no device file carries
 * and no selected push brings. Non-chapter units (volume, whole, unknown)
 * are never comparable, so deleting one is always a loss; a range is
 * covered only when every integer in it stays carried.
 *
 * @returns {{loss:Set<string>, lossIfSelected:Set<string>}} op ids
 */
function seriesLosses(plan, sp, selected) {
  const loss = new Set();
  const lossIfSelected = new Set();
  const isSel = (id) => !!id && selected.get(id) === true;
  const carriers = new Map();
  const add = (label) => carriers.set(label, (carriers.get(label) || 0) + 1);
  const labelsOf = (f) => (f.unit === "chapter" ? [f.label] : f.unit === "range" ? _rangeInts(f.start, f.end) || [] : []);
  for (const f of sp.devFiles) {
    if (isSel(f.deleteOpId)) continue;
    for (const l of labelsOf(f)) add(l);
  }
  const brought = new Set();
  for (const b of sp.brings) if (isSel(b.opId)) for (const l of labelsOf(b)) brought.add(l);
  // A series being removed from the target (8b) keeps its chapters on the
  // PC, so a label the PC holds is carried there.
  const onPc = sp.evict ? sp.evict.pcLabels : null;
  const carried = (label, minus) => (carriers.get(label) || 0) - minus > 0 || brought.has(label) || (!!onPc && onPc.has(label));
  for (const f of sp.devFiles) {
    if (!f.deleteOpId) continue;
    const op = plan.ops.get(f.deleteOpId);
    if (!op || op.sidecar) continue;
    const sel = isSel(f.deleteOpId);
    // An unselected file is still counted among the carriers; "if selected"
    // asks about the state without it.
    const minus = sel ? 0 : 1;
    let lost;
    if (f.unit === "chapter") lost = !carried(f.label, minus);
    else if (f.unit === "range") {
      const ints = _rangeInts(f.start, f.end);
      lost = !ints || ints.some((l) => !carried(l, minus));
    } else lost = !(sp.evict && sp.evict.pcNames.has(slotKey(f.name, plan.caseInsensitive)));
    if (!lost) continue;
    if (sel) loss.add(f.deleteOpId);
    else lossIfSelected.add(f.deleteOpId);
  }
  return { loss, lossIfSelected };
}

/**
 * Totals, losses and the acknowledgments Start needs, for a selection.
 * @param {object} plan
 * @param {Map<string,boolean>} selected   effectiveSelection() result.
 */
function evaluateSelection(plan, selected) {
  const totals = {
    push: { count: 0, bytes: 0 },
    update: { count: 0, bytes: 0 },
    replace: { count: 0, bytes: 0 },
    rename: { count: 0, bytes: 0 },
    delete: { count: 0, bytes: 0 },
    series: 0,
    files: 0,
    bytes: 0,
  };
  const losses = [];
  const lossIfSelected = [];
  let deletes = 0;
  let overwrites = 0;
  // Gated deletes (header DELETE GATES): the executor removes them only once
  // the PC's copy of their chapter(s) landed, so Review words them as swaps.
  let swaps = 0;
  let massSeries = 0;
  const perSeries = new Map();
  for (const sp of plan.series) {
    const counts = { push: 0, update: 0, replace: 0, rename: 0, delete: 0, bytes: 0, loss: 0 };
    let chapterDeletes = 0;
    for (const id of sp.opIds) {
      if (!selected.get(id)) continue;
      const op = plan.ops.get(id);
      counts[op.kind] += 1;
      totals[op.kind].count += 1;
      if (op.kind === "delete") {
        totals.delete.bytes += op.devFile ? op.devFile.size : 0;
        deletes += 1;
        if (op.gate) swaps += 1;
        if (!op.sidecar && op.unit !== "sidecar") chapterDeletes += 1;
      }
      // Overwriting a foreign chapter destroys a copy the PC never had, as a
      // delete does (adb SEND unlinks first), and auto-replace ticks them in
      // bulk after a Link: they count toward the mass check, which is the
      // wrong-Link guard for same-named files.
      if (op.kind === "replace" && op.reason === "foreign" && !op.sidecar && isChapterFormat(op.name)) {
        overwrites += 1;
        chapterDeletes += 1;
      }
      if (op.kind !== "delete" && op.kind !== "rename") {
        totals[op.kind].bytes += op.size;
        totals.files += 1;
        totals.bytes += op.size;
        counts.bytes += op.size;
      }
    }
    const l = seriesLosses(plan, sp, selected);
    for (const id of l.loss) losses.push(id);
    for (const id of l.lossIfSelected) lossIfSelected.push(id);
    counts.loss = l.loss.size;
    if (sp.deviceChapterFiles >= MASS_MIN && chapterDeletes / sp.deviceChapterFiles >= MASS_RATIO) massSeries += 1;
    if (counts.push || counts.update || counts.replace || counts.rename || counts.delete) totals.series += 1;
    perSeries.set(sp.seriesKey, counts);
  }
  // count is what Start's acknowledgment must equal (ackMassDelete), so it
  // covers the overwrites too. The split is for Review's copy (copy.js
  // massDeleteCopy): overwrites + swaps are replaced by the PC's copy,
  // deletes - swaps are removals nothing replaces (extras, sidecars, losses).
  const massDelete = {
    required: massSeries > 0 || deletes + overwrites >= MASS_TOTAL_DELETES,
    count: deletes + overwrites,
    deletes,
    overwrites,
    swaps,
    series: massSeries,
  };
  const repushing = plan.series.filter(
    (sp) => sp.state === "gone" && sp.opIds.some((id) => selected.get(id) && plan.ops.get(id).kind === "push"),
  ).length;
  const g = plan.goneTotal;
  const massRepush = {
    required: g.bound >= MASS_MIN && g.gone / g.bound >= MASS_RATIO && repushing > 0,
    count: repushing,
  };
  return { totals, losses, lossIfSelected, massDelete, massRepush, perSeries };
}

/**
 * sync:set-selection. Returns the new stored ops (selection.json `ops`) and
 * the series whose ops changed; rebind changes go through applyRebind.
 * @param {object} plan
 * @param {object} storedOps
 * @param {object} req  {changes:[{opId, selected}]} or {bulk:'covered-extras'|'split-parts'|'group'|'series', seriesKey?, group?, reason?, selected?}
 *   A `group` bulk with `reason` touches only that kind's ops with that
 *   reason: the review's group checkboxes are per (kind, reason)
 *   (planReview), so "untick every sidecar delete" leaves extras alone.
 */
function changeSelection(plan, storedOps, req) {
  const ops = { ...(storedOps || {}) };
  const current = effectiveSelection(plan, ops);
  const changed = new Set();
  const set = (op, value) => {
    ops[op.id] = { sha: op.sha, selected: !!value };
    if (current.get(op.id) !== !!value) changed.add(op.seriesKey);
  };
  if (Array.isArray(req.changes)) {
    for (const c of req.changes) {
      const op = plan.ops.get(c && c.opId);
      if (!op) return refuse("invalid", `unknown op ${c && c.opId}`);
      set(op, c.selected);
    }
    return { ok: true, ops, changedSeries: [...changed] };
  }
  const inScope = (sp) => !req.seriesKey || sp.seriesKey === req.seriesKey;
  if (req.bulk === "series") {
    // The ledger row's checkbox (UI-pass U2-3). Off = every offered op of the
    // series off. On = forget the stored choices, so the plan's own
    // pre-selection applies again: never "tick everything", which would
    // select replace candidates and unproven deletes nobody looked at.
    const sp = plan.bySeriesKey.get(req.seriesKey);
    if (!sp) return refuse("invalid", "unknown series");
    for (const id of sp.opIds) {
      const op = plan.ops.get(id);
      if (req.selected === false) set(op, false);
      else {
        delete ops[id];
        if (current.get(id) !== !!op.preselected) changed.add(op.seriesKey);
      }
    }
  } else if (req.bulk === "group") {
    if (!["push", "update", "replace", "rename", "delete"].includes(req.group)) return refuse("invalid", "unknown group");
    if (req.reason != null && typeof req.reason !== "string") return refuse("invalid", "reason must be a string");
    for (const sp of plan.series) {
      if (!inScope(sp)) continue;
      for (const id of sp.opIds) {
        const op = plan.ops.get(id);
        if (op.kind === req.group && (req.reason == null || op.reason === req.reason)) set(op, req.selected !== false);
      }
    }
  } else if (req.bulk === "covered-extras") {
    // Extras of any chapter format (legacy PDFs included) whose label the PC
    // covers under the current selection. Series whose labels don't line up
    // (DELETE_BLOCKING_TAGS) are left out: "covered" means nothing there.
    for (const sp of plan.series) {
      if (!inScope(sp) || sp.blockingTags.length) continue;
      for (const id of sp.opIds) {
        const op = plan.ops.get(id);
        if (op.kind !== "delete" || op.sidecar || op.unit !== "chapter") continue;
        const cover = sp.cover.get(op.label) || [];
        if (cover.some((c) => (c.opId == null ? c.strong : current.get(c.opId)))) set(op, true);
      }
    }
  } else if (req.bulk === "split-parts") {
    // Split parts stay losses (their labels aren't on the PC); this only
    // saves the clicks. Acknowledgment still goes through ackLosses.
    for (const sp of plan.series) {
      if (!inScope(sp) || !sp.tags.includes("sub-chapter-split")) continue;
      const pcLabels = new Set(sp.brings.map((b) => b.label));
      for (const [label, cover] of sp.cover) if (cover.length) pcLabels.add(label);
      for (const id of sp.opIds) {
        const op = plan.ops.get(id);
        if (op.kind !== "delete" || op.unit !== "chapter") continue;
        const m = String(op.label).match(/^(-?\d+)\.\d+$/);
        if (m && pcLabels.has(m[1]) && !pcLabels.has(op.label)) set(op, true);
      }
    }
  } else {
    return refuse("invalid", "unknown selection request");
  }
  return { ok: true, ops, changedSeries: [...changed] };
}

/**
 * A Removals policy change (service.js update-target, grep
 * pruneDefaultChoices): drop every stored choice that only repeats what
 * `plan` (built under the OLD policy) pre-selects, so the new policy's
 * pre-selection applies to it; a choice that differs from the old default
 * is a flip the user made and stays (user decision Q3, plan
 * device-sync-auto-replace.md). changeSelection stores defaults too (a
 * group checkbox re-setting what was already ticked), hence this pass.
 * Choices for ops not in `plan`, or with another sha, are kept: their old
 * default is unknown, and a stale sha already doesn't apply.
 * @returns {object} the stored ops, the same object when nothing dropped.
 */
function pruneDefaultChoices(plan, storedOps) {
  const stored = storedOps || {};
  let out = null;
  for (const [id, s] of Object.entries(stored)) {
    const op = plan.ops.get(id);
    if (!op || !s || s.sha !== op.sha || !!s.selected !== !!op.preselected) continue;
    if (!out) out = { ...stored };
    delete out[id];
  }
  return out || stored;
}

/**
 * sync:set-selection {rebind}. Validates against the plan's choices and
 * returns the new `rebinds` map; the caller re-plans, since the folder name
 * is part of every op id.
 */
function applyRebind(plan, rebinds, req) {
  const out = { ...(rebinds || {}) };
  const sp = plan.series.find((x) => x.rebind && x.rebind.shardId === req.shardId);
  if (!sp) return refuse("invalid", "no gone folder with that id");
  const cur = out[req.shardId] || {};
  const next = { source: cur.source || null, name: cur.name || null, declined: !!cur.declined };
  if (req.source != null) {
    const c = sp.rebind.choices.find((x) => x.source === req.source);
    if (!c || !c.available) return refuse("invalid", "that folder name isn't available on the device");
    next.source = c.source;
    next.name = c.name;
  }
  if (typeof req.declined === "boolean") next.declined = req.declined;
  out[req.shardId] = next;
  return { ok: true, rebinds: out };
}

function _signature(op) {
  return JSON.stringify([
    op.kind,
    op.folder,
    op.name,
    op.from,
    op.to,
    op.size,
    op.sha,
    op.pcPath,
    op.devFile ? [op.devFile.size, op.devFile.devMtime] : null,
    // Not the gate: a delete that turned gated or gate-free since the review
    // is still the reviewed removal (the loss check re-runs on the fresh
    // plan), and executor.apply simulates space over the FRESH ops, so a
    // changed gate can only make it refuse, never overrun.
  ]);
}

/**
 * Rule 6: apply re-lists and re-plans, then runs only reviewed op ids that
 * still exist with identical parameters. New ops (a chapter that finished
 * downloading since the review) are not run: nobody reviewed them.
 *
 * @param {object} a
 * @param {object} a.reviewed     The plan the selection was made on.
 * @param {object} a.fresh        The re-plan.
 * @param {object} a.storedOps    selection.json `ops`.
 * @param {string[]} [a.ackLosses]  Loss op ids the user ticked individually.
 * @param {boolean|number} [a.ackMassDelete]  true, or the acknowledged count
 * @param {boolean|number} [a.ackMassRepush]
 * @returns {{ok:boolean, ops:object[], dropped:Array<{opId,reason}>, blocking:object[], evaluation:object}}
 */
function reconcileForApply(a) {
  if (!a.fresh || a.fresh.ok === false) {
    return { ok: false, ops: [], dropped: [], blocking: [{ kind: "refused", refusal: a.fresh }], evaluation: null };
  }
  const reviewedSel = effectiveSelection(a.reviewed, a.storedOps);
  const keep = new Map();
  const dropped = [];
  for (const [id, sel] of reviewedSel) {
    if (!sel) continue;
    const fresh = a.fresh.ops.get(id);
    if (!fresh) dropped.push({ opId: id, reason: "vanished" });
    else if (_signature(fresh) !== _signature(a.reviewed.ops.get(id))) dropped.push({ opId: id, reason: "changed" });
    else keep.set(id, true);
  }
  // A new or re-created folder needs a chapter: sidecars alone would show
  // in Komikku as an empty series.
  for (const sp of a.fresh.series) {
    if (sp.state !== "new" && sp.state !== "gone") continue;
    const hasChapter = sp.opIds.some((id) => keep.get(id) && !a.fresh.ops.get(id).sidecar);
    if (hasChapter) continue;
    for (const id of sp.opIds) {
      if (keep.get(id)) {
        keep.delete(id);
        dropped.push({ opId: id, reason: "no-chapter" });
      }
    }
  }
  const selected = new Map();
  for (const id of a.fresh.ops.keys()) selected.set(id, keep.get(id) === true);
  const ev = evaluateSelection(a.fresh, selected);
  const acked = new Set(a.ackLosses || []);
  const blocking = [];
  const unacked = ev.losses.filter((id) => !acked.has(id));
  if (unacked.length) blocking.push({ kind: "loss", opIds: unacked });
  // A mass acknowledgment is the COUNT the user saw (UI-pass U2-2): a
  // selection change after the tick that grows the set needs a new tick.
  // `true` is the in-process form (tests); service.js _ackArgs never passes it.
  const ackOk = (ack, n) => ack === true || (Number.isInteger(ack) && ack === n);
  if (ev.massDelete.required && !ackOk(a.ackMassDelete, ev.massDelete.count)) blocking.push({ kind: "mass-delete", count: ev.massDelete.count });
  if (ev.massRepush.required && !ackOk(a.ackMassRepush, ev.massRepush.count)) blocking.push({ kind: "mass-repush", count: ev.massRepush.count });
  if (a.fresh.sanity.length) blocking.push({ kind: "listing-sanity", folders: a.fresh.sanity.map((x) => x.folder) });
  const ops = [];
  for (const sp of a.fresh.series) {
    const order = { rename: 0, push: 1, update: 1, replace: 1, delete: 2 };
    const mine = sp.opIds.filter((id) => keep.get(id)).map((id) => a.fresh.ops.get(id));
    mine.sort((x, y) => order[x.kind] - order[y.kind]);
    ops.push(...mine);
  }
  return { ok: blocking.length === 0, ops, dropped, blocking, evaluation: ev };
}

/**
 * The connect prompt's line for one target (rule 10), plus what Set up
 * would verify on a first sync.
 */
function promptSummary(plan, selected, device) {
  const ev = evaluateSelection(plan, selected);
  const t = ev.totals;
  const removedOnDevice = [];
  for (const sp of plan.series) {
    if (sp.state !== "gone") continue;
    const pushes = sp.opIds.map((id) => plan.ops.get(id)).filter((op) => op.kind === "push" && selected.get(op.id));
    if (!pushes.length) continue;
    removedOnDevice.push({
      series: sp.title || sp.folder,
      name: sp.deviceFolder,
      fellBack: !!(sp.rebind && sp.rebind.fellBack),
      files: pushes.length,
      bytes: pushes.reduce((n, op) => n + op.size, 0),
    });
  }
  const verifyBytes = verifyBytesOf(device, plan.needsVerify.map((nv) => nv.folder));
  return {
    targetId: plan.targetId,
    targetName: plan.targetName,
    firstSync: plan.firstSync,
    series: t.series,
    files: t.files,
    bytes: t.bytes,
    deletions: t.delete.count,
    replaces: t.replace.count,
    renames: t.rename.count,
    losses: ev.losses.length,
    verifyFolders: plan.needsVerify.length,
    verifyBytes,
    removedOnDevice,
    nothingToDo: t.files === 0 && t.delete.count === 0 && t.rename.count === 0 && !plan.needsVerify.length,
  };
}

/**
 * One prompt per device listing each of its targets (rule 10). The prompt
 * offers Review, never a one-click apply (approved design D2).
 */
/**
 * Bytes a read-back of `folders` would read: the device listing's sizes.
 * The prompt's "Set up (reads ~N GB)" and prepare's needs-mode refusal both
 * use it, so the card and the Sync tab's chooser name one number.
 */
function verifyBytesOf(device, folders) {
  const dev = device || {};
  let n = 0;
  for (const name of folders || []) {
    const f = dev.folders instanceof Map ? dev.folders.get(name) : null;
    if (f && f.files) n += f.files.reduce((m, x) => m + (x.size || 0), 0);
  }
  return n;
}

/**
 * The connect prompt's card (service.js _afterJob, one per connection).
 * `attention` = the connection's targets whose connect plan was refused or
 * failed ({targetId, targetName, code, message, firstSync, folders, bytes};
 * UI-pass U3-4): a target that needs an answer is reason enough to ask, so
 * the card is empty only when nothing is to do AND nothing needs attention.
 */
function promptCard(serial, summaries, attention) {
  const targets = (summaries || []).filter((s) => s && !s.nothingToDo);
  const att = (attention || []).filter(Boolean);
  return {
    serial,
    targets,
    attention: att,
    empty: targets.length === 0 && att.length === 0,
  };
}

/**
 * Rule 13: library badges from the record, never from a device listing.
 * A PC file counts as on the target when the record holds an entry of ours
 * at its slot (or under a kept name) with its size and its warm-cache sha;
 * a file without a cached sha, or too new to be in the record, is pending.
 * So a series that just finished downloading never shows "synced".
 *
 * @param {object} a  {pc, view, target, lastPlan?}
 * @returns {Map<string, {state:'synced'|'pending'|'absent'|'removed', pending:number, asOf?:number}>}
 *   keyed by the series' folderPath (or folder when no path is known).
 */
function libraryStatus(a) {
  const cfg = a.target;
  const ci = !!a.view.caseInsensitive;
  const series = (a.pc.series || []).map((r) => _normalizeSeries(r, cfg, []));
  const { bySeries } = bindShards(a.view.shards, series);
  const goneSet = new Set(a.lastPlan && a.lastPlan.ok ? a.lastPlan.gone.map((g) => g.shardId) : []);
  const out = new Map();
  for (const s of series) {
    if (!_isIncluded(s, cfg) || s.imageOnly || !s.mirrored.length) continue;
    const key = s.folderPath || s.folder;
    const sh = bySeries.get(s.seriesKey);
    if (!sh) {
      out.set(key, { state: "absent", pending: s.mirrored.length });
      continue;
    }
    if (goneSet.has(sh.shardId)) {
      out.set(key, { state: "removed", pending: s.mirrored.length, asOf: a.lastPlan.asOf });
      continue;
    }
    const bySha = new Map();
    for (const e of sh.files.values()) {
      if (e && ORIGINS_OURS.has(e.origin) && e.sha256) bySha.set(`${e.sha256}\0${e.size}`, e);
    }
    let pending = 0;
    for (const f of s.mirrored) {
      const e = sh.files.get(slotKey(f.name, ci));
      const atSlot = e && ORIGINS_OURS.has(e.origin) && e.size === f.size && !!f.sha256 && e.sha256 === f.sha256;
      const kept = !atSlot && !!f.sha256 && bySha.has(`${f.sha256}\0${f.size}`);
      if (!atSlot && !kept) pending += 1;
    }
    out.set(key, { state: pending ? "pending" : "synced", pending });
  }
  return out;
}

const STRIP_RANK = Object.freeze({ delete: 5, push: 4, warn: 3, synced: 2, absent: 1, other: 0 });

/**
 * The Chapter Strip: the series' chapter range as run-length segments.
 * States: synced (on the target), push (selected push/update/replace),
 * warn (replace candidates and extras), delete (selected delete or loss),
 * absent (not on the target and not selected), other (volumes, ranges,
 * whole files, unparsed; drawn hatched, after the chapters).
 */
function chapterStrip(plan, sp, selected) {
  const items = new Map();
  const others = [];
  const put = (label, state) => {
    const cur = items.get(label);
    if (!cur || STRIP_RANK[state] > STRIP_RANK[cur]) items.set(label, state);
  };
  for (const r of sp.rows) {
    // Unmanaged files are never offered, and sidecars aren't chapters.
    if (r.kind === "unmanaged" || SIDECAR_NAMES.includes(r.pcName || r.name)) continue;
    if (r.unit !== "chapter" || r.label == null) {
      others.push(r.name);
      continue;
    }
    const sel = r.opId ? selected.get(r.opId) === true : false;
    let state;
    switch (r.kind) {
      case "in-sync":
      case "kept":
        state = "synced";
        break;
      case "push":
      case "update":
        state = sel ? "push" : "absent";
        break;
      case "replace":
        state = sel ? "push" : "warn";
        break;
      case "delete":
        state = sel ? "delete" : "warn";
        break;
      case "rename":
        continue;
      default:
        state = "absent";
    }
    put(r.label, state);
  }
  const labels = [...items.keys()].sort(compareLabels);
  const segs = [];
  for (const l of labels) {
    const st = items.get(l);
    const last = segs[segs.length - 1];
    if (last && last.state === st) {
      last.count += 1;
      last.to = l;
    } else segs.push({ state: st, count: 1, from: l, to: l });
  }
  if (others.length) segs.push({ state: "other", count: others.length, from: null, to: null });
  return segs;
}

/**
 * sync:series-detail rows: every file of the series with its op, the
 * effective selection and the loss markers, plus anomalies, suggestions
 * and the rebind choice (decision 10).
 */
function seriesDetail(plan, seriesKey, selected) {
  const sp = plan.bySeriesKey.get(seriesKey);
  if (!sp) {
    const d = plan.deviceOnly.find((x) => x.key === seriesKey);
    if (!d) return refuse("invalid", "unknown series");
    return {
      ok: true,
      seriesKey,
      deviceOnly: d,
      rows: plan.deviceOnlyRows.get(seriesKey) || [],
      suggestions: plan.suggestions.filter((g) => g.deviceFolder === d.name),
      anomalies: [],
    };
  }
  const l = seriesLosses(plan, sp, selected);
  const rows = sp.rows.map((r) => {
    const op = r.opId ? plan.ops.get(r.opId) : null;
    return {
      ...r,
      opId: r.opId,
      preselected: op ? op.preselected : false,
      selected: op ? selected.get(op.id) === true : false,
      loss: op ? l.loss.has(op.id) : false,
      lossIfSelected: op ? l.lossIfSelected.has(op.id) : false,
      settling: r.kind === "settling",
      blockedBy: op ? op.blockedBy : [],
    };
  });
  return {
    ok: true,
    seriesKey,
    folder: sp.folder,
    deviceFolder: sp.deviceFolder,
    state: sp.state,
    heldReason: sp.heldReason,
    removing: !!sp.evict,
    tags: sp.tags,
    autoHeld: sp.autoHeld,
    rows,
    anomalies: plan.anomalies.filter((a) => a.seriesKey === seriesKey),
    suggestions: plan.suggestions.filter((g) => g.seriesKey === seriesKey),
    rebind: sp.rebind,
    strip: chapterStrip(plan, sp, selected),
  };
}

/** Ledger rows for sync:get-state / the Sync tab (no per-file payload). */
function planSummary(plan, selected) {
  const ev = evaluateSelection(plan, selected);
  const lossSet = new Set(ev.losses);
  const ackedAnomaly = new Set(plan.anomalies.filter((a) => a.acknowledged).map((a) => a.id));
  const verifyKeys = new Set(plan.needsVerify.map((n) => n.seriesKey));
  return {
    targetId: plan.targetId,
    asOf: plan.asOf,
    firstSync: plan.firstSync,
    totals: ev.totals,
    losses: ev.losses.length,
    massDelete: ev.massDelete,
    massRepush: ev.massRepush,
    needsVerify: plan.needsVerify.length,
    settleAt: plan.settleAt,
    series: plan.series.map((sp) => ({
      seriesKey: sp.seriesKey,
      title: sp.title,
      folder: sp.folder,
      deviceFolder: sp.deviceFolder,
      state: sp.state,
      heldReason: sp.heldReason,
      removing: !!sp.evict,
      counts: { ...sp.counts, ...ev.perSeries.get(sp.seriesKey), ..._offeredCounts(plan, sp) },
      tags: sp.tags,
      anomalies: sp.anomalyIds.length,
      attention: _attention(plan, sp, selected, lossSet, ackedAnomaly, verifyKeys),
      suggestions: plan.suggestions.filter((g) => g.seriesKey === sp.seriesKey && !g.ignored).length,
      hasLoss: sp.opIds.some((id) => lossSet.has(id)),
      rebind: sp.rebind ? { chosen: sp.rebind.chosen, fellBack: sp.rebind.fellBack, declined: sp.rebind.declined } : null,
      strip: chapterStrip(plan, sp, selected),
    })),
    deviceOnly: plan.deviceOnly,
    anomalies: plan.anomalies.length,
    suggestions: plan.suggestions.filter((g) => !g.ignored).length,
    // Enough for the device-only rows to draw Link without a detail pull.
    suggestionList: plan.suggestions
      .filter((g) => !g.ignored)
      .map((g) => ({ id: g.id, seriesKey: g.seriesKey, deviceFolder: g.deviceFolder, tier: g.tier, strong: g.strong })),
  };
}

/**
 * Per-series offer counts for the ledger row's checkbox (UI-pass U2-4):
 * `offered` = ops the row's box acts on, `preselected` = how many of them the
 * plan ticks by itself. offered 0 → no box; preselected 0 → the box opens the
 * detail instead (bulk 'series' on would change nothing).
 */
function _offeredCounts(plan, sp) {
  let offered = 0;
  let preselected = 0;
  for (const id of sp.opIds) {
    offered += 1;
    if (plan.ops.get(id).preselected) preselected += 1;
  }
  return { offered, preselected };
}

/**
 * Why a series lands under "Needs attention" (spec §5 tiles), as reason
 * codes the renderer only maps to copy (copy.js ATTENTION_COPY). Excluded
 * series aren't attention (the user chose that); a series being removed is
 * shown under Removals instead.
 */
function _attention(plan, sp, selected, lossSet, ackedAnomaly, verifyKeys) {
  const out = [];
  if (sp.state === "held" && sp.heldReason !== "excluded" && !sp.evict) out.push("held");
  if (sp.anomalyIds.some((id) => !ackedAnomaly.has(id))) out.push("anomaly");
  if (sp.opIds.some((id) => lossSet.has(id))) out.push("loss");
  if (sp.blockedBy.length) out.push("suggestion");
  if (verifyKeys.has(sp.seriesKey)) out.push("needs-verify");
  if (sp.rebind && sp.rebind.fellBack) out.push("rebind-fell-back");
  // A replace candidate is a `replace` op, or the delete of the target's own
  // copy of that chapter (reason `replacement`) the plan left unticked.
  const isCandidate = (op) => op.kind === "replace" || (op.kind === "delete" && op.reason === "replacement");
  if (sp.opIds.some((id) => isCandidate(plan.ops.get(id)) && !selected.get(id))) out.push("replace-unticked");
  return out;
}

/** The selected ops, in plan order (service.js _capacity: space.js simulateRun). */
function selectedOps(plan, selected) {
  const out = [];
  for (const op of plan.ops.values()) if (selected.get(op.id) === true) out.push(op);
  return out;
}

const REVIEW_KIND_ORDER = Object.freeze(["push", "update", "replace", "rename", "delete"]);

/**
 * sync:review — everything the Start review shows, in one payload: every
 * offered op grouped by (kind, reason) with its selection and loss markers,
 * the per-series loss list the user acknowledges one tick per series (UI
 * decision 5: a tick adds that series' `lossOpIds` to apply's ackLosses), the
 * mass flags, and the gone folders whose re-push name is a choice. service.js
 * adds gen, capacity and the planning policy. Group checkboxes call
 * set-selection {bulk:'group', group: kind, reason}.
 */
function planReview(plan, selected) {
  const ev = evaluateSelection(plan, selected);
  const groups = new Map();
  const losses = [];
  const rebinds = [];
  for (const sp of plan.series) {
    const l = seriesLosses(plan, sp, selected);
    const series = sp.title || sp.folder;
    if (l.loss.size) {
      const ids = [...l.loss];
      losses.push({ seriesKey: sp.seriesKey, series, lossOpIds: ids, labels: ids.map((id) => plan.ops.get(id).label).filter((x) => x != null) });
    }
    if (sp.state === "gone" && sp.rebind) rebinds.push({ seriesKey: sp.seriesKey, series, rebind: sp.rebind });
    for (const id of sp.opIds) {
      const op = plan.ops.get(id);
      const key = `${op.kind}\0${op.reason || ""}`;
      let g = groups.get(key);
      if (!g) groups.set(key, (g = { kind: op.kind, reason: op.reason || null, count: 0, selectedCount: 0, bytes: 0, selectedBytes: 0, ops: [] }));
      const sel = selected.get(id) === true;
      const bytes = op.kind === "delete" ? (op.devFile ? op.devFile.size : 0) : op.kind === "rename" ? 0 : op.size || 0;
      g.count += 1;
      g.bytes += bytes;
      if (sel) {
        g.selectedCount += 1;
        g.selectedBytes += bytes;
      }
      g.ops.push({
        opId: id,
        seriesKey: sp.seriesKey,
        series,
        folder: op.folder,
        name: op.name,
        from: op.from,
        to: op.to,
        unit: op.unit,
        label: op.label,
        size: bytes,
        sidecar: !!op.sidecar,
        selected: sel,
        preselected: !!op.preselected,
        loss: l.loss.has(id),
        lossIfSelected: l.lossIfSelected.has(id),
        blockedBy: op.blockedBy || [],
      });
    }
  }
  const list = [...groups.values()].sort(
    (a, b) => REVIEW_KIND_ORDER.indexOf(a.kind) - REVIEW_KIND_ORDER.indexOf(b.kind) || String(a.reason).localeCompare(String(b.reason)),
  );
  return {
    ok: true,
    targetId: plan.targetId,
    asOf: plan.asOf,
    firstSync: plan.firstSync,
    deletePolicy: plan.deletePolicy,
    totals: ev.totals,
    groups: list,
    losses,
    massDelete: ev.massDelete,
    massRepush: ev.massRepush,
    rebinds,
    sanity: plan.sanity.map((x) => x.folder),
    needsVerify: plan.needsVerify.length,
  };
}

module.exports = {
  SETTLE_MS,
  isSettling,
  ADOPT_RATIO,
  MASS_RATIO,
  MASS_MIN,
  MASS_TOTAL_DELETES,
  EVICT_REASON,
  SERIES_STATES,
  HELD_REASONS,
  STRONG_TIERS,
  pcSeriesKey,
  deviceOnlyKey,
  opId,
  renameOpId,
  bindShards,
  goneCandidates,
  isIncluded: _isIncluded,
  inputRefusal,
  buildPlan,
  effectiveSelection,
  seriesLosses,
  evaluateSelection,
  changeSelection,
  pruneDefaultChoices,
  applyRebind,
  reconcileForApply,
  promptSummary,
  promptCard,
  verifyBytesOf,
  libraryStatus,
  chapterStrip,
  seriesDetail,
  planSummary,
  selectedOps,
  planReview,
};
