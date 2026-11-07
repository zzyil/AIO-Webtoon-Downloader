// This is the standard shadcn/ui utility for merging CSS class names.
// clsx handles conditional classes, tailwind-merge resolves conflicts
// (e.g. if you pass both "p-2" and "p-4", it keeps only "p-4").

import { clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs) {
  return twMerge(clsx(inputs));
}

// Render a millisecond duration as "Ns" (under 60s) or "Mm Ss" (>= 60s).
// Returns "" when ms is falsy so callers can safely conditional-render.
// Used by QueueTab's per-row duration label and useDownloader's log divider
// entry that punctuates each completed run.
//
// NOTE: UpdatesCenter.jsx has its OWN formatDuration with a DIFFERENT format
// ("Nms" / "N.Ns" / "MmSSs", e.g. "5.3s", "1m05s") for the scan-duration
// readout — deliberately not merged here because its output differs from this
// one's ("5s", "1m 5s"). Grep formatDuration if you touch either.
export function formatDuration(ms) {
  if (!ms) return "";
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  return `${min}m ${sec % 60}s`;
}

// Numeric-aware string comparator ("Ch 2" before "Ch 10", not after "Ch 100").
// A hoisted Intl.Collator, NOT per-call String.localeCompare: localeCompare
// constructs a fresh collator on every invocation, which is measurable at a few
// hundred library entries x sort. sensitivity:"base" also makes it
// case/accent-insensitive, matching what a user expects from an A-Z picker.
//
// DO NOT use this on the ISO-8601 date sorts (LibraryTab's "date"/"date-asc"):
// those already sort correctly as plain strings, and numeric:true would parse
// the digit groups across the '-' and ':' separators and break them.
//
// Cross-file: electron/library.js carries a MIRROR TWIN of this const (CommonJS
// there can't import from src/), same arrangement as electron/resource-limits.js
// vs src/lib/resourceLimits.js. Grep naturalCompare if you touch either.
export const naturalCompare = new Intl.Collator(undefined, {
  numeric: true,
  sensitivity: "base",
}).compare;

// Coarse "time remaining" label for the live download ETA: "45s" / "3m" /
// "1h 12m". Deliberately COARSER than formatDuration above — an estimate that
// renders "12m 37s" implies a precision the per-chapter EMA doesn't have, and
// the seconds digit would churn on every 100ms progress flush. Returns "" for
// null/negative so callers can conditional-render.
//
// Sub-minute is the one place seconds appear, because "0m" would read as done.
// Producer: electron/downloader.js stamps progress.etaMs per chapter tick;
// consumer: QueueTab's active card. Grep etaMs.
export function formatEta(ms) {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "";
  const totalSec = Math.round(ms / 1000);
  if (totalSec < 60) return `${Math.max(1, totalSec)}s`;
  const totalMin = Math.round(totalSec / 60);
  if (totalMin < 60) return `${totalMin}m`;
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return m ? `${h}h ${m}m` : `${h}h`;
}

// Decimal bytes for Device Sync's sizes and capacity ("6.8 GB", "412 MB",
// "57 GB"): one decimal below 10 GB, none above, as in the approved mockup
// (sync-temp/design/approved, fmtBytes). Decimal because the capacity gauge
// sits beside the tablet's own storage screen, which Android shows in
// decimal units. LibraryTab's local formatSize is binary and left alone, so
// the same folder can read slightly differently on the two tabs.
// Returns "" for null / non-finite so callers can conditional-render; a
// negative value (an "after sync" that doesn't fit) keeps its sign.
export function formatBytes(b) {
  if (b == null || !Number.isFinite(b)) return "";
  const sign = b < 0 ? "−" : "";
  const n = Math.abs(b);
  if (n >= 1e12) return `${sign}${(n / 1e12).toFixed(n >= 10e12 ? 0 : 1)} TB`;
  if (n >= 1e9) return `${sign}${(n / 1e9).toFixed(n >= 10e9 ? 0 : 1)} GB`;
  if (n >= 1e6) return `${sign}${Math.round(n / 1e6)} MB`;
  if (n >= 1e3) return `${sign}${Math.round(n / 1e3)} KB`;
  return `${sign}${Math.round(n)} B`;
}

// Collapse a list of chapter numbers into a compact human range string:
// ["51","52","53","55","60"] → "51-53, 55, 60". Consecutive runs (gap ≤ 1.001,
// so decimals like 60.1 join their neighbor) collapse to "start-end"; isolated
// numbers stay bare. Returns "" for an empty/missing list.
//
// Canonical home for what used to be copy-pasted in LibraryTab.jsx and
// UpdatesCenter.jsx (both rendered "new chapters" ranges). Grep
// chaptersToRangeString. Input may be strings or numbers — mapped through
// Number() before sorting.
//
// `opts.excluding` is a list of chapter numbers that MUST NOT fall inside any
// emitted range, and it exists because this string is not only a label — the
// update-check download paths feed it to `--chapters`, where aio-dl.py's
// is_chapter_wanted treats "10-11" as the closed interval and would happily
// download a 10.5 that is not in `chapters` at all. That is exactly what a
// user who crossed out 10.5 (or unticked it) asked us not to do. So a run is
// extended only when nothing excluded sits strictly between the two ends;
// otherwise the range is cut and a new one started.
//
// With no exclusions the output is byte-identical to before — every caller
// that just renders a label is unaffected. Note this only fixes over-selection
// the caller KNOWS about: a range can still span an already-downloaded chapter
// the caller never passed either list, which is long-standing behavior (that
// chapter simply gets re-downloaded) and deliberately left alone here.
export function chaptersToRangeString(chapters, opts = {}) {
  if (!chapters || chapters.length === 0) return "";
  const nums = chapters.map(Number).sort((a, b) => a - b);
  const excluded = (opts.excluding || []).map(Number).filter((n) => !Number.isNaN(n));
  // Strict inequalities: an excluded value equal to either end would mean the
  // two lists overlap, and then the caller's own intent is what should win.
  const blocked = (lo, hi) => excluded.some((x) => x > lo && x < hi);
  const ranges = [];
  let start = nums[0], end = nums[0];
  for (let i = 1; i < nums.length; i++) {
    if (nums[i] - end <= 1.001 && !blocked(end, nums[i])) {
      end = nums[i];
    } else {
      ranges.push(start === end ? String(start) : `${start}-${end}`);
      start = nums[i];
      end = nums[i];
    }
  }
  ranges.push(start === end ? String(start) : `${start}-${end}`);
  return ranges.join(", ");
}

// Two-letter monogram for a cover fallback when no image is available.
// Splits the title into words, takes the first letter of the first two,
// uppercases them.
//
// Parameterized because the two call sites differ in EXACT output and both
// must be preserved byte-for-byte (this helper only DEDUPES them, it does not
// change either result):
//   - LibraryTab PdfCover: getInitials(title) — split on /[\s-]+/, NO
//     filter-empties, NO fallback. `title` is always a non-empty card title
//     there. (Keeping filter off matters for titles with a leading separator:
//     " A B" must yield "A", not "AB".)
//   - UpdatesCenter RowCover: getInitials(title, { splitUnderscore: true,
//     filterEmpty: true, fallback: "??" }) — underscore is also a separator,
//     empty segments dropped, "??" when nothing usable remains.
// Grep getInitials.
export function getInitials(
  title,
  { splitUnderscore = false, filterEmpty = false, fallback = "" } = {}
) {
  const sep = splitUnderscore ? /[\s\-_]+/ : /[\s-]+/;
  let words = (title || "").split(sep);
  if (filterEmpty) words = words.filter(Boolean);
  return (
    words
      .slice(0, 2)
      .map((w) => (w[0] || "").toUpperCase())
      .join("") || fallback
  );
}
