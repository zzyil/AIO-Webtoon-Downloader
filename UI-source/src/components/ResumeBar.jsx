// ============================================================
// RESUME BAR
//
// Pinned at the bottom of the window. Shows unfinished downloads
// found by scanning for tmp_* folders.
//
// FORMAT OVERRIDE:
// Each item has a format dropdown. The Python script's
// --restore-parameters flag keeps --format separate from
// the restored params, so you can switch from PDF to EPUB
// (or any other format) when resuming without re-downloading.
//
// Props (from useDownloader):
//   resumable: [{ hid, tmpDir, params, cachedChapters, url, title }]
//   onResume:  ({ url, tmpDir, format, epubLayout }) => void
//   onDelete:  (tmpDir) => void
//   onRefresh: () => void
// ============================================================

import React, { useState } from "react";
import { Button, Badge } from "@/components/ui/primitives";
import { Play, Trash2, ChevronUp, ChevronDown, RefreshCw } from "lucide-react";
import { cn } from "@/lib/utils";

// ── CONFIGURABLE: available output formats ──
const FORMATS = [
  { value: "pdf", label: "PDF" },
  { value: "epub", label: "EPUB" },
  { value: "cbz", label: "CBZ" },
  { value: "none", label: "Images" },
];

export default function ResumeBar({ resumable, onResume, onDelete, onRefresh }) {
  const [expanded, setExpanded] = useState(false);
  // Per-item URL input (for items missing a URL)
  const [urlInputs, setUrlInputs] = useState({});
  // Per-item format override: { [hid]: "epub" }
  // If not set, uses the original format from params
  const [formatOverrides, setFormatOverrides] = useState({});
  // Per-item EPUB layout (only relevant when format is epub)
  const [epubLayouts, setEpubLayouts] = useState({});
  // Which item is pending delete confirmation (hid or null).
  // Using React state instead of window.confirm() because the native
  // confirm dialog breaks Electron's renderer focus/input handling.
  const [confirmDeleteHid, setConfirmDeleteHid] = useState(null);

  if (!resumable || resumable.length === 0) return null;

  // True when the user has any URL input field open (item missing a known
  // URL and they've clicked Resume to enter one). When set, the panel
  // toggle is disabled — collapsing would unmount the URL input mid-edit
  // and lose the typed text + caret position.
  const hasUrlBeingEdited = Object.keys(urlInputs).length > 0;

  // Get the effective format for an item (override or original).
  // Lookup chain (most recent → fallback):
  //   1. formatOverrides[hid] — user picked a different format from the
  //      dropdown for this resume (lets you switch PDF→EPUB on resume).
  //   2. item.format — top-level field populated by downloader.scanResumable
  //      from run_meta.json (which aio-dl.py writes on every run with the
  //      original --format value).
  //   3. item.params?.format — historic fallback. run_params.json
  //      DELIBERATELY omits format (see aio-dl.py:get_behavior_params), so
  //      this branch is only useful for legacy tmp folders where some other
  //      tool wrote params with the field.
  //   4. "pdf" — last-resort safety net.
  const getFormat = (item) =>
    formatOverrides[item.hid] || item.format || item.params?.format || "pdf";

  const getEpubLayout = (item) =>
    epubLayouts[item.hid] || "vertical";

  const handleResume = (item) => {
    const url = item.params?.url || item.url || urlInputs[item.hid];

    if (!url) {
      // Show the URL input field for this item
      setUrlInputs((prev) => ({ ...prev, [item.hid]: "" }));
      return;
    }

    const format = getFormat(item);

    onResume({
      url,
      tmpDir: item.tmpDir,
      format,
      epubLayout: format === "epub" ? getEpubLayout(item) : undefined,
      // Threaded so a queued resume card shows a name + cached-chapter count.
      // (useDownloader falls back to params?.title, but callers don't pass params.)
      title: item.params?.title || item.title,
      cachedChapters: item.cachedChapters,
    });

    // Clean up state for this item
    setUrlInputs((prev) => {
      const next = { ...prev };
      delete next[item.hid];
      return next;
    });
  };

  const handleDelete = (item) => {
    if (confirmDeleteHid === item.hid) {
      // Second click — actually delete
      setConfirmDeleteHid(null);
      onDelete(item.tmpDir);
    } else {
      // First click — show confirmation state
      setConfirmDeleteHid(item.hid);
      // Auto-cancel after 3 seconds if user doesn't confirm
      setTimeout(() => {
        setConfirmDeleteHid((prev) => (prev === item.hid ? null : prev));
      }, 3000);
    }
  };

  // Toggle handler gated on hasUrlBeingEdited to prevent unmounting the
  // URL input mid-typing. Used by both the header click and the chevron.
  const handleToggle = () => {
    if (hasUrlBeingEdited) return;
    setExpanded(!expanded);
  };

  return (
    <div className="border-t bg-card/80 backdrop-blur-sm flex-shrink-0">
      {/* Header — always visible */}
      <div
        role="button"
        tabIndex={hasUrlBeingEdited ? -1 : 0}
        onClick={handleToggle}
        onKeyDown={(e) => {
          if (hasUrlBeingEdited) return;
          if (e.key === "Enter" || e.key === " ") setExpanded(!expanded);
        }}
        aria-expanded={expanded}
        aria-disabled={hasUrlBeingEdited}
        title={hasUrlBeingEdited ? "Finish entering the URL first, or press Esc to cancel" : (expanded ? "Collapse" : "Expand")}
        className={cn(
          "flex items-center justify-between w-full px-4 py-2 transition-colors select-none",
          hasUrlBeingEdited
            ? "cursor-default"
            : "hover:bg-accent/30 cursor-pointer"
        )}
      >
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full bg-yellow-500 animate-pulse" />
          <span className="text-xs font-semibold">
            {resumable.length} unfinished download{resumable.length > 1 ? "s" : ""}
          </span>
        </div>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6"
            onClick={(e) => {
              e.stopPropagation();
              onRefresh();
            }}
            title="Refresh"
          >
            <RefreshCw className="w-3 h-3" />
          </Button>
          {expanded ? (
            <ChevronDown className={cn("w-4 h-4", hasUrlBeingEdited ? "text-muted-foreground/40" : "text-muted-foreground")} />
          ) : (
            <ChevronUp className={cn("w-4 h-4", hasUrlBeingEdited ? "text-muted-foreground/40" : "text-muted-foreground")} />
          )}
        </div>
      </div>

      {/* Expanded list */}
      {expanded && (
        <div className="px-4 pb-3 space-y-2 max-h-56 overflow-y-auto animate-slide-up">
          {resumable.map((item) => {
            const currentFormat = getFormat(item);
            // Same lookup chain as getFormat() but WITHOUT the user override —
            // this is "what format was the original run?", not "what will we
            // resume with?". `item.format` comes from run_meta.json (the
            // canonical record); `item.params?.format` is the historic
            // run_params.json fallback (intentionally omitted by aio-dl.py
            // since 2026-05-08, but kept in the chain for legacy tmp folders).
            // Without `item.format` here, the "was X" indicator misfires
            // whenever run_meta exposes a non-default format and the user
            // hasn't manually overridden — e.g. a CBZ resume showing "was PDF".
            const originalFormat = item.format || item.params?.format || "pdf";
            const isFormatChanged = currentFormat !== originalFormat;
            const hasUrl = !!(item.params?.url || item.url);
            const showUrlInput = urlInputs.hasOwnProperty(item.hid);

            // Build info tags
            const tags = [];
            if (item.params?.quality) tags.push(`Q${item.params.quality}`);
            if (item.params?.scaling && item.params.scaling !== 100) {
              tags.push(`${item.params.scaling}%`);
            }

            return (
              <div
                key={item.hid}
                className="flex items-start gap-3 p-3 rounded-md bg-background border"
              >
                {/* Info column */}
                <div className="flex-1 min-w-0">
                  {/* Title + cached chapters */}
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-medium truncate">
                      {item.params?.title || item.title || `tmp_${item.hid}`}
                    </span>
                    {item.cachedChapters > 0 && (
                      <Badge variant="default" className="text-[10px]">
                        {item.cachedChapters} ch cached
                      </Badge>
                    )}
                  </div>

                  {/* Format selector + info tags */}
                  <div className="flex items-center gap-2 mt-1.5 flex-wrap">
                    {/* Format dropdown — lets you change the output format before resuming */}
                    <select
                      value={currentFormat}
                      onChange={(e) =>
                        setFormatOverrides((prev) => ({
                          ...prev,
                          [item.hid]: e.target.value,
                        }))
                      }
                      className={cn(
                        "h-6 px-1.5 rounded border text-[11px] font-medium bg-background",
                        "focus:outline-none focus:ring-1 focus:ring-ring cursor-pointer",
                        isFormatChanged
                          ? "border-primary text-primary"
                          : "border-input text-muted-foreground"
                      )}
                      title="Output format (can be changed when resuming)"
                    >
                      {FORMATS.map((f) => (
                        <option key={f.value} value={f.value}>
                          {f.label}
                        </option>
                      ))}
                    </select>

                    {/* EPUB layout toggle — only shown when EPUB is selected */}
                    {currentFormat === "epub" && (
                      <div className="flex gap-0.5 animate-slide-in">
                        {["vertical", "page"].map((layout) => (
                          <button
                            key={layout}
                            onClick={() =>
                              setEpubLayouts((prev) => ({
                                ...prev,
                                [item.hid]: layout,
                              }))
                            }
                            className={cn(
                              "px-1.5 py-0.5 rounded text-[10px] font-medium border transition-colors",
                              getEpubLayout(item) === layout
                                ? "border-primary bg-primary/10 text-primary"
                                : "border-transparent text-muted-foreground hover:bg-accent"
                            )}
                          >
                            {layout.charAt(0).toUpperCase() + layout.slice(1)}
                          </button>
                        ))}
                      </div>
                    )}

                    {/* Show "changed" indicator when format differs from original */}
                    {isFormatChanged && (
                      <span className="text-[10px] text-primary">
                        was {originalFormat.toUpperCase()}
                      </span>
                    )}

                    {/* Quality/scaling tags */}
                    {tags.map((t, i) => (
                      <Badge key={i} variant="secondary" className="text-[10px]">
                        {t}
                      </Badge>
                    ))}
                  </div>

                  {/* URL input — shown when Resume clicked but no URL known */}
                  {showUrlInput && (
                    <div className="mt-2 flex gap-2 animate-slide-up">
                      <input
                        type="text"
                        value={urlInputs[item.hid] || ""}
                        onChange={(e) =>
                          setUrlInputs((prev) => ({ ...prev, [item.hid]: e.target.value }))
                        }
                        placeholder="Paste the manga URL to resume…"
                        className={cn(
                          "flex-1 h-7 px-2 rounded border border-input bg-background",
                          "text-xs font-mono placeholder:text-muted-foreground",
                          "focus:outline-none focus:ring-1 focus:ring-ring"
                        )}
                        autoFocus
                        onKeyDown={(e) => {
                          if (e.key === "Enter" && urlInputs[item.hid]) handleResume(item);
                          if (e.key === "Escape") {
                            setUrlInputs((prev) => {
                              const next = { ...prev };
                              delete next[item.hid];
                              return next;
                            });
                          }
                        }}
                      />
                      <Button
                        size="sm"
                        className="h-7 text-xs"
                        disabled={!urlInputs[item.hid]}
                        onClick={() => handleResume(item)}
                      >
                        Go
                      </Button>
                    </div>
                  )}

                  {/* Warning if no URL is known */}
                  {!hasUrl && !showUrlInput && (
                    <p className="text-[10px] text-yellow-600 dark:text-yellow-400 mt-1">
                      URL unknown — click Resume to enter it
                    </p>
                  )}
                </div>

                {/* Buttons column */}
                <div className="flex gap-1 shrink-0 pt-0.5">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleResume(item)}
                    className="h-7 text-xs gap-1"
                  >
                    <Play className="w-3 h-3" />
                    Resume
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => handleDelete(item)}
                    onBlur={() => setConfirmDeleteHid(null)}
                    className={cn(
                      "h-7 text-xs",
                      confirmDeleteHid === item.hid
                        ? "text-destructive bg-destructive/10 hover:bg-destructive/20 hover:text-destructive"
                        : "text-destructive hover:text-destructive"
                    )}
                    title="Delete temp folder"
                  >
                    <Trash2 className="w-3 h-3" />
                    {confirmDeleteHid === item.hid && (
                      <span className="ml-1">Sure?</span>
                    )}
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
