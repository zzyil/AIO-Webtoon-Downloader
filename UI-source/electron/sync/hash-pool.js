// ============================================================
// DEVICE SYNC — HASH POOL (main process)
//
// Owns SHA-256 of PC library files and the PC hash cache
// (userData/sync/pc-hash-cache.json, keyed by absolute path →
// {size, mtimeNs, sha256}).
//
//   * Workers are worker_threads built from an INLINE source with
//     {eval: true}: no worker script is loaded from app.asar, so the packaged
//     build needs nothing unpacked. If a worker can't start or dies, that
//     task and every later one hash on the main thread (streaming), so a
//     plan never fails for want of threads.
//   * Workers read synchronously (fs.readSync), so hashing doesn't occupy
//     the shared libuv fs thread pool the rest of main uses.
//   * MAIN IS THE ONLY WRITER of the cache (deviation 1): workers return
//     results and never touch the disk. The cache is flushed every
//     FLUSH_EVERY new hashes, at each job end (flush()) and from
//     shutdownNow() (flushSync()). Losing it costs a re-hash, never a wrong
//     answer: an entry only answers for the exact (size, mtimeNs) it was
//     hashed at.
//   * A file that changes while it is hashed (size or mtime differ before
//     and after) is reported `changed` and not cached.
//   * Per-file errors (vanished, locked) come back per file and never abort
//     the batch; a failed walk never prunes the cache (prune() is called
//     only with a complete walk's paths).
//   * Throttle (deviation 3): setConcurrency(n). service.js drops it to 1
//     while a download or a Check All sweep runs.
//   * recordObserved(): hash-while-push (deviation 2) writes the sha of what
//     was actually sent, so a file that changed without changing size or
//     mtime doesn't propose the same update forever.
//
// Read by: prepare.js (plan hashing), executor.js (recordObserved; folder
// Verify), service.js (P4: pre-warm, throttle, shutdownNow).
// Depends on: store.js (writeJsonAtomic / Sync).
// ============================================================

const fs = require("fs");
const os = require("os");
const crypto = require("crypto");
const store = require("./store");

const FLUSH_EVERY = 500;
const MAX_WORKERS = 4;
const CACHE_VERSION = 1;
const READ_CHUNK = 1024 * 1024;

// The worker, as source. Kept self-contained: it may require only Node
// builtins (eval'd workers have no module path of their own).
const WORKER_SOURCE = `
const { parentPort } = require("worker_threads");
const fs = require("fs");
const crypto = require("crypto");
const CHUNK = ${READ_CHUNK};
const buf = Buffer.allocUnsafe(CHUNK);
parentPort.on("message", (m) => {
  let fd = null;
  try {
    const st0 = fs.statSync(m.path, { bigint: true });
    if (!st0.isFile()) throw Object.assign(new Error("not a regular file"), { code: "ENOTFILE" });
    fd = fs.openSync(m.path, "r");
    const h = crypto.createHash("sha256");
    let size = 0;
    for (;;) {
      const n = fs.readSync(fd, buf, 0, CHUNK, null);
      if (!n) break;
      h.update(buf.subarray(0, n));
      size += n;
    }
    fs.closeSync(fd);
    fd = null;
    const st1 = fs.statSync(m.path, { bigint: true });
    const changed = st1.size !== st0.size || st1.mtimeNs !== st0.mtimeNs || BigInt(size) !== st0.size;
    parentPort.postMessage({ id: m.id, changed, sha256: h.digest("hex"), size: Number(st0.size), mtimeNs: String(st0.mtimeNs) });
  } catch (e) {
    if (fd != null) { try { fs.closeSync(fd); } catch (_) {} }
    parentPort.postMessage({ id: m.id, error: (e && e.code) || String(e && e.message) });
  }
});
`;

/** Hash on the main thread (the fallback), with the worker's semantics. */
async function hashFileMain(p) {
  try {
    const st0 = await fs.promises.stat(p, { bigint: true });
    if (!st0.isFile()) return { error: "ENOTFILE" };
    const h = crypto.createHash("sha256");
    let size = 0;
    await new Promise((resolve, reject) => {
      const rs = fs.createReadStream(p, { highWaterMark: READ_CHUNK });
      rs.on("data", (c) => {
        h.update(c);
        size += c.length;
      });
      rs.on("error", reject);
      rs.on("end", resolve);
    });
    const st1 = await fs.promises.stat(p, { bigint: true });
    const changed = st1.size !== st0.size || st1.mtimeNs !== st0.mtimeNs || BigInt(size) !== st0.size;
    return { changed, sha256: h.digest("hex"), size: Number(st0.size), mtimeNs: String(st0.mtimeNs) };
  } catch (e) {
    return { error: (e && e.code) || String(e && e.message) };
  }
}

/**
 * Workers for a CPU budget: the Resource Limits percent of the cores,
 * at least 1, at most MAX_WORKERS.
 */
function workerCountFor(cpuPercent, cpuCount = (os.cpus() || []).length || 1) {
  const pct = Number.isFinite(cpuPercent) ? Math.max(0, Math.min(100, cpuPercent)) : 100;
  return Math.max(1, Math.min(MAX_WORKERS, Math.floor((cpuCount * pct) / 100)));
}

function _defaultWorkerFactory() {
  const { Worker } = require("worker_threads");
  return new Worker(WORKER_SOURCE, { eval: true });
}

class HashPool {
  /**
   * @param {object} o
   * @param {string} [o.cachePath]  pc-hash-cache.json; omitted = no cache file
   * @param {number} [o.workers]    concurrency (1..MAX_WORKERS)
   * @param {store.WriteQueue} [o.queue]
   * @param {() => object} [o.workerFactory]  tests force the fallback with a throwing one
   * @param {number} [o.flushEvery]
   */
  constructor({ cachePath = null, workers = 1, queue = null, workerFactory = _defaultWorkerFactory, flushEvery = FLUSH_EVERY } = {}) {
    this.cachePath = cachePath;
    this.queue = queue || new store.WriteQueue();
    this.workerFactory = workerFactory;
    this.flushEvery = flushEvery;
    this.limit = Math.max(1, Math.min(MAX_WORKERS, workers | 0 || 1));
    this.cache = new Map();
    this.loaded = false;
    this.dirty = 0;
    this.sinceFlush = 0;
    this.fallback = false;
    this.workers = []; // {w, busy:Task|null}
    this.tasks = [];
    this.seq = 0;
    this.closed = false;
    // spawned: workers that started; hashed − mainThread ran in a worker
    // (service.js logs these at each pre-warm end, P5's marker).
    this.stats = { hashed: 0, hits: 0, mainThread: 0, workerDeaths: 0, spawned: 0 };
  }

  /** Read the cache file once. A missing or unreadable cache is just empty. */
  async load() {
    if (this.loaded || !this.cachePath) {
      this.loaded = true;
      return;
    }
    const r = await store.readJson(this.cachePath);
    if (r.state === "ok" && r.value && r.value.version === CACHE_VERSION && r.value.entries && typeof r.value.entries === "object") {
      for (const [p, e] of Object.entries(r.value.entries)) {
        if (e && typeof e.sha256 === "string" && Number.isFinite(e.size) && typeof e.mtimeNs === "string") this.cache.set(p, e);
      }
    }
    this.loaded = true;
  }

  /** The cached sha for exactly this (size, mtimeNs), else null. */
  lookup(p, size, mtimeNs) {
    const e = this.cache.get(p);
    return e && e.size === size && e.mtimeNs === String(mtimeNs) ? e.sha256 : null;
  }

  _put(p, size, mtimeNs, sha256) {
    this.cache.set(p, { size, mtimeNs: String(mtimeNs), sha256 });
    this.dirty += 1;
    this.sinceFlush += 1;
  }

  /**
   * Hash-while-push (deviation 2): the sha of the bytes actually sent, for
   * the (size, mtimeNs) the executor re-stat'ed before and after the push.
   */
  recordObserved(p, { size, mtimeNs, sha256 }) {
    if (!p || !sha256 || mtimeNs == null) return;
    this._put(p, size, mtimeNs, sha256);
  }

  /** Keep only these paths (a complete walk's). Never called after a failed walk. */
  prune(keepPaths) {
    const keep = keepPaths instanceof Set ? keepPaths : new Set(keepPaths);
    for (const p of [...this.cache.keys()]) {
      if (!keep.has(p)) {
        this.cache.delete(p);
        this.dirty += 1;
      }
    }
  }

  setConcurrency(n) {
    this.limit = Math.max(1, Math.min(MAX_WORKERS, n | 0 || 1));
    // Idle workers past the limit go now; busy ones when their task ends.
    for (const slot of this.workers.slice()) {
      if (this.workers.length <= this.limit) break;
      if (!slot.busy) this._retire(slot);
    }
    this._dispatch();
  }

  /**
   * Hash `files`, consulting the cache unless mode is 'rehash'.
   * @param {Array<{path:string, size:number, mtimeNs:string}>} files
   * @param {object} [o]
   * @param {'cached'|'rehash'} [o.mode]
   * @param {AbortSignal} [o.signal]
   * @param {(done:number, total:number, bytesDone:number, bytesTotal:number)=>void} [o.onProgress]
   * @returns {Promise<Map<string, {sha256:string, size:number, mtimeNs:string}|{error:string}>>}
   *   size and mtimeNs are the stat the sha belongs to: a file that changed
   *   between the walk and its hash answers for its new stat, and the caller
   *   compares. error 'changed': the file changed while it was hashed (not
   *   cached).
   */
  async hashFiles(files, { mode = "cached", signal, onProgress } = {}) {
    await this.load();
    const out = new Map();
    const todo = [];
    let bytesTotal = 0;
    let bytesDone = 0;
    for (const f of files) {
      bytesTotal += f.size || 0;
      const hit = mode !== "rehash" ? this.lookup(f.path, f.size, f.mtimeNs) : null;
      if (hit) {
        out.set(f.path, { sha256: hit, size: f.size, mtimeNs: String(f.mtimeNs) });
        bytesDone += f.size || 0;
        this.stats.hits += 1;
      } else todo.push(f);
    }
    let done = files.length - todo.length;
    if (onProgress) onProgress(done, files.length, bytesDone, bytesTotal);
    await Promise.all(
      todo.map((f) =>
        this._submit(f.path, signal).then(async (r) => {
          if (r.error) out.set(f.path, { error: r.error });
          else if (r.changed) out.set(f.path, { error: "changed" });
          else {
            out.set(f.path, { sha256: r.sha256, size: r.size, mtimeNs: String(r.mtimeNs) });
            this._put(f.path, r.size, r.mtimeNs, r.sha256);
            this.stats.hashed += 1;
          }
          done += 1;
          bytesDone += f.size || 0;
          if (onProgress) onProgress(done, files.length, bytesDone, bytesTotal);
          if (this.sinceFlush >= this.flushEvery) await this.flush().catch(() => {});
        }),
      ),
    );
    if (signal && signal.aborted) {
      const e = new Error("cancelled");
      e.kind = "cancelled";
      throw e;
    }
    return out;
  }

  /** One file, no cache (folder-target Verify hashes the copy itself). */
  hashFile(p, { signal } = {}) {
    return this._submit(p, signal);
  }

  _submit(p, signal) {
    return new Promise((resolve) => {
      if (signal && signal.aborted) return resolve({ error: "cancelled" });
      const task = { id: ++this.seq, path: p, resolve, signal, started: false };
      if (signal) {
        task.onAbort = () => {
          if (task.started) return; // in flight: its result still lands in the cache
          const i = this.tasks.indexOf(task);
          if (i >= 0) this.tasks.splice(i, 1);
          resolve({ error: "cancelled" });
        };
        signal.addEventListener("abort", task.onAbort, { once: true });
      }
      this.tasks.push(task);
      this._dispatch();
    });
  }

  _finish(task, r) {
    if (task.signal && task.onAbort) task.signal.removeEventListener("abort", task.onAbort);
    task.resolve(r);
  }

  _dispatch() {
    while (this.tasks.length) {
      if (this.fallback || this.closed) {
        const task = this.tasks.shift();
        task.started = true;
        this.stats.mainThread += 1;
        hashFileMain(task.path).then((r) => this._finish(task, r));
        continue;
      }
      let slot = this.workers.find((s) => !s.busy);
      if (!slot && this.workers.length < this.limit) slot = this._spawn();
      // A factory that just failed switched to the main thread: the queued
      // tasks go there now (returning would leave them waiting forever).
      if (!slot && this.fallback) continue;
      if (!slot) return;
      const task = this.tasks.shift();
      task.started = true;
      slot.busy = task;
      slot.w.postMessage({ id: task.id, path: task.path });
    }
  }

  _spawn() {
    let w;
    try {
      w = this.workerFactory();
    } catch (_) {
      this.fallback = true;
      return null;
    }
    this.stats.spawned += 1;
    const slot = { w, busy: null };
    w.on("message", (m) => {
      const task = slot.busy;
      slot.busy = null;
      if (task && m && m.id === task.id) this._finish(task, m);
      if (this.workers.length > this.limit) this._retire(slot);
      this._dispatch();
    });
    const dead = () => {
      if (!this.workers.includes(slot)) return;
      this.workers.splice(this.workers.indexOf(slot), 1);
      this.stats.workerDeaths += 1;
      // A worker that dies takes the fast path with it: hash on main from now on.
      this.fallback = true;
      const task = slot.busy;
      slot.busy = null;
      if (task) {
        this.stats.mainThread += 1;
        hashFileMain(task.path).then((r) => this._finish(task, r));
      }
      this._dispatch();
    };
    w.on("error", dead);
    w.on("exit", dead);
    if (typeof w.unref === "function") w.unref();
    this.workers.push(slot);
    return slot;
  }

  _retire(slot) {
    const i = this.workers.indexOf(slot);
    if (i >= 0) this.workers.splice(i, 1);
    slot.w.removeAllListeners("exit");
    slot.w.removeAllListeners("error");
    Promise.resolve(slot.w.terminate()).catch(() => {});
  }

  _body() {
    const entries = {};
    for (const [p, e] of this.cache) entries[p] = e;
    return { version: CACHE_VERSION, entries };
  }

  /** Write the cache when it changed. */
  async flush() {
    if (!this.cachePath || !this.dirty) return;
    const n = this.dirty;
    this.dirty = 0;
    this.sinceFlush = 0;
    try {
      await this.queue.write(this.cachePath, this._body());
    } catch (e) {
      this.dirty += n;
      throw e;
    }
  }

  /** shutdownNow(): the synchronous writer. */
  flushSync() {
    if (!this.cachePath || !this.dirty) return;
    store.writeJsonAtomicSync(this.cachePath, this._body());
    this.dirty = 0;
    this.sinceFlush = 0;
  }

  /** Stop the workers. Queued tasks finish on the main thread. */
  close() {
    this.closed = true;
    for (const slot of this.workers.slice()) {
      const task = slot.busy;
      this._retire(slot);
      if (task) hashFileMain(task.path).then((r) => this._finish(task, r));
    }
    this._dispatch();
  }
}

module.exports = {
  FLUSH_EVERY,
  MAX_WORKERS,
  CACHE_VERSION,
  WORKER_SOURCE,
  HashPool,
  hashFileMain,
  workerCountFor,
};
