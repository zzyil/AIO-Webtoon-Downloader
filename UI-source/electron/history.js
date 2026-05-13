// ============================================================
// HISTORY MANAGER
//
// Persists download history and user settings to JSON files
// in Electron's userData folder:
//   Windows: %AppData%/aio-downloader-ui/
//
// Two files:
//   download_history.json  → list of past downloads
//   settings.json          → user preferences and defaults
// ============================================================

const fs = require("fs");
const path = require("path");

class HistoryManager {
  constructor(userDataPath) {
    this._dataDir = userDataPath;
    this._historyPath = path.join(userDataPath, "download_history.json");
    this._settingsPath = path.join(userDataPath, "settings.json");

    // Make sure the data directory exists
    fs.mkdirSync(userDataPath, { recursive: true });

    // Load existing data from disk (or start with empty arrays/objects)
    this._history = this._loadJson(this._historyPath, []);
    this._settings = this._loadJson(this._settingsPath, {});
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
}

module.exports = { HistoryManager };
