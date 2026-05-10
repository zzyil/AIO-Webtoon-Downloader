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

// Copy aio_search_cli.py — root-level CLI glue that aio-dl.py imports at
// runtime when --search is passed (deferred imports around aio-dl.py:3811).
// It's not in sites/ so the recursive copy below doesn't pick it up.
// Without this, --search crashes with ModuleNotFoundError in installed builds.
//
// If you add another root-level Python module that aio-dl.py imports
// (sibling of aio-dl.py, not under sites/), copy it here too.
const searchCliSrc = path.join(AIO_SOURCE, "aio_search_cli.py");
if (!fs.existsSync(searchCliSrc)) {
  console.error(`ERROR: aio_search_cli.py not found at ${searchCliSrc}`);
  process.exit(1);
}
fs.copyFileSync(searchCliSrc, path.join(DEST, "aio_search_cli.py"));
console.log("  ✓ aio_search_cli.py");

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
