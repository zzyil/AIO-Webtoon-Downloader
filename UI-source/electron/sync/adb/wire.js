// ============================================================
// DEVICE SYNC — ADB WIRE CODECS (pure)
//
// Owns the bytes of the three adb protocols the client speaks:
//   * the adb SERVER protocol on 127.0.0.1:5037 (smart-socket requests,
//     OKAY/FAIL, protocol strings, `devices -l` rows, track-devices frames);
//   * the adbd SYNC protocol (`sync:`: STAT/STA2/LST2, LIST/LIS2, SEND, RECV,
//     QUIT, at the exact AOSP struct sizes);
//   * the adbd SHELL v2 protocol (`shell,v2,raw:`: 1-byte id + u32 LE length).
// Nothing here opens a socket; adb/client.js does, and
// tools/fake-adb-server.js uses the same codecs from the device side.
//
// Every fact below was read in AOSP packages/modules/adb at main, commit
// 1cf2f01 (non-UI plan, "The adb wire client"); file:line refs are to that
// commit.
//
// BYTE LENGTHS EVERYWHERE: the 4-hex request length and SyncRequest
// .path_length count UTF-8 bytes. JS `.length` is wrong for `×` and `’`, which
// real series names contain. So every encoder takes a Buffer or encodes the
// string itself; nothing takes a length from a JS string.
//
// Read by: adb/client.js, tools/fake-adb-server.js,
// tools/_test_device_sync_adb.js (golden bytes).
// ============================================================

// --- sizes and limits -------------------------------------------------------

// SYNC_DATA_MAX in file_sync_protocol.h: the largest DATA payload either side
// sends or accepts.
const SYNC_DATA_MAX = 64 * 1024;

// handle_sync_command (daemon/file_sync_service.cpp): path_length > 1024 is
// answered with FAIL "path too long" and the session ends.
const SYNC_PATH_MAX = 1024;

// SEND v1's spec is "<path>,<mode>" with the mode in decimal, the CLI's own
// "%s,%d" (client/file_sync_client.cpp:585). 33188 = 0o100644.
const SEND_MODE_DEFAULT = 0o100644;

// The 1,024-byte limit covers path + ",33188", so a remote path may use at most
// 1,018 bytes. transports.js validatePath enforces it before anything is sent.
const REMOTE_PATH_MAX = SYNC_PATH_MAX - Buffer.byteLength(`,${SEND_MODE_DEFAULT}`);

// A shell service string ("shell,v2,raw:" + command) must fit an OPEN packet
// on an adbd that only negotiated the legacy 4 KiB payload (MAX_PAYLOAD_V1).
// 3,072 keeps headroom; callers that build long commands (rm/mv batches in
// transports.js) chunk to fit, and client.shell() refuses anything longer.
const SHELL_SERVICE_MAX = 3072;

// Sync message sizes (file_sync_protocol.h, all packed).
const SYNC_MSG_SIZE = 8; //   SyncRequest / sync_data / sync_status: id + u32
const STAT_V1_SIZE = 16; //   sync_stat_v1
const STAT_V2_SIZE = 72; //   sync_stat_v2
const DENT_V1_SIZE = 20; //   sync_dent_v1 (+ name)
const DENT_V2_SIZE = 76; //   sync_dent_v2 (+ name)

// MKID('S','T','A','T') is 'S' | 'T'<<8 | ..., so the little-endian u32 on the
// wire is the four ASCII bytes in order: ids are written and compared as
// 4-char latin1 strings.
const ID = Object.freeze({
  STAT: "STAT", // lstat v1 (16-byte reply, mode 0 on error)
  STA2: "STA2", // stat v2 (follows symlinks)
  LST2: "LST2", // lstat v2
  LIST: "LIST",
  LIS2: "LIS2",
  DENT: "DENT",
  DNT2: "DNT2",
  SEND: "SEND",
  SND2: "SND2",
  RECV: "RECV",
  RCV2: "RCV2",
  DONE: "DONE",
  DATA: "DATA",
  OKAY: "OKAY",
  FAIL: "FAIL",
  QUIT: "QUIT",
});

// shell_protocol.h ShellProtocol::Id.
const SHELL_ID = Object.freeze({
  STDIN: 0,
  STDOUT: 1,
  STDERR: 2,
  EXIT: 3,
  CLOSE_STDIN: 4,
  WINDOW_SIZE: 5,
});
const SHELL_HEADER_SIZE = 5;

// st_mode bits (Linux; the device is always Linux).
const S_IFMT = 0o170000;
const S_IFDIR = 0o040000;
const S_IFREG = 0o100000;
const S_IFLNK = 0o120000;

// The Linux errno values adbd puts on the wire. STA2 sends errno_to_wire(),
// LIS2 the raw errno; both are Linux numbers on a device.
const ERRNO = Object.freeze({
  EPERM: 1,
  ENOENT: 2,
  EIO: 5,
  EACCES: 13,
  EEXIST: 17,
  ENOTDIR: 20,
  EISDIR: 21,
  ENOSPC: 28,
  EROFS: 30,
  ENAMETOOLONG: 36,
  ELOOP: 40,
});

// --- small helpers ------------------------------------------------------------

function toBytes(s) {
  return Buffer.isBuffer(s) ? s : Buffer.from(String(s), "utf8");
}

/** 4 lowercase hex digits, as adb's "%04x". Throws past 0xffff (the framing can't say it). */
function hex4(n) {
  if (!Number.isInteger(n) || n < 0 || n > 0xffff) {
    throw new RangeError(`adb frame length out of range: ${n}`);
  }
  return n.toString(16).padStart(4, "0");
}

/** Parse a 4-hex length; null when the bytes aren't hex (a protocol fault). */
function parseHex4(buf) {
  const s = Buffer.isBuffer(buf) ? buf.toString("latin1", 0, 4) : String(buf).slice(0, 4);
  if (!/^[0-9a-fA-F]{4}$/.test(s)) return null;
  return parseInt(s, 16);
}

function u64ToNumber(v) {
  const n = Number(v);
  if (!Number.isSafeInteger(n)) throw new RangeError(`64-bit value past 2^53: ${v}`);
  return n;
}

function modeType(mode) {
  switch (mode & S_IFMT) {
    case S_IFDIR:
      return "dir";
    case S_IFREG:
      return "file";
    case S_IFLNK:
      return "link";
    case 0:
      return "none";
    default:
      return "other";
  }
}

const _utf8Strict = new TextDecoder("utf-8", { fatal: true });

/**
 * Device names are bytes. Returns {raw, name, display}: `name` is the exact
 * string only when `raw` is valid UTF-8 (else null: such a name is unmanaged,
 * never planned and never passed to rm/mv in a lossily decoded form);
 * `display` is always printable (U+FFFD for bad bytes).
 */
function decodeName(raw) {
  let name = null;
  try {
    name = _utf8Strict.decode(raw);
  } catch (_) {
    name = null;
  }
  return { raw, name, display: name != null ? name : raw.toString("utf8") };
}

// --- server protocol ----------------------------------------------------------

/** A smart-socket request: 4-hex UTF-8 byte length + the service. */
function encodeHostRequest(service) {
  const body = toBytes(service);
  return Buffer.concat([Buffer.from(hex4(body.length), "latin1"), body]);
}

/** SendProtocolString (adb_io.cpp:37): the same framing, used in replies. */
const encodeProtocolString = encodeHostRequest;

/** A server status: "OKAY", or "FAIL" + protocol string (adb_io.cpp:68-74). */
function encodeHostStatus(okay, message) {
  if (okay) return Buffer.from("OKAY", "latin1");
  return Buffer.concat([Buffer.from("FAIL", "latin1"), encodeProtocolString(message || "")]);
}

/**
 * Incremental splitter for 4-hex framed payloads: track-devices frames
 * (transport.cpp device_tracker_send, "%04x" + list) and protocol strings.
 * push() returns every frame completed by the chunk, as Buffers; an empty
 * "0000" frame yields an empty Buffer (the tracker sends it when the last
 * device leaves). A non-hex header throws: the stream is unrecoverable.
 */
class FrameSplitter {
  constructor() {
    this._q = new ByteQueue();
  }

  push(chunk) {
    this._q.push(chunk);
    const out = [];
    for (;;) {
      if (this._q.length < 4) break;
      const len = parseHex4(this._q.peek(4));
      if (len == null) throw new Error("adb protocol fault: bad frame length");
      if (this._q.length < 4 + len) break;
      this._q.take(4);
      out.push(this._q.take(len));
    }
    return out;
  }

  get buffered() {
    return this._q.length;
  }
}

// connection states (adb.cpp:144-172 to_string(ConnectionState)); "no
// permissions…" is kCsNoPerm's multi-word text (diagnose_usb.cpp:83-90).
const DEVICE_STATES = Object.freeze([
  "device",
  "offline",
  "unauthorized",
  "authorizing",
  "connecting",
  "detached",
  "bootloader",
  "recovery",
  "rescue",
  "sideload",
  "host",
]);
const _STATE_SET = new Set(DEVICE_STATES);
const NO_PERM_RE = /^no permissions(?:[^[\n]*\[[^\]\n]*\])?/;

// An empty serial is printed as this placeholder (transport.cpp
// append_transport). `host:tport:serial:` can't select it: MatchesTarget
// never matches an empty serial (transport.cpp:1289-1291).
const NO_SERIAL = "(no serial number)";

function _stateAt(text, i) {
  const m = NO_PERM_RE.exec(text.slice(i));
  if (m) return { state: m[0], kind: "no-permissions", end: i + m[0].length };
  const sp = text.indexOf(" ", i);
  const tok = sp === -1 ? text.slice(i) : text.slice(i, sp);
  if (_STATE_SET.has(tok)) return { state: tok, kind: tok, end: i + tok.length };
  return null;
}

/**
 * One `devices -l` row (transport.cpp:1407-1433): the serial padded "%-22s",
 * a space, the state, then " <devpath>" " product:" " model:" " device:"
 * (each only when non-empty) and always " transport_id:N" last.
 *
 * A serial can contain spaces ("(no serial number)"), so the keyed fields are
 * stripped from the END, and the state is the first known-state token that
 * starts at column 23 or later (the padding guarantees it can't start
 * earlier). Returns null for a row that has no recognizable state.
 */
function parseDevicesLRow(line) {
  let rest = line.replace(/\r$/, "");
  if (!rest.trim()) return null;
  const keyed = {};
  for (const key of ["transport_id", "device", "model", "product"]) {
    const m = new RegExp(` ${key}:(\\S*)$`).exec(rest);
    if (m) {
      keyed[key] = m[1];
      rest = rest.slice(0, m.index);
    }
  }
  let found = null;
  for (let i = 22; i < rest.length; i += 1) {
    if (rest[i] !== " ") continue;
    const st = _stateAt(rest, i + 1);
    if (st) {
      found = { at: i, ...st };
      break;
    }
  }
  if (!found) return null;
  const serial = rest.slice(0, found.at).replace(/ +$/, "");
  const devpath = rest.slice(found.end).trim() || null;
  const tid = keyed.transport_id;
  return {
    serial,
    state: found.state,
    stateKind: found.kind,
    devpath,
    product: keyed.product || null,
    model: keyed.model || null,
    device: keyed.device || null,
    transportId: tid != null && /^\d+$/.test(tid) ? Number(tid) : null,
    targetable: serial !== NO_SERIAL && serial !== "",
  };
}

/** `host:devices-l` / track-devices-l payload → rows (unparseable rows dropped). */
function parseDevicesL(text) {
  const out = [];
  for (const line of String(text).split("\n")) {
    const row = parseDevicesLRow(line);
    if (row) out.push(row);
  }
  return out;
}

/** `host:devices` short form: "serial\tstate" per line. */
function parseDevicesShort(text) {
  const out = [];
  for (const line of String(text).split("\n")) {
    const l = line.replace(/\r$/, "");
    const tab = l.lastIndexOf("\t");
    if (tab <= 0) continue;
    const serial = l.slice(0, tab);
    const state = l.slice(tab + 1);
    const kind = NO_PERM_RE.test(state) ? "no-permissions" : _STATE_SET.has(state) ? state : null;
    if (!kind) continue;
    out.push({ serial, state, stateKind: kind, transportId: null, targetable: serial !== NO_SERIAL && serial !== "" });
  }
  return out;
}

/** The device side of a row, for the fake server (append_transport, long form). */
function formatDevicesLRow(t) {
  const serial = t.serial ? t.serial : NO_SERIAL;
  let s = `${serial.padEnd(22, " ")} ${t.state}`;
  const add = (key, v, alnum) => {
    if (!v) return;
    s += ` ${key}${alnum ? String(v).replace(/[^A-Za-z0-9]/g, "_") : String(v).replace(/\n/g, "_")}`;
  };
  add("", t.devpath, false);
  add("product:", t.product, false);
  add("model:", t.model, true);
  add("device:", t.device, false);
  s += ` transport_id:${t.transportId}`;
  return `${s}\n`;
}

/** `host-serial:<s>:features` payload: comma-joined (FeatureSetToString). */
function parseFeatures(text) {
  return new Set(
    String(text)
      .split(",")
      .map((f) => f.trim())
      .filter(Boolean),
  );
}

// --- sync protocol --------------------------------------------------------------

function _idBuf(id) {
  if (typeof id !== "string" || id.length !== 4) throw new Error(`bad sync id: ${id}`);
  return Buffer.from(id, "latin1");
}

/** id + u32 LE: SyncRequest header, DATA/DONE header, OKAY status, QUIT. */
function encodeSyncMsg(id, value) {
  const b = Buffer.alloc(SYNC_MSG_SIZE);
  _idBuf(id).copy(b, 0);
  b.writeUInt32LE(value >>> 0, 4);
  return b;
}

/**
 * A sync request: id + u32 path_length (UTF-8 bytes) + path, not
 * NUL-terminated. Throws past SYNC_PATH_MAX so a too-long path never reaches
 * adbd (which would FAIL and end the session).
 */
function encodeSyncRequest(id, path) {
  const p = toBytes(path);
  if (p.length > SYNC_PATH_MAX) throw new RangeError(`sync path is ${p.length} bytes (max ${SYNC_PATH_MAX})`);
  if (p.includes(0)) throw new RangeError("sync path contains NUL");
  return Buffer.concat([encodeSyncMsg(id, p.length), p]);
}

/** SEND v1: "<path>,<decimal mode>"; adbd splits at the LAST comma (:563). */
function encodeSendV1(path, mode = SEND_MODE_DEFAULT) {
  return encodeSyncRequest(ID.SEND, Buffer.concat([toBytes(path), Buffer.from(`,${mode >>> 0}`, "latin1")]));
}

function encodeDataHeader(len) {
  if (len > SYNC_DATA_MAX) throw new RangeError(`DATA chunk of ${len} bytes (max ${SYNC_DATA_MAX})`);
  return encodeSyncMsg(ID.DATA, len);
}

/** DONE carrying the mtime adbd lutimes() the file to after OKAY. */
function encodeDone(mtimeSec) {
  return encodeSyncMsg(ID.DONE, Math.max(0, Math.floor(mtimeSec || 0)));
}

const encodeQuit = () => encodeSyncMsg(ID.QUIT, 0);

/** Sync-level FAIL: "FAIL" + u32 LE length + text (SendSyncFail, :265-272). Not hex. */
function encodeSyncFail(message) {
  const m = toBytes(message);
  return Buffer.concat([encodeSyncMsg(ID.FAIL, m.length), m]);
}

function decodeSyncMsg(buf) {
  return { id: buf.toString("latin1", 0, 4), value: buf.readUInt32LE(4) };
}

function decodeStatV1(buf) {
  return {
    id: buf.toString("latin1", 0, 4),
    mode: buf.readUInt32LE(4),
    size: buf.readUInt32LE(8),
    mtime: buf.readUInt32LE(12),
  };
}

function _decodeStatV2At(buf) {
  return {
    id: buf.toString("latin1", 0, 4),
    error: buf.readUInt32LE(4),
    dev: buf.readBigUInt64LE(8),
    ino: buf.readBigUInt64LE(16),
    mode: buf.readUInt32LE(24),
    nlink: buf.readUInt32LE(28),
    uid: buf.readUInt32LE(32),
    gid: buf.readUInt32LE(36),
    size: u64ToNumber(buf.readBigUInt64LE(40)),
    atime: Number(buf.readBigInt64LE(48)),
    mtime: Number(buf.readBigInt64LE(56)),
    ctime: Number(buf.readBigInt64LE(64)),
  };
}

const decodeStatV2 = (buf) => _decodeStatV2At(buf);

function decodeDentV1(buf) {
  return { ...decodeStatV1(buf), namelen: buf.readUInt32LE(16) };
}

function decodeDentV2(buf) {
  return { ..._decodeStatV2At(buf), namelen: buf.readUInt32LE(72) };
}

// Device-side encoders (the fake server). Missing fields encode as 0, which
// is what adbd sends for a failed stat (do_stat_v2 / do_list memset).

function encodeStatV1(o = {}) {
  const b = Buffer.alloc(STAT_V1_SIZE);
  _idBuf(o.id || ID.STAT).copy(b, 0);
  b.writeUInt32LE((o.mode || 0) >>> 0, 4);
  b.writeUInt32LE((o.size || 0) >>> 0, 8);
  b.writeUInt32LE((o.mtime || 0) >>> 0, 12);
  return b;
}

function _writeStatV2(b, o) {
  _idBuf(o.id).copy(b, 0);
  b.writeUInt32LE((o.error || 0) >>> 0, 4);
  b.writeBigUInt64LE(BigInt(o.dev || 0), 8);
  b.writeBigUInt64LE(BigInt(o.ino || 0), 16);
  b.writeUInt32LE((o.mode || 0) >>> 0, 24);
  b.writeUInt32LE((o.nlink || 0) >>> 0, 28);
  b.writeUInt32LE((o.uid || 0) >>> 0, 32);
  b.writeUInt32LE((o.gid || 0) >>> 0, 36);
  b.writeBigUInt64LE(BigInt(o.size || 0), 40);
  b.writeBigInt64LE(BigInt(o.atime || 0), 48);
  b.writeBigInt64LE(BigInt(o.mtime || 0), 56);
  b.writeBigInt64LE(BigInt(o.ctime || 0), 64);
}

function encodeStatV2(o = {}) {
  const b = Buffer.alloc(STAT_V2_SIZE);
  _writeStatV2(b, { ...o, id: o.id || ID.STA2 });
  return b;
}

/** A listing entry; `name` is raw bytes. v2 → DNT2 (76 B), else DENT (20 B). */
function encodeDent(v2, o, name) {
  const n = toBytes(name);
  if (v2) {
    const b = Buffer.alloc(DENT_V2_SIZE);
    _writeStatV2(b, { ...o, id: o.id || ID.DNT2 });
    b.writeUInt32LE(n.length, 72);
    return Buffer.concat([b, n]);
  }
  const b = Buffer.alloc(DENT_V1_SIZE);
  _idBuf(o.id || ID.DENT).copy(b, 0);
  b.writeUInt32LE((o.mode || 0) >>> 0, 4);
  b.writeUInt32LE((o.size || 0) >>> 0, 8);
  b.writeUInt32LE((o.mtime || 0) >>> 0, 12);
  b.writeUInt32LE(n.length, 16);
  return Buffer.concat([b, n]);
}

/**
 * The DONE that ends a listing is a FULL dent record, zeroed, with id DONE
 * (do_list's `done:` label memsets the whole msg). A reader that takes only 8
 * bytes desyncs the next reply.
 */
function encodeDentDone(v2) {
  const b = Buffer.alloc(v2 ? DENT_V2_SIZE : DENT_V1_SIZE);
  _idBuf(ID.DONE).copy(b, 0);
  return b;
}

// --- shell v2 -------------------------------------------------------------------

function encodeShellPacket(id, data) {
  const d = data == null ? Buffer.alloc(0) : toBytes(data);
  const h = Buffer.alloc(SHELL_HEADER_SIZE);
  h.writeUInt8(id, 0);
  h.writeUInt32LE(d.length, 1);
  return Buffer.concat([h, d]);
}

/** Incremental shell-v2 demuxer: push(chunk) → [{id, data}] completed packets. */
class ShellDemuxer {
  constructor() {
    this._q = new ByteQueue();
  }

  push(chunk) {
    this._q.push(chunk);
    const out = [];
    for (;;) {
      if (this._q.length < SHELL_HEADER_SIZE) break;
      const h = this._q.peek(SHELL_HEADER_SIZE);
      const len = h.readUInt32LE(1);
      if (this._q.length < SHELL_HEADER_SIZE + len) break;
      this._q.take(SHELL_HEADER_SIZE);
      out.push({ id: h[0], data: this._q.take(len) });
    }
    return out;
  }

  get buffered() {
    return this._q.length;
  }
}

// --- byte queue -------------------------------------------------------------------

/**
 * FIFO of Buffers with exact-length take(): the one place reply bytes are
 * split, so a packet that straddles socket chunks (at ANY offset) reads the
 * same as one that arrives whole. take() copies only when a read straddles
 * chunks.
 */
class ByteQueue {
  constructor() {
    this._chunks = [];
    this._off = 0;
    this.length = 0;
  }

  push(chunk) {
    if (!chunk || !chunk.length) return;
    this._chunks.push(chunk);
    this.length += chunk.length;
  }

  _read(n, consume) {
    if (n > this.length) throw new RangeError(`ByteQueue: want ${n}, have ${this.length}`);
    if (n === 0) return Buffer.alloc(0);
    const first = this._chunks[0];
    if (first.length - this._off >= n) {
      const out = first.subarray(this._off, this._off + n);
      if (consume) {
        this._off += n;
        this.length -= n;
        if (this._off === first.length) {
          this._chunks.shift();
          this._off = 0;
        }
      }
      return out;
    }
    const out = Buffer.allocUnsafe(n);
    let filled = 0;
    let i = 0;
    let off = this._off;
    while (filled < n) {
      const c = this._chunks[i];
      const k = Math.min(c.length - off, n - filled);
      c.copy(out, filled, off, off + k);
      filled += k;
      off += k;
      if (off === c.length) {
        i += 1;
        off = 0;
      }
    }
    if (consume) {
      this._chunks.splice(0, i);
      this._off = off;
      this.length -= n;
    }
    return out;
  }

  peek(n) {
    return this._read(n, false);
  }

  take(n) {
    return this._read(n, true);
  }
}

module.exports = {
  SYNC_DATA_MAX,
  SYNC_PATH_MAX,
  SEND_MODE_DEFAULT,
  REMOTE_PATH_MAX,
  SHELL_SERVICE_MAX,
  SYNC_MSG_SIZE,
  STAT_V1_SIZE,
  STAT_V2_SIZE,
  DENT_V1_SIZE,
  DENT_V2_SIZE,
  ID,
  SHELL_ID,
  SHELL_HEADER_SIZE,
  S_IFMT,
  S_IFDIR,
  S_IFREG,
  S_IFLNK,
  ERRNO,
  DEVICE_STATES,
  NO_SERIAL,
  hex4,
  parseHex4,
  modeType,
  decodeName,
  encodeHostRequest,
  encodeProtocolString,
  encodeHostStatus,
  FrameSplitter,
  parseDevicesLRow,
  parseDevicesL,
  parseDevicesShort,
  formatDevicesLRow,
  parseFeatures,
  encodeSyncMsg,
  encodeSyncRequest,
  encodeSendV1,
  encodeDataHeader,
  encodeDone,
  encodeQuit,
  encodeSyncFail,
  decodeSyncMsg,
  decodeStatV1,
  decodeStatV2,
  decodeDentV1,
  decodeDentV2,
  encodeStatV1,
  encodeStatV2,
  encodeDent,
  encodeDentDone,
  encodeShellPacket,
  ShellDemuxer,
  ByteQueue,
};
