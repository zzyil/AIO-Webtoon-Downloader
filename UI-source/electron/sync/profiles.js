// ============================================================
// DEVICE SYNC — READER PROFILES AND TARGET CONFIG (pure)
//
// Owns what a target mirrors and how its fields default:
//   * PROFILES — the reader presets (everything, komikku, pdf) and custom.
//     A preset FIXES formats, sidecars, .nomedia and the name options
//     (CUSTOM_FIELDS); only a custom target stores its own. validateTarget
//     refuses a stored custom field on a preset, so a preset target can't
//     carry dormant overrides (user decision 2026-10-16: "custom allows for
//     the customization of everything including .nomedia");
//   * resolveTargetConfig(target) — every per-target field filled in;
//   * isMirrored / isChapterFormat / isSidecar — the per-file questions the
//     planner asks;
//   * validateTarget — what sync:config-op refuses (root containment, field
//     sets);
//   * recordIdentity / recordIdentityChanged — parent rule 12: a record
//     describes one place, so a device, root or volume change clears it
//     (service.js writes a new recordEpoch). The PROFILE is not part of it
//     (user decision 2026-10-16): a preset only filters which files mirror,
//     and editing a custom target's formats never cleared history either.
//     Headers written before that still carry a `profile` key; nothing reads it.
//
// Read by: planner.js, service.js (config ops), pc-inventory.js.
// The Komikku facts come from sync-temp/plans/komikkuspec.md (LocalSource).
// ============================================================

const path = require("path");
const { NAMING_POLICIES } = require("./naming");

// Every archive a reader might treat as a chapter. Unclaimed device files of
// these types are delete candidates whatever the profile mirrors (parent
// rule 4); anything else on the device is unmanaged and never offered.
const CHAPTER_FORMATS = Object.freeze(["pdf", "cbz", "zip", "cbr", "rar", "epub"]);

const SIDECAR_NAMES = Object.freeze(["cover.jpg", "details.json"]);

// CompareManga sync_to_tablet SIDECARS: AIO bookkeeping removed from every
// managed device folder before its pushes. Dot-files only (validateTarget):
// a mirrored name here would be deleted and re-pushed on every sync.
const DEFAULT_CLEANUP_NAMES = Object.freeze([".aio_series.json", ".mangafire_hid", ".series_hid", ".cover.webp"]);

// Order is the editor's order (TargetEditor reads PROFILE_IDS via copy.js).
const PROFILES = Object.freeze({
  // Every chapter format, both sidecars, .nomedia. The default: on a library
  // of one format it copies exactly what the komikku preset would.
  everything: Object.freeze({
    formats: CHAPTER_FORMATS,
    sidecars: SIDECAR_NAMES,
    nomedia: true,
    flags: Object.freeze({}),
  }),
  // Komikku/Mihon LocalSource: archives plus cover.jpg and details.json; PDF
  // isn't a LocalSource format, a "~" decimal breaks its ChapterRecognition,
  // and a series without cover.jpg shows no cover. The flags WARN; nothing is
  // renamed (user decision 2026-10-16: device names always equal PC names).
  komikku: Object.freeze({
    formats: Object.freeze(["cbz", "zip", "cbr", "rar", "epub"]),
    sidecars: SIDECAR_NAMES,
    nomedia: true,
    flags: Object.freeze({ pdf: true, tilde: true, missingCover: true }),
  }),
  // PDF readers: PDFs and the folder cover; .nomedia keeps the covers out of
  // the gallery.
  pdf: Object.freeze({
    formats: Object.freeze(["pdf"]),
    sidecars: Object.freeze(["cover.jpg"]),
    nomedia: true,
    flags: Object.freeze({}),
  }),
  // The target's own values; these are only the defaults of a field it
  // leaves null (service.js update-target seeds them from the preset it
  // leaves, so switching to custom changes no file).
  custom: Object.freeze({
    formats: CHAPTER_FORMATS,
    sidecars: SIDECAR_NAMES,
    nomedia: true,
    flags: Object.freeze({}),
  }),
});
const PROFILE_IDS = Object.freeze(Object.keys(PROFILES));
const DEFAULT_PROFILE = "everything";
// Fields only a custom target stores. A preset resolves them to its own
// values (formats/sidecars/nomedia) or to the fixed name options below.
const CUSTOM_FIELDS = Object.freeze(["formats", "sidecars", "nomedia", "namingPolicy", "renameToMatch", "cleanupNames"]);
const FIXED_NAMING_POLICY = "strip-hid";

const TARGET_KINDS = Object.freeze(["adb", "folder"]);
const SELECTION_MODES = Object.freeze(["all-except", "only"]);
// A stored verifyMode is the user's answer to a needs-mode refusal (a device
// without sha256sum): every later prepare of the target uses it, automatic
// connect plans included, so they don't refuse again. Not part of the record
// identity: changing it changes how folders are checked, not which record
// they belong to. device-hash isn't storable; it is the default when the
// device can hash. Consumers: service.js _planOpts, contract.js VERIFY_MODES.
const STORED_VERIFY_MODES = Object.freeze(["read-back", "adopt-size"]);
// A target's removal policy (approved design D5): guarded offers and
// pre-selects proven removals, add-only never pre-selects one, auto-replace
// is guarded plus pre-selecting the swap of a hash-verified foreign copy for
// the PC's (plan sync-temp/plans/device-sync-auto-replace.md; the rule lives
// in planner.js _planExisting, grep autoReplace). A permanent
// per-target field; the global syncDeletePolicy is only the value a NEW
// target starts with (service.js add-target stamps it). A target saved before
// the field existed resolves null here and the planner falls back to the
// global (planner.js buildPlan, grep `cfg.deletePolicy`).
const DELETE_POLICIES = Object.freeze(["guarded", "add-only", "auto-replace"]);

function _arr(v) {
  return Array.isArray(v) ? v.slice() : [];
}

/**
 * Every per-target field resolved. The returned object is what the planner
 * and executor read; the stored target keeps its nulls.
 */
function resolveTargetConfig(target) {
  const t = target && typeof target === "object" ? target : {};
  const profileId = PROFILES[t.profile] ? t.profile : DEFAULT_PROFILE;
  const p = PROFILES[profileId];
  // A preset ignores any stored custom field (validateTarget refuses one; this
  // covers a hand-edited sync-targets.json).
  const own = profileId === "custom" ? t : {};
  const formats = Array.isArray(own.formats) && own.formats.length ? own.formats : p.formats;
  const sidecars = Array.isArray(own.sidecars) ? own.sidecars : p.sidecars;
  return {
    id: t.id,
    name: t.name || "",
    kind: TARGET_KINDS.includes(t.kind) ? t.kind : "folder",
    serial: t.serial || null,
    root: t.root || "",
    volumeId: t.volumeId == null ? null : t.volumeId,
    profile: profileId,
    formats: formats.map((f) => String(f).toLowerCase()),
    sidecars: sidecars.slice(),
    flags: p.flags,
    nomedia: typeof own.nomedia === "boolean" ? own.nomedia : p.nomedia,
    namingPolicy: NAMING_POLICIES.includes(own.namingPolicy) ? own.namingPolicy : FIXED_NAMING_POLICY,
    renameToMatch: own.renameToMatch === true,
    cleanupNames: Array.isArray(own.cleanupNames) ? own.cleanupNames.slice() : DEFAULT_CLEANUP_NAMES.slice(),
    selection: SELECTION_MODES.includes(t.selection) ? t.selection : "all-except",
    verifyMode: STORED_VERIFY_MODES.includes(t.verifyMode) ? t.verifyMode : null,
    deletePolicy: DELETE_POLICIES.includes(t.deletePolicy) ? t.deletePolicy : null,
    only: _arr(t.only),
    excludes: _arr(t.excludes),
    aliases: _arr(t.aliases),
    ignoredSuggestions: _arr(t.ignoredSuggestions),
    acknowledged: _arr(t.acknowledged),
  };
}

function _ext(name) {
  const m = String(name).match(/\.([^./\\]+)$/);
  return m ? m[1].toLowerCase() : "";
}

function isChapterFormat(name) {
  return CHAPTER_FORMATS.includes(_ext(name));
}

// Sidecar names are exact: Komikku looks for "cover.jpg", not "Cover.JPG".
function isSidecar(name, cfg) {
  return cfg.sidecars.includes(String(name));
}

/** True when a depth-1 PC file is part of what this target mirrors. */
function isMirrored(name, cfg) {
  const n = String(name);
  if (!n || n.startsWith(".")) return false;
  return isSidecar(n, cfg) || cfg.formats.includes(_ext(n));
}

/**
 * True when `child` is `parent` or lies inside it. `platform` picks the path
 * flavor so the test suite can check Windows rules on Linux; Windows paths
 * compare case-insensitively, as NTFS does.
 */
function pathContains(parent, child, platform) {
  const P = platform === "win32" ? path.win32 : path.posix;
  const fold = (s) => (platform === "win32" ? s.toLowerCase() : s);
  const a = fold(P.resolve(String(parent)));
  const b = fold(P.resolve(String(child)));
  if (a === b) return true;
  const rel = P.relative(a, b);
  return !!rel && !rel.startsWith("..") && !P.isAbsolute(rel);
}

/**
 * Field errors for a target about to be saved (sync:config-op add-target /
 * update-target). Empty array when valid.
 *
 * A folder target's root may not be inside, or contain, the library root or
 * userData: a target at <library>/_mirror would copy the library into itself
 * on every sync, and one containing userData would mirror into the app's own
 * state (non-UI plan, review #20).
 *
 * @param {object} target
 * @param {{libraryRoot?:string, userDataDir?:string, platform?:string}} ctx
 * @returns {Array<{field:string, message:string}>}
 */
function validateTarget(target, ctx) {
  const errors = [];
  const t = target || {};
  const c = ctx || {};
  const platform = c.platform || process.platform;
  if (!TARGET_KINDS.includes(t.kind)) errors.push({ field: "kind", message: "kind must be adb or folder" });
  if (!PROFILES[t.profile]) errors.push({ field: "profile", message: "pick a reader" });
  if (!t.root || typeof t.root !== "string") {
    errors.push({ field: "root", message: "pick the folder to sync into" });
  } else if (t.kind === "adb") {
    if (!t.root.startsWith("/") || /[\x00\r\n]/.test(t.root) || t.root.split("/").includes("..")) {
      errors.push({ field: "root", message: "the device folder must be an absolute path without '..'" });
    }
    if (!t.serial) errors.push({ field: "serial", message: "pick a device" });
  } else if (t.kind === "folder") {
    for (const [label, other] of [["the library", c.libraryRoot], ["the app's data folder", c.userDataDir]]) {
      if (!other) continue;
      if (pathContains(other, t.root, platform) || pathContains(t.root, other, platform)) {
        errors.push({ field: "root", message: `the sync folder can't be inside or contain ${label}` });
      }
    }
  }
  if (t.profile === "custom") {
    if (Array.isArray(t.formats) && !t.formats.length) {
      errors.push({ field: "formats", message: "a custom reader needs at least one chapter format" });
    }
  } else if (PROFILES[t.profile]) {
    for (const f of CUSTOM_FIELDS) {
      if (t[f] != null) errors.push({ field: f, message: "only a Custom reader can change this" });
    }
  }
  if (t.nomedia != null && typeof t.nomedia !== "boolean") errors.push({ field: "nomedia", message: ".nomedia must be on or off" });
  if (t.renameToMatch != null && typeof t.renameToMatch !== "boolean") {
    errors.push({ field: "renameToMatch", message: "rename to match must be on or off" });
  }
  if (Array.isArray(t.formats) && t.formats.some((f) => !CHAPTER_FORMATS.includes(String(f).toLowerCase()))) {
    errors.push({ field: "formats", message: `formats must be among ${CHAPTER_FORMATS.join(", ")}` });
  }
  if (Array.isArray(t.sidecars) && t.sidecars.some((s) => !SIDECAR_NAMES.includes(s))) {
    errors.push({ field: "sidecars", message: `sidecars must be among ${SIDECAR_NAMES.join(", ")}` });
  }
  if (
    Array.isArray(t.cleanupNames) &&
    t.cleanupNames.some((n) => typeof n !== "string" || !n.startsWith(".") || n.includes("/") || n.length < 2)
  ) {
    errors.push({ field: "cleanupNames", message: "cleanup names must be dot-files" });
  }
  if (t.namingPolicy != null && !NAMING_POLICIES.includes(t.namingPolicy)) {
    errors.push({ field: "namingPolicy", message: "unknown naming policy" });
  }
  if (t.selection != null && !SELECTION_MODES.includes(t.selection)) {
    errors.push({ field: "selection", message: "unknown series selection mode" });
  }
  if (t.verifyMode != null && !STORED_VERIFY_MODES.includes(t.verifyMode)) {
    errors.push({ field: "verifyMode", message: "verify mode must be read-back or adopt-size" });
  }
  if (t.deletePolicy != null && !DELETE_POLICIES.includes(t.deletePolicy)) {
    errors.push({ field: "deletePolicy", message: "removals must be guarded, add-only or auto-replace" });
  }
  if (Array.isArray(t.excludes) && t.excludes.some((e) => e && e.removeFiles != null && typeof e.removeFiles !== "boolean")) {
    errors.push({ field: "excludes", message: "removeFiles must be true or false" });
  }
  return errors;
}

/**
 * The fields a record header pins (parent rule 3 / rule 12): a record is
 * only meaningful for this kind, device or folder (and its volume) and root.
 * canonicalRoot is measured by the transport and compared separately.
 * Consumers: provenance.js loadRecordView (grep RECORD_IDENTITY_FIELDS),
 * executor.js _ensureHeader, service.js _identityClear.
 */
const RECORD_IDENTITY_FIELDS = Object.freeze(["kind", "serial", "root", "volumeId"]);

function recordIdentity(target) {
  const t = target || {};
  return {
    kind: t.kind || null,
    serial: t.kind === "adb" ? t.serial || null : null,
    root: t.root || null,
    volumeId: t.kind === "folder" && t.volumeId != null ? String(t.volumeId) : null,
  };
}

/** Rule 12: true when an edit moves the target to another place. The volume
 * is measured, not edited, so it isn't compared here. */
function recordIdentityChanged(before, after) {
  const a = recordIdentity(before);
  const b = recordIdentity(after);
  return a.kind !== b.kind || a.serial !== b.serial || a.root !== b.root;
}

module.exports = {
  CHAPTER_FORMATS,
  SIDECAR_NAMES,
  DEFAULT_CLEANUP_NAMES,
  PROFILES,
  PROFILE_IDS,
  DEFAULT_PROFILE,
  CUSTOM_FIELDS,
  TARGET_KINDS,
  SELECTION_MODES,
  STORED_VERIFY_MODES,
  DELETE_POLICIES,
  resolveTargetConfig,
  isChapterFormat,
  isSidecar,
  isMirrored,
  pathContains,
  validateTarget,
  RECORD_IDENTITY_FIELDS,
  recordIdentity,
  recordIdentityChanged,
};
