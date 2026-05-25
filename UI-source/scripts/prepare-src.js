// ============================================================
// PREPARE-SRC.JS
//
// This script copies the Python source files (aio-dl.py, sites/,
// requirements.txt) from your AIO-Webtoon-Downloader folder into
// a "python-src" folder inside this project.
//
// electron-builder then includes python-src/ in the installer as
// "extraResources", so the installed app has everything it needs.
//
// This runs automatically when you do: npm run electron:build
//
// WHY IS THIS NEEDED?
//   electron-builder can only include files that are INSIDE the
//   project folder. Your AIO scripts live in a separate folder,
//   so we copy them here before building the installer.
// ============================================================

const fs = require("fs");
const path = require("path");

// ── CONFIGURABLE: Where your AIO source files are ──
// Default: the repo root, computed from this script's location.
//   UI-source/scripts/prepare-src.js → UI-source/scripts/ → UI-source/ → repo root
// This works in CI (GitHub Actions checks out the repo into the workspace
// root and runs `npm run electron:build` from UI-source/) and on any
// contributor's clone, regardless of where they put it. Override via the
// AIO_SOURCE_DIR environment variable when the source lives elsewhere.
const DEFAULT_AIO_SOURCE = path.resolve(__dirname, "..", "..");

const AIO_SOURCE = process.env.AIO_SOURCE_DIR || DEFAULT_AIO_SOURCE;

// Where to copy files to (inside this project)
const DEST = path.join(__dirname, "..", "python-src");

// ── HELPERS ──

/**
 * Recursively copy a folder, skipping __pycache__ directories.
 */
function copyDirSync(src, dest) {
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    // Skip Python cache folders — they're not needed and waste space
    if (entry.name === "__pycache__") continue;
    const srcPath = path.join(src, entry.name);
    const destPath = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      copyDirSync(srcPath, destPath);
    } else {
      fs.copyFileSync(srcPath, destPath);
    }
  }
}

// ── MAIN ──

console.log("");
console.log("== Preparing Python source files for packaging ==");
console.log(`  From: ${AIO_SOURCE}`);
console.log(`  To:   ${DEST}`);
console.log("");

// Verify source exists
if (!fs.existsSync(AIO_SOURCE)) {
  console.error(`ERROR: AIO source folder not found: ${AIO_SOURCE}`);
  console.error("Set AIO_SOURCE_DIR environment variable or edit DEFAULT_AIO_SOURCE in this script.");
  process.exit(1);
}

// Clean destination
if (fs.existsSync(DEST)) {
  fs.rmSync(DEST, { recursive: true });
}
fs.mkdirSync(DEST, { recursive: true });

// Copy aio-dl.py
const scriptSrc = path.join(AIO_SOURCE, "aio-dl.py");
if (!fs.existsSync(scriptSrc)) {
  console.error(`ERROR: aio-dl.py not found at ${scriptSrc}`);
  process.exit(1);
}
fs.copyFileSync(scriptSrc, path.join(DEST, "aio-dl.py"));
console.log("  ✓ aio-dl.py");

// Copy requirements.txt
const reqSrc = path.join(AIO_SOURCE, "requirements.txt");
if (!fs.existsSync(reqSrc)) {
  console.error(`ERROR: requirements.txt not found at ${reqSrc}`);
  process.exit(1);
}
fs.copyFileSync(reqSrc, path.join(DEST, "requirements.txt"));
console.log("  ✓ requirements.txt");

// Copy root-level helper modules used by aio-dl.py, the REST API, and
// Electron IPC. They are siblings of aio-dl.py, so sites/ recursion does not
// pick them up.
for (const file of [
  "aio_search_cli.py",
  "aio_config.py",
  "library_state.py",
  "metadata_editor.py",
  "metadata_cli.py",
  "migrate_library.py",
  "api.py",
]) {
  const src = path.join(AIO_SOURCE, file);
  if (!fs.existsSync(src)) {
    console.error(`ERROR: ${file} not found at ${src}`);
    process.exit(1);
  }
  fs.copyFileSync(src, path.join(DEST, file));
  console.log(`  ✓ ${file}`);
}

// aio_config.json is a runtime USER config file that's gitignored at source
// (users customize it locally to override library output_dir / HID-marker
// filenames; absent file means "use the defaults baked into aio_config.py").
// The CI release builds — and any fresh clone — therefore don't have it,
// which used to crash this step. Handle both shapes:
//   - File exists in source     → copy it (a contributor was testing their
//                                  own overrides; preserve them in the bundle).
//   - File missing from source  → generate an empty `{}` in the bundle. The
//                                  runtime's load_aio_config() also handles
//                                  the truly-missing case (returns {}), but
//                                  shipping a valid JSON file matches the
//                                  rest of python-src/ and silences any
//                                  ENOENT log noise from the Electron side.
// Cross-file: aio_config.py:CONFIG_FILENAME (line 12), load_aio_config (line 20).
const configSrc = path.join(AIO_SOURCE, "aio_config.json");
const configDest = path.join(DEST, "aio_config.json");
if (fs.existsSync(configSrc)) {
  fs.copyFileSync(configSrc, configDest);
  console.log("  ✓ aio_config.json (copied from source)");
} else {
  fs.writeFileSync(configDest, "{}\n", "utf-8");
  console.log("  ✓ aio_config.json (generated default empty config)");
}

// Copy sites/ folder
const sitesSrc = path.join(AIO_SOURCE, "sites");
if (!fs.existsSync(sitesSrc)) {
  console.error(`ERROR: sites/ folder not found at ${sitesSrc}`);
  process.exit(1);
}
copyDirSync(sitesSrc, path.join(DEST, "sites"));

// Count copied site files
const siteFiles = fs.readdirSync(path.join(DEST, "sites")).filter((f) => f.endsWith(".py"));
console.log(`  ✓ sites/ (${siteFiles.length} handlers)`);

console.log("");
console.log("  Done! python-src/ is ready for electron-builder.");
console.log("");
