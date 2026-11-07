// ============================================================
// DEVICE SYNC — ADB CLIENT (sockets)
//
// Owns every socket to the adb server: host services (version, devices -l,
// features, track-devices-l), transport binding, shell, and the SyncSession
// (stat, list, push, pull) with the session lifecycle the non-UI plan pins
// ("The adb wire client" → "Session lifecycle"). No adb.exe client ever runs:
// Node speaks the server protocol itself (adb/locate.js only runs
// `adb version` and `adb start-server`).
//
// SESSION LIFECYCLE (review #1, second review #8):
//   * one sync session = one socket (host:tport:serial:<s>, then "sync:");
//     adbd serves its requests one at a time and ENDS the session after any
//     FAIL (handle_sync_command returns false), so a FAIL kills the socket
//     here too and the next operation reopens it lazily, free of charge;
//   * only the FAIL itself is charged (AdbError.charges = 1, kind remote-fail
//     unless it is ENOSPC/EROFS);
//   * an EOF/ECONNRESET/EPIPE during an operation on a session believed alive
//     is rechecked against `devices -l` after recheckMs: serial gone or a new
//     transport id → device-lost; still `device` → session-closed, charged 1
//     and retried ONCE on a fresh session. A deterministic drop on one path
//     therefore costs 2 per file and trips the executor's budget instead of
//     looping (executor.js owns the budget; it sums .charges);
//   * pushes are acknowledged one file at a time; while DATA streams the one
//     framed reader of the socket is already waiting for the status, so an
//     early FAIL (ENOSPC mid-file) stops the stream instead of sending the
//     rest of a multi-GB file;
//   * writes honor backpressure (await drain) and reads pause the socket past
//     a high-water mark, so memory stays bounded both ways;
//   * an idle watchdog (default 60 s, per-call override for Verify) counts
//     socket PROGRESS: bytes received, bytes flushed to the kernel, bytes the
//     consumer took. The wait for a push's OKAY gets a drain allowance
//     (delayed_ack, see DRAIN_WINDOW_BYTES). The tracking socket is exempt;
//   * waits on caller code (the PC source, a pull sink) are raced against
//     the socket's end, so a cancel or the watchdog ends them too;
//   * anything that can refuse a device service (abort, a re-attached
//     device) is checked BEFORE the service is sent: a shell command runs as
//     soon as its service opens.
//
// ERRORS: everything thrown at a caller is an AdbError whose `kind` is one of
// ADB_ERROR_KINDS; executor.js maps each kind to its policy (the plan's error
// table). Programming errors (a path over 1,024 bytes, a shell command over
// SHELL_SERVICE_MAX) throw RangeError/TypeError before anything is sent.
//
// Read by: transports.js (AdbTransport), monitor.js (track), service.js
// (list-devices, browse-remote, locate-adb's server check).
// Depends on: adb/wire.js. Tested by tools/_test_device_sync_adb.js against
// tools/fake-adb-server.js.
// ============================================================

const net = require("net");
const crypto = require("crypto");
const wire = require("./wire");

const DEFAULT_PORT = 5037;
const DEFAULTS = Object.freeze({
  idleMs: 60000,
  connectTimeoutMs: 5000,
  // The plan's "~1 s recheck": time for the server to notice an unplug before
  // `devices -l` is trusted to say whether the device is still there.
  recheckMs: 1000,
  maxShellOutput: 64 * 1024 * 1024,
});

// Read side: pause the socket past HIGH, resume under LOW. Both exceed the
// largest exact read (a 64 KiB DATA payload + header), so a pending read can
// never wait on bytes a pause is holding back.
const READ_HIGH_WATER = 1024 * 1024;
const READ_LOW_WATER = 256 * 1024;

// After DONE, adbd may still be writing what the server sent ahead under
// delayed_ack (INITIAL_DELAYED_ACK_BYTES, 32 MiB, adb.h:38, granted at OPEN,
// adb.cpp:540-544) plus the loopback buffers, and nothing this side can see
// moves meanwhile. Under the 60 s watchdog alone, a device draining 32 MiB
// slower than about 0.5 MiB/s would time out. So the OKAY wait gets
// min(sent, DRAIN_WINDOW_BYTES) more at DRAIN_FLOOR_BYTES_PER_SEC: a full
// window (32 MiB + the loopback buffers) then gets 100 s, which holds down to
// about 0.4 MiB/s. P5 measures the real wait on the tablet.
const DRAIN_WINDOW_BYTES = 40 * 1024 * 1024;
const DRAIN_FLOOR_BYTES_PER_SEC = 1024 * 1024;

function _drainAllowanceMs(sentBytes) {
  return Math.ceil((Math.min(sentBytes, DRAIN_WINDOW_BYTES) / DRAIN_FLOOR_BYTES_PER_SEC) * 1000);
}

// One kind per policy row of the plan's error table. record-write-failed is
// thrown by store.js (P3), never by this file; it is listed so the executor
// switches over one set.
const ADB_ERROR_KINDS = Object.freeze([
  "device-lost",
  "session-closed",
  "unauthorized",
  "connecting",
  "offline",
  "server-unavailable",
  "no-space",
  "record-write-failed",
  "read-only",
  "remote-fail",
  "timeout",
  "cancelled",
]);
const _KIND_SET = new Set(ADB_ERROR_KINDS);
const _DEFAULT_CHARGES = { "session-closed": 1, "remote-fail": 1, timeout: 1 };

class AdbError extends Error {
  /**
   * @param {string} kind  one of ADB_ERROR_KINDS
   * @param {string} message
   * @param {{charges?:number, receivedFail?:boolean, failText?:string,
   *          serial?:string, op?:string, path?:string, sent?:boolean, cause?:any}} [extra]
   *   charges: what this failure costs the executor's consecutive-failure
   *   budget (retried attempts included). receivedFail: adbd sent FAIL, so
   *   the session is dead and the reopen is free. sent (shell only): false =
   *   the command never reached the device; true = it may have run, so the
   *   caller re-reads the device before trusting what it planned; null = n/a.
   */
  constructor(kind, message, extra = {}) {
    super(message);
    if (!_KIND_SET.has(kind)) throw new Error(`unknown AdbError kind: ${kind}`);
    this.name = "AdbError";
    this.kind = kind;
    this.charges = extra.charges != null ? extra.charges : _DEFAULT_CHARGES[kind] || 0;
    this.receivedFail = !!extra.receivedFail;
    this.failText = extra.failText != null ? extra.failText : null;
    this.serial = extra.serial || null;
    this.op = extra.op || null;
    this.path = extra.path || null;
    this.sent = extra.sent != null ? !!extra.sent : null;
    if (extra.cause) this.cause = extra.cause;
  }
}

/**
 * A server FAIL text → kind. The texts are acquire_one_transport's
 * (transport.cpp:912-1004): `device 'A06B4A372090333' not found` is the text
 * behind the scripts' 106 cascaded failures.
 */
function classifyHostFail(text) {
  const t = String(text || "");
  if (/^device '.*' not found$/s.test(t)) return "device-lost";
  if (/^no devices(\/emulators)? found$/.test(t) || /^no device with transport id /.test(t)) return "device-lost";
  if (/^device unauthorized/.test(t) || /^insufficient permissions for device/.test(t)) return "unauthorized";
  if (t === "device still connecting" || t === "device still authorizing") return "connecting";
  if (/^device offline/.test(t)) return "offline";
  return "remote-fail";
}

/** An old server answers `host:tport:` with this (sockets.cpp:882). */
function isUnknownServiceFail(text) {
  return /^unknown host service/.test(String(text || ""));
}

/** A sync FAIL text → kind. adbd formats "<what>: strerror(errno)" (:274-276). */
function classifySyncFail(text) {
  const t = String(text || "");
  if (/No space left on device/.test(t)) return "no-space";
  if (/Read-only file system/.test(t)) return "read-only";
  return "remote-fail";
}

/**
 * Where the server listens, as the adb CLI resolves it
 * (client/commandline.cpp:1665-1697): ADB_SERVER_SOCKET (tcp:<port> or
 * tcp:<host>:<port>) wins, else ANDROID_ADB_SERVER_ADDRESS +
 * ANDROID_ADB_SERVER_PORT, else 127.0.0.1:5037. 127.0.0.1 rather than the
 * CLI's "localhost": on Windows "localhost" may resolve to ::1 first, and the
 * server listens on IPv4 loopback.
 * @returns {{host:string, port:number} | {error:string}}
 */
function resolveServerAddress(env = process.env) {
  const sock = env.ADB_SERVER_SOCKET;
  if (sock) {
    const m = /^tcp:(?:(.+):)?(\d+)$/.exec(sock);
    if (!m) return { error: `unsupported ADB_SERVER_SOCKET: ${sock}` };
    const port = Number(m[2]);
    if (!(port >= 1 && port <= 65535)) return { error: `bad port in ADB_SERVER_SOCKET: ${sock}` };
    return { host: m[1] || "127.0.0.1", port };
  }
  let port = DEFAULT_PORT;
  const p = env.ANDROID_ADB_SERVER_PORT;
  if (p != null && String(p).length) {
    if (!/^\d+$/.test(String(p)) || Number(p) < 1 || Number(p) > 65535) {
      return { error: `ANDROID_ADB_SERVER_PORT must be 1-65535: ${p}` };
    }
    port = Number(p);
  }
  return { host: env.ANDROID_ADB_SERVER_ADDRESS || "127.0.0.1", port };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** A host-protocol socket that ended (no FAIL) → AdbError, by why it ended. */
function _hostEnd(e, what, extra = {}) {
  if (e && e.aborted) return new AdbError("cancelled", "cancelled", { ...extra, cause: e });
  if (e && e.timedOut) return new AdbError("timeout", `adb server ${what} timed out`, { ...extra, cause: e });
  return new AdbError("server-unavailable", `adb server closed the connection ${what}`, { ...extra, cause: e });
}

function _pathBytes(path) {
  const b = Buffer.isBuffer(path) ? path : Buffer.from(String(path), "utf8");
  if (b.length > wire.SYNC_PATH_MAX) throw new RangeError(`remote path is ${b.length} bytes (max ${wire.SYNC_PATH_MAX})`);
  if (b.includes(0)) throw new RangeError("remote path contains NUL");
  return b;
}

function _pathText(p) {
  return Buffer.isBuffer(p) ? p.toString("utf8") : String(p);
}

/**
 * A one-shot signal that many short waits can race. or(p, value) settles
 * like p, or with `value` once fire() runs, whichever comes first. Each
 * waiter leaves when its own work settles: racing a long-pending promise per
 * 64 KiB chunk instead would pile one reaction per chunk on it until it
 * settles (per file for a push's status, per session for a socket's end).
 */
class Latch {
  constructor() {
    this.fired = false;
    this._waiters = new Set();
  }

  fire() {
    if (this.fired) return;
    this.fired = true;
    const ws = [...this._waiters];
    this._waiters.clear();
    for (const w of ws) w();
  }

  or(p, value) {
    const work = Promise.resolve(p);
    if (this.fired) {
      work.catch(() => {});
      return Promise.resolve(value);
    }
    return new Promise((resolve, reject) => {
      const w = () => resolve(value);
      this._waiters.add(w);
      work.then(
        (v) => {
          this._waiters.delete(w);
          resolve(v);
        },
        (e) => {
          this._waiters.delete(w);
          reject(e);
        },
      );
    });
  }

  get waiting() {
    return this._waiters.size;
  }
}

const _ENDED = Symbol("ended");

// ---------------------------------------------------------------------------
// Conn: one socket, one framed reader, backpressured writer, idle watchdog.
// ---------------------------------------------------------------------------

/**
 * Wraps a connected socket. read(n) returns exactly n bytes or rejects with a
 * conn-end error ({connEnd:true, code, timedOut, aborted}); only one read may
 * be pending at a time, which is what makes it THE framed reader of the
 * socket. A conn-end error is never shown to callers: SyncSession/AdbDevice
 * turn it into an AdbError.
 */
class Conn {
  constructor(socket) {
    this.socket = socket;
    this.q = new wire.ByteQueue();
    this.ended = false;
    this.err = null;
    this.timedOut = false;
    this.aborted = false;
    this.flushed = 0;
    this.lastProgress = Date.now();
    this._waiter = null;
    this._reading = false;
    this._paused = false;
    this._idleMs = 0;
    this._timer = null;
    this._abortCleanup = null;
    // Fires once the socket is done for (FIN, close, error, destroy): what
    // guard() races caller code against.
    this.endLatch = new Latch();
    socket.setNoDelay(true);
    socket.on("data", (c) => {
      this.q.push(c);
      this._progress();
      if (!this._paused && this.q.length > READ_HIGH_WATER) {
        this._paused = true;
        socket.pause();
      }
      this._wake();
    });
    socket.on("end", () => this._end());
    socket.on("close", () => this._end());
    socket.on("error", (e) => {
      if (!this.err) this.err = e;
      this._end();
    });
  }

  _end() {
    this.ended = true;
    this.endLatch.fire();
    this._wake();
  }

  get alive() {
    return !this.ended && !this.socket.destroyed;
  }

  _progress() {
    this.lastProgress = Date.now();
  }

  _wake() {
    const w = this._waiter;
    this._waiter = null;
    if (w) w();
  }

  endError() {
    const why = this.aborted ? "aborted" : this.timedOut ? "idle timeout" : this.err ? this.err.message : "EOF";
    const e = new Error(`adb socket ended: ${why}`);
    e.connEnd = true;
    e.code = this.err && this.err.code ? this.err.code : "EOF";
    e.timedOut = this.timedOut;
    e.aborted = this.aborted;
    return e;
  }

  _maybeResume() {
    if (this._paused && this.q.length < READ_LOW_WATER) {
      this._paused = false;
      this.socket.resume();
    }
  }

  async read(n) {
    if (this._reading) throw new Error("Conn.read: a read is already pending");
    this._reading = true;
    try {
      while (this.q.length < n) {
        if (this.ended) throw this.endError();
        this._maybeResume();
        await new Promise((r) => {
          this._waiter = r;
        });
      }
      const b = this.q.take(n);
      this._progress();
      this._maybeResume();
      return b;
    } finally {
      this._reading = false;
    }
  }

  /** Everything until the peer closes (shell output); capped. */
  async readToEnd(max) {
    const parts = [];
    let total = 0;
    for (;;) {
      if (this.q.length) {
        const b = this.q.take(this.q.length);
        total += b.length;
        if (total > max) throw new RangeError(`adb output over ${max} bytes`);
        parts.push(b);
        this._progress();
        this._maybeResume();
        continue;
      }
      if (this.ended) {
        // A clean FIN is the normal end of a shell stream; a reset or a
        // watchdog/abort kill is not.
        if (this.err || this.timedOut || this.aborted) throw this.endError();
        return Buffer.concat(parts);
      }
      this._maybeResume();
      await new Promise((r) => {
        this._waiter = r;
      });
    }
  }

  /**
   * Write one or more buffers; resolves once the socket accepts more (drain),
   * rejects with a conn-end error if the socket dies first. Flushed bytes
   * (write callbacks) are what feed the watchdog.
   */
  write(bufs) {
    const list = Array.isArray(bufs) ? bufs : [bufs];
    if (!this.alive) return Promise.reject(this.endError());
    return new Promise((resolve, reject) => {
      const s = this.socket;
      let ok = true;
      s.cork();
      for (const b of list) {
        const len = b.length;
        ok = s.write(b, (err) => {
          if (!err) {
            this.flushed += len;
            this._progress();
          }
        });
      }
      s.uncork();
      if (ok) {
        resolve();
        return;
      }
      const done = (fn) => {
        s.off("drain", onDrain);
        s.off("close", onClose);
        fn();
      };
      const onDrain = () => done(resolve);
      const onClose = () => done(() => reject(this.endError()));
      s.on("drain", onDrain);
      s.on("close", onClose);
    });
  }

  /**
   * Await caller code (a PC source read, a sink) without outliving the
   * socket: rejects with the conn-end error once the socket ends, so a cancel
   * or the watchdog still ends the operation while that code is stuck (a
   * sleeping USB disk, a dead network share).
   */
  async guard(p) {
    const v = await this.endLatch.or(p, _ENDED);
    if (v === _ENDED) throw this.endError();
    return v;
  }

  /** Arm the idle watchdog (0 = off) and bind an AbortSignal for one operation. */
  arm(idleMs, signal) {
    this.disarm();
    this._idleMs = idleMs || 0;
    this._progress();
    this._schedule();
    if (signal) {
      const onAbort = () => this.abort();
      if (signal.aborted) onAbort();
      else {
        signal.addEventListener("abort", onAbort, { once: true });
        this._abortCleanup = () => signal.removeEventListener("abort", onAbort);
      }
    }
  }

  /** Change the armed idle budget mid-operation (the abort binding stays). */
  setIdle(idleMs) {
    this._idleMs = idleMs || 0;
    this._schedule();
  }

  get idleMs() {
    return this._idleMs;
  }

  disarm() {
    if (this._timer) clearTimeout(this._timer);
    this._timer = null;
    this._idleMs = 0;
    if (this._abortCleanup) this._abortCleanup();
    this._abortCleanup = null;
  }

  _schedule() {
    if (this._timer) clearTimeout(this._timer);
    this._timer = null;
    if (!this._idleMs) return;
    const due = this.lastProgress + this._idleMs - Date.now();
    this._timer = setTimeout(
      () => {
        this._timer = null;
        if (!this._idleMs) return;
        if (Date.now() - this.lastProgress >= this._idleMs) {
          this.timedOut = true;
          this.destroy();
        } else this._schedule();
      },
      Math.max(1, due),
    );
  }

  destroy() {
    if (!this.socket.destroyed) this.socket.destroy();
    this._end();
  }

  /** destroy() as a cancel: pending work reads as `aborted` (kind cancelled). */
  abort() {
    this.aborted = true;
    this.destroy();
  }

  // --- server-protocol reads ---

  /** "OKAY" → {ok:true}; "FAIL"+string → {ok:false, message}; else a protocol fault. */
  async readHostStatus() {
    const s = (await this.read(4)).toString("latin1");
    if (s === "OKAY") return { ok: true };
    if (s === "FAIL") return { ok: false, message: (await this.readProtocolString()).toString("utf8") };
    throw new AdbError("remote-fail", `adb protocol fault (status ${JSON.stringify(s)})`);
  }

  async readProtocolString() {
    const len = wire.parseHex4(await this.read(4));
    if (len == null) throw new AdbError("remote-fail", "adb protocol fault (bad length)");
    return this.read(len);
  }
}

// ---------------------------------------------------------------------------
// AdbClient: host services. Stateless apart from the tport capability.
// ---------------------------------------------------------------------------

class AdbClient {
  /**
   * @param {object} [opts]
   * @param {object} [opts.env]   for resolveServerAddress (default process.env)
   * @param {string} [opts.host]  overrides env
   * @param {number} [opts.port]  overrides env
   * @param {number} [opts.idleMs]
   * @param {number} [opts.connectTimeoutMs]
   * @param {number} [opts.recheckMs]
   */
  constructor(opts = {}) {
    const addr = opts.host || opts.port ? { host: opts.host || "127.0.0.1", port: opts.port || DEFAULT_PORT } : resolveServerAddress(opts.env || process.env);
    this.addressError = addr.error || null;
    this.host = addr.host || "127.0.0.1";
    this.port = addr.port || DEFAULT_PORT;
    this.idleMs = opts.idleMs != null ? opts.idleMs : DEFAULTS.idleMs;
    this.connectTimeoutMs = opts.connectTimeoutMs || DEFAULTS.connectTimeoutMs;
    this.recheckMs = opts.recheckMs != null ? opts.recheckMs : DEFAULTS.recheckMs;
    // null = untested; false after an old server FAILs host:tport: as an
    // unknown service (then host:transport: is used, ids from devices -l).
    this.tportSupported = null;
  }

  /** A connected Conn, or server-unavailable (ECONNREFUSED and friends). */
  connect() {
    if (this.addressError) return Promise.reject(new AdbError("server-unavailable", this.addressError));
    return new Promise((resolve, reject) => {
      const socket = net.connect({ host: this.host, port: this.port });
      const timer = setTimeout(() => {
        socket.destroy();
        reject(new AdbError("server-unavailable", `adb server at ${this.host}:${this.port} did not answer in ${this.connectTimeoutMs} ms`));
      }, this.connectTimeoutMs);
      socket.once("connect", () => {
        clearTimeout(timer);
        socket.removeAllListeners("error");
        resolve(new Conn(socket));
      });
      socket.once("error", (e) => {
        clearTimeout(timer);
        reject(new AdbError("server-unavailable", `adb server at ${this.host}:${this.port}: ${e.code || e.message}`, { cause: e }));
      });
    });
  }

  /**
   * One request on a fresh socket, OKAY expected. Returns the open Conn so the
   * caller can read the payload. A FAIL becomes an AdbError by the host-fail
   * table; an EOF before the status means the server went away.
   */
  async _request(service, { idleMs, signal } = {}) {
    const conn = await this.connect();
    conn.arm(idleMs != null ? idleMs : this.idleMs, signal);
    try {
      await conn.write(wire.encodeHostRequest(service));
      const st = await conn.readHostStatus();
      if (!st.ok) {
        conn.destroy();
        throw new AdbError(classifyHostFail(st.message), st.message, { failText: st.message });
      }
      return conn;
    } catch (e) {
      conn.disarm();
      conn.destroy();
      if (e instanceof AdbError) throw e;
      throw _hostEnd(e, `during ${service}`);
    }
  }

  async _query(service) {
    const conn = await this._request(service);
    try {
      return await conn.readProtocolString();
    } catch (e) {
      if (e instanceof AdbError) throw e;
      throw _hostEnd(e, `during ${service}`);
    } finally {
      conn.disarm();
      conn.destroy();
    }
  }

  /** The server's ADB_SERVER_VERSION (41 for platform-tools 1.0.41). */
  async version() {
    const s = (await this._query("host:version")).toString("latin1");
    const v = parseInt(s, 16);
    if (!Number.isFinite(v)) throw new AdbError("remote-fail", `bad version reply ${JSON.stringify(s)}`);
    return v;
  }

  /** `devices -l` rows (wire.parseDevicesL). */
  async devices() {
    return wire.parseDevicesL((await this._query("host:devices-l")).toString("utf8"));
  }

  /** The device's feature set (shell_v2, stat_v2, ls_v2, …). */
  async features(serial) {
    return wire.parseFeatures((await this._query(`host-serial:${serial}:features`)).toString("utf8"));
  }

  /**
   * Persistent `host:track-devices-l`. Never times out and never writes after
   * the request (the server closes a tracker it receives data on,
   * transport.cpp device_tracker_enqueue). Monitor.js owns backoff and the
   * polling fallback.
   * @param {{onDevices:(rows:object[])=>void, onEnd:(err:AdbError|null)=>void}} handlers
   * @returns {{close:()=>void}}
   */
  track(handlers) {
    let socket = null;
    let closed = false;
    let ended = false;
    const end = (err) => {
      if (ended) return;
      ended = true;
      if (socket && !socket.destroyed) socket.destroy();
      handlers.onEnd(closed ? null : err);
    };
    this.connect().then(
      (conn) => {
        socket = conn.socket;
        socket.removeAllListeners("data");
        socket.removeAllListeners("end");
        socket.removeAllListeners("close");
        socket.removeAllListeners("error");
        if (closed) {
          socket.destroy();
          end(null);
          return;
        }
        const head = new wire.ByteQueue();
        let splitter = null;
        socket.on("data", (chunk) => {
          try {
            if (!splitter) {
              head.push(chunk);
              if (head.length < 4) return;
              const status = head.peek(4).toString("latin1");
              if (status === "FAIL") {
                // FAIL + protocol string; wait for all of it.
                if (head.length < 8) return;
                const len = wire.parseHex4(head.peek(8).subarray(4));
                if (len == null || head.length < 8 + len) return;
                const msg = head.take(8 + len).subarray(8).toString("utf8");
                end(new AdbError(classifyHostFail(msg), msg, { failText: msg }));
                return;
              }
              if (status !== "OKAY") {
                end(new AdbError("remote-fail", `adb protocol fault (track status ${JSON.stringify(status)})`));
                return;
              }
              head.take(4);
              splitter = new wire.FrameSplitter();
              chunk = head.length ? head.take(head.length) : Buffer.alloc(0);
            }
            for (const frame of splitter.push(chunk)) handlers.onDevices(wire.parseDevicesL(frame.toString("utf8")));
          } catch (e) {
            end(new AdbError("remote-fail", `track-devices: ${e.message}`, { cause: e }));
          }
        });
        const onGone = (e) => end(new AdbError("server-unavailable", `track-devices ended: ${(e && (e.code || e.message)) || "EOF"}`, { cause: e }));
        socket.on("error", onGone);
        socket.on("close", () => onGone(null));
        socket.write(wire.encodeHostRequest("host:track-devices-l"));
      },
      (e) => end(e),
    );
    return {
      close() {
        closed = true;
        if (socket && !socket.destroyed) socket.destroy();
      },
    };
  }

  /**
   * Bind a fresh socket to the device and open a device service on it.
   * `host:tport:serial:<s>` answers OKAY + the 8-byte transport id
   * (adb.cpp:1303-1345). Only an UNKNOWN-SERVICE FAIL falls back to
   * `host:transport:<s>` (id from `devices -l`); `device '…' not found` is a
   * device-lost, never a reason to retry differently.
   *
   * Everything that can refuse the open runs BEFORE the service is sent,
   * because adbd runs a shell command (rm, mv) as soon as its service opens:
   *   * `pinnedTransportId`: a bound id other than this one is a re-attached
   *     device (device-lost);
   *   * `signal`: an abort during the bind is `cancelled`, sent:false.
   * Errors carry `sent`: false until the service is written; after that,
   * without a FAIL, the command may have started (true).
   * @param {{pinnedTransportId?:number|null, signal?:AbortSignal}} [opts]
   * @returns {Promise<{conn:Conn, transportId:number|null}>}
   */
  async openService(serial, service, { pinnedTransportId = null, signal } = {}) {
    if (!serial) throw new TypeError("openService: serial required");
    const reattached = (to) => new AdbError("device-lost", `${serial} was re-attached (transport ${pinnedTransportId} → ${to})`, { serial, sent: false });
    let bound = null;
    if (this.tportSupported !== false) {
      const conn = await this.connect();
      conn.arm(this.idleMs, signal);
      try {
        await conn.write(wire.encodeHostRequest(`host:tport:serial:${serial}`));
        const st = await conn.readHostStatus();
        if (st.ok) {
          this.tportSupported = true;
          bound = { conn, transportId: u64(await conn.read(8)) };
        } else {
          conn.destroy();
          if (!isUnknownServiceFail(st.message)) {
            throw new AdbError(classifyHostFail(st.message), st.message, { failText: st.message, serial });
          }
          this.tportSupported = false;
        }
      } catch (e) {
        conn.disarm();
        conn.destroy();
        if (e instanceof AdbError) {
          if (e.sent == null) e.sent = false;
          throw e;
        }
        throw _hostEnd(e, `binding ${serial}`, { serial, sent: false });
      }
    }
    if (!bound) {
      // An old server: no id comes back with the bind, so the pin is checked
      // against `devices -l` first. A replug between the two requests can
      // still slip through; tport closes that window on every current server.
      const rows = await this.devices();
      const row = rows.find((r) => r.serial === serial);
      if (pinnedTransportId != null) {
        if (!row) throw new AdbError("device-lost", `${serial} disconnected`, { serial, sent: false });
        if (row.transportId != null && row.transportId !== pinnedTransportId) throw reattached(row.transportId);
      }
      let conn;
      try {
        conn = await this._request(`host:transport:${serial}`, { signal });
      } catch (e) {
        if (e instanceof AdbError && e.sent == null) e.sent = false;
        throw e;
      }
      bound = { conn, transportId: row ? row.transportId : null };
    }
    const { conn } = bound;
    if (pinnedTransportId != null && bound.transportId != null && bound.transportId !== pinnedTransportId) {
      conn.disarm();
      conn.destroy();
      throw reattached(bound.transportId);
    }
    if (signal && signal.aborted) {
      conn.disarm();
      conn.destroy();
      throw new AdbError("cancelled", "cancelled", { serial, sent: false });
    }
    // From the first byte of the service on, the command may reach adbd, so
    // any failure below without a FAIL status is sent:true.
    try {
      await conn.write(wire.encodeHostRequest(service));
      const st = await conn.readHostStatus();
      if (!st.ok) {
        conn.destroy();
        // "closed": the device side refused or dropped the stream before it
        // was ready (local_socket_close_notify, sockets.cpp:605-611). A FAIL
        // means the service never started.
        const err = new AdbError(classifyHostFail(st.message), `${service}: ${st.message}`, { failText: st.message, serial, sent: false });
        err.serviceRefused = st.message === "closed";
        throw err;
      }
      conn.disarm();
      return bound;
    } catch (e) {
      conn.disarm();
      conn.destroy();
      if (e instanceof AdbError) {
        if (e.sent == null) e.sent = true;
        throw e;
      }
      e.transportId = bound.transportId;
      e.sent = true;
      throw e; // conn-end: the caller rechecks the device
    }
  }

  /** A handle that pins one device's transport id for the length of a run. */
  device(serial, opts = {}) {
    return new AdbDevice(this, serial, opts);
  }
}

function u64(buf) {
  const v = buf.readBigUInt64LE(0);
  return v <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(v) : null;
}

// ---------------------------------------------------------------------------
// AdbDevice: one serial for one run. Pins the transport id: the same serial
// coming back with a new id is a removal plus an add, i.e. device-lost.
// ---------------------------------------------------------------------------

class AdbDevice {
  constructor(client, serial, opts = {}) {
    this.client = client;
    this.serial = serial;
    this.transportId = opts.transportId != null ? opts.transportId : null;
    this.idleMs = opts.idleMs != null ? opts.idleMs : client.idleMs;
    this.recheckMs = opts.recheckMs != null ? opts.recheckMs : client.recheckMs;
    this._features = null;
  }

  async features() {
    if (!this._features) this._features = await this.client.features(this.serial);
    return this._features;
  }

  /**
   * Open a device service. The first bind pins the transport id; later binds
   * to another id (a re-attached device) are refused before the service is
   * sent (openService). Errors keep openService's `sent`.
   */
  async open(service, { signal } = {}) {
    let bound;
    try {
      bound = await this.client.openService(this.serial, service, { pinnedTransportId: this.transportId, signal });
    } catch (e) {
      if (e instanceof AdbError) {
        if (!e.serviceRefused) throw e;
        const r = await this._refused(e);
        r.sent = false;
        throw r;
      }
      const r = await this.classifyEnd(e);
      r.sent = !!e.sent;
      throw r;
    }
    if (this.transportId == null && bound.transportId != null) this.transportId = bound.transportId;
    return bound.conn;
  }

  async _refused(e) {
    const st = await this._state();
    if (st.kind === "alive") return e; // the device refused this service
    return st.error;
  }

  /** devices -l after recheckMs → {kind:'alive'} or {error: AdbError}. */
  async _state() {
    if (this.recheckMs) await sleep(this.recheckMs);
    let rows;
    try {
      rows = await this.client.devices();
    } catch (e) {
      return { error: e instanceof AdbError ? e : new AdbError("server-unavailable", String(e && e.message), { cause: e }) };
    }
    const row = rows.find((r) => r.serial === this.serial);
    const lost = (why) => ({ error: new AdbError("device-lost", `${this.serial} ${why}`, { serial: this.serial }) });
    if (!row) return lost("disconnected");
    if (this.transportId != null && row.transportId != null && row.transportId !== this.transportId) {
      return lost(`was re-attached (transport ${this.transportId} → ${row.transportId})`);
    }
    switch (row.stateKind) {
      case "device":
        return { kind: "alive" };
      case "unauthorized":
      case "no-permissions":
        return { error: new AdbError("unauthorized", `${this.serial} is ${row.state}`, { serial: this.serial }) };
      case "connecting":
      case "authorizing":
        return { error: new AdbError("connecting", `${this.serial} is ${row.state}`, { serial: this.serial }) };
      default:
        return { error: new AdbError("offline", `${this.serial} is ${row.state}`, { serial: this.serial }) };
    }
  }

  /**
   * A socket that ended without a FAIL → AdbError. Abort and watchdog kills
   * are decided locally; anything else is rechecked against `devices -l`
   * (an EOF while the serial is still a `device` is session-closed).
   */
  async classifyEnd(e, ctx = {}) {
    const extra = { serial: this.serial, op: ctx.op, path: ctx.path, cause: e };
    if (e && e.aborted) return new AdbError("cancelled", "cancelled", extra);
    if (e && e.timedOut) return new AdbError("timeout", `no progress for ${ctx.idleMs || this.idleMs} ms${ctx.op ? ` (${ctx.op})` : ""}`, extra);
    const st = await this._state();
    if (st.kind === "alive") return new AdbError("session-closed", `adb session closed (${(e && e.code) || "EOF"})${ctx.op ? ` during ${ctx.op}` : ""}`, extra);
    return st.error;
  }

  /**
   * Run a shell command; rc-checked by the caller (exitCode). shell_v2 when
   * the device has it: stdout/stderr apart, the exit code from its packet,
   * and CloseStdin sent first (client/commandline.cpp:546). Without it, the
   * legacy `shell:` stream merges stderr into stdout and may use CRLF, so the
   * exit code rides on a per-call sentinel echoed after the command and CRLF
   * is normalized. The sentinel's echo follows a NEWLINE, not "; ": a
   * command that ends in ";" (or a comment) must not turn into a syntax error
   * that swallows the exit code. Never retried: shell commands (mv, rm)
   * aren't idempotent.
   *
   * An abort is honored up to the moment the service is sent. Every AdbError
   * carries `sent`: false = the command never reached the device; true =
   * it may have run (the caller re-reads the device before trusting it).
   * @returns {Promise<{stdout:Buffer, stderr:Buffer, exitCode:number, v2:boolean}>}
   */
  async shell(cmd, { idleMs, signal, maxOutput } = {}) {
    let feats;
    try {
      feats = await this.features();
    } catch (e) {
      if (e instanceof AdbError) e.sent = false;
      throw e;
    }
    const v2 = feats.has("shell_v2");
    const sentinel = `__AIO_RC_${crypto.randomBytes(6).toString("hex")}__`;
    const service = v2 ? `shell,v2,raw:${cmd}` : `shell:${cmd}\necho "${sentinel}$?"`;
    if (Buffer.byteLength(service) > wire.SHELL_SERVICE_MAX) {
      throw new RangeError(`shell service is ${Buffer.byteLength(service)} bytes (max ${wire.SHELL_SERVICE_MAX}); chunk the command`);
    }
    if (signal && signal.aborted) throw new AdbError("cancelled", "cancelled", { serial: this.serial, op: "shell", sent: false });
    const conn = await this.open(service, { signal });
    const idle = idleMs != null ? idleMs : this.idleMs;
    conn.arm(idle, signal);
    try {
      if (v2) await conn.write(wire.encodeShellPacket(wire.SHELL_ID.CLOSE_STDIN));
      const raw = await conn.readToEnd(maxOutput || DEFAULTS.maxShellOutput);
      return v2 ? _parseShellV2(raw) : _parseShellLegacy(raw, sentinel);
    } catch (e) {
      if (e instanceof RangeError) {
        e.sent = true;
        throw e;
      }
      let err = e;
      if (!(e instanceof AdbError)) err = await this.classifyEnd(e && e.noExit ? { code: "EOF" } : e, { op: "shell", idleMs: idle });
      err.sent = true;
      throw err;
    } finally {
      conn.disarm();
      conn.destroy();
    }
  }

  /** A sync session on this device (opened lazily, reopened after a FAIL). */
  sync(opts = {}) {
    return new SyncSession(this, opts);
  }
}

function _parseShellV2(raw) {
  const demux = new wire.ShellDemuxer();
  const out = [];
  const err = [];
  let exitCode = null;
  for (const p of demux.push(raw)) {
    if (p.id === wire.SHELL_ID.STDOUT) out.push(p.data);
    else if (p.id === wire.SHELL_ID.STDERR) err.push(p.data);
    else if (p.id === wire.SHELL_ID.EXIT) exitCode = p.data.length ? p.data[0] : 0;
  }
  if (exitCode == null) {
    const e = new Error("shell ended without an exit packet");
    e.noExit = true;
    throw e;
  }
  return { stdout: Buffer.concat(out), stderr: Buffer.concat(err), exitCode, v2: true };
}

function _crlfToLf(buf) {
  if (!buf.includes(13)) return buf;
  const out = Buffer.allocUnsafe(buf.length);
  let j = 0;
  for (let i = 0; i < buf.length; i += 1) {
    if (buf[i] === 13 && buf[i + 1] === 10) continue;
    out[j++] = buf[i];
  }
  return out.subarray(0, j);
}

function _parseShellLegacy(raw, sentinel) {
  const text = _crlfToLf(raw);
  const s = Buffer.from(sentinel, "latin1");
  const at = text.lastIndexOf(s);
  if (at === -1) {
    const e = new Error("legacy shell ended before its exit-code sentinel");
    e.noExit = true;
    throw e;
  }
  const m = /^(\d+)/.exec(text.subarray(at + s.length, at + s.length + 8).toString("latin1"));
  if (!m) {
    const e = new Error("legacy shell sentinel without an exit code");
    e.noExit = true;
    throw e;
  }
  return { stdout: text.subarray(0, at), stderr: Buffer.alloc(0), exitCode: Number(m[1]), v2: false };
}

// ---------------------------------------------------------------------------
// SyncSession
// ---------------------------------------------------------------------------

/**
 * stat/list/push/pull over one `sync:` socket, serialized. See the file
 * header for the lifecycle. Results:
 *   stat → {ok, error, mode, type, size, mtime}. STA2 never FAILs: a stat
 *          error arrives in `error` (errno) with the rest zeroed. On a v1
 *          device (no stat_v2) STAT is an lstat and reports mode 0 on any
 *          error, so ok=false carries error -1 (cause unknown).
 *   list → {entries:[{raw,name,display,mode,type,size,mtime}],
 *           errored:[{raw,name,display,error}]}. `.`/`..` are dropped. A
 *          missing, unreadable or non-directory path answers DONE only, i.e.
 *          looks EMPTY: callers STA2 a folder before trusting an empty
 *          listing (transports.js). LIS2 entries whose lstat failed come back
 *          in `errored`, never as files.
 *   push → {bytes, sha256} of what was actually sent (hash-while-push).
 *   pull → {size, sha256, data?}.
 */
class SyncSession {
  /**
   * @param {AdbDevice} device
   * @param {{idleMs?:number, onCharge?:(err:AdbError)=>void}} [opts]
   *   onCharge: called for each charge as it happens, including the charge of
   *   an attempt that a retry then rescued (logging; the thrown error's
   *   .charges is the op's total).
   */
  constructor(device, opts = {}) {
    this.device = device;
    this.idleMs = opts.idleMs != null ? opts.idleMs : device.idleMs;
    this.onCharge = opts.onCharge || null;
    this.conn = null;
    this.opens = 0;
    this.destroyed = false;
    this._chain = Promise.resolve();
    this._v2 = null;
  }

  _cancelled(op, path) {
    return new AdbError("cancelled", "cancelled", { serial: this.device.serial, op, path: _pathText(path) });
  }

  async _caps() {
    if (!this._v2) {
      const f = await this.device.features();
      this._v2 = { stat: f.has("stat_v2"), list: f.has("ls_v2") };
    }
    return this._v2;
  }

  _serial(fn) {
    const run = this._chain.then(fn, fn);
    this._chain = run.then(
      () => {},
      () => {},
    );
    return run;
  }

  _kill() {
    if (this.conn) this.conn.destroy();
    this.conn = null;
  }

  /**
   * Run one operation with the lifecycle rules. `fn(conn)` does the protocol;
   * a FAIL it reads comes back as an AdbError (receivedFail) and kills the
   * session; a conn-end is rechecked, charged and retried once when `retry`.
   */
  _op(op, path, fn, { retry = true, signal, idleMs } = {}) {
    return this._serial(async () => {
      let charges = 0;
      for (let attempt = 0; ; attempt += 1) {
        if ((signal && signal.aborted) || this.destroyed) throw this._cancelled(op, path);
        let conn = null;
        try {
          if (!this.conn || !this.conn.alive) {
            this.conn = null;
            const fresh = await this.device.open("sync:", { signal });
            // destroy() while the bind was in flight: don't revive the session.
            if (this.destroyed) {
              fresh.destroy();
              throw this._cancelled(op, path);
            }
            this.conn = fresh;
            this.opens += 1;
          }
          conn = this.conn;
          conn.arm(idleMs != null ? idleMs : this.idleMs, signal);
          const r = await fn(conn);
          conn.disarm();
          return r;
        } catch (e) {
          if (conn) conn.disarm();
          this._kill();
          if (e instanceof AdbError) {
            e.charges += charges;
            if (!e.path) e.path = _pathText(path);
            if (!e.op) e.op = op;
            throw e;
          }
          if (!e || !e.connEnd) throw e; // a bug or a RangeError: not a device fault
          const err = await this.device.classifyEnd(e, { op, path: _pathText(path), idleMs: idleMs || this.idleMs });
          if (err.kind !== "session-closed") {
            err.charges += charges;
            throw err;
          }
          charges += 1;
          if (this.onCharge) this.onCharge(err);
          if (!retry || attempt >= 1) {
            err.charges = charges;
            throw err;
          }
        }
      }
    });
  }

  /** Read one sync reply header; a FAIL becomes an AdbError (receivedFail). */
  async _head(conn, expect) {
    const head = await conn.read(wire.SYNC_MSG_SIZE);
    const m = wire.decodeSyncMsg(head);
    if (m.id === wire.ID.FAIL) {
      const text = (await conn.read(m.value)).toString("utf8");
      throw new AdbError(classifySyncFail(text), text, { receivedFail: true, failText: text, serial: this.device.serial });
    }
    if (expect && !expect.includes(m.id)) {
      throw new AdbError("remote-fail", `sync protocol fault: got ${JSON.stringify(m.id)}, expected ${expect.join("/")}`, { serial: this.device.serial });
    }
    return { head, ...m };
  }

  stat(path, opts = {}) {
    const p = _pathBytes(path);
    return this._op(
      "stat",
      p,
      async (conn) => {
        const caps = await this._caps();
        if (caps.stat) {
          await conn.write(wire.encodeSyncRequest(wire.ID.STA2, p));
          const h = await this._head(conn, [wire.ID.STA2]);
          const r = wire.decodeStatV2(Buffer.concat([h.head, await conn.read(wire.STAT_V2_SIZE - wire.SYNC_MSG_SIZE)]));
          return { ok: r.error === 0, error: r.error, mode: r.mode, type: wire.modeType(r.mode), size: r.size, mtime: r.mtime };
        }
        await conn.write(wire.encodeSyncRequest(wire.ID.STAT, p));
        const h = await this._head(conn, [wire.ID.STAT]);
        const r = wire.decodeStatV1(Buffer.concat([h.head, await conn.read(wire.STAT_V1_SIZE - wire.SYNC_MSG_SIZE)]));
        return { ok: r.mode !== 0, error: r.mode !== 0 ? 0 : -1, mode: r.mode, type: wire.modeType(r.mode), size: r.size, mtime: r.mtime };
      },
      opts,
    );
  }

  list(path, opts = {}) {
    const p = _pathBytes(path);
    return this._op(
      "list",
      p,
      async (conn) => {
        const caps = await this._caps();
        const v2 = caps.list;
        const dentSize = v2 ? wire.DENT_V2_SIZE : wire.DENT_V1_SIZE;
        await conn.write(wire.encodeSyncRequest(v2 ? wire.ID.LIS2 : wire.ID.LIST, p));
        const entries = [];
        const errored = [];
        for (;;) {
          const h = await this._head(conn, [v2 ? wire.ID.DNT2 : wire.ID.DENT, wire.ID.DONE]);
          // Read the FULL record before acting on the id: DONE is a whole
          // zeroed dent (76 or 20 bytes), not an 8-byte message.
          const rec = Buffer.concat([h.head, await conn.read(dentSize - wire.SYNC_MSG_SIZE)]);
          if (h.id === wire.ID.DONE) break;
          const d = v2 ? wire.decodeDentV2(rec) : wire.decodeDentV1(rec);
          const raw = await conn.read(d.namelen);
          if ((raw.length === 1 && raw[0] === 0x2e) || (raw.length === 2 && raw[0] === 0x2e && raw[1] === 0x2e)) continue;
          const nm = wire.decodeName(raw);
          if (v2 && d.error) {
            errored.push({ ...nm, error: d.error });
            continue;
          }
          entries.push({ ...nm, mode: d.mode, type: wire.modeType(d.mode), size: d.size, mtime: d.mtime });
        }
        return { entries, errored };
      },
      opts,
    );
  }

  /**
   * SEND one file and wait for its OKAY/FAIL before returning.
   * @param {object} o
   * @param {string|Buffer} o.remotePath  ≤ wire.REMOTE_PATH_MAX bytes
   * @param {() => (Buffer|AsyncIterable<Buffer>|Promise<Buffer|AsyncIterable<Buffer>>)} o.open
   *   called once per attempt (a retry re-reads the source from the start)
   * @param {number} [o.mode]   st_mode bits for SEND v1 (default 0o100644)
   * @param {number} [o.mtime]  seconds, carried by DONE (adbd lutimes after OKAY;
   *                            nothing in the engine relies on it, see the plan)
   * @param {(bytesSent:number)=>void} [o.onProgress]  file bytes the socket accepted
   * @param {AbortSignal} [o.signal]
   */
  push(o) {
    const p = _pathBytes(o.remotePath);
    const mode = o.mode != null ? o.mode : wire.SEND_MODE_DEFAULT;
    // Built now so an over-long spec throws before anything is sent.
    const sendReq = wire.encodeSendV1(p, mode);
    return this._op(
      "push",
      p,
      async (conn) => {
        const hash = crypto.createHash("sha256");
        let sent = 0;
        let status = null;
        let src = null;
        let it = null;
        try {
          // The source is caller code (a PC file): every wait on it is
          // guarded, so a stuck disk can't outlive a cancel or the watchdog.
          const opening = Promise.resolve().then(() => o.open());
          try {
            src = await conn.guard(opening);
          } catch (e) {
            opening.then(_destroySource, () => {});
            throw e;
          }
          it = _chunks(src)[Symbol.asyncIterator]();
          await conn.write(sendReq);
          // THE framed reader: already waiting for the one status adbd will
          // send, early (open/write failure) or after DONE.
          const arrived = new Latch();
          const statusP = this._head(conn, [wire.ID.OKAY]).then(
            (h) => {
              status = { ok: true, h };
              arrived.fire();
            },
            (e) => {
              status = { err: e };
              arrived.fire();
            },
          );
          for (;;) {
            // The status is checked before every wait and every wait races it:
            // an early FAIL stops the stream whether it lands while the source
            // produces a chunk or while a write waits on drain, and a socket
            // end after it can't replace it. (A FAIL's bytes precede the
            // socket's end, so it is parsed before a guard could report that
            // end.)
            if (status) break;
            const next = await arrived.or(conn.guard(it.next()), null);
            if (!next || next.done || status) break;
            const chunk = next.value;
            hash.update(chunk);
            const wrote = conn.write([wire.encodeDataHeader(chunk.length), chunk]);
            await arrived.or(wrote, null);
            if (status) {
              wrote.catch(() => {});
              break;
            }
            await wrote;
            sent += chunk.length;
            if (o.onProgress) o.onProgress(sent);
          }
          if (!status) {
            // DONE races the status too: a FAIL that lands while DONE waits
            // behind adbd's discard must not turn into a watchdog timeout.
            const done = conn.write(wire.encodeDone(o.mtime != null ? o.mtime : Date.now() / 1000));
            await arrived.or(done, null);
            if (status) {
              done.catch(() => {});
            } else {
              await done;
              if (conn.idleMs) conn.setIdle(conn.idleMs + _drainAllowanceMs(sent));
            }
          }
          await statusP;
        } finally {
          if (it) it.return().catch(() => {});
          _destroySource(src);
        }
        if (status.err) throw status.err;
        // A trailing message length on OKAY is always 0 for SEND; nothing to read.
        return { bytes: sent, sha256: hash.digest("hex") };
      },
      { signal: o.signal, idleMs: o.idleMs },
    );
  }

  /**
   * RECV one file. With `sink` (a function called per chunk, may return a
   * Promise) nothing is collected; otherwise the bytes come back in `data`,
   * capped by maxBytes (RangeError past it; the session is killed, since the
   * rest of the reply can't be skipped).
   */
  pull(path, { sink, maxBytes = 64 * 1024 * 1024, signal, idleMs } = {}) {
    const p = _pathBytes(path);
    return this._op(
      "pull",
      p,
      async (conn) => {
        await conn.write(wire.encodeSyncRequest(wire.ID.RECV, p));
        const hash = crypto.createHash("sha256");
        const parts = [];
        let size = 0;
        for (;;) {
          const h = await this._head(conn, [wire.ID.DATA, wire.ID.DONE]);
          if (h.id === wire.ID.DONE) break;
          if (h.value > wire.SYNC_DATA_MAX) throw new AdbError("remote-fail", `oversized DATA (${h.value})`, { serial: this.device.serial });
          const chunk = await conn.read(h.value);
          size += chunk.length;
          hash.update(chunk);
          if (sink) await conn.guard(Promise.resolve().then(() => sink(chunk)));
          else {
            if (size > maxBytes) throw new RangeError(`pull of ${_pathText(p)} exceeds ${maxBytes} bytes`);
            parts.push(chunk);
          }
        }
        return { size, sha256: hash.digest("hex"), data: sink ? undefined : Buffer.concat(parts) };
      },
      { signal, idleMs },
    );
  }

  /**
   * A small file (details.json, a cover) or null when it isn't a regular
   * file that exists. STA2 first, RECV only then: a RECV of a missing file
   * FAILs and ends the session, which would also cost the reads queued
   * behind it.
   */
  async readSmall(path, { maxBytes = 4 * 1024 * 1024, signal } = {}) {
    const st = await this.stat(path, { signal });
    if (!st.ok || st.type !== "file") return null;
    if (st.size > maxBytes) return null;
    const r = await this.pull(path, { maxBytes, signal });
    return r.data;
  }

  /** QUIT politely (adbd closes without a reply), then drop the socket. */
  async close() {
    await this._serial(async () => {
      const c = this.conn;
      this.conn = null;
      if (c && c.alive) {
        try {
          await c.write(wire.encodeQuit());
        } catch (_) {
          // already gone; nothing to tell
        }
      }
      if (c) c.destroy();
    });
  }

  /**
   * Drop the socket now and end the session for good (cancel, shutdown). An
   * operation in flight ends as `cancelled` and is not retried; later ones
   * are refused. adbd unlinks a partial SEND.
   */
  destroy() {
    this.destroyed = true;
    if (this.conn) this.conn.abort();
    this.conn = null;
  }
}

function _destroySource(src) {
  if (src && typeof src.destroy === "function") src.destroy();
}

/** Buffer → ≤64 KiB slices; async iterable → re-sliced to ≤64 KiB. */
async function* _chunks(src) {
  if (Buffer.isBuffer(src)) {
    for (let i = 0; i < src.length; i += wire.SYNC_DATA_MAX) yield src.subarray(i, i + wire.SYNC_DATA_MAX);
    return;
  }
  for await (const c of src) {
    const b = Buffer.isBuffer(c) ? c : Buffer.from(c);
    for (let i = 0; i < b.length; i += wire.SYNC_DATA_MAX) yield b.subarray(i, i + wire.SYNC_DATA_MAX);
  }
}

module.exports = {
  ADB_ERROR_KINDS,
  DEFAULTS,
  DEFAULT_PORT,
  AdbError,
  AdbClient,
  AdbDevice,
  SyncSession,
  classifyHostFail,
  classifySyncFail,
  isUnknownServiceFail,
  resolveServerAddress,
  // The OKAY wait's extra allowance; tools/_live_device_sync.js compares
  // the tablet's measured wait against it (P5).
  drainAllowanceMs: _drainAllowanceMs,
  // Exported for tools/_test_device_sync_adb.js only (the watchdog's
  // flushed-bytes rule is tested on a bare socket pair; Latch's waiter
  // accounting directly).
  _Conn: Conn,
  _Latch: Latch,
};
