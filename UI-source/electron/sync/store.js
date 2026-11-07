// ============================================================
// DEVICE SYNC — STORE (main process; the only writer under userData/sync)
//
// Owns how sync state reaches the disk (non-UI plan, deviation 1 and
// "Persistent files"):
//   * writeJsonAtomic — a unique tmp `<name>.<pid>.<seq>.tmp`, fsync, close,
//     rename over the target. The rename is retried on EBUSY/EPERM/EACCES
//     (antivirus, an indexer holding the file): RENAME_TRIES async tries with
//     backoff, then StoreError('record-write-failed'), which aborts the run.
//     history.js:_saveJson (102-137) is NOT the model: it falls back to a
//     non-atomic copy and swallows errors;
//   * writeJsonAtomicSync — the synchronous twin, for shutdownNow() only;
//   * WriteQueue — one async single-flight writer per path that coalesces to
//     the latest content, so overlapping writes never share a tmp file and a
//     burst of record changes costs one write, not one per change;
//   * RecordStore — one target's sharded record: header.json, one shard per
//     managed device folder (folders/<shardId>.json, random stable ids),
//     selection.json, run logs (30 kept), find-sources.json. One recordEpoch
//     ties them together: a file with another epoch is ignored and
//     garbage-collected (provenance.js loadRecordView does the filtering);
//   * sweepTmp — the startup sweep of stale tmp files.
//
// DURABILITY, stated exactly: a write is durable across a process kill and
// atomic, but not durable, across an OS crash. libuv renames with
// MoveFileExW(…, MOVEFILE_REPLACE_EXISTING) and no write-through
// (src/win/fs.c:2341), and Node can't fsync a directory on Windows; on POSIX
// the directory is fsync'd too. After a power loss the previous shard may
// come back, which is safe: the slot then fails its (size, devMtime) check
// and reads as changed, never as ours.
//
// Read by: prepare.js and executor.js (shards, header, logs), hash-pool.js
// (the PC hash cache through writeJsonAtomic), service.js (P4: selection,
// config, shutdownNow). grep: record-write-failed
// ============================================================

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

// Rename retries: 5 tries, waits between them. A persistent lock past ~1.5 s
// is not transient and fails the write loudly.
const RENAME_TRIES = 5;
const RENAME_BACKOFF_MS = Object.freeze([50, 100, 200, 400]);
const RETRY_CODES = Object.freeze(new Set(["EBUSY", "EPERM", "EACCES"]));
const LOGS_KEPT = 30;
// `<name>.<pid>.<seq>.tmp`: the only tmp shape this module creates, and the
// only one the sweep deletes.
const TMP_RE = /\.\d+\.\d+\.tmp$/;
const TARGET_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
const SHARD_ID_RE = /^[0-9a-f]{16}$/;

class StoreError extends Error {
  constructor(code, message, extra = {}) {
    super(message);
    this.name = "StoreError";
    this.code = code;
    // Shares the executor's vocabulary: every run-ending fault has a kind.
    this.kind = code;
    this.charges = 0;
    if (extra.cause) this.cause = extra.cause;
    if (extra.file) this.file = extra.file;
  }
}

function _sleepAsync(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function _sleepSync(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

/**
 * The filesystem calls the writers make, injectable so tests can produce
 * EBUSY, ENOSPC or a lock that never clears.
 */
const REAL_OPS = Object.freeze({
  async mkdir(dir) {
    await fs.promises.mkdir(dir, { recursive: true });
  },
  async writeTmp(tmp, data) {
    const fh = await fs.promises.open(tmp, "wx");
    try {
      await fh.writeFile(data);
      await fh.sync();
    } finally {
      await fh.close();
    }
  },
  rename: (a, b) => fs.promises.rename(a, b),
  unlink: (p) => fs.promises.unlink(p),
  async fsyncDir(dir) {
    // Windows can't open a directory for fsync (see the header).
    if (process.platform === "win32") return;
    let fh = null;
    try {
      fh = await fs.promises.open(dir, "r");
      await fh.sync();
    } catch (_) {
      // best effort: the rename itself already happened
    } finally {
      if (fh) await fh.close().catch(() => {});
    }
  },
  sleep: _sleepAsync,
});

const REAL_OPS_SYNC = Object.freeze({
  mkdir(dir) {
    fs.mkdirSync(dir, { recursive: true });
  },
  writeTmp(tmp, data) {
    const fd = fs.openSync(tmp, "wx");
    try {
      fs.writeFileSync(fd, data);
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
  },
  rename: (a, b) => fs.renameSync(a, b),
  unlink: (p) => fs.unlinkSync(p),
  fsyncDir(dir) {
    if (process.platform === "win32") return;
    let fd = null;
    try {
      fd = fs.openSync(dir, "r");
      fs.fsyncSync(fd);
    } catch (_) {
      // best effort
    } finally {
      if (fd != null) fs.closeSync(fd);
    }
  },
  sleep: _sleepSync,
});

let _seq = 0;
function tmpNameFor(file) {
  _seq += 1;
  return `${file}.${process.pid}.${_seq}.tmp`;
}

function _serialize(value) {
  return typeof value === "string" ? value : JSON.stringify(value);
}

/**
 * Write `value` (an object, or an already serialized string) to `file`
 * atomically. Resolves once the rename landed.
 * @throws {StoreError} code 'record-write-failed' (tmp removed first)
 */
async function writeJsonAtomic(file, value, ops = REAL_OPS) {
  const data = _serialize(value);
  const tmp = tmpNameFor(file);
  try {
    await ops.mkdir(path.dirname(file));
    await ops.writeTmp(tmp, data);
  } catch (e) {
    await Promise.resolve(ops.unlink(tmp)).catch(() => {});
    throw new StoreError("record-write-failed", `writing ${path.basename(file)} failed: ${e && e.message}`, { cause: e, file });
  }
  for (let attempt = 1; ; attempt += 1) {
    try {
      await ops.rename(tmp, file);
      break;
    } catch (e) {
      if (!RETRY_CODES.has(e && e.code) || attempt >= RENAME_TRIES) {
        await Promise.resolve(ops.unlink(tmp)).catch(() => {});
        throw new StoreError("record-write-failed", `replacing ${path.basename(file)} failed after ${attempt} tries: ${e && e.message}`, { cause: e, file });
      }
      await ops.sleep(RENAME_BACKOFF_MS[attempt - 1]);
    }
  }
  await ops.fsyncDir(path.dirname(file));
}

/** The synchronous twin of writeJsonAtomic; shutdownNow() only. */
function writeJsonAtomicSync(file, value, ops = REAL_OPS_SYNC) {
  const data = _serialize(value);
  const tmp = tmpNameFor(file);
  try {
    ops.mkdir(path.dirname(file));
    ops.writeTmp(tmp, data);
  } catch (e) {
    try {
      ops.unlink(tmp);
    } catch (_) {
      // nothing was created
    }
    throw new StoreError("record-write-failed", `writing ${path.basename(file)} failed: ${e && e.message}`, { cause: e, file });
  }
  for (let attempt = 1; ; attempt += 1) {
    try {
      ops.rename(tmp, file);
      break;
    } catch (e) {
      if (!RETRY_CODES.has(e && e.code) || attempt >= RENAME_TRIES) {
        try {
          ops.unlink(tmp);
        } catch (_) {
          // already gone
        }
        throw new StoreError("record-write-failed", `replacing ${path.basename(file)} failed after ${attempt} tries: ${e && e.message}`, { cause: e, file });
      }
      ops.sleep(RENAME_BACKOFF_MS[attempt - 1]);
    }
  }
  ops.fsyncDir(path.dirname(file));
}

/**
 * Read a JSON file.
 * @returns {{state:'ok', value:any} | {state:'missing'} | {state:'corrupt', error:string}}
 *   A zero-length file is corrupt, not missing: something wrote it.
 */
async function readJson(file) {
  let text;
  try {
    text = await fs.promises.readFile(file, "utf8");
  } catch (e) {
    if (e && e.code === "ENOENT") return { state: "missing" };
    return { state: "corrupt", error: `${e && e.code}: ${e && e.message}` };
  }
  if (!text.length) return { state: "corrupt", error: "empty file" };
  try {
    return { state: "ok", value: JSON.parse(text) };
  } catch (e) {
    return { state: "corrupt", error: e.message };
  }
}

/** Synchronous readJson, for the startup paths that can't await. */
function readJsonSync(file) {
  let text;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch (e) {
    if (e && e.code === "ENOENT") return { state: "missing" };
    return { state: "corrupt", error: `${e && e.code}: ${e && e.message}` };
  }
  if (!text.length) return { state: "corrupt", error: "empty file" };
  try {
    return { state: "ok", value: JSON.parse(text) };
  } catch (e) {
    return { state: "corrupt", error: e.message };
  }
}

/**
 * One async single-flight writer per path. write() snapshots the content
 * at call time; while a write of that path runs, later calls replace the
 * one waiting behind it, and every caller whose content was superseded
 * resolves when the newer content lands (or rejects with its error).
 */
class WriteQueue {
  constructor({ ops = REAL_OPS } = {}) {
    this.ops = ops;
    this.slots = new Map();
    this.abandoned = false;
    this.writes = 0;
  }

  write(file, value) {
    if (this.abandoned) {
      return Promise.reject(new StoreError("record-write-failed", "the store was abandoned at shutdown", { file }));
    }
    const data = _serialize(value);
    let slot = this.slots.get(file);
    if (!slot) {
      slot = { running: false, next: null, idle: [] };
      this.slots.set(file, slot);
    }
    if (slot.next) {
      slot.next.data = data;
      return slot.next.promise;
    }
    const job = { data, promise: null, resolve: null, reject: null };
    job.promise = new Promise((res, rej) => {
      job.resolve = res;
      job.reject = rej;
    });
    slot.next = job;
    if (!slot.running) this._pump(file, slot);
    return job.promise;
  }

  async _pump(file, slot) {
    slot.running = true;
    while (slot.next) {
      const job = slot.next;
      slot.next = null;
      try {
        if (this.abandoned) throw new StoreError("record-write-failed", "the store was abandoned at shutdown", { file });
        await writeJsonAtomic(file, job.data, this.ops);
        this.writes += 1;
        job.resolve();
      } catch (e) {
        job.reject(e);
      }
    }
    slot.running = false;
    this.slots.delete(file);
    for (const r of slot.idle) r();
  }

  /** Resolves when nothing is queued or running for `file` (or any path). */
  whenIdle(file) {
    if (file == null) return Promise.all([...this.slots.keys()].map((f) => this.whenIdle(f))).then(() => {});
    const slot = this.slots.get(file);
    if (!slot) return Promise.resolve();
    return new Promise((r) => slot.idle.push(r));
  }

  /**
   * shutdownNow(): stop starting writes. A write already inside its rename
   * finishes on its own; everything queued rejects. The synchronous writer
   * takes over from here.
   */
  abandon() {
    this.abandoned = true;
    for (const slot of this.slots.values()) {
      if (slot.next) {
        const job = slot.next;
        slot.next = null;
        job.reject(new StoreError("record-write-failed", "the store was abandoned at shutdown"));
      }
    }
  }
}

/**
 * Delete stale tmp files under `dir` (recursively). Run at startup only:
 * the single-instance lock means no other writer exists then.
 * @returns {Promise<number>} files removed
 */
async function sweepTmp(dir) {
  let removed = 0;
  async function walk(d) {
    let ents;
    try {
      ents = await fs.promises.readdir(d, { withFileTypes: true });
    } catch (_) {
      return;
    }
    for (const e of ents) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) await walk(p);
      else if (TMP_RE.test(e.name)) {
        try {
          await fs.promises.unlink(p);
          removed += 1;
        } catch (_) {
          // locked: the next startup tries again
        }
      }
    }
  }
  await walk(dir);
  return removed;
}

function newEpoch() {
  return crypto.randomBytes(8).toString("hex");
}

function newShardId() {
  return crypto.randomBytes(8).toString("hex");
}

/**
 * A shard as the record view holds it ({shardId, …, files: Map slot→entry})
 * → its file body. Entries are keyed by the slot they were planned under;
 * loadRecordView re-keys by name on load, so the key's case rule never
 * matters on disk.
 */
function shardBody(shard, epoch) {
  const files = {};
  const src = shard.files instanceof Map ? shard.files : new Map(Object.entries(shard.files || {}));
  for (const [slot, e] of src) if (e) files[slot] = e;
  const body = {
    recordEpoch: epoch,
    name: shard.name,
    identityKey: shard.identityKey || null,
    urlKey: shard.urlKey || null,
    pcFolder: shard.pcFolder || null,
    verifiedAt: shard.verifiedAt || null,
    verifyMode: shard.verifyMode || null,
    files,
  };
  if (shard.renameIntent) body.renameIntent = shard.renameIntent;
  return body;
}

/**
 * One target's record under <root>/targets/<targetId>/.
 *
 * Writes go through the shared WriteQueue. Every shard and selection write
 * stamps the header's current epoch; a write while no header exists throws
 * (the executor writes the header first, at a target's first write).
 */
class RecordStore {
  /**
   * @param {object} o
   * @param {string} o.root      userData/sync
   * @param {string} o.targetId
   * @param {WriteQueue} [o.queue]
   * @param {() => number} [o.now]
   */
  constructor({ root, targetId, queue, now } = {}) {
    if (!TARGET_ID_RE.test(String(targetId || ""))) throw new TypeError(`invalid target id: ${targetId}`);
    this.root = root;
    this.targetId = targetId;
    this.queue = queue || new WriteQueue();
    this.now = now || Date.now;
    this.dir = path.join(root, "targets", targetId);
    this.headerPath = path.join(this.dir, "header.json");
    this.foldersDir = path.join(this.dir, "folders");
    this.selectionPath = path.join(this.dir, "selection.json");
    this.findSourcesPath = path.join(this.dir, "find-sources.json");
    this.logsDir = path.join(this.dir, "logs");
    this.coversDir = path.join(this.dir, "covers");
    this.epoch = null;
    this.header = null;
  }

  shardPath(shardId) {
    if (!SHARD_ID_RE.test(String(shardId))) throw new TypeError(`invalid shard id: ${shardId}`);
    return path.join(this.foldersDir, `${shardId}.json`);
  }

  /**
   * Read every record file.
   * @returns {Promise<{header:object|null, shards:object[], corrupt:Array<{shardId,error}>, selection:object|null}>}
   *   shards carry their shardId (from the file name). A shard that fails to
   *   parse (zero-length included) is set aside as `<id>.json.corrupt` and
   *   listed in `corrupt`: a random id can't say which folder it described,
   *   so that folder plans as unbound (deviation 1, corrected in P1).
   */
  async load() {
    const h = await readJson(this.headerPath);
    const header = h.state === "ok" && h.value && typeof h.value === "object" ? h.value : null;
    this.header = header && header.recordEpoch ? header : null;
    this.epoch = this.header ? this.header.recordEpoch : null;
    const shards = [];
    const corrupt = [];
    let names = [];
    try {
      names = await fs.promises.readdir(this.foldersDir);
    } catch (_) {
      names = [];
    }
    for (const n of names.sort()) {
      const m = /^([0-9a-f]{16})\.json$/.exec(n);
      if (!m) continue;
      const file = path.join(this.foldersDir, n);
      const r = await readJson(file);
      if (r.state === "ok" && r.value && typeof r.value === "object" && typeof r.value.name === "string") {
        shards.push({ ...r.value, shardId: m[1] });
        continue;
      }
      if (r.state === "missing") continue;
      corrupt.push({ shardId: m[1], error: r.state === "ok" ? "not a shard" : r.error });
      await fs.promises.rename(file, `${file}.corrupt`).catch(() => {});
    }
    const s = await readJson(this.selectionPath);
    const selection = s.state === "ok" && s.value && typeof s.value === "object" ? s.value : null;
    return { header, shards, corrupt, selection, headerState: h.state };
  }

  /** Write the header; its recordEpoch becomes the store's epoch. */
  async writeHeader(header) {
    if (!header || !header.recordEpoch) throw new TypeError("header needs a recordEpoch");
    await this.queue.write(this.headerPath, header);
    this.header = { ...header };
    this.epoch = header.recordEpoch;
  }

  /**
   * Write the header built by `make()` when the target has none yet.
   * Single flight: concurrent first writers (an apply's first shard, a
   * selection change, sync:link) share ONE write, so they can't mint two
   * epochs and leave each other's shards or selection stale.
   */
  async ensureHeader(make) {
    if (this.epoch) return;
    if (!this._headerInit) {
      this._headerInit = (async () => {
        if (!this.epoch) await this.writeHeader(make());
      })().finally(() => {
        this._headerInit = null;
      });
    }
    await this._headerInit;
  }

  /** Merge fields into the current header (same epoch). */
  async updateHeader(fields) {
    this._needEpoch();
    await this.writeHeader({ ...this.header, ...(fields || {}), recordEpoch: this.epoch });
  }

  _needEpoch() {
    if (!this.epoch) throw new StoreError("record-write-failed", "no record header: write the header first");
    return this.epoch;
  }

  async writeShard(shard) {
    const epoch = this._needEpoch();
    await this.queue.write(this.shardPath(shard.shardId), shardBody(shard, epoch));
  }

  /** Remove a shard once nothing is queued for it (a late write can't revive it). */
  async deleteShard(shardId) {
    const file = this.shardPath(shardId);
    await this.queue.whenIdle(file);
    try {
      await fs.promises.unlink(file);
    } catch (e) {
      if (!e || e.code !== "ENOENT") throw new StoreError("record-write-failed", `removing shard ${shardId} failed: ${e && e.message}`, { cause: e });
    }
  }

  async writeSelection(sel) {
    const epoch = this._needEpoch();
    await this.queue.write(this.selectionPath, { recordEpoch: epoch, ops: sel.ops || {}, rebinds: sel.rebinds || {} });
  }

  /**
   * Forget-record and rule 12's clear: ONE atomic header write with a new
   * epoch. Old shards and the old selection are dead from that moment, so a
   * half-finished delete afterwards can never resurrect their entries; gc()
   * removes them lazily.
   */
  async clear(headerFields) {
    const epoch = newEpoch();
    await this.writeHeader({ ...(headerFields || {}), recordEpoch: epoch, createdAt: this.now() });
    return epoch;
  }

  /**
   * Delete shards whose epoch is stale (ids from loadRecordView's
   * staleShardIds). Best effort: a locked file stays for the next pass and is
   * ignored meanwhile.
   * @returns {Promise<number>} shards removed
   */
  async gc(staleShardIds) {
    let removed = 0;
    for (const id of staleShardIds || []) {
      if (!SHARD_ID_RE.test(String(id))) continue;
      try {
        await this.deleteShard(id);
        removed += 1;
      } catch (_) {
        // locked; retried at the next plan
      }
    }
    return removed;
  }

  /**
   * Bind a device folder to a series: Link (rule 2), or a gone series'
   * re-bind (decision 10). A gone series' shard is REWRITTEN in place
   * (name, binding, empty files, unverified), so a series never has two
   * shards on one target; otherwise a new shard is created. The folder is
   * then verified before its ops are planned (rule 3).
   * @returns {Promise<string>} the shardId
   */
  async bindFolder({ name, binding, shardId }) {
    const id = shardId || newShardId();
    await this.writeShard({
      shardId: id,
      name,
      identityKey: binding && binding.identityKey,
      urlKey: binding && binding.urlKey,
      pcFolder: binding && binding.pcFolder,
      verifiedAt: null,
      verifyMode: null,
      files: new Map(),
    });
    return id;
  }

  /** Run log: logs/<ts>-<runId>.json; the newest LOGS_KEPT are kept. */
  async writeLog(log) {
    const started = log.startedAt || this.now();
    const name = `${new Date(started).toISOString().replace(/[:.]/g, "-")}-${String(log.runId || "run").replace(/[^A-Za-z0-9_-]/g, "")}.json`;
    const file = path.join(this.logsDir, name);
    await this.queue.write(file, log);
    let names = [];
    try {
      names = (await fs.promises.readdir(this.logsDir)).filter((n) => n.endsWith(".json")).sort();
    } catch (_) {
      return file;
    }
    for (const old of names.slice(0, Math.max(0, names.length - LOGS_KEPT))) {
      await fs.promises.unlink(path.join(this.logsDir, old)).catch(() => {});
    }
    return file;
  }
}

module.exports = {
  RENAME_TRIES,
  RENAME_BACKOFF_MS,
  LOGS_KEPT,
  TMP_RE,
  REAL_OPS,
  REAL_OPS_SYNC,
  StoreError,
  tmpNameFor,
  writeJsonAtomic,
  writeJsonAtomicSync,
  readJson,
  readJsonSync,
  WriteQueue,
  sweepTmp,
  newEpoch,
  newShardId,
  shardBody,
  RecordStore,
};
