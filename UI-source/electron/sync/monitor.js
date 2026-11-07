// ============================================================
// DEVICE SYNC — MONITOR (main process)
//
// Owns knowing what is attached, and when to ask the user about it:
//   DeviceTracker  `host:track-devices-l` on one persistent socket. A socket
//                  that fails before its first frame counts as a straight
//                  failure; backoff 2 → 60 s between attempts, and after
//                  TRACK_FAILS_BEFORE_POLL straight failures `host:devices-l`
//                  is also polled every POLL_MS until tracking delivers a
//                  frame again. No adb.exe child: adb/client.js speaks the
//                  protocol (the parent's CLI `track-devices` is gone).
//   FolderWatcher  folder targets: FolderTransport.checkPresence() (3 s
//                  timeout, one in flight per target) every POLL_MS; a
//                  present folder leaves after ABSENT_POLLS_TO_LEAVE misses.
//   PromptMarks    userData/sync/prompted.json: "prompted once per
//                  connection" (parent rule 10) survives an app restart.
//   PromptMachine  rule 10's focus-aware state machine:
//                    connect → quick plan → prompt now if focused, else on
//                    the next focus while still connected; a disconnect
//                    withdraws it. One prompt per device (it lists every
//                    target on that device); one per folder target.
//                  The machine decides only whether a card is open; the
//                  renderer fills it from the live plans (UI-pass U3-3,
//                  sync-temp/plans/device-sync-ui-pass.md). pending() is the
//                  planning list behind its "Preparing" card.
//
// CONNECTION KEYS. adb: `serial#transportId` — the server hands a re-plugged
// device a new transport id, so a re-plug is a new connection. folder:
// `folder:<targetId>`. A mark is stored under `<connKey>#<bootEpoch>`:
// transport ids restart at 1 in every server process (transport.cpp:284-285),
// so after a PC reboot the same small id must not match an old mark
// (review #5). bootEpoch = Date.now() − os.uptime()·1000, to the minute.
// A mark is HONORED FOR MARK_TTL_MS ONLY (second review #9): Windows Fast
// Startup keeps os.uptime() (GetTickCount64) across "Shut down" while the
// server restarts its ids, so without the limit the morning re-plug would
// match yesterday's mark. Marks are deleted when the device leaves and when
// the tracking socket is lost (the server restarted, so ids restart).
//
// Everything that touches time, sockets or disk is injected (client, timers,
// now, bootEpoch, the marks file ops), so tools/_test_device_sync_monitor.js
// runs it against tools/fake-adb-server.js with a fake clock.
//
// Read by: service.js. Depends on: adb/client.js (track, devices; through
// the injected client), store.js (WriteQueue, readJson for the marks file).
// ============================================================

const os = require("os");
const path = require("path");
const { WriteQueue, readJson } = require("./store");

const BACKOFF_MIN_MS = 2000;
const BACKOFF_MAX_MS = 60000;
const TRACK_FAILS_BEFORE_POLL = 3;
const POLL_MS = 5000;
const ABSENT_POLLS_TO_LEAVE = 2;
const MARK_TTL_MS = 30 * 60 * 1000;
const MARKS_FILE = "prompted.json";

/** Date.now() − uptime, rounded to the minute (the mark's reboot guard). */
function bootEpochNow({ now = Date.now, uptime = os.uptime } = {}) {
  return Math.round((now() - uptime() * 1000) / 60000) * 60000;
}

function adbConnKey(row) {
  return `${row.serial}#${row.transportId == null ? "?" : row.transportId}`;
}

function folderConnKey(targetId) {
  return `folder:${targetId}`;
}

// ------------------------------------------------------------ DeviceTracker

class DeviceTracker {
  /**
   * @param {object} d
   * @param {import('./adb/client').AdbClient} d.client
   * @param {() => Promise<{ok:boolean}>} [d.ensureServer]  called before an
   *   attempt that follows a server-unavailable end (service decides whether
   *   a server start is allowed)
   * @param {(rows:object[], info:{source:'track'|'poll'|'lost'}) => void} d.onDevices
   *   every complete device list; `lost` = the tracking socket ended after
   *   it had delivered frames, or a poll failed: rows is [] (unknown)
   * @param {(state:{mode:string, error:string|null, failStreak:number}) => void} [d.onState]
   * @param {{setTimeout:Function, clearTimeout:Function, setInterval:Function, clearInterval:Function}} [d.timers]
   */
  constructor(d) {
    this.client = d.client;
    this.ensureServer = d.ensureServer || null;
    this.onDevices = d.onDevices;
    this.onState = d.onState || (() => {});
    this.timers = d.timers || { setTimeout, clearTimeout, setInterval, clearInterval };
    this.running = false;
    this.handle = null;
    this.failStreak = 0;
    this.retryTimer = null;
    this.pollTimer = null;
    this.polling = false;
    this.pollInFlight = false;
    this.lastErrorKind = null;
    this.mode = "stopped";
    this.error = null;
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.failStreak = 0;
    this._setState("connecting", null);
    this._attempt();
  }

  stop() {
    this.running = false;
    if (this.retryTimer) this.timers.clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this._stopPolling();
    if (this.handle) this.handle.close();
    this.handle = null;
    this._setState("stopped", null);
  }

  /** The delay before attempt number n+1 after n straight failures. */
  static backoffMs(n) {
    return Math.min(BACKOFF_MAX_MS, BACKOFF_MIN_MS * 2 ** Math.max(0, n - 1));
  }

  _setState(mode, error) {
    this.mode = mode;
    this.error = error;
    try {
      this.onState({ mode, error, failStreak: this.failStreak });
    } catch (_) {
      // a listener fault must not stop tracking
    }
  }

  _emit(rows, source) {
    try {
      this.onDevices(rows, { source });
    } catch (_) {
      // a listener fault must not stop tracking
    }
  }

  async _attempt() {
    this.retryTimer = null;
    if (!this.running) return;
    if (this.lastErrorKind === "server-unavailable" && this.ensureServer) {
      try {
        await this.ensureServer();
      } catch (_) {
        // the attempt below reports the failure
      }
      if (!this.running) return;
    }
    let gotFrame = false;
    const handle = this.client.track({
      onDevices: (rows) => {
        if (this.handle !== handle) return;
        if (!gotFrame) {
          gotFrame = true;
          this.failStreak = 0;
          this.lastErrorKind = null;
          this._stopPolling();
          this._setState("tracking", null);
        }
        this._emit(rows, "track");
      },
      onEnd: (err) => {
        if (this.handle !== handle) return;
        this.handle = null;
        if (!this.running) return;
        this.lastErrorKind = (err && err.kind) || "server-unavailable";
        const msg = (err && err.message) || "track ended";
        if (gotFrame) {
          // The socket was live: the server went away or restarted. Devices
          // are unknown until it answers again; retry soon.
          this._emit([], "lost");
          this.failStreak = 0;
          this._setState("connecting", msg);
          this._schedule(BACKOFF_MIN_MS);
          return;
        }
        this.failStreak += 1;
        if (this.failStreak >= TRACK_FAILS_BEFORE_POLL) this._startPolling();
        this._setState(this.polling ? "polling" : "connecting", msg);
        this._schedule(DeviceTracker.backoffMs(this.failStreak));
      },
    });
    this.handle = handle;
  }

  _schedule(ms) {
    if (!this.running || this.retryTimer) return;
    this.retryTimer = this.timers.setTimeout(() => this._attempt(), ms);
  }

  _startPolling() {
    if (this.polling) return;
    this.polling = true;
    this.pollTimer = this.timers.setInterval(() => this._poll(), POLL_MS);
    this._poll();
  }

  _stopPolling() {
    if (this.pollTimer) this.timers.clearInterval(this.pollTimer);
    this.pollTimer = null;
    this.polling = false;
  }

  async _poll() {
    if (!this.polling || this.pollInFlight) return;
    this.pollInFlight = true;
    try {
      const rows = await this.client.devices();
      if (this.polling) this._emit(rows, "poll");
    } catch (e) {
      if (this.polling) {
        this.lastErrorKind = (e && e.kind) || "server-unavailable";
        this._emit([], "lost");
      }
    } finally {
      this.pollInFlight = false;
    }
  }
}

// ------------------------------------------------------------ FolderWatcher

class FolderWatcher {
  /**
   * @param {object} d
   * @param {(targetId:string, present:boolean) => void} d.onChange  only on a change (and the first answer)
   * @param {object} [d.timers]
   */
  constructor(d) {
    this.onChange = d.onChange;
    this.timers = d.timers || { setInterval, clearInterval };
    this.targets = new Map(); // targetId → {transport, present:null|boolean}
    this.timer = null;
  }

  /** Replace the watched set: [{targetId, transport}] (FolderTransport each). */
  setTargets(list) {
    const next = new Map();
    for (const t of list || []) {
      const old = this.targets.get(t.targetId);
      next.set(t.targetId, old && old.transport === t.transport ? old : { transport: t.transport, present: null });
    }
    for (const [id, old] of this.targets) {
      if (!next.has(id) && old.present) this._notify(id, false);
    }
    this.targets = next;
    if (next.size && !this.timer) {
      this.timer = this.timers.setInterval(() => this.checkAll(), POLL_MS);
    } else if (!next.size && this.timer) {
      this.timers.clearInterval(this.timer);
      this.timer = null;
    }
    if (next.size) this.checkAll();
  }

  stop() {
    this.setTargets([]);
  }

  presence() {
    const out = {};
    for (const [id, t] of this.targets) out[id] = t.present;
    return out;
  }

  /** One presence check per target (each transport keeps one in flight). */
  async checkAll() {
    await Promise.all(
      [...this.targets].map(async ([id, t]) => {
        let present = false;
        // checkPresence hands every caller the ONE in-flight check
        // (transports.js FolderTransport), and checkAll runs from both the
        // poll and setTargets: two continuations of one check must count as
        // one answer, or a single slow stat reached ABSENT_POLLS_TO_LEAVE
        // by itself (review low: a config edit during a slow poll
        // "disconnected" the folder).
        let check;
        try {
          check = t.transport.checkPresence();
        } catch (_) {
          check = Promise.resolve({ present: false });
        }
        try {
          present = !!(await check).present;
        } catch (_) {
          present = false;
        }
        const cur = this.targets.get(id);
        if (!cur || cur !== t) return;
        if (t.counted === check) return;
        t.counted = check;
        // UI-pass U3-8: a present folder reads gone only after
        // ABSENT_POLLS_TO_LEAVE misses in a row. checkPresence's 3 s
        // timeout reads absent, and a leave deletes the prompt mark, so one
        // slow poll of a network share used to re-prompt it.
        if (present) t.misses = 0;
        else if (t.present === true && (t.misses = (t.misses || 0) + 1) < ABSENT_POLLS_TO_LEAVE) return;
        if (t.present !== present) {
          t.present = present;
          t.misses = 0;
          this._notify(id, present);
        }
      }),
    );
  }

  _notify(id, present) {
    try {
      this.onChange(id, present);
    } catch (_) {
      // a listener fault must not stop watching
    }
  }
}

// ------------------------------------------------------------ PromptMarks

class PromptMarks {
  /**
   * @param {object} d
   * @param {string} d.dir         userData/sync
   * @param {() => number} [d.now]
   * @param {number} [d.bootEpoch]
   * @param {WriteQueue} [d.queue]
   */
  constructor(d) {
    this.file = path.join(d.dir, MARKS_FILE);
    this.now = d.now || Date.now;
    this.bootEpoch = d.bootEpoch != null ? d.bootEpoch : bootEpochNow();
    this.queue = d.queue || new WriteQueue();
    this.marks = new Map();
    this.loaded = false;
  }

  key(connKey) {
    return `${connKey}#${this.bootEpoch}`;
  }

  async load() {
    if (this.loaded) return;
    this.loaded = true;
    // Missing or corrupt: start empty (worst case one extra prompt).
    const r = await readJson(this.file);
    const v = r.state === "ok" ? r.value : null;
    const marks = v && v.marks && typeof v.marks === "object" ? v.marks : {};
    const t = this.now();
    for (const [k, ts] of Object.entries(marks)) {
      if (Number.isFinite(ts) && t - ts < MARK_TTL_MS) this.marks.set(k, ts);
    }
  }

  /** A mark for this connection, younger than MARK_TTL_MS. */
  honored(connKey) {
    const ts = this.marks.get(this.key(connKey));
    return ts != null && this.now() - ts < MARK_TTL_MS;
  }

  set(connKey) {
    this.marks.set(this.key(connKey), this.now());
    return this._save();
  }

  /** Delete every mark of this connection key (any boot epoch). */
  remove(connKey) {
    let n = 0;
    for (const k of [...this.marks.keys()]) {
      if (k.startsWith(`${connKey}#`)) {
        this.marks.delete(k);
        n += 1;
      }
    }
    return n ? this._save() : Promise.resolve();
  }

  /** Delete every adb mark (the tracking socket was lost: ids restart). */
  removeAdb() {
    let n = 0;
    for (const k of [...this.marks.keys()]) {
      if (!k.startsWith("folder:")) {
        this.marks.delete(k);
        n += 1;
      }
    }
    return n ? this._save() : Promise.resolve();
  }

  _save() {
    const t = this.now();
    const marks = {};
    for (const [k, ts] of this.marks) if (t - ts < MARK_TTL_MS) marks[k] = ts;
    // A failed write costs at most one extra prompt after a restart.
    return this.queue.write(this.file, { version: 1, marks }).catch(() => {});
  }
}

// ------------------------------------------------------------ PromptMachine

class PromptMachine {
  /**
   * @param {object} d
   * @param {PromptMarks} d.marks
   * @param {() => boolean} d.isFocused
   * @param {(ev:{action:'show'|'withdraw', prompt:object, reason?:string}) => void} d.onPrompt
   * @param {(pending:object[]) => void} [d.onPending]  the planning list changed
   *   (UI-pass U3-3: the renderer's "Preparing" card); never fires through
   *   onPrompt, so the show/withdraw sequence stays what it was
   * @param {() => number} [d.now]
   */
  constructor(d) {
    this.marks = d.marks;
    this.isFocused = d.isFocused;
    this.onPrompt = d.onPrompt;
    this.onPending = d.onPending || (() => {});
    this.now = d.now || Date.now;
    this.conns = new Map(); // connKey → {state, serial, targetIds, promptId, since, prompt}
    this.seq = 0;
  }

  /**
   * A connection appeared. Returns true when the caller should run the quick
   * plans (one per target) and report them with planned().
   * States: planning → waiting-focus | shown → answered | resolved;
   * planning → answered (Not now on the Preparing card); marked (an honored
   * mark: no prompt this connection); quiet (nothing to report).
   * The promptId exists from `planning` on (U3-3), so the Preparing card the
   * renderer draws from pending() can be answered before the plans land.
   */
  connected({ connKey, serial = null, targetIds }) {
    if (this.conns.has(connKey)) return false;
    if (this.marks.honored(connKey)) {
      this.conns.set(connKey, { state: "marked", serial, targetIds: [...targetIds], promptId: null, since: this.now(), prompt: null });
      return false;
    }
    this.seq += 1;
    this.conns.set(connKey, { state: "planning", serial, targetIds: [...targetIds], promptId: `p${this.seq}-${this.now()}`, since: this.now(), prompt: null });
    this._pendingChanged();
    return true;
  }

  /**
   * The quick plans finished. `card` is planner.promptCard() over the
   * connection's targets; an empty card (nothing to do anywhere, nothing
   * refused) is quiet, but marked, so the same connection doesn't plan
   * again on every refocus. A connection answered while planning stays
   * silent.
   */
  planned(connKey, card) {
    const c = this.conns.get(connKey);
    if (!c || c.state !== "planning") return null;
    if (!card || card.empty || !((card.targets || []).length || (card.attention || []).length)) {
      c.state = "quiet";
      this.marks.set(connKey);
      this._pendingChanged();
      return null;
    }
    c.prompt = { promptId: c.promptId, connKey, serial: c.serial, targetIds: [...c.targetIds], card, createdAt: this.now() };
    if (this._focused()) this._show(c);
    else c.state = "waiting-focus";
    this._pendingChanged();
    return c.prompt;
  }

  /** The app window gained focus: show what waited for it. */
  focus() {
    for (const c of this.conns.values()) if (c.state === "waiting-focus") this._show(c);
  }

  /** The connection left: withdraw its prompt; its mark goes too. */
  disconnected(connKey) {
    const c = this.conns.get(connKey);
    this.conns.delete(connKey);
    this.marks.remove(connKey);
    if (c && c.prompt && (c.state === "shown" || c.state === "waiting-focus")) {
      this._emit({ action: "withdraw", prompt: c.prompt });
    }
    if (c && c.state === "planning") this._pendingChanged();
  }

  /** Every adb connection is unknown (tracking socket lost). */
  lostAdb() {
    for (const [k, c] of [...this.conns]) if (!k.startsWith("folder:")) this.disconnected(k);
    this.marks.removeAdb();
  }

  /**
   * The user answered (a shown card, or the Preparing card of a connection
   * still planning). Returns the prompt, or null for an unknown/stale id.
   */
  respond(promptId) {
    for (const c of this.conns.values()) {
      if (c.promptId !== promptId) continue;
      if (c.state === "shown") {
        c.state = "answered";
        return c.prompt;
      }
      if (c.state === "planning") {
        const connKey = this._keyOf(c);
        c.state = "answered";
        this.marks.set(connKey);
        this._pendingChanged();
        return { promptId: c.promptId, connKey, serial: c.serial, targetIds: [...c.targetIds], card: null, createdAt: c.since };
      }
    }
    return null;
  }

  /**
   * U3-6: the plans moved on and nothing is left to do on this connection.
   * A shown or waiting card is withdrawn with `reason`; a waiting one is
   * marked like a quiet connection. Returns true when it closed a card.
   */
  resolve(connKey, reason) {
    const c = this.conns.get(connKey);
    if (!c || !c.prompt || (c.state !== "shown" && c.state !== "waiting-focus")) return false;
    if (c.state === "waiting-focus") this.marks.set(connKey);
    c.state = "resolved";
    this._emit({ action: "withdraw", prompt: c.prompt, reason });
    return true;
  }

  /** Connection keys with an open (shown or waiting) card listing targetId. */
  openFor(targetId) {
    const out = [];
    for (const [k, c] of this.conns) if ((c.state === "shown" || c.state === "waiting-focus") && c.targetIds.includes(targetId)) out.push(k);
    return out;
  }

  /** The open card of a connection (shown or waiting), or null. */
  promptOf(connKey) {
    const c = this.conns.get(connKey);
    return c && (c.state === "shown" || c.state === "waiting-focus") ? c.prompt : null;
  }

  /** Shown prompts, oldest first (get-state's `prompt` is the first). */
  shown() {
    return [...this.conns.values()].filter((c) => c.state === "shown").map((c) => c.prompt).sort((a, b) => a.createdAt - b.createdAt);
  }

  /** Connections still planning, oldest first: the renderer's Preparing cards. */
  pending() {
    return [...this.conns]
      .filter(([, c]) => c.state === "planning")
      .map(([k, c]) => ({ promptId: c.promptId, connKey: k, serial: c.serial, targetIds: [...c.targetIds], since: c.since }))
      .sort((a, b) => a.since - b.since);
  }

  pendingConn(connKey) {
    const c = this.conns.get(connKey);
    return c ? c.state : null;
  }

  connKeys() {
    return [...this.conns.keys()];
  }

  _keyOf(c) {
    for (const [k, x] of this.conns) if (x === c) return k;
    return null;
  }

  _focused() {
    try {
      return !!this.isFocused();
    } catch (_) {
      return false;
    }
  }

  _show(c) {
    c.state = "shown";
    this.marks.set(c.prompt.connKey);
    this._emit({ action: "show", prompt: c.prompt });
  }

  _emit(ev) {
    try {
      this.onPrompt(ev);
    } catch (_) {
      // a listener fault must not wedge the machine
    }
  }

  _pendingChanged() {
    try {
      this.onPending(this.pending());
    } catch (_) {
      // a listener fault must not wedge the machine
    }
  }
}

module.exports = {
  BACKOFF_MIN_MS,
  BACKOFF_MAX_MS,
  TRACK_FAILS_BEFORE_POLL,
  POLL_MS,
  ABSENT_POLLS_TO_LEAVE,
  MARK_TTL_MS,
  MARKS_FILE,
  bootEpochNow,
  adbConnKey,
  folderConnKey,
  DeviceTracker,
  FolderWatcher,
  PromptMarks,
  PromptMachine,
};
