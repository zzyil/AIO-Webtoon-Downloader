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

// Collapse a list of chapter numbers into a compact human range string:
// ["51","52","53","55","60"] → "51-53, 55, 60". Consecutive runs (gap ≤ 1.001,
// so decimals like 60.1 join their neighbor) collapse to "start-end"; isolated
// numbers stay bare. Returns "" for an empty/missing list.
//
// Canonical home for what used to be copy-pasted in LibraryTab.jsx and
// UpdatesCenter.jsx (both rendered "new chapters" ranges). Grep
// chaptersToRangeString. Input may be strings or numbers — mapped through
// Number() before sorting.
export function chaptersToRangeString(chapters) {
  if (!chapters || chapters.length === 0) return "";
  const nums = chapters.map(Number).sort((a, b) => a - b);
  const ranges = [];
  let start = nums[0], end = nums[0];
  for (let i = 1; i < nums.length; i++) {
    if (nums[i] - end <= 1.001) {
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
