// ============================================================
// HISTORY MANAGER
//
// Persists download history, user settings, and the pending
// download queue to JSON files in Electron's userData folder:
//   Windows: %AppData%/aio-downloader-ui/
//
// Three files:
//   download_history.json  → list of past downloads
//   settings.json          → user preferences and defaults
//   download_queue.json    → queue snapshot, restored on next launch
//
// Read by electron/main.js only (it owns every IPC handler that
// touches these); the renderer reaches them through preload.js.
// ============================================================

const fs = require("fs");
const path = require("path");

// Watermark for the one-time settings fixups in _migrateSettings. Bump when
// you add a step there; `settingsSchemaVersion` is stamped into settings.json
// so each step runs exactly once per install.
const SETTINGS_SCHEMA_VERSION = 1;

class HistoryManager {
  constructor(userDataPath) {
    this._dataDir = userDataPath;
    this._historyPath = path.join(userDataPath, "download_history.json");
    this._settingsPath = path.join(userDataPath, "settings.json");
    this._queuePath = path.join(userDataPath, "download_queue.json");

    // Make sure the data directory exists
    fs.mkdirSync(userDataPath, { recursive: true });

    // Load existing data from disk (or start with empty arrays/objects)
    this._history = this._loadJson(this._historyPath, []);
    this._settings = this._loadJson(this._settingsPath, {});
    // null (not {}) when absent — the renderer treats a missing snapshot as
    // "nothing to restore" rather than "restore an empty queue".
    this._queue = this._loadJson(this._queuePath, null);

    // Runs HERE, in the constructor, and NOT in SettingsTab: main.js reads
    // settings for the updater at startup (grep initAppUpdater), long before
    // the renderer mounts — a renderer-side migration would leave auto-update
    // off until the user happened to open Settings and press Save.
    this._migrateSettings();
  }

  /**
   * One-time, versioned settings fixups. `settingsSchemaVersion` (absent = 0)
   * is the watermark; the stamp is persisted, so a relaunch is a no-op and a
   * future step is one more `if (from < N)` block plus a bump of the const.
   */
  _migrateSettings() {
    const from = Number(this._settings.settingsSchemaVersion) || 0;
    if (from >= SETTINGS_SCHEMA_VERSION) return;

    // v1 — appAutoUpdate flipped from opt-IN to opt-OUT, and every reader now
    // tests `!== false`. SettingsTab.handleSave spreads the ENTIRE ~90-key
    // draft, so anyone who ever pressed Save carries an explicit
    // `appAutoUpdate: false` written by the old default — indistinguishable
    // from a deliberate opt-out, and it would pin them off forever. That
    // stored false carries no intent, so drop it once and let the new default
    // apply. An explicit `true` is left alone.
    if (from < 1 && this._settings.appAutoUpdate === false) {
      delete this._settings.appAutoUpdate;
    }

    this._settings.settingsSchemaVersion = SETTINGS_SCHEMA_VERSION;
    this._saveJson(this._settingsPath, this._settings);
  }

  /**
   * Safely read a JSON file from disk.
   * Returns the fallback value if the file doesn't exist or is corrupted.
   */
  _loadJson(filePath, fallback) {
    try {
      if (fs.existsSync(filePath)) {
        const raw = fs.readFileSync(filePath, "utf8");
        return JSON.parse(raw);
      }
    } catch (err) {
      console.error(`Failed to load ${filePath}:`, err.message);
    }
    return fallback;
  }

  /**
   * Write data to a JSON file on disk.
   * Uses a temporary file + rename to avoid corrupted writes if the
   * app crashes mid-write.
   *
   * Windows EBUSY/EACCES handling: AV scanners and OneDrive shims
   * occasionally hold filePath open (typically for milliseconds) which
   * makes rename fail. Falls back to copyFileSync, which is happier
   * sharing a target with another reader. Always cleans up the tmp
   * file in the finally block — without this, a failed rename used
   * to leak the .tmp file alongside subsequent successful renames,
   * AND in-memory state diverged from disk for the entire app session.
   */
  _saveJson(filePath, data) {
    const tmp = filePath + ".tmp";
    let wrote = false;
    try {
      fs.writeFileSync(tmp, JSON.stringify(data, null, 2), "utf8");
      wrote = true;
      try {
        fs.renameSync(tmp, filePath);
      } catch (err) {
        if (err && (err.code === "EBUSY" || err.code === "EACCES" || err.code === "EPERM")) {
          // Windows lock contention. Copy is more permissive than rename
          // because it doesn't need exclusive access to the target's
          // directory entry, just write access to the file contents.
          try {
            fs.copyFileSync(tmp, filePath);
          } catch (copyErr) {
            console.error(
              `Failed to save ${filePath} (rename + copy fallback): ${copyErr.message}`,
            );
          }
        } else {
          console.error(`Failed to save ${filePath}: ${err.message}`);
        }
      }
    } catch (err) {
      // Write itself failed (disk full, permissions, etc.). Tmp may not
      // exist; the finally cleanup handles either case.
      console.error(`Failed to save ${filePath} (write phase): ${err.message}`);
    } finally {
      // Always clean up tmp regardless of which branch errored, so we
      // don't accumulate stale .tmp files alongside the real file.
      if (wrote) {
        try { fs.unlinkSync(tmp); } catch {}
      }
    }
  }

  // ── History ──

  getAll() {
    return [...this._history];
  }

  /**
   * Add or update an entry in the download history.
   * Called when a download completes, fails, or is cancelled.
   */
  updateEntry(downloadId, result) {
    const entry = {
      downloadId,
      timestamp: new Date().toISOString(),
      ...result,
    };

    // Check if this downloadId already exists (update it)
    const idx = this._history.findIndex((h) => h.downloadId === downloadId);
    if (idx >= 0) {
      this._history[idx] = { ...this._history[idx], ...entry };
    } else {
      // Add to the front (most recent first)
      this._history.unshift(entry);
    }

    // Keep only the last 200 entries to avoid the file growing forever
    if (this._history.length > 200) {
      this._history = this._history.slice(0, 200);
    }

    this._saveJson(this._historyPath, this._history);
  }

  // ── Settings ──

  getSettings() {
    return { ...this._settings };
  }

  /**
   * Persist settings, with defense-in-depth filtering of volatile path
   * values for the three path keys (pythonCmd, scriptPath, workingDir).
   *
   * The bug we're guarding against (2026-05-13): AppImage mounts its
   * contents at a random `/tmp/.mount_<basename><random>/` path that
   * changes on every launch. macOS Gatekeeper App Translocation runs
   * a fresh-installed .app from `/private/var/folders/.../AppTranslocation/<UUID>/`
   * and that UUID also rotates. Both flows can leak into the renderer's
   * Settings state and round-trip back to disk if the upstream
   * round-trip-prevention layer regresses. This filter drops the bad
   * values silently rather than persisting paths that will ENOENT on
   * the next launch.
   *
   * Three patterns rejected (any one matches → drop the key):
   *   - /^\/tmp\/\.mount_/                      AppImage squashfs mount
   *   - /\/AppTranslocation\/[0-9A-F-]+\//      Gatekeeper translocation
   *   - /\/Volumes\/[^/]+\.app\//                .app launched from DMG
   *
   * Non-path keys pass through unchanged. Other fields aren't filtered
   * because the failure mode is path-specific — a stale verboseAlways
   * doesn't break anything.
   */
  saveSettings(newSettings) {
    const VOLATILE_PATH_PATTERNS = [
      /^\/tmp\/\.mount_/,
      /\/AppTranslocation\/[0-9A-F-]+\//,
      /\/Volumes\/[^/]+\.app\//,
    ];
    const PATH_KEYS = ["pythonCmd", "scriptPath", "workingDir"];

    const filtered = { ...newSettings };
    for (const key of PATH_KEYS) {
      const value = filtered[key];
      if (typeof value !== "string" || value === "") continue;
      // Normalize to forward slashes once so the patterns work on values
      // that arrived with backslashes (Windows-side cross-platform code
      // that touched these fields). Pattern matching uses POSIX form.
      const normalized = value.replace(/\\/g, "/");
      for (const pattern of VOLATILE_PATH_PATTERNS) {
        if (pattern.test(normalized)) {
          console.warn(
            `[history] Rejecting volatile-path write for ${key}: ${value} ` +
            `(matched ${pattern}). Path will fall back to the runtime-resolved default.`
          );
          delete filtered[key];
          break;
        }
      }
    }

    this._settings = { ...this._settings, ...filtered };
    this._saveJson(this._settingsPath, this._settings);
  }

  // ── Download queue snapshot ──
  //
  // The queue is pure React state (useDownloader.js) and dies with the
  // renderer, so it's mirrored to disk and replayed on the next launch.
  // Written by useDownloader's debounced persist effect through main.js's
  // "queue:save" IPC; read once on mount via "queue:get". Shape:
  //   {
  //     queue:   [ <queue items verbatim, see useDownloader's STATE SHAPES> ],
  //     running: [ { hid, url, displayUrl, title, type, tmpDir, args } ],
  //     savedAt: <epoch ms>
  //   }
  // `running` = what was mid-download at close. The renderer reconstitutes
  // those against a FRESH scanResumable and drops any whose tmp_<hid> folder
  // is gone, so nothing here is trusted as proof that work is resumable.

  /** null when no snapshot has ever been written. */
  getQueueSnapshot() {
    return this._queue;
  }

  saveQueueSnapshot(snap) {
    this._queue =
      snap && typeof snap === "object"
        ? snap
        : { queue: [], running: [], savedAt: Date.now() };
    this._saveJson(this._queuePath, this._queue);
  }
}

module.exports = { HistoryManager };
