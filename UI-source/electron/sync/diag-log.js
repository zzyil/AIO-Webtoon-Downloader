// ============================================================
// DEVICE SYNC — SERVICE LOG (main process)
//
// Owns userData/sync/logs/service.log: one JSON object per line,
// {t: ISO time, ev, ...fields}. The installed app is a GUI exe whose stderr
// goes nowhere, so this file is the only place the service's own lifecycle
// and error lines survive. P5's packaged smoke reads it as its marker
// (non-UI plan P5; deviation 15): `start` says where sync/service loaded
// from (inside app.asar or not), `prewarm` says whether hashing ran in the
// eval'd workers, and `stop` / `quit-now` say how the service ended.
//
// Rules:
//   * never throws, never rejects: every write is wrapped, and a failure
//     only loses that line;
//   * writes only while the caller's `active()` answers true. service.js
//     answers false while sync is off and when syncDiagnosticLog is false
//     (sync-settings.js; the UI pass exposes the toggle), so "off means
//     inert" holds;
//   * synchronous appends: lines are few (start, pre-warm end, stop, error
//     lines, and a handful per sync job: start, one per progress step, end)
//     and shutdownNow() runs inside app.on("quit"), where an async write
//     would never land;
//   * bounded: past MAX_BYTES the file becomes service.1.log (replacing the
//     previous one) and a new file starts, so at most ~2 × MAX_BYTES.
//
// Read by: service.js (grep slog). Depends on: Node builtins only.
// ============================================================

const fs = require("fs");
const path = require("path");
const util = require("util");

const FILE = "service.log";
const OLD_FILE = "service.1.log";
const MAX_BYTES = 256 * 1024;
// One error line can carry a stack; longer is truncated, not dropped.
const MAX_LINE = 8 * 1024;

class ServiceLog {
  /**
   * @param {object} o
   * @param {string} o.dir         userData/sync/logs
   * @param {() => boolean} o.active  write gate, asked per line
   * @param {() => number} [o.now]
   * @param {number} [o.maxBytes]
   */
  constructor({ dir, active, now = Date.now, maxBytes = MAX_BYTES }) {
    this.dir = dir;
    this.file = path.join(dir, FILE);
    this.oldFile = path.join(dir, OLD_FILE);
    this.active = active;
    this.now = now;
    this.maxBytes = maxBytes;
    this.size = null; // learned on the first write
  }

  /** Append one event line when active. `fields` must be JSON-able. */
  event(ev, fields) {
    try {
      if (!this.active()) return false;
      let line = JSON.stringify({ t: new Date(this.now()).toISOString(), ev, ...(fields || {}) });
      if (line.length > MAX_LINE) line = JSON.stringify({ t: new Date(this.now()).toISOString(), ev, truncated: true, text: line.slice(0, MAX_LINE) });
      this._append(line + "\n");
      return true;
    } catch (_) {
      return false;
    }
  }

  /** An error line from service.js's _log arguments (strings, Errors, anything). */
  error(args) {
    try {
      if (!this.active()) return false;
      // Fit the ESCAPED text under the line cap with room for {t, ev}, so
      // event() never has to cut the line itself (that keeps a JSON prefix,
      // not the message's start). A stack's newlines double when escaped.
      const room = MAX_LINE - 128;
      let text = util.format(...(Array.isArray(args) ? args : [args])).slice(0, room);
      while (JSON.stringify(text).length > room) text = text.slice(0, Math.floor(text.length * 0.9));
      return this.event("error", { text });
    } catch (_) {
      return false;
    }
  }

  _append(data) {
    if (this.size == null) {
      fs.mkdirSync(this.dir, { recursive: true });
      try {
        this.size = fs.statSync(this.file).size;
      } catch (_) {
        this.size = 0;
      }
    }
    const n = Buffer.byteLength(data);
    if (this.size > 0 && this.size + n > this.maxBytes) {
      try {
        fs.renameSync(this.file, this.oldFile);
        this.size = 0;
      } catch (e) {
        // ENOENT: deleted under us, so the next append starts it fresh.
        // Otherwise a reader holding it open on Windows: keep appending,
        // retry next line.
        if (e && e.code === "ENOENT") this.size = 0;
      }
    }
    try {
      fs.appendFileSync(this.file, data);
    } catch (e) {
      // The logs folder was deleted while the app ran: recreate it once.
      if (!e || e.code !== "ENOENT") throw e;
      fs.mkdirSync(this.dir, { recursive: true });
      this.size = 0;
      fs.appendFileSync(this.file, data);
    }
    this.size += n;
  }
}

/** True when `file` sits inside an asar archive (the packaged build's sync/). */
function insideAsar(file) {
  return String(file).split(/[\\/]/).some((seg) => /\.asar$/i.test(seg));
}

module.exports = { ServiceLog, insideAsar, FILE, OLD_FILE, MAX_BYTES };
