// ============================================================
// DEVICE SYNC — PC INVENTORY (main process)
//
// Owns the walk of the library root (getConfiguredOutputRoot) that feeds the
// planner its `pc` input. Async with fs.promises, yielding between series;
// never the synchronous scanLibrary, which would block main for a whole
// library.
//
// FAILING HARD: a missing, unreadable or non-directory root answers
// {ok:false}; the planner then refuses with library-missing. An empty
// result is returned as is, and the planner refuses it while the record
// manages folders (D: is removable, and an empty walk would otherwise orphan
// every folder).
//
// OUTPUT (planner.js "INPUT SHAPES"):
//   {ok, root, series:[{folder, folderPath, title, identityKey, url,
//    anilistId, synonyms[], imageOnly, malformedJson[], readError?,
//    files:[{name, size, mtimeMs, mtimeNs, path}]}], warnings[]}
// files are every non-dot regular file at depth 1 (the planner picks the
// mirrored ones and uses the rest for the slot guard). sha256 is filled in
// by prepare.js from hash-pool.js. Identity, url, title and AniList fields
// come from .aio_series.json (aio-dl.py writes url, title, site, hid,
// anilist_id, anilist_synonyms); identityKey is library.js's
// seriesIdentityKey, so sync groups series exactly as the Library does.
//
// A series folder that can't be read is still listed (readError, no files),
// so its bound device folder stays held with it instead of reading as
// orphaned.
//
// Read by: prepare.js. Depends on: library.js (seriesIdentityKey),
// profiles.js (isChapterFormat).
// ============================================================

const fs = require("fs");
const path = require("path");
const { seriesIdentityKey } = require("../library");
const { isChapterFormat } = require("./profiles");

const SERIES_META = ".aio_series.json";
// details.json is a mirrored sidecar; it is parsed only to flag it when it
// is malformed (contentAnomalies' malformed-json). Larger is not a sidecar.
const DETAILS_MAX_BYTES = 256 * 1024;
const META_MAX_BYTES = 4 * 1024 * 1024;

function _yield() {
  return new Promise((r) => setImmediate(r));
}

async function _statFile(fsp, p) {
  const st = await fsp.stat(p, { bigint: true });
  if (!st.isFile()) return null;
  return { size: Number(st.size), mtimeMs: Number(st.mtimeMs), mtimeNs: String(st.mtimeNs) };
}

async function _readJsonSmall(fsp, p, max) {
  try {
    const st = await fsp.stat(p);
    if (!st.isFile() || st.size > max) return { state: "skipped" };
    const text = await fsp.readFile(p, "utf8");
    return { state: "ok", value: JSON.parse(text) };
  } catch (e) {
    if (e && e.code === "ENOENT") return { state: "missing" };
    return { state: "malformed", error: e && e.message };
  }
}

async function _readSeries(fsp, root, folder, warnings) {
  const folderPath = path.join(root, folder);
  const s = {
    folder,
    folderPath,
    title: null,
    identityKey: null,
    url: null,
    anilistId: null,
    synonyms: [],
    imageOnly: false,
    malformedJson: [],
    files: [],
  };
  let ents;
  try {
    ents = await fsp.readdir(folderPath, { withFileTypes: true });
  } catch (e) {
    s.readError = (e && e.code) || "unreadable";
    warnings.push({ kind: "folder-unreadable", folder, error: s.readError });
    return s;
  }
  let hasImages = false;
  let hasMeta = false;
  let hasDetails = false;
  for (const d of ents.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))) {
    if (d.name === "images" && d.isDirectory()) hasImages = true;
    if (d.name === SERIES_META) hasMeta = true;
    if (d.name.startsWith(".")) continue;
    if (!d.isFile() && !d.isSymbolicLink()) continue;
    const p = path.join(folderPath, d.name);
    let st;
    try {
      st = await _statFile(fsp, p);
    } catch (e) {
      // Vanished or locked between readdir and stat: skipped for this plan.
      warnings.push({ kind: "file-unreadable", folder, name: d.name, error: (e && e.code) || "unreadable" });
      continue;
    }
    if (!st) continue;
    if (d.name === "details.json") hasDetails = true;
    s.files.push({ name: d.name, size: st.size, mtimeMs: st.mtimeMs, mtimeNs: st.mtimeNs, path: p });
  }
  if (hasMeta) {
    const m = await _readJsonSmall(fsp, path.join(folderPath, SERIES_META), META_MAX_BYTES);
    if (m.state === "ok" && m.value && typeof m.value === "object" && !Array.isArray(m.value)) {
      const meta = m.value;
      s.identityKey = seriesIdentityKey(meta);
      const url = Array.isArray(meta.url) ? meta.url[0] : meta.url;
      s.url = typeof url === "string" && url.trim() ? url.trim() : null;
      s.title = typeof meta.title === "string" && meta.title.trim() ? meta.title.trim() : null;
      s.anilistId = meta.anilist_id == null || meta.anilist_id === "" ? null : String(meta.anilist_id);
      s.synonyms = Array.isArray(meta.anilist_synonyms) ? meta.anilist_synonyms.filter((x) => typeof x === "string") : [];
    } else if (m.state === "malformed" || m.state === "ok") {
      s.malformedJson.push(SERIES_META);
    }
  }
  if (hasDetails) {
    const d = await _readJsonSmall(fsp, path.join(folderPath, "details.json"), DETAILS_MAX_BYTES);
    if (d.state === "malformed") s.malformedJson.push("details.json");
  }
  // The Library's own rule (library.js scanLibrary): no archive at depth 1
  // but an images/ tree is an image-only (--format none) series.
  s.imageOnly = hasImages && !s.files.some((f) => isChapterFormat(f.name));
  return s;
}

/**
 * Walk the library root.
 * @param {string} root
 * @param {{fsp?: typeof fs.promises}} [o]  fsp is injectable for fault tests.
 */
async function walkLibrary(root, { fsp = fs.promises } = {}) {
  if (!root) return { ok: false, error: "no-root" };
  let ents;
  try {
    const st = await fsp.stat(root);
    if (!st.isDirectory()) return { ok: false, error: "not-a-directory", root };
    ents = await fsp.readdir(root, { withFileTypes: true });
  } catch (e) {
    return { ok: false, error: e && e.code === "ENOENT" ? "missing" : "unreadable", detail: e && e.message, root };
  }
  const warnings = [];
  const series = [];
  for (const d of ents.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))) {
    if (d.name.startsWith(".")) continue;
    let isDir = d.isDirectory();
    if (!isDir && d.isSymbolicLink()) {
      try {
        isDir = (await fsp.stat(path.join(root, d.name))).isDirectory();
      } catch (_) {
        isDir = false;
      }
    }
    if (!isDir) continue;
    series.push(await _readSeries(fsp, root, d.name, warnings));
    await _yield();
  }
  return { ok: true, root, series, warnings };
}

/**
 * Everything of a walk the planner reads, as one comparable string: two
 * walks with equal signatures plan the same. service.js notifyLibraryChanged
 * compares a fresh walk with the plan's own (prep.pc), so a library scan
 * that changed nothing doesn't re-plan. Leaves out folderPath / path (the
 * root and the names already say them), mtimeMs (mtimeNs says it more
 * precisely) and sha256 (prepare.js fills it in from the size and mtime).
 * A field the planner starts reading must be added here.
 */
function pcSignature(pc) {
  if (!pc || !pc.ok) return null;
  return JSON.stringify([
    pc.root,
    pc.series.map((s) => [
      s.folder,
      s.readError || null,
      s.title,
      s.identityKey,
      s.url,
      s.anilistId,
      s.synonyms,
      s.imageOnly,
      s.malformedJson,
      s.files.map((f) => [f.name, f.size, f.mtimeNs]),
    ]),
  ]);
}

module.exports = { walkLibrary, pcSignature, SERIES_META };
