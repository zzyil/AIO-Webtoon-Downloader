// ============================================================
// DEVICE SYNC — TRANSPORTS (main process)
//
// Owns HOW the engine touches a target. Two implementations of one
// interface, so prepare.js and executor.js never branch on the kind:
//   * AdbTransport    — over adb/client.js: one AdbDevice, one SyncSession
//                       per connection; shell only where the sync protocol
//                       has no verb (realpath, rm, mv, mkdir, rmdir, find,
//                       sha256sum, stat -f / df);
//   * FolderTransport — a plain folder (a USB drive, a network share) through
//                       fs.promises: dot-temp + rename for writes, fs.statfs
//                       for free space, an async presence stat with a 3 s
//                       timeout (never existsSync: an offline share would
//                       block main).
// MTP would be a third implementation (WPD-backed); not built.
//
// THE INTERFACE (every method is async unless marked):
//   probe()            → {canonicalRoot, caseInsensitive, caseMeasured, caps,
//                         volumeId?}; once per connection (cached)
//   adoptProbe(probe)  (sync) seed this transport with an earlier probe() of
//                         the same connection instead of probing again (the
//                         case probe lists the root). prepare.js refreshPlan
//                         only: a refresh trusts the cached facts, and any
//                         stat that contradicts them escalates to a full plan
//   statRoot()         → {ok, isDir, error}
//   stat(rel)          → {exists, type, size, devMtime, error}
//   listRoot()         → listing of the root (see _listing below)
//   listFolder(name, {afterPush}) → listing of one device folder
//   listRootDirsAlt()  → {ok, keys:Set} the root's directories by a second,
//                         independent method (gone confirmation)
//   readBack(rel)      → {ok, sha256, size} | {ok:false, error}  (RECV + hash)
//   readSmall(rel, max) → Buffer | null
//   push({rel, pcPath, size, signal, onProgress}) → {bytes, sha256}
//   pushBuffer(rel, buf)
//   mkdirp(rel); remove(rels) → {ok, failed, sent}; move(fromRel, toRel) →
//   {ok, reason?}; rmdirIfEmpty(rel) → {removed}
//   hashFolder(name, {largest, signal}) → {ok, rc, hashes:Map name→sha}
//   freeSpace() → bytes | null (what an apply needs is space.js's, by
//     `kind`: adb overwrites in place, a folder writes temp-then-rename)
//   storage() → {free, total} (each bytes | null): the Sync tab's gauge
//     (prepare.js _readFreeSpace); freeSpace() is storage().free
//   waitReady({timeoutMs, signal}) → {state}   (adb `connecting` policy)
//   close(); destroy() (sync: cancel, shutdown)
// `rel` is a path relative to the canonical root ("Folder/name.cbz"); every
// one passes validatePath before it is used.
//
// LISTINGS ({ok, error?, method, dirs:[{name}], files:[{name,size,devMtime}],
// errored:[{name,error}], undecodable:[{display,hex}], other, keys, dirKeys};
// keys are the raw name bytes as latin1 strings) hold
// every decodable entry, dot-names included (the planner hides them; the
// executor's cleanup step needs them). A name that isn't valid UTF-8 is
// unmanaged: it is listed under `undecodable`, never planned, and never
// passed to rm or mv in a lossily decoded form. devMtime is whole seconds,
// truncated, for every method (provenance.normalizeDevMtime).
//
// QUIET FAILURES (non-UI plan, "Quiet failures"): LIS2 of a missing or
// unreadable path answers DONE only, so listFolder STA2s the folder first
// and an error or a non-directory makes the listing {ok:false}; listRoot
// does the same for the root. A shell-path listing (no ls_v2) is preceded by
// a STA2 on the push session, which adbd serves only after the last push's
// lutimes (second review #16).
//
// CASE PROBE (P3 decision): read-only. An existing entry is STA2'd under a
// case-flipped name (ASCII letters swapped): ENOENT means case-sensitive, the
// same object means case-insensitive. The root is tried first, then up to
// three of its folders. With nothing to flip (an empty root) the target is
// ASSUMED case-insensitive, the safe direction (a wrong "sensitive" could let
// a case-only delete remove the new file), caseMeasured is false, and the
// next connection probes again. Nothing is ever written to probe.
//
// Read by: prepare.js, executor.js, service.js (P4: browse-remote, presence).
// Depends on: adb/client.js, adb/wire.js, hash-pool.js (folder Verify).
// grep: validatePath, shq
// ============================================================

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const wire = require("./adb/wire");
const { AdbError } = require("./adb/client");
const { normalizeDevMtime } = require("./provenance");

const SEGMENT_MAX_BYTES = 255;
const REMOTE_PATH_MAX = wire.REMOTE_PATH_MAX;
// Room for the service prefix (`shell,v2,raw:`) and the legacy sentinel echo
// (`\necho "__AIO_RC_<12 hex>__$?"`) inside SHELL_SERVICE_MAX.
const SHELL_OVERHEAD = 64;
const SHELL_CMD_MAX = wire.SHELL_SERVICE_MAX - SHELL_OVERHEAD;
// Verify's idle budget: 60 s plus the largest file at this rate (a
// deliberately low floor for slow flash; sha256sum prints nothing while it
// hashes one file, review #19).
const VERIFY_BASE_MS = 60 * 1000;
const VERIFY_BYTES_PER_SEC = 8 * 1024 * 1024;
const PRESENCE_TIMEOUT_MS = 3000;
const CONNECTING_POLL_MS = 1000;
const FOLDER_TMP_PREFIX = ".aio-tmp-";
const RETRY_CODES = new Set(["EBUSY", "EPERM", "EACCES"]);
const RENAME_TRIES = 5;
const RENAME_BACKOFF_MS = [50, 100, 200, 400];
const PROBE_FLIPS = 5;
const PROBE_SUBFOLDERS = 3;
const SHA256_EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

// ------------------------------------------------------------------
// Paths and quoting
// ------------------------------------------------------------------

function _wellFormed(s) {
  if (typeof s.isWellFormed === "function") return s.isWellFormed();
  return !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(^|[^\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(s);
}

/**
 * Why a device folder or file name can't be used as one path segment, or
 * null. Shared by both transports.
 */
function segmentProblem(seg) {
  if (typeof seg !== "string") return "not-a-string";
  if (!seg) return "empty-segment";
  if (seg === "." || seg === "..") return "dot-segment";
  if (/[\r\n\0]/.test(seg)) return "control-char";
  if (seg.includes("/")) return "separator";
  if (!_wellFormed(seg)) return "invalid-utf8";
  if (Buffer.byteLength(seg, "utf8") > SEGMENT_MAX_BYTES) return "segment-too-long";
  return null;
}

/**
 * The remote-path gate (ports sync_to_tablet.py's _validate_tablet_path):
 * strictly under the root and not the root itself, no `.`/`..` or empty
 * segments, no CR/LF/NUL, valid UTF-8, ≤255 bytes per segment and ≤1,018
 * bytes in all (adbd's 1,024-byte sync path limit covers the path plus
 * `,33188`). "Under" is tested against `root + "/"`, so a root of
 * `…/Documents` never admits `…/DocumentsX/…`.
 * @returns {string|null} the problem, or null when the path is valid
 */
function validatePath(root, p) {
  if (typeof root !== "string" || typeof p !== "string") return "not-a-string";
  if (!root.startsWith("/")) return "root-not-absolute";
  if (/[\r\n\0]/.test(p) || /[\r\n\0]/.test(root)) return "control-char";
  if (!_wellFormed(p)) return "invalid-utf8";
  const r = root.replace(/\/+$/, "");
  if (!r) return "root-is-filesystem-root";
  for (const seg of r.split("/").slice(1)) if (!seg || seg === "." || seg === "..") return "root-not-canonical";
  if (!p.startsWith(`${r}/`)) return p === r ? "is-root" : "outside-root";
  const rest = p.slice(r.length + 1);
  if (!rest) return "is-root";
  for (const seg of rest.split("/")) {
    const why = segmentProblem(seg);
    if (why) return why;
  }
  if (Buffer.byteLength(p, "utf8") > REMOTE_PATH_MAX) return "path-too-long";
  return null;
}

/**
 * POSIX single-quote a word for the device shell (sh/mksh): everything is
 * literal inside '…', and a ' becomes '\''. NUL can't be passed at all.
 */
function shq(s) {
  const t = String(s);
  if (t.includes("\0")) throw new RangeError("shq: NUL can't be quoted");
  return `'${t.replace(/'/g, "'\\''")}'`;
}

/** Swap the case of every ASCII letter; null when nothing changes. */
function flipAsciiCase(name) {
  const out = name.replace(/[A-Za-z]/g, (c) => (c === c.toLowerCase() ? c.toUpperCase() : c.toLowerCase()));
  return out === name ? null : out;
}

function _hex(buf) {
  return Buffer.from(buf).toString("hex");
}

function _emptyListing(method) {
  return { ok: true, method, dirs: [], files: [], errored: [], undecodable: [], other: 0, keys: new Set(), dirKeys: new Set() };
}

/** Sort a listing's arrays by name so every method returns the same order. */
function _sortListing(l) {
  const by = (a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
  l.dirs.sort(by);
  l.files.sort(by);
  return l;
}

// ------------------------------------------------------------------
// AdbTransport
// ------------------------------------------------------------------

class AdbTransport {
  /**
   * @param {object} o
   * @param {import('./adb/client').AdbClient} o.client
   * @param {string} o.serial
   * @param {string} o.root       the target's configured root (may be /sdcard/…)
   * @param {number} [o.idleMs]
   * @param {(err:AdbError)=>void} [o.onCharge]
   */
  constructor({ client, serial, root, idleMs, onCharge } = {}) {
    this.kind = "adb";
    this.client = client;
    this.serial = serial;
    this.root = root;
    this.device = client.device(serial, idleMs != null ? { idleMs } : {});
    this.session = this.device.sync({ onCharge });
    this._probe = null;
    this.canonicalRoot = null;
    this.destroyed = false;
  }

  _abs(rel) {
    const root = this.canonicalRoot || this.root;
    const p = rel ? `${root.replace(/\/+$/, "")}/${rel}` : root;
    if (rel) {
      const why = validatePath(root, p);
      if (why) throw new RangeError(`invalid device path (${why}): ${JSON.stringify(p)}`);
    }
    return p;
  }

  async _features() {
    return this.device.features();
  }

  /** Run a shell command; returns {rc, stdout, stderr}. Transport faults throw. */
  async _sh(cmd, { idleMs, signal } = {}) {
    const r = await this.device.shell(cmd, { idleMs, signal });
    return { rc: r.exitCode, stdout: r.stdout, stderr: r.stderr };
  }

  /**
   * A shell command whose non-zero rc is a device-side failure: classified
   * like a sync FAIL (ENOSPC → no-space, EROFS → read-only, else remote-fail
   * charged 1), with sent:true (it ran).
   */
  async _shOk(cmd, what, opts) {
    const r = await this._sh(cmd, opts);
    if (r.rc !== 0) {
      const text = `${r.stderr.toString("utf8")}${r.stdout.toString("utf8")}`.trim();
      const kind = /No space left on device/.test(text) ? "no-space" : /Read-only file system/.test(text) ? "read-only" : "remote-fail";
      throw new AdbError(kind, `${what} failed (rc ${r.rc}): ${text}`, { serial: this.serial, op: what, sent: true });
    }
    return r;
  }

  /** See the header (adoptProbe): later paths use the canonical root, listings pick their protocol off caps. */
  adoptProbe(probe) {
    this._probe = probe;
    this.canonicalRoot = probe.canonicalRoot || this.root.replace(/\/+$/, "");
    this._caps = probe.caps;
  }

  async probe({ force = false } = {}) {
    if (this._probe && !force) return this._probe;
    const feats = await this._features();
    const caps = {
      lsV2: feats.has("ls_v2"),
      statV2: feats.has("stat_v2"),
      shellV2: feats.has("shell_v2"),
      sha256sum: false,
      findPrintf: false,
      statC: false,
      statF: false,
      df: false,
    };
    // realpath: /sdcard is a symlink that `find` won't descend, and every
    // later path is built from the canonical root.
    const rp = await this._sh(`realpath ${shq(this.root)}`);
    const out = rp.stdout.toString("utf8").replace(/\r?\n$/, "");
    if (rp.rc === 0 && out.startsWith("/") && !out.includes("\n") && !validatePath(out, `${out}/x`)) this.canonicalRoot = out;
    else this.canonicalRoot = this.root.replace(/\/+$/, "");
    const root = this.canonicalRoot;
    const sha = await this._sh("sha256sum /dev/null");
    caps.sha256sum = sha.rc === 0 && sha.stdout.toString("utf8").startsWith(SHA256_EMPTY);
    const sf = await this._sh(`stat -f -c '%a %S' ${shq(root)}`);
    caps.statF = sf.rc === 0 && /^\d+ \d+\s*$/.test(sf.stdout.toString("utf8"));
    if (!caps.statF) {
      const df = await this._sh(`df -P -k ${shq(root)}`);
      caps.df = df.rc === 0 && _parseDf(df.stdout.toString("utf8")) != null;
    }
    if (!caps.lsV2) {
      const fp = await this._sh(`find ${shq(root)} -maxdepth 0 -printf '%y|%s|%T@|%f\\n'`);
      caps.findPrintf = fp.rc === 0 && /^d\|\d+\|\d+(\.\d+)?\|/.test(fp.stdout.toString("utf8"));
      if (!caps.findPrintf) {
        const sc = await this._sh(`stat -c '%F|%s|%Y|%n' ${shq(root)}`);
        caps.statC = sc.rc === 0 && /^directory\|\d+\|\d+\|/.test(sc.stdout.toString("utf8"));
      }
    }
    this._caps = caps;
    const cs = await this._probeCase();
    this._probe = { canonicalRoot: root, caseInsensitive: cs.caseInsensitive, caseMeasured: cs.measured, caps };
    return this._probe;
  }

  /** The read-only case probe (see the header). */
  async _probeCase() {
    const tryIn = async (dirRel) => {
      const l = await this._lis(dirRel);
      if (!l.ok) return null;
      const present = new Set([...l.dirs, ...l.files].map((e) => e.name));
      let tries = 0;
      for (const e of [...l.dirs, ...l.files]) {
        const flipped = flipAsciiCase(e.name);
        if (!flipped || present.has(flipped) || segmentProblem(flipped)) continue;
        if (tries++ >= PROBE_FLIPS) break;
        const rel = (n) => (dirRel ? `${dirRel}/${n}` : n);
        const a = await this.session.stat(this._abs(rel(e.name)));
        const b = await this.session.stat(this._abs(rel(flipped)));
        if (b.error === wire.ERRNO.ENOENT) return { caseInsensitive: false, measured: true };
        if (a.ok && b.ok && a.mode === b.mode && a.size === b.size && a.mtime === b.mtime) return { caseInsensitive: true, measured: true };
      }
      return { next: l.dirs.filter((d) => !d.name.startsWith(".")).slice(0, PROBE_SUBFOLDERS).map((d) => (dirRel ? `${dirRel}/${d.name}` : d.name)) };
    };
    const top = await tryIn("");
    if (top && top.measured) return top;
    for (const sub of (top && top.next) || []) {
      const r = await tryIn(sub);
      if (r && r.measured) return r;
    }
    return { caseInsensitive: true, measured: false };
  }

  async statRoot() {
    const st = await this.session.stat(this._abs(""));
    return { ok: st.ok && st.type === "dir", isDir: st.type === "dir", error: st.error };
  }

  async stat(rel) {
    const st = await this.session.stat(this._abs(rel));
    return {
      exists: st.ok,
      type: st.ok ? st.type : null,
      size: st.ok ? st.size : null,
      devMtime: st.ok ? normalizeDevMtime(st.mtime) : null,
      error: st.error,
    };
  }

  /** LIS2 (LIST on an old adbd) of `rel` without the folder STA2 (callers do it). */
  async _lis(rel) {
    const r = await this.session.list(this._abs(rel));
    const l = _emptyListing(this._caps && this._caps.lsV2 === false ? "list-v1" : "lis2");
    for (const e of r.entries) {
      l.keys.add(e.raw.toString("latin1"));
      if (e.type === "dir") l.dirKeys.add(e.raw.toString("latin1"));
      if (e.name == null) {
        l.undecodable.push({ display: e.display, hex: _hex(e.raw) });
        continue;
      }
      if (e.type === "dir") l.dirs.push({ name: e.name });
      else if (e.type === "file") l.files.push({ name: e.name, size: e.size, devMtime: normalizeDevMtime(e.mtime) });
      else l.other += 1;
    }
    for (const e of r.errored) {
      l.keys.add(e.raw.toString("latin1"));
      if (e.name != null) l.errored.push({ name: e.name, error: e.error });
      else l.undecodable.push({ display: e.display, hex: _hex(e.raw) });
    }
    return _sortListing(l);
  }

  /** find -printf / stat -c listing for a device without ls_v2 (v1 sizes are 32-bit). */
  async _shellList(rel) {
    const dir = this._abs(rel);
    const caps = this._caps;
    let r;
    let method;
    if (caps.findPrintf) {
      method = "find-printf";
      r = await this._shOk(`find ${shq(dir)} -mindepth 1 -maxdepth 1 -printf '%y|%s|%T@|%f\\n'`, "list");
    } else {
      method = "stat-c";
      r = await this._shOk(`find ${shq(dir)} -mindepth 1 -maxdepth 1 -exec stat -c '%F|%s|%Y|%n' {} +`, "list");
    }
    const l = _emptyListing(method);
    const prefix = Buffer.from(`${dir}/`, "utf8");
    const buf = r.stdout;
    let start = 0;
    for (let i = 0; i <= buf.length; i += 1) {
      if (i < buf.length && buf[i] !== 0x0a) continue;
      const line = buf.subarray(start, i);
      start = i + 1;
      if (!line.length) continue;
      // type|size|mtime|name — the name is last and may itself hold `|`.
      const cut = [];
      for (let j = 0; j < line.length && cut.length < 3; j += 1) if (line[j] === 0x7c) cut.push(j);
      if (cut.length < 3) return { ok: false, method, error: "unparsable-listing" };
      const type = line.subarray(0, cut[0]).toString("latin1");
      const size = Number(line.subarray(cut[0] + 1, cut[1]).toString("latin1"));
      const mtime = Number(line.subarray(cut[1] + 1, cut[2]).toString("latin1"));
      let raw = line.subarray(cut[2] + 1);
      if (method === "stat-c") {
        if (!raw.subarray(0, prefix.length).equals(prefix)) return { ok: false, method, error: "unparsable-listing" };
        raw = raw.subarray(prefix.length);
      }
      if (!Number.isFinite(size) || !Number.isFinite(mtime) || !raw.length) return { ok: false, method, error: "unparsable-listing" };
      const isDir = type === "d" || type === "directory";
      l.keys.add(Buffer.from(raw).toString("latin1"));
      if (isDir) l.dirKeys.add(Buffer.from(raw).toString("latin1"));
      const nm = wire.decodeName(Buffer.from(raw));
      if (nm.name == null) {
        l.undecodable.push({ display: nm.display, hex: _hex(raw) });
        continue;
      }
      const isFile = type === "f" || type === "regular file" || type === "regular empty file";
      if (isDir) l.dirs.push({ name: nm.name });
      else if (isFile) l.files.push({ name: nm.name, size, devMtime: normalizeDevMtime(mtime) });
      else l.other += 1;
    }
    return _sortListing(l);
  }

  async _list(rel, { afterPush = false } = {}) {
    await this.probe();
    const st = await this.session.stat(this._abs(rel));
    if (!st.ok || st.type !== "dir") return { ok: false, method: null, error: st.ok ? "not-a-directory" : `errno-${st.error}`, errno: st.error };
    if (this._caps.lsV2 || (!this._caps.findPrintf && !this._caps.statC)) return this._lis(rel);
    // The STA2 above ran on the push session: adbd serves it only after the
    // previous SEND's lutimes, so this shell listing can't race it.
    void afterPush;
    return this._shellList(rel);
  }

  async listRoot() {
    return this._list("");
  }

  async listFolder(name, opts) {
    const why = segmentProblem(name);
    if (why) return { ok: false, method: null, error: why };
    return this._list(name, opts);
  }

  /**
   * The root's directories by the rc-checked shell `find` (decision 10:
   * when anything reads as gone, a readdir error that ended LIS2 early must
   * not turn the hidden folder into a re-push). keys are raw-byte strings
   * (latin1), comparable with a listing's keys.
   */
  async listRootDirsAlt() {
    await this.probe();
    const root = this._abs("");
    const r = await this._shOk(`find ${shq(root)} -mindepth 1 -maxdepth 1 -type d`, "list-dirs");
    const prefix = Buffer.from(`${root}/`, "utf8");
    const keys = new Set();
    const buf = r.stdout;
    let start = 0;
    for (let i = 0; i <= buf.length; i += 1) {
      if (i < buf.length && buf[i] !== 0x0a) continue;
      const line = buf.subarray(start, i);
      start = i + 1;
      if (!line.length) continue;
      if (!line.subarray(0, prefix.length).equals(prefix)) return { ok: false, error: "unparsable-listing" };
      keys.add(Buffer.from(line.subarray(prefix.length)).toString("latin1"));
    }
    return { ok: true, keys };
  }

  async readBack(rel, { signal, idleMs } = {}) {
    try {
      const r = await this.session.pull(this._abs(rel), { sink: () => {}, signal, idleMs });
      return { ok: true, sha256: r.sha256, size: r.size };
    } catch (e) {
      if (e instanceof AdbError && e.kind === "remote-fail") return { ok: false, error: e.message, charges: e.charges };
      throw e;
    }
  }

  async readSmall(rel, maxBytes) {
    try {
      return await this.session.readSmall(this._abs(rel), { maxBytes });
    } catch (e) {
      if (e instanceof AdbError && e.kind === "remote-fail") return null;
      throw e;
    }
  }

  /**
   * SEND one PC file (hash-while-push: the sha is of the bytes sent). A PC
   * read error is tagged `source: true`: the PC side failed, not the device.
   */
  async push({ rel, pcPath, signal, onProgress, idleMs }) {
    const open = () => {
      const rs = fs.createReadStream(pcPath, { highWaterMark: wire.SYNC_DATA_MAX });
      return _tagSource(rs);
    };
    return this.session.push({ remotePath: this._abs(rel), open, signal, onProgress, idleMs });
  }

  async pushBuffer(rel, buf, { signal } = {}) {
    return this.session.push({ remotePath: this._abs(rel), open: () => buf, signal });
  }

  async mkdirp(rel, { signal } = {}) {
    await this._shOk(`mkdir -p -- ${shq(this._abs(rel))}`, "mkdir", { signal });
  }

  /**
   * rm in chunks under the shell cap. Never `-f`: a missing file is an error
   * the caller should see. On a non-zero rc or a fault after a chunk was sent
   * the caller re-lists the folder (`sent` contract): which files are gone is
   * read from the device, not inferred from rm's output.
   * @returns {{ok:boolean, sent:boolean, failed:string[], error?:AdbError, tooLong:string[]}}
   */
  async remove(rels, { signal } = {}) {
    const res = { ok: true, sent: false, failed: [], tooLong: [] };
    const base = "rm --";
    let chunk = [];
    let len = base.length;
    const flush = async () => {
      if (!chunk.length) return true;
      const words = chunk.map((c) => c.q);
      const names = chunk.map((c) => c.rel);
      chunk = [];
      len = base.length;
      let r;
      try {
        r = await this._sh(`${base} ${words.join(" ")}`, { signal });
      } catch (e) {
        if (e instanceof AdbError) {
          res.ok = false;
          res.error = e;
          if (e.sent) res.sent = true;
          res.failed.push(...names);
          return false;
        }
        throw e;
      }
      res.sent = true;
      if (r.rc !== 0) {
        res.ok = false;
        res.failed.push(...names);
        res.stderr = `${res.stderr || ""}${r.stderr.toString("utf8")}`;
      }
      return true;
    };
    for (const rel of rels) {
      const q = shq(this._abs(rel));
      if (base.length + 1 + q.length > SHELL_CMD_MAX) {
        res.ok = false;
        res.tooLong.push(rel);
        continue;
      }
      if (len + 1 + q.length > SHELL_CMD_MAX && !(await flush())) return res;
      chunk.push({ rel, q });
      len += 1 + q.length;
    }
    await flush();
    return res;
  }

  /**
   * mv after a STA2 of the destination returned ENOENT: `mv A B` onto an
   * existing directory B moves A INTO B. On case-insensitive storage a
   * case-only target STA2s as the source itself, so case-only renames go
   * through a temp name (the executor's job).
   * @returns {{ok:true} | {ok:false, reason:'destination-exists'|'source-missing'|string}}
   */
  async move(fromRel, toRel, { signal } = {}) {
    const dst = await this.session.stat(this._abs(toRel));
    if (dst.error !== wire.ERRNO.ENOENT) return { ok: false, reason: dst.ok ? "destination-exists" : `destination-errno-${dst.error}` };
    const src = await this.session.stat(this._abs(fromRel));
    if (!src.ok) return { ok: false, reason: "source-missing" };
    await this._shOk(`mv -- ${shq(this._abs(fromRel))} ${shq(this._abs(toRel))}`, "mv", { signal });
    return { ok: true };
  }

  async rmdirIfEmpty(rel, { signal } = {}) {
    const r = await this._sh(`rmdir -- ${shq(this._abs(rel))}`, { signal });
    return { removed: r.rc === 0, stderr: r.stderr.toString("utf8") };
  }

  /**
   * Verify's device-hash mode for one folder: rc-checked find + sha256sum,
   * with the scaled idle budget. A line GNU-escaped with a leading `\` (a
   * name holding `\` or a newline) is skipped: that file then has no hash
   * and keeps a still-valid entry or none (provenance.mergeVerify).
   */
  async hashFolder(name, { largest = 0, signal } = {}) {
    const dir = this._abs(name);
    const idleMs = VERIFY_BASE_MS + Math.ceil((largest / VERIFY_BYTES_PER_SEC) * 1000);
    const r = await this._sh(`find ${shq(dir)} -maxdepth 1 -type f ! -name '.*' -exec sha256sum {} +`, { idleMs, signal });
    const hashes = new Map();
    const prefix = `${dir}/`;
    for (const line of r.stdout.toString("utf8").split("\n")) {
      const m = /^([0-9a-f]{64}) [ *](.+)$/.exec(line);
      if (!m || !m[2].startsWith(prefix)) continue;
      const n = m[2].slice(prefix.length);
      if (!n.includes("/")) hashes.set(n, m[1]);
    }
    return { ok: r.rc === 0, rc: r.rc, hashes, idleMs };
  }

  async freeSpace() {
    return (await this.storage()).free;
  }

  async storage() {
    const caps = (await this.probe()).caps;
    const root = this._abs("");
    if (caps.statF) {
      const r = await this._sh(`stat -f -c '%a %b %S' ${shq(root)}`);
      const m = /^(\d+) (\d+) (\d+)/.exec(r.stdout.toString("utf8"));
      if (r.rc === 0 && m) return { free: Number(m[1]) * Number(m[3]), total: Number(m[2]) * Number(m[3]) };
    }
    if (caps.df) {
      const r = await this._sh(`df -P -k ${shq(root)}`);
      const kb = r.rc === 0 ? _parseDf(r.stdout.toString("utf8")) : null;
      if (kb != null) return { free: kb.avail * 1024, total: kb.total == null ? null : kb.total * 1024 };
    }
    return { free: null, total: null };
  }

  /**
   * The `connecting` policy (P3 decision): poll `devices -l` until the
   * device leaves connecting/authorizing, or timeoutMs.
   * @returns {Promise<{state:'device'|'timeout'|'gone'|'unauthorized'|'offline'|'cancelled'}>}
   */
  async waitReady({ timeoutMs = 60000, signal, pollMs = CONNECTING_POLL_MS } = {}) {
    const end = Date.now() + timeoutMs;
    for (;;) {
      if (signal && signal.aborted) return { state: "cancelled" };
      let rows;
      try {
        rows = await this.client.devices();
      } catch (e) {
        return { state: "gone", error: e };
      }
      const row = rows.find((x) => x.serial === this.serial);
      if (!row) return { state: "gone" };
      if (this.device.transportId != null && row.transportId != null && row.transportId !== this.device.transportId) return { state: "gone" };
      if (row.stateKind === "device") return { state: "device" };
      if (row.stateKind === "unauthorized" || row.stateKind === "no-permissions") return { state: "unauthorized" };
      if (row.stateKind !== "connecting" && row.stateKind !== "authorizing") return { state: "offline" };
      if (Date.now() >= end) return { state: "timeout" };
      await new Promise((r) => setTimeout(r, Math.min(pollMs, Math.max(1, end - Date.now()))));
    }
  }

  async close() {
    await this.session.close();
  }

  destroy() {
    this.destroyed = true;
    this.session.destroy();
  }
}

function _parseDf(text) {
  // POSIX `df -P -k`: header, then "fs 1024-blocks used available capacity mount".
  const lines = text.split(/\r?\n/).filter(Boolean);
  if (lines.length < 2) return null;
  const cols = lines[1].trim().split(/\s+/);
  const avail = Number(cols[3]);
  const total = Number(cols[1]);
  return cols.length >= 6 && Number.isFinite(avail) ? { avail, total: Number.isFinite(total) ? total : null } : null;
}

/** Tag errors from a PC read stream so the executor can tell them apart. */
function _tagSource(rs) {
  rs.on("error", (e) => {
    if (e && typeof e === "object") e.source = true;
  });
  return rs;
}

// ------------------------------------------------------------------
// FolderTransport
// ------------------------------------------------------------------

function _fsKind(e) {
  const c = e && e.code;
  if (c === "ENOSPC") return "no-space";
  if (c === "EROFS") return "read-only";
  return "remote-fail";
}

function _fsError(e, op, p) {
  if (e instanceof AdbError) return e;
  if (e && e.source) return e;
  if (e && (e.name === "AbortError" || e.code === "ABORT_ERR")) return new AdbError("cancelled", "cancelled", { op, path: p, cause: e });
  return new AdbError(_fsKind(e), `${op} ${p}: ${(e && e.message) || e}`, { op, path: p, cause: e, sent: true });
}

class FolderTransport {
  /**
   * @param {object} o
   * @param {string} o.root
   * @param {import('./hash-pool').HashPool} [o.hashPool]  for Verify
   * @param {typeof fs.promises} [o.fsp]  injectable for fault tests
   * @param {number} [o.presenceTimeoutMs]
   */
  constructor({ root, hashPool = null, fsp = fs.promises, presenceTimeoutMs = PRESENCE_TIMEOUT_MS } = {}) {
    this.kind = "folder";
    this.root = root;
    this.hashPool = hashPool;
    this.fsp = fsp;
    this.presenceTimeoutMs = presenceTimeoutMs;
    this.canonicalRoot = null;
    this._probe = null;
    this._presence = null;
    this.destroyed = false;
    this._controllers = new Set();
  }

  _abs(rel) {
    const root = this.canonicalRoot || this.root;
    if (!rel) return root;
    const segs = rel.split("/");
    for (const s of segs) {
      const why = segmentProblem(s) || (s.includes("\\") && process.platform === "win32" ? "separator" : null);
      if (why) throw new RangeError(`invalid folder path (${why}): ${JSON.stringify(rel)}`);
    }
    return path.join(root, ...segs);
  }

  /** See the header (adoptProbe). */
  adoptProbe(probe) {
    this._probe = probe;
    this.canonicalRoot = probe.canonicalRoot || this.root;
  }

  async probe({ force = false } = {}) {
    if (this._probe && !force) return this._probe;
    let canonicalRoot = this.root;
    let volumeId = null;
    try {
      canonicalRoot = await this.fsp.realpath(this.root);
      volumeId = String((await this.fsp.stat(canonicalRoot)).dev);
    } catch (_) {
      // statRoot/listRoot report the root's state; the probe stays usable
    }
    this.canonicalRoot = canonicalRoot;
    const cs = await this._probeCase();
    this._probe = {
      canonicalRoot,
      volumeId,
      caseInsensitive: cs.caseInsensitive,
      caseMeasured: cs.measured,
      caps: { sha256sum: true, local: true },
    };
    return this._probe;
  }

  async _probeCase() {
    const same = async (a, b) => {
      try {
        const [x, y] = await Promise.all([this.fsp.stat(a, { bigint: true }), this.fsp.stat(b, { bigint: true })]);
        return x.dev === y.dev && x.ino === y.ino ? "same" : "other";
      } catch (e) {
        return e && e.code === "ENOENT" ? "enoent" : "error";
      }
    };
    const tryIn = async (dirRel) => {
      const l = await this._read(dirRel);
      if (!l.ok) return null;
      const all = [...l.dirs, ...l.files];
      const present = new Set(all.map((e) => e.name));
      let tries = 0;
      for (const e of all) {
        const flipped = flipAsciiCase(e.name);
        if (!flipped || present.has(flipped)) continue;
        if (tries++ >= PROBE_FLIPS) break;
        const rel = (n) => (dirRel ? `${dirRel}/${n}` : n);
        const r = await same(this._abs(rel(e.name)), this._abs(rel(flipped)));
        if (r === "enoent") return { caseInsensitive: false, measured: true };
        if (r === "same") return { caseInsensitive: true, measured: true };
      }
      return { next: l.dirs.filter((d) => !d.name.startsWith(".")).slice(0, PROBE_SUBFOLDERS).map((d) => (dirRel ? `${dirRel}/${d.name}` : d.name)) };
    };
    const top = await tryIn("");
    if (top && top.measured) return top;
    for (const sub of (top && top.next) || []) {
      const r = await tryIn(sub);
      if (r && r.measured) return r;
    }
    return { caseInsensitive: true, measured: false };
  }

  async statRoot() {
    try {
      const st = await this.fsp.stat(this._abs(""));
      return { ok: st.isDirectory(), isDir: st.isDirectory(), error: 0 };
    } catch (e) {
      return { ok: false, isDir: false, error: (e && e.code) || "unreadable" };
    }
  }

  async stat(rel) {
    try {
      const st = await this.fsp.lstat(this._abs(rel));
      return {
        exists: true,
        type: st.isDirectory() ? "dir" : st.isFile() ? "file" : "other",
        size: st.isFile() ? st.size : null,
        devMtime: normalizeDevMtime(st.mtimeMs / 1000),
        error: 0,
      };
    } catch (e) {
      return { exists: false, type: null, size: null, devMtime: null, error: e && e.code === "ENOENT" ? wire.ERRNO.ENOENT : (e && e.code) || "unreadable" };
    }
  }

  async _read(rel) {
    const dir = this._abs(rel);
    let ents;
    try {
      ents = await this.fsp.readdir(dir, { withFileTypes: true, encoding: "buffer" });
    } catch (e) {
      return { ok: false, method: "readdir", error: (e && e.code) || "unreadable" };
    }
    const l = _emptyListing("readdir");
    for (const d of ents) {
      const raw = Buffer.isBuffer(d.name) ? d.name : Buffer.from(String(d.name), "utf8");
      l.keys.add(raw.toString("latin1"));
      if (d.isDirectory()) l.dirKeys.add(raw.toString("latin1"));
      const nm = wire.decodeName(raw);
      if (nm.name == null) {
        l.undecodable.push({ display: nm.display, hex: _hex(raw) });
        continue;
      }
      if (d.isDirectory()) {
        l.dirs.push({ name: nm.name });
        continue;
      }
      if (!d.isFile()) {
        l.other += 1;
        continue;
      }
      try {
        const st = await this.fsp.lstat(path.join(dir, nm.name));
        l.files.push({ name: nm.name, size: st.size, devMtime: normalizeDevMtime(st.mtimeMs / 1000) });
      } catch (e) {
        l.errored.push({ name: nm.name, error: (e && e.code) || "unreadable" });
      }
    }
    return _sortListing(l);
  }

  async listRoot() {
    await this.probe();
    const st = await this.statRoot();
    if (!st.ok) return { ok: false, method: null, error: `root-${st.error || "not-a-directory"}` };
    return this._read("");
  }

  async listFolder(name) {
    const why = segmentProblem(name);
    if (why) return { ok: false, method: null, error: why };
    return this._read(name);
  }

  /** The OS listing is the only one a folder has; readdir errors aren't silent. */
  async listRootDirsAlt() {
    const l = await this._read("");
    if (!l.ok) return { ok: false, error: l.error };
    const keys = new Set();
    for (const d of l.dirs) keys.add(Buffer.from(d.name, "utf8").toString("latin1"));
    return { ok: true, keys };
  }

  async readBack(rel, { signal } = {}) {
    if (!this.hashPool) throw new Error("FolderTransport.readBack needs a hashPool");
    const r = await this.hashPool.hashFile(this._abs(rel), { signal });
    if (r.error) return { ok: false, error: r.error };
    // A file written while it was hashed has no one content to report.
    if (r.changed) return { ok: false, error: "changed" };
    return { ok: true, sha256: r.sha256, size: r.size };
  }

  async readSmall(rel, maxBytes) {
    try {
      const p = this._abs(rel);
      const st = await this.fsp.stat(p);
      if (!st.isFile() || st.size > maxBytes) return null;
      return await this.fsp.readFile(p);
    } catch (_) {
      return null;
    }
  }

  _controller(signal) {
    const ac = new AbortController();
    const onAbort = () => ac.abort();
    if (signal) {
      if (signal.aborted) ac.abort();
      else signal.addEventListener("abort", onAbort, { once: true });
    }
    this._controllers.add(ac);
    return {
      signal: ac.signal,
      done: () => {
        this._controllers.delete(ac);
        if (signal) signal.removeEventListener("abort", onAbort);
      },
    };
  }

  /**
   * Copy to a dot-temp beside the target, fsync, rename over it. An
   * interrupted copy leaves the old file untouched (pending resolution's
   * "prev survived" row) plus a temp the sweep removes.
   */
  async push({ rel, pcPath, signal, onProgress }) {
    if (this.destroyed) throw new AdbError("cancelled", "cancelled", { op: "push", path: rel });
    const dst = this._abs(rel);
    const tmp = path.join(path.dirname(dst), `${FOLDER_TMP_PREFIX}${crypto.randomBytes(8).toString("hex")}`);
    const ctl = this._controller(signal);
    const hash = crypto.createHash("sha256");
    let bytes = 0;
    let out = null;
    let src = null;
    try {
      try {
        src = await this.fsp.open(pcPath, "r");
      } catch (e) {
        e.source = true;
        throw e;
      }
      out = await this.fsp.open(tmp, "wx");
      const buf = Buffer.allocUnsafe(1024 * 1024);
      for (;;) {
        if (ctl.signal.aborted) throw new AdbError("cancelled", "cancelled", { op: "push", path: rel });
        let n;
        try {
          ({ bytesRead: n } = await src.read(buf, 0, buf.length, null));
        } catch (e) {
          e.source = true;
          throw e;
        }
        if (!n) break;
        const chunk = buf.subarray(0, n);
        hash.update(chunk);
        await out.write(chunk, 0, n, null);
        bytes += n;
        if (onProgress) onProgress(bytes);
      }
      await out.sync();
      await out.close();
      out = null;
      await _renameRetry(this.fsp, tmp, dst);
      return { bytes, sha256: hash.digest("hex") };
    } catch (e) {
      if (out) await out.close().catch(() => {});
      await this.fsp.unlink(tmp).catch(() => {});
      if (!(await this._rootPresent())) throw new AdbError("device-lost", `folder target ${this.root} is gone`, { op: "push", path: rel, cause: e });
      throw _fsError(e, "push", rel);
    } finally {
      if (src) await src.close().catch(() => {});
      ctl.done();
    }
  }

  async pushBuffer(rel, buf) {
    const dst = this._abs(rel);
    const tmp = path.join(path.dirname(dst), `${FOLDER_TMP_PREFIX}${crypto.randomBytes(8).toString("hex")}`);
    try {
      await this.fsp.writeFile(tmp, buf, { flag: "wx" });
      await _renameRetry(this.fsp, tmp, dst);
      return { bytes: buf.length, sha256: crypto.createHash("sha256").update(buf).digest("hex") };
    } catch (e) {
      await this.fsp.unlink(tmp).catch(() => {});
      throw _fsError(e, "push", rel);
    }
  }

  async _rootPresent() {
    try {
      await this.fsp.stat(this.canonicalRoot || this.root);
      return true;
    } catch (_) {
      return false;
    }
  }

  async mkdirp(rel) {
    try {
      await this.fsp.mkdir(this._abs(rel), { recursive: true });
    } catch (e) {
      throw _fsError(e, "mkdir", rel);
    }
  }

  async remove(rels) {
    const res = { ok: true, sent: false, failed: [], tooLong: [] };
    for (const rel of rels) {
      try {
        res.sent = true;
        await this.fsp.unlink(this._abs(rel));
      } catch (e) {
        res.ok = false;
        res.failed.push(rel);
        if (!res.error) res.error = _fsError(e, "rm", rel);
      }
    }
    return res;
  }

  async move(fromRel, toRel) {
    const dst = await this.stat(toRel);
    if (dst.error !== wire.ERRNO.ENOENT) return { ok: false, reason: dst.exists ? "destination-exists" : `destination-${dst.error}` };
    const src = await this.stat(fromRel);
    if (!src.exists) return { ok: false, reason: "source-missing" };
    try {
      await _renameRetry(this.fsp, this._abs(fromRel), this._abs(toRel));
    } catch (e) {
      throw _fsError(e, "mv", fromRel);
    }
    return { ok: true };
  }

  async rmdirIfEmpty(rel) {
    try {
      await this.fsp.rmdir(this._abs(rel));
      return { removed: true };
    } catch (e) {
      return { removed: false, error: e && e.code };
    }
  }

  /** Verify on a folder hashes the copy itself (exact; no device shell). */
  async hashFolder(name, { signal } = {}) {
    if (!this.hashPool) throw new Error("FolderTransport.hashFolder needs a hashPool");
    const l = await this.listFolder(name);
    if (!l.ok) return { ok: false, rc: 1, hashes: new Map() };
    const hashes = new Map();
    let ok = true;
    for (const f of l.files) {
      if (f.name.startsWith(".")) continue;
      const r = await this.hashPool.hashFile(this._abs(`${name}/${f.name}`), { signal });
      if (r.error || r.changed) ok = false;
      else hashes.set(f.name, r.sha256);
    }
    return { ok, rc: ok ? 0 : 1, hashes };
  }

  /** Remove this transport's own leftovers (dot-temps) from one folder. */
  async sweepLeftovers(name) {
    const l = await this.listFolder(name);
    if (!l.ok) return 0;
    let n = 0;
    for (const f of l.files) {
      if (!f.name.startsWith(FOLDER_TMP_PREFIX)) continue;
      try {
        await this.fsp.unlink(this._abs(`${name}/${f.name}`));
        n += 1;
      } catch (_) {
        // locked; the next run sweeps it
      }
    }
    return n;
  }

  async freeSpace() {
    return (await this.storage()).free;
  }

  async storage() {
    try {
      if (typeof this.fsp.statfs !== "function") return { free: null, total: null };
      const s = await this.fsp.statfs(this.canonicalRoot || this.root);
      return { free: Number(s.bavail) * Number(s.bsize), total: Number(s.blocks) * Number(s.bsize) };
    } catch (_) {
      return { free: null, total: null };
    }
  }

  /**
   * Presence for the monitor: an async stat raced with a timeout, one check
   * in flight per target (a second caller joins the first).
   * @returns {Promise<{present:boolean, timedOut:boolean}>}
   */
  checkPresence() {
    if (this._presence) return this._presence;
    let timer = null;
    const p = Promise.race([
      this.fsp.stat(this.root).then(
        (st) => ({ present: st.isDirectory(), timedOut: false }),
        () => ({ present: false, timedOut: false }),
      ),
      new Promise((r) => {
        timer = setTimeout(() => r({ present: false, timedOut: true }), this.presenceTimeoutMs);
      }),
    ]).finally(() => {
      clearTimeout(timer);
      this._presence = null;
    });
    this._presence = p;
    return p;
  }

  async waitReady() {
    return { state: "device" };
  }

  async close() {}

  destroy() {
    this.destroyed = true;
    for (const ac of this._controllers) ac.abort();
  }
}

async function _renameRetry(fsp, a, b) {
  for (let attempt = 1; ; attempt += 1) {
    try {
      await fsp.rename(a, b);
      return;
    } catch (e) {
      if (!RETRY_CODES.has(e && e.code) || attempt >= RENAME_TRIES) throw e;
      await new Promise((r) => setTimeout(r, RENAME_BACKOFF_MS[attempt - 1]));
    }
  }
}

module.exports = {
  SEGMENT_MAX_BYTES,
  REMOTE_PATH_MAX,
  SHELL_CMD_MAX,
  VERIFY_BASE_MS,
  VERIFY_BYTES_PER_SEC,
  PRESENCE_TIMEOUT_MS,
  FOLDER_TMP_PREFIX,
  validatePath,
  segmentProblem,
  shq,
  flipAsciiCase,
  AdbTransport,
  FolderTransport,
};
