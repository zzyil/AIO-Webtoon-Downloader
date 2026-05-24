// ============================================================
// LIBRARY SCANNER + THUMBNAIL GENERATOR
//
// Scans the mangas/ output directory and returns info about
// each downloaded manga series. Generates cover thumbnails
// from PDFs using mupdf (WASM) in the main process.
//
// WHY MUPDF IN MAIN PROCESS (not pdfjs in renderer):
//   Manga PDFs can be 200-800+ MB each. pdfjs-dist loaded them
//   into the renderer process memory (which has strict limits),
//   causing "Render process gone" OOM crashes. mupdf runs in
//   the main process which has higher memory limits, and we
//   process ONE PDF at a time so peak memory = 1 PDF's worth.
//   After each thumbnail is generated, the PDF data is freed.
//
// FOLDER STRUCTURE (created by aio-dl.py):
//
//   mangas/
//     Solo Leveling/
//       .mangafire_hid         ← hidden marker (ignored)
//       Solo Leveling.pdf
//       Solo Leveling Ch 1-50.pdf
//       images/                ← only if --keep-images
//         Chapter_1/
//           0001.jpg ...
//     One Piece/
//       One Piece.epub
// ============================================================

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

// File extensions we care about (the final output files)
const OUTPUT_EXTENSIONS = new Set(["pdf", "epub", "cbz"]);

// ── CONFIGURABLE ──
// Width of generated thumbnails in pixels.
// Smaller = faster to generate and less memory used.
const THUMB_WIDTH = 180;

// JPEG quality (0-100) for saved thumbnails.
// 75 is a good balance for small thumbnail files.
const JPEG_QUALITY = 75;

// ── MUPDF (lazy-loaded) ──
// mupdf is an ESM-only package, so we use dynamic import().
// It's loaded once on first use and cached for subsequent calls.
let mupdfModule = null;

async function loadMupdf() {
  if (!mupdfModule) {
    mupdfModule = await import("mupdf");
  }
  return mupdfModule;
}

// ============================================================
// CHAPTER EXTRACTION FROM FILENAMES
//
// aio-dl.py names files like:
//
//   Legacy / non-Komikku format (decimal encoded as `~` for sort):
//     "Title Ch 5.pdf"       → chapter 5
//     "Title Ch 5~5.pdf"     → chapter 5.5 (`~` replaces `.` for sort)
//     "Title Ch 1-50.pdf"    → combined range: chapters 1 through 50
//     "Title Ch 1~5-10.pdf"  → combined range: chapters 1.5 through 10
//     "Title.pdf"            → full series (no chapter marker)
//
//   Komikku-compatible format (--komikku, see komikkuspec.md):
//     "Ch.005.cbz"           → chapter 5 (zero-padded; raw `.` decimal)
//     "Ch.005.5 - Title.cbz" → chapter 5.5 with title suffix
//     "Vol.01 Ch.005.cbz"    → chapter 5 with volume prefix
//
// This helper extracts chapter numbers from actual files on disk
// so we can compare them with the site's chapter list.
//
// Cross-file: Komikku filename builder lives in aio-dl.py's
// _komikku_chapter_filename (grep that name). The output goes through
// this scanner only when settings.useFileBasedChapterCheck is on; the
// default JSON-based check (via .aio_series.json:chapters_downloaded)
// is format-agnostic and works for both layouts.
// ============================================================

// Match a Komikku-style chapter filename. Anchored at start, so a legacy
// "<title> Ch <num>" file never trips this (the title prefix wouldn't be
// "Vol.XX " or empty). The number capture allows integer + optional
// decimal + optional single alphabetic suffix, e.g. "005", "005.5",
// "005a", "005.5a". The leading volume prefix is permissive (`\S+`) so
// non-numeric volume labels — rare, only hit by Python's str-fallback
// branch in _komikku_chapter_filename — still parse.
const KOMIKKU_CH_RE = /^(?:Vol\.\S+\s+)?Ch\.(\d+(?:\.\d+)?[a-z]?)/i;

/**
 * Extract chapter numbers from an array of file objects.
 *
 * @param {Array} files - [{ name: "Title Ch 5.pdf", ... }, ...]
 * @returns {{ chapters: Set<string>, ranges: Array<{start:number, end:number}> }}
 *   chapters = individual chapter numbers (already normalized to match
 *              the stringified-number form that --list-chapters emits)
 *   ranges   = combined-file ranges like [{start:1, end:50}]
 */
function extractChaptersFromFiles(files) {
  const chapters = new Set();
  const ranges = [];

  for (const file of files) {
    // Strip the file extension via the last `.xxx` token. Komikku-style
    // filenames carry "Ch.005" / "Ch.005.5" where the chapter number
    // itself contains a `.`; the trailing-`.xxx` strip is anchored to
    // the very last dot, so "Ch.005.5 - Title.cbz" → "Ch.005.5 - Title"
    // leaves the decimal intact for the regex below.
    const nameNoExt = file.name.replace(/\.[^.]+$/, "");

    // ── Komikku format (--komikku) ──
    // Anchored at ^ because Komikku files NEVER carry a series-title
    // prefix (the parent folder IS the title). A legacy file would have
    // the series title in front of " Ch " and wouldn't match this regex.
    const komikkuMatch = nameNoExt.match(KOMIKKU_CH_RE);
    if (komikkuMatch) {
      const normalized = _normalizeChapterToken(komikkuMatch[1]);
      if (normalized != null) {
        chapters.add(normalized);
        continue;
      }
    }

    // ── Legacy format ──
    // " Ch " marker (space-Ch-space). The legacy formatter rewrites the
    // decimal `.` to `~` so lexical sort matches chapter order.
    const chIdx = nameNoExt.lastIndexOf(" Ch ");
    if (chIdx === -1) continue;

    const chPart = nameNoExt.slice(chIdx + 4).trim(); // everything after " Ch "
    if (!chPart) continue;

    // Check if it's a range like "1-50" or "1~5-50" or "1~5-50~5"
    // Range pattern: <number(~decimal)?>-<number(~decimal)?>
    const rangeMatch = chPart.match(
      /^(\d+(?:~\d+)?)\s*-\s*(\d+(?:~\d+)?)$/
    );

    if (rangeMatch) {
      // It's a combined file covering a range of chapters
      const start = parseFloat(rangeMatch[1].replace("~", "."));
      const end = parseFloat(rangeMatch[2].replace("~", "."));
      if (!isNaN(start) && !isNaN(end)) {
        ranges.push({ start, end });
      }
    } else {
      // Individual chapter: convert ~ back to . (e.g. "5~5" → "5.5")
      const chapNum = chPart.replace("~", ".");
      // Validate it looks like a number
      if (!isNaN(parseFloat(chapNum))) {
        chapters.add(chapNum);
      }
    }
  }

  return { chapters, ranges };
}

/**
 * Convert a Komikku-style chapter token like "005", "005.5", or "005a"
 * to the canonical stringified-number form that --list-chapters emits
 * for site chapters (e.g. "5", "5.5", "5a"). Returns null when the
 * token isn't a parseable number — caller skips non-numeric chapters
 * because they can't be matched against the numeric site list anyway.
 *
 * Why this matters: Komikku zero-pads to 3 digits via `:03d` in
 * _komikku_chapter_filename for sort stability, but the site list
 * uses bare ints/floats. Without normalization, "005" !== "5" at the
 * Set-membership step and every chapter shows up as "missing".
 */
function _normalizeChapterToken(raw) {
  const alphaMatch = raw.match(/^([\d.]+)([a-z])$/i);
  if (alphaMatch) {
    const num = parseFloat(alphaMatch[1]);
    if (isNaN(num)) return null;
    return `${num}${alphaMatch[2].toLowerCase()}`;
  }
  const num = parseFloat(raw);
  if (isNaN(num)) return null;
  return String(num);
}

/**
 * Get the full set of chapters present on device by cross-referencing
 * file-based chapters/ranges with the site's chapter list.
 *
 * For individual chapter files, the chapter is directly added.
 * For range files (e.g. "Ch 1-50.pdf"), any site chapter whose number
 * falls within [start, end] is considered present on device.
 *
 * @param {Array}      files        - file objects from scanLibrary
 * @param {Array<string>} siteChapters - chapter numbers from the site
 * @returns {Set<string>} chapter numbers present on device
 */
function getChaptersOnDevice(files, siteChapters) {
  const { chapters, ranges } = extractChaptersFromFiles(files);

  // For each range file, add any site chapter that falls within the range
  if (ranges.length > 0 && siteChapters) {
    for (const ch of siteChapters) {
      const num = parseFloat(ch);
      if (isNaN(num)) continue;
      for (const range of ranges) {
        if (num >= range.start && num <= range.end) {
          chapters.add(String(ch));
          break;
        }
      }
    }
  }

  return chapters;
}

// ============================================================
// SCAN LIBRARY
// ============================================================

/**
 * Scan the mangas directory and return info about each series.
 *
 * @param {string} mangasDir     - Path to the mangas/ folder
 * @param {string} thumbCacheDir - Path to thumbnail cache folder
 * @returns {Array} Array of series objects
 */
function scanLibrary(mangasDir, thumbCacheDir) {
  const entries = [];

  if (!mangasDir || !fs.existsSync(mangasDir)) return entries;

  let folders;
  try {
    folders = fs.readdirSync(mangasDir, { withFileTypes: true });
  } catch {
    return entries;
  }

  for (const folder of folders) {
    // Skip hidden folders (like .aio_coord) and non-directories
    if (!folder.isDirectory() || folder.name.startsWith(".")) continue;

    const folderPath = path.join(mangasDir, folder.name);

    let contents;
    try {
      contents = fs.readdirSync(folderPath, { withFileTypes: true });
    } catch {
      continue;
    }

    // ── Collect output files ──
    const files = [];
    let coverPdfPath = null;

    for (const item of contents) {
      if (!item.isFile() || item.name.startsWith(".")) continue;

      const ext = path.extname(item.name).toLowerCase().slice(1);
      if (!OUTPUT_EXTENSIONS.has(ext)) continue;

      const filePath = path.join(folderPath, item.name);
      let stat;
      try {
        stat = fs.statSync(filePath);
      } catch {
        continue;
      }

      files.push({
        name: item.name,
        path: filePath,
        ext,
        size: stat.size,
        modifiedAt: stat.mtime.toISOString(),
      });

      // Use the first PDF as the cover source (page 1 = manga cover)
      if (ext === "pdf" && !coverPdfPath) {
        coverPdfPath = filePath;
      }
    }

    // Skip empty folders (no output files)
    if (files.length === 0) continue;

    // ── Count chapters (if images/ exists from --keep-images) ──
    let chapterCount = 0;
    const imagesDir = path.join(folderPath, "images");
    if (fs.existsSync(imagesDir)) {
      try {
        chapterCount = fs
          .readdirSync(imagesDir, { withFileTypes: true })
          .filter(
            (d) =>
              d.isDirectory() &&
              (d.name.startsWith("ch_") || d.name.startsWith("Chapter_"))
          ).length;
      } catch {}
    }

    // ── Compute totals ──
    const totalSize = files.reduce((sum, f) => sum + f.size, 0);
    const lastModified = files.reduce(
      (latest, f) => (f.modifiedAt > latest ? f.modifiedAt : latest),
      ""
    );

    // ── Check for series metadata (written by aio-dl.py) ──
    // .aio_series.json contains the source URL, downloaded chapters,
    // status, authors etc. — needed for the "check for updates" feature
    // AND for looking up the web cover URL below.
    let seriesMeta = null;
    const metaPath = path.join(folderPath, ".aio_series.json");
    if (fs.existsSync(metaPath)) {
      try {
        seriesMeta = JSON.parse(fs.readFileSync(metaPath, "utf8"));
      } catch {}
    }

    // ── Check for cached thumbnail ──
    // Priority: web cover (from seriesMeta.cover) > PDF page-1 render
    let thumbPath = null;
    let webCoverCached = false;

    // 1. Check for cached web cover image (official cover from the site).
    //    These are keyed by the cover URL hash with a "cover_" prefix.
    if (seriesMeta?.cover && thumbCacheDir) {
      const coverHash = crypto
        .createHash("md5")
        .update(seriesMeta.cover)
        .digest("hex");
      const coverCandidate = path.join(thumbCacheDir, "cover_" + coverHash + ".jpg");
      if (fs.existsSync(coverCandidate)) {
        thumbPath = coverCandidate;
        webCoverCached = true;
      }
    }

    // 2. Fallback: check for cached PDF page-1 thumbnail (rendered by mupdf)
    if (!thumbPath && coverPdfPath && thumbCacheDir) {
      const hash = crypto
        .createHash("md5")
        .update(coverPdfPath)
        .digest("hex");
      const candidate = path.join(thumbCacheDir, hash + ".jpg");
      if (fs.existsSync(candidate)) {
        thumbPath = candidate;
      }
    }

    entries.push({
      title: folder.name,
      folderPath,
      files: files.sort((a, b) => a.name.localeCompare(b.name)),
      coverPdfPath,
      thumbPath,
      webCoverCached,
      chapterCount,
      totalSize,
      lastModified,
      seriesMeta,
    });
  }

  // Sort by title by default
  entries.sort((a, b) => a.title.localeCompare(b.title));
  return entries;
}

// ============================================================
// THUMBNAIL GENERATION (using mupdf)
// ============================================================

/**
 * Generate a JPEG thumbnail from the first page of a PDF.
 *
 * Uses mupdf (WASM) to render page 1 at THUMB_WIDTH pixels wide.
 * The result is saved as a JPEG file in the thumbnail cache folder.
 *
 * Memory safety: mupdf runs in the main process. We explicitly
 * destroy() all mupdf objects after use to free WASM memory.
 * Only one PDF is processed at a time (enforced by the caller).
 *
 * @param {string} pdfPath       - Path to the source PDF file
 * @param {string} thumbCacheDir - Path to thumbnail cache folder
 * @returns {Promise<string>}    - Path to the generated thumbnail
 */
async function generateThumbnail(pdfPath, thumbCacheDir) {
  const mupdf = await loadMupdf();

  // ── Output path: MD5 hash of the PDF path → .jpg ──
  fs.mkdirSync(thumbCacheDir, { recursive: true });
  const hash = crypto.createHash("md5").update(pdfPath).digest("hex");
  const thumbFile = path.join(thumbCacheDir, hash + ".jpg");

  // Skip if already generated (race condition guard)
  if (fs.existsSync(thumbFile)) return thumbFile;

  // ── Read PDF from disk ──
  // This loads the file into a Node.js Buffer (backed by V8 external memory).
  // For a 500 MB PDF, this uses ~500 MB temporarily.
  const fileData = fs.readFileSync(pdfPath);

  // ── Open with mupdf ──
  // The buffer is copied into WASM linear memory. After this call,
  // the Node.js Buffer can be garbage collected.
  let doc = null;
  let page = null;
  let pixmap = null;

  try {
    doc = mupdf.Document.openDocument(fileData, "application/pdf");

    // Load only page 1 (index 0)
    page = doc.loadPage(0);

    // Calculate scale to fit THUMB_WIDTH
    const bounds = page.getBounds(); // [x0, y0, x1, y1]
    const pageWidth = bounds[2] - bounds[0];
    const scale = THUMB_WIDTH / pageWidth;
    const matrix = mupdf.Matrix.scale(scale, scale);

    // Render page to a pixel buffer (RGB, no alpha)
    pixmap = page.toPixmap(matrix, mupdf.ColorSpace.DeviceRGB, false);

    // Convert to JPEG and save to disk
    const jpegData = pixmap.asJPEG(JPEG_QUALITY);
    fs.writeFileSync(thumbFile, jpegData);

    return thumbFile;
  } finally {
    // ── CLEANUP (critical for memory) ──
    // Destroy all mupdf objects in reverse order to free WASM memory.
    // Without this, each thumbnail would leak ~500 MB.
    if (pixmap) try { pixmap.destroy(); } catch {}
    if (page) try { page.destroy(); } catch {}
    if (doc) try { doc.destroy(); } catch {}
  }
}

/**
 * Generate thumbnails for all entries that are missing them.
 *
 * Processes ONE PDF at a time to keep memory bounded.
 * Calls onReady(folderPath, thumbPath) after each successful generation.
 * Skips and continues on errors.
 *
 * @param {Array}    items        - [{ pdfPath, folderPath }, ...]
 * @param {string}   thumbCacheDir - Path to thumbnail cache folder
 * @param {Function} onReady      - Callback(folderPath, thumbPath) per success
 */
async function generateMissingThumbnails(items, thumbCacheDir, onReady) {
  for (const item of items) {
    try {
      const thumbPath = await generateThumbnail(item.pdfPath, thumbCacheDir);
      if (onReady) onReady(item.folderPath, thumbPath);
    } catch (err) {
      console.warn("Thumbnail failed for:", item.pdfPath, err.message || err);
      // Continue with the next one — don't let one failure stop all
    }
  }
}

/**
 * Save a thumbnail image to the cache directory.
 * (Legacy — kept for compatibility, but new code uses generateThumbnail)
 */
function saveThumbnail(pdfPath, base64Data, thumbCacheDir) {
  fs.mkdirSync(thumbCacheDir, { recursive: true });
  const hash = crypto.createHash("md5").update(pdfPath).digest("hex");
  const thumbFile = path.join(thumbCacheDir, hash + ".jpg");
  fs.writeFileSync(thumbFile, Buffer.from(base64Data, "base64"));
  return thumbFile;
}

// ============================================================
// WEB COVER DOWNLOAD
//
// Downloads the official cover image from the manga's source page.
// These are small images (50-200 KB) so this is much faster than
// rendering a 500 MB PDF with mupdf. The downloaded image is
// cached with a "cover_" prefix to distinguish from PDF thumbs.
// ============================================================

const https = require("https");
const http = require("http");

// Headers used when fetching cover images from the various manga site CDNs.
// MangaDex's `uploads.mangadex.org` (and its CF/edge proxies) and several
// other manga CDNs reject requests with no User-Agent or default Node UA.
// We mirror the User-Agent that the Python side already sends successfully
// (configure_session in sites/mangadex.py) so the Library's cover-cache
// fetch matches the in-band download path.
const COVER_REQUEST_HEADERS = {
  "User-Agent":
    "AIO-Webtoon-Downloader/1.0 " +
    "(+https://github.com/Thundia2/AIO-Webtoon-Downloader)",
  Accept: "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
  "Accept-Language": "en-US,en;q=0.5",
};

// Maximum redirect chain depth before we give up. Three is enough for the
// CDN hops we see in practice (uploads.* → Cloudflare → origin) without
// risking an infinite loop on a misconfigured server.
const COVER_MAX_REDIRECTS = 3;

/**
 * Download a single image from a URL and save it to the cache.
 *
 * @param {string} imageUrl       - The cover image URL (https://...)
 * @param {string} thumbCacheDir  - Path to thumbnail cache folder
 * @param {number} redirectsLeft  - Remaining redirects we'll follow (default 3)
 * @returns {Promise<string>}     - Path to the cached image file
 */
function downloadCoverImage(imageUrl, thumbCacheDir, redirectsLeft = COVER_MAX_REDIRECTS) {
  return new Promise((resolve, reject) => {
    fs.mkdirSync(thumbCacheDir, { recursive: true });
    // Hash the ORIGINAL URL (not the redirect target) so the cache key stays
    // stable across redirect-chain reconfiguration on the upstream CDN.
    const hash = crypto.createHash("md5").update(imageUrl).digest("hex");
    const coverFile = path.join(thumbCacheDir, "cover_" + hash + ".jpg");

    // Skip if already downloaded
    if (fs.existsSync(coverFile)) {
      resolve(coverFile);
      return;
    }

    // Pick http or https based on the URL
    const client = imageUrl.startsWith("https") ? https : http;

    const request = client.get(
      imageUrl,
      { timeout: 15000, headers: COVER_REQUEST_HEADERS },
      (response) => {
        // Follow redirects up to COVER_MAX_REDIRECTS deep. A 3xx without a
        // Location header is malformed; reject with a clear error rather
        // than falling through to the generic status check (which would
        // surface as "HTTP 302 for ..." — confusing for users debugging
        // a CDN issue).
        if ([301, 302, 303, 307, 308].includes(response.statusCode)) {
          const redirectUrl = response.headers.location;
          if (!redirectUrl) {
            reject(new Error(
              `HTTP ${response.statusCode} for ${imageUrl} (no Location header)`,
            ));
            return;
          }
          if (redirectsLeft <= 0) {
            reject(new Error(
              `Too many redirects (>${COVER_MAX_REDIRECTS}) following ${imageUrl}`,
            ));
            return;
          }
          // Resolve relative redirects against the request URL, otherwise
          // the recursive call would fail with "Invalid URL".
          const absoluteRedirect = redirectUrl.startsWith("http")
            ? redirectUrl
            : new URL(redirectUrl, imageUrl).toString();
          // Pass redirectsLeft - 1 with the ORIGINAL imageUrl preserved on
          // the cache key path? No — we recurse with the redirect target
          // as the new imageUrl, so the cache key uses the redirected URL.
          // That's fine for the hot-cache case (already-resolved URL maps
          // to the same file) but means the FIRST resolution per CDN-edge
          // change re-downloads. Acceptable tradeoff.
          downloadCoverImage(absoluteRedirect, thumbCacheDir, redirectsLeft - 1)
            .then(resolve)
            .catch(reject);
          return;
        }

        if (response.statusCode !== 200) {
          reject(new Error(`HTTP ${response.statusCode} for ${imageUrl}`));
          return;
        }

        // Collect the image data into a buffer
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          try {
            const buffer = Buffer.concat(chunks);
            // Sanity check: must be at least 100 bytes (not an error page)
            if (buffer.length < 100) {
              reject(new Error("Downloaded image too small"));
              return;
            }
            fs.writeFileSync(coverFile, buffer);
            resolve(coverFile);
          } catch (err) {
            reject(err);
          }
        });
        response.on("error", reject);
      },
    );

    request.on("timeout", () => {
      request.destroy();
      reject(new Error("Timeout downloading cover"));
    });
    request.on("error", reject);
  });
}

/**
 * Download cover images for all entries that have a cover URL
 * but no cached cover image yet. Processes sequentially.
 *
 * @param {Array}    items        - [{ coverUrl, folderPath }, ...]
 * @param {string}   thumbCacheDir - Path to thumbnail cache folder
 * @param {Function} onReady      - Callback(folderPath, coverPath) per success
 */
async function downloadMissingCovers(items, thumbCacheDir, onReady) {
  for (const item of items) {
    try {
      const coverPath = await downloadCoverImage(item.coverUrl, thumbCacheDir);
      if (onReady) onReady(item.folderPath, coverPath);
    } catch (err) {
      console.warn("Cover download failed for:", item.coverUrl, err.message || err);
    }
  }
}

/**
 * Remove cached cover_<hash>.jpg files that no entry in the library
 * references anymore. Series whose cover URL has changed leave behind
 * the old hash file; this sweep keeps the cache from growing unbounded
 * across months of metadata refreshes.
 *
 * Cheap: runs after each scan-library, only enumerates the cache dir
 * once and unlinks the diff. Skips files we don't recognize (cover_*.jpg
 * are the only ones we manage; PDF page-1 thumbs use the bare hash
 * without a "cover_" prefix and are kept until their PDF is deleted —
 * separate cleanup path).
 *
 * @param {Array} entries - scanLibrary output (each may carry seriesMeta.cover)
 * @param {string} thumbCacheDir - Path to thumbnail cache folder
 * @returns {number} - Count of files removed
 */
function cleanupOrphanCovers(entries, thumbCacheDir) {
  if (!thumbCacheDir || !fs.existsSync(thumbCacheDir)) return 0;
  const referenced = new Set();
  for (const e of entries) {
    if (e?.seriesMeta?.cover) {
      const h = crypto.createHash("md5").update(e.seriesMeta.cover).digest("hex");
      referenced.add("cover_" + h + ".jpg");
    }
  }
  let removed = 0;
  try {
    const files = fs.readdirSync(thumbCacheDir);
    for (const f of files) {
      if (!f.startsWith("cover_") || !f.endsWith(".jpg")) continue;
      if (referenced.has(f)) continue;
      try {
        fs.unlinkSync(path.join(thumbCacheDir, f));
        removed += 1;
      } catch {}
    }
  } catch {}
  return removed;
}

module.exports = { scanLibrary, saveThumbnail, generateMissingThumbnails, downloadMissingCovers, cleanupOrphanCovers, extractChaptersFromFiles, getChaptersOnDevice };
