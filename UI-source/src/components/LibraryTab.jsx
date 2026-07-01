import React, { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { Button, Input, Badge } from "@/components/ui/primitives";
import {
  Search,
  RefreshCw,
  BookOpen,
  FolderOpen,
  Trash2,
  FileText,
  ArrowLeft,
  ArrowUpDown,
  Download,
  ExternalLink,
  Globe,
  Bell,
  Loader2,
  AlertCircle,
  Link,
  User,
  Tag,
  PencilLine,
  Save,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import UpdatesCenter from "./UpdatesCenter";

// Convert a Windows file path to a localfile:// URL the renderer can load.
function fileToUrl(filePath) {
  if (!filePath) return null;
  const normalized = filePath.replace(/\\/g, "/");
  return "localfile:///" + encodeURI(normalized);
}

// ── CONFIGURABLE ──
const FORMAT_COLORS = {
  pdf: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  epub: "bg-emerald-500/20 text-emerald-400 border-emerald-500/30",
  cbz: "bg-orange-500/20 text-orange-400 border-orange-500/30",
  // --format none (image-only) series. Badge text is "images" (see
  // getEntryFormats); the per-chapter rows in the detail view reuse this too.
  images: "bg-violet-500/20 text-violet-400 border-violet-500/30",
};

const STATUS_COLORS = {
  Ongoing: "bg-blue-500/20 text-blue-400 border-blue-500/40",
  Releasing: "bg-blue-500/20 text-blue-400 border-blue-500/40",
  Completed: "bg-emerald-500/20 text-emerald-400 border-emerald-500/40",
  Finished: "bg-emerald-500/20 text-emerald-400 border-emerald-500/40",
};

const SORT_OPTIONS = [
  { value: "title", label: "Title A→Z" },
  { value: "title-desc", label: "Title Z→A" },
  { value: "date", label: "Newest first" },
  { value: "date-asc", label: "Oldest first" },
  { value: "size", label: "Largest first" },
  { value: "size-asc", label: "Smallest first" },
];

// ============================================================
// HELPERS
// ============================================================
function formatSize(bytes) {
  if (bytes === 0) return "0 B";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
  if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + " MB";
  return (bytes / 1073741824).toFixed(2) + " GB";
}

function formatDate(isoString) {
  if (!isoString) return "";
  const d = new Date(isoString);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

// Format badges for a series. Archive series derive them from their file
// extensions; an image-only (--format none) series has no archive files, so
// it gets a single synthetic "images" badge. Returns [] only for the
// degenerate case of no files and not image-only (shouldn't happen — the
// scanner's payload-required gate filters those out).
function getEntryFormats(entry) {
  const fileFormats = [...new Set((entry.files || []).map((f) => f.ext))];
  if (fileFormats.length > 0) return fileFormats;
  if (entry.isImageOnly) return ["images"];
  return [];
}

/**
 * Turn ["51","52","53","55","60"] into "51-53, 55, 60"
 */
function chaptersToRangeString(chapters) {
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

// ============================================================
// PDF COVER THUMBNAIL
// ============================================================
function PdfCover({ entry }) {
  if (entry.thumbPath) {
    return (
      <img
        src={fileToUrl(entry.thumbPath)}
        alt={entry.title}
        className="w-full h-full object-cover rounded"
        loading="lazy"
      />
    );
  }
  // Image-only (--format none) fallback: no PDF to render and maybe no cached
  // web cover yet, so show the first downloaded page straight from disk.
  // Chromium decodes webp/avif/png/jpg/gif natively; object-cover crops the
  // long-strip aspect. If a web cover later downloads, thumbPath wins on the
  // next render (it's checked first). coverImagePath comes from library.js.
  if (entry.coverImagePath) {
    return (
      <img
        src={fileToUrl(entry.coverImagePath)}
        alt={entry.title}
        className="w-full h-full object-cover rounded"
        loading="lazy"
      />
    );
  }
  if (entry.coverPdfPath) {
    return <div className="w-full h-full bg-muted animate-pulse rounded" />;
  }
  const initials = entry.title
    .split(/[\s-]+/)
    .slice(0, 2)
    .map((w) => w[0] || "")
    .join("")
    .toUpperCase();
  return (
    <div className="w-full h-full flex items-center justify-center bg-gradient-to-br from-primary/20 to-primary/5 rounded">
      <span className="text-2xl font-bold text-primary/60">{initials}</span>
    </div>
  );
}

// ============================================================
// MANGA CARD (grid item)
// ============================================================
function MangaCard({ entry, newCount, onClick }) {
  const formats = getEntryFormats(entry);
  const status = entry.seriesMeta?.status;

  return (
    <button
      onClick={onClick}
      className={cn(
        "group flex flex-col rounded-lg overflow-hidden text-left",
        "bg-card/60 border border-border/50",
        "hover:border-primary/40 hover:bg-card/80",
        "transition-all duration-150 cursor-pointer",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-primary"
      )}
    >
      {/* Cover with overlay badges */}
      <div className="aspect-[3/4] w-full overflow-hidden bg-muted/30 relative">
        <PdfCover entry={entry} />

        {/* Status badge (top-left) */}
        {status && (
          <span
            className={cn(
              "absolute top-1.5 left-1.5 text-[8px] font-bold uppercase px-1.5 py-0.5 rounded border backdrop-blur-sm",
              STATUS_COLORS[status] || "bg-muted/80 text-muted-foreground border-border"
            )}
          >
            {status}
          </span>
        )}

        {/* New chapters badge (top-right) */}
        {newCount > 0 && (
          <span className="absolute top-1.5 right-1.5 text-[9px] font-bold px-1.5 py-0.5 rounded bg-orange-500/90 text-white shadow-sm">
            {newCount} new
          </span>
        )}
      </div>

      {/* Info area */}
      <div className="p-2.5 flex flex-col gap-1 min-w-0">
        <h3 className="text-xs font-semibold leading-tight truncate" title={entry.title}>
          {entry.title}
        </h3>
        <div className="flex gap-1 flex-wrap">
          {formats.map((fmt) => (
            <span
              key={fmt}
              className={cn(
                "text-[8px] font-bold uppercase px-1.5 py-0.5 rounded border",
                FORMAT_COLORS[fmt] || "bg-muted text-muted-foreground border-border"
              )}
            >
              {fmt}
            </span>
          ))}
        </div>
        <div className="flex items-center gap-2 text-[10px] text-muted-foreground mt-0.5">
          <span>{formatSize(entry.totalSize)}</span>
          <span>&middot;</span>
          {entry.isImageOnly ? (
            <span>{entry.imageCount} image{entry.imageCount !== 1 ? "s" : ""}</span>
          ) : (
            <span>{entry.files.length} file{entry.files.length !== 1 ? "s" : ""}</span>
          )}
          {entry.chapterCount > 0 && (
            <>
              <span>&middot;</span>
              <span>{entry.chapterCount} ch</span>
            </>
          )}
        </div>
      </div>
    </button>
  );
}

// ============================================================
// UPDATE CHECKER SECTION (inside detail view)
// ============================================================
function UpdateSection({ entry, onStartDownload, onSwitchTab, settings }) {
  const meta = entry.seriesMeta;
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [manualUrl, setManualUrl] = useState("");
  const [saving, setSaving] = useState(false);

  const handleCheck = async () => {
    setChecking(true);
    setError(null);
    setResult(null);
    try {
      const res = await window.electronAPI.checkForUpdates(entry.folderPath);
      if (res.error) {
        setError(res.message || res.error);
      } else {
        setResult(res);
      }
    } catch (err) {
      setError(err.message || "Check failed");
    }
    setChecking(false);
  };

  const handleDownloadNew = () => {
    if (!result?.newChapters?.length || !meta?.url) return;
    const rangeStr = chaptersToRangeString(result.newChapters);

    // Start with the user's saved default settings from the Settings tab,
    // then override format/language/site from the series metadata and set
    // the chapter range to only the missing ones.
    const d = settings?.defaults || {};
    const args = {
      format: meta.format || d.format || "pdf",
      quality: d.quality ?? 85,
      chapters: rangeStr,
      language: meta.language || "en",
      site: meta.site || undefined,
      verbose: settings?.verboseAlways ?? true,
    };

    // Apply all the same toggles that DownloadTab uses
    if (d.scaling && d.scaling !== 100) args.scaling = d.scaling;
    if (d.keepChapters) args.keepChapters = true;
    if (d.noFinalFile) args.noFinalFile = true;
    if (d.keepImages) args.keepImages = true;
    if (d.noProcessing) args.noProcessing = true;
    if (d.noCleanup) args.noCleanup = true;
    if (d.imageWorkers && d.imageWorkers !== 3) args.imageWorkers = d.imageWorkers;
    if (d.httpTimeout && d.httpTimeout !== 30) args.httpTimeout = d.httpTimeout;
    if (d.httpMaxRetries && d.httpMaxRetries !== 6) args.httpMaxRetries = d.httpMaxRetries;
    // Multi-source lazy discovery needs no injection here: the App.jsx
    // wrapper spreads settings.defaults (which carries multiSource +
    // multiSourceLazy) under these args, and downloader.js's chokepoint
    // emits --multi-source-lazy whenever multiSource is on and
    // multiSourceLazy isn't an explicit false.

    onStartDownload(meta.url, args);
    onSwitchTab("queue");
  };

  const handleSaveUrl = async () => {
    if (!manualUrl.trim()) return;
    setSaving(true);
    try {
      const res = await window.electronAPI.saveSeriesMeta(entry.folderPath, {
        url: manualUrl.trim(),
        title: entry.title,
      });
      if (res.ok) {
        // Mutate the entry's seriesMeta so the UI refreshes immediately
        entry.seriesMeta = res.meta;
        setManualUrl("");
      } else {
        setError(res.error || "Failed to save");
      }
    } catch (err) {
      setError(err.message);
    }
    setSaving(false);
  };

  // ── No metadata: show manual URL entry ──
  if (!meta?.url) {
    return (
      <div className="rounded-lg border border-border/50 bg-card/30 p-4 space-y-3">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <AlertCircle className="w-3.5 h-3.5 shrink-0" />
          <span>No source URL saved. Enter it to enable update checking.</span>
        </div>
        <div className="flex gap-2">
          <Input
            value={manualUrl}
            onChange={(e) => setManualUrl(e.target.value)}
            placeholder="https://mangafire.to/manga/..."
            className="h-8 text-xs flex-1"
          />
          <Button
            size="sm"
            onClick={handleSaveUrl}
            disabled={saving || !manualUrl.trim()}
            className="text-xs gap-1.5 shrink-0"
          >
            {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Link className="w-3 h-3" />}
            Save
          </Button>
        </div>
        {error && <p className="text-[10px] text-destructive">{error}</p>}
      </div>
    );
  }

  // ── Has metadata: show update check UI ──
  return (
    <div className="rounded-lg border border-border/50 bg-card/30 p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Globe className="w-3.5 h-3.5" />
          <span className="truncate max-w-[280px]" title={meta.url}>
            {meta.site || "unknown"}
          </span>
          {meta.chapters_downloaded?.length > 0 && (
            <>
              <span>&middot;</span>
              <span>{meta.chapters_downloaded.length} ch downloaded</span>
            </>
          )}
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={handleCheck}
          disabled={checking}
          className="text-xs gap-1.5 shrink-0"
        >
          {checking
            ? <Loader2 className="w-3 h-3 animate-spin" />
            : <RefreshCw className="w-3 h-3" />}
          {checking ? "Checking…" : "Check for Updates"}
        </Button>
      </div>

      {error && (
        <div className="flex items-center gap-2 text-xs text-destructive">
          <AlertCircle className="w-3.5 h-3.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {result && (
        <div className="space-y-2">
          {result.newChapters.length > 0 ? (
            <div className="rounded-md border border-orange-500/30 bg-orange-500/10 p-3 space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-orange-400">
                  {result.newChapters.length}
                  {result.checkMode === "files"
                    ? ` chapter${result.newChapters.length !== 1 ? "s" : ""} missing from device`
                    : ` new chapter${result.newChapters.length !== 1 ? "s" : ""} available`}
                </span>
                <span className="text-[10px] text-muted-foreground">
                  {result.downloaded} / {result.total} total
                </span>
              </div>
              <p className="text-[10px] text-muted-foreground">
                Chapters: {chaptersToRangeString(result.newChapters)}
              </p>
              <Button size="sm" onClick={handleDownloadNew} className="text-xs gap-1.5 w-full">
                <Download className="w-3 h-3" />
                Download Missing Chapters
              </Button>
              {/* Mode indicator */}
              <p className="text-[9px] text-muted-foreground/60 text-right">
                Checked via {result.checkMode === "files" ? "file scan" : "download history"}
              </p>
            </div>
          ) : (
            <div className="flex items-center gap-2 text-xs text-emerald-400">
              <span>&#10003;</span>
              <span>
                Up to date ({result.total} on site, {result.downloaded} on device)
              </span>
              <span className="text-[9px] text-muted-foreground/60 ml-auto">
                via {result.checkMode === "files" ? "file scan" : "history"}
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ============================================================
// DETAIL VIEW
// ============================================================
function MetadataEditorPanel({ entry, onClose, onSaved }) {
  const editableFiles = entry.files.filter((f) => ["cbz", "epub", "pdf"].includes(f.ext));
  const primaryFile = editableFiles[0];
  const [form, setForm] = useState({
    title: entry.title || "",
    writers: "",
    pencillers: "",
    genres: "",
    publisher: "",
    synopsis: "",
  });
  const [coverPath, setCoverPath] = useState("");
  const [applyAll, setApplyAll] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      if (!primaryFile || !window.electronAPI?.readMetadata) return;
      setBusy(true);
      setError(null);
      try {
        const metadata = await window.electronAPI.readMetadata(primaryFile.path);
        if (!cancelled && metadata) {
          setForm((prev) => ({
            ...prev,
            ...metadata,
            writers: Array.isArray(metadata.writers) ? metadata.writers.join(", ") : (metadata.writers || ""),
            pencillers: Array.isArray(metadata.pencillers) ? metadata.pencillers.join(", ") : (metadata.pencillers || ""),
            genres: Array.isArray(metadata.genres) ? metadata.genres.join(", ") : (metadata.genres || ""),
          }));
        }
      } catch (err) {
        if (!cancelled) setError(err.message || "Could not read metadata");
      } finally {
        if (!cancelled) setBusy(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, [primaryFile?.path]);

  const updateField = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  const handlePickCover = async () => {
    const picked = await window.electronAPI?.pickFile?.([
      { name: "Images", extensions: ["jpg", "jpeg", "png", "webp"] },
    ]);
    if (picked) setCoverPath(picked);
  };

  const handleSave = async () => {
    if (!primaryFile || !window.electronAPI?.updateMetadata) return;
    setBusy(true);
    setError(null);
    const payload = {
      ...form,
      writers: form.writers.split(",").map((s) => s.trim()).filter(Boolean),
      pencillers: form.pencillers.split(",").map((s) => s.trim()).filter(Boolean),
      genres: form.genres.split(",").map((s) => s.trim()).filter(Boolean),
    };
    try {
      const targets = applyAll ? editableFiles : [primaryFile];
      for (const file of targets) {
        await window.electronAPI.updateMetadata(file.path, payload, coverPath || null);
      }
      onSaved?.();
      onClose();
    } catch (err) {
      setError(err.message || "Could not update metadata");
    } finally {
      setBusy(false);
    }
  };

  if (!primaryFile) return null;

  return (
    <div className="mt-4 border border-border/40 bg-card/30 rounded-lg p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
          Embedded Metadata
        </h3>
        <Button variant="ghost" size="sm" onClick={onClose} className="h-7 w-7 p-0">
          <X className="w-3.5 h-3.5" />
        </Button>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <Input value={form.title} onChange={(e) => updateField("title", e.target.value)} placeholder="Title" />
        <Input value={form.publisher} onChange={(e) => updateField("publisher", e.target.value)} placeholder="Publisher" />
        <Input value={form.writers} onChange={(e) => updateField("writers", e.target.value)} placeholder="Writers" />
        <Input value={form.pencillers} onChange={(e) => updateField("pencillers", e.target.value)} placeholder="Pencillers" />
        <Input className="col-span-2" value={form.genres} onChange={(e) => updateField("genres", e.target.value)} placeholder="Genres" />
      </div>
      <textarea
        className="w-full min-h-20 rounded-md border border-input bg-background px-3 py-2 text-xs"
        value={form.synopsis}
        onChange={(e) => updateField("synopsis", e.target.value)}
        placeholder="Synopsis"
      />
      <div className="flex items-center gap-2">
        <Button variant="outline" size="sm" onClick={handlePickCover} className="text-xs">
          Cover
        </Button>
        <span className="min-w-0 flex-1 truncate text-[10px] text-muted-foreground">
          {coverPath || "No cover selected"}
        </span>
        <label className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
          <input type="checkbox" checked={applyAll} onChange={(e) => setApplyAll(e.target.checked)} />
          Apply to all
        </label>
        <Button size="sm" onClick={handleSave} disabled={busy} className="text-xs gap-1.5">
          {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Save className="w-3 h-3" />}
          Save
        </Button>
      </div>
      {error && <p className="text-[10px] text-destructive">{error}</p>}
    </div>
  );
}

function DetailView({ entry, onBack, onRefresh, onStartDownload, onSwitchTab, settings }) {
  const [deleting, setDeleting] = useState(false);
  // Two-step delete confirmation (avoids window.confirm which breaks
  // Electron's renderer focus/input handling)
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteError, setDeleteError] = useState(null);
  const [showMetadataEditor, setShowMetadataEditor] = useState(false);

  const handleOpenFile = async (filePath) => {
    if (window.electronAPI?.openFile) {
      await window.electronAPI.openFile(filePath);
    }
  };

  const handleOpenFolder = () => {
    if (window.electronAPI?.openFolder) {
      window.electronAPI.openFolder(entry.folderPath);
    }
  };

  // Open a single chapter's image folder in the OS file explorer. Image-only
  // (--format none) series have no archive files to "open", so the per-chapter
  // rows in the Files section call this instead. openFolder → shell.openPath
  // accepts any path (see main.js "open-folder" handler).
  const handleOpenChapter = (chapterPath) => {
    if (window.electronAPI?.openFolder) {
      window.electronAPI.openFolder(chapterPath);
    }
  };

  const handleDelete = async () => {
    if (!confirmDelete) {
      // First click — show confirmation
      setConfirmDelete(true);
      // Auto-cancel after 4 seconds
      setTimeout(() => setConfirmDelete(false), 4000);
      return;
    }
    // Second click — actually delete
    setDeleting(true);
    setConfirmDelete(false);
    if (window.electronAPI?.deleteSeries) {
      const result = await window.electronAPI.deleteSeries(entry.folderPath);
      if (result.ok) {
        onRefresh();
        onBack();
      } else {
        setDeleteError("Failed to delete: " + result.error);
        setDeleting(false);
      }
    }
  };

  const formats = getEntryFormats(entry);
  const meta = entry.seriesMeta;

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-3 px-5 py-3 border-b bg-card/30">
        <Button variant="ghost" size="sm" onClick={onBack} className="gap-1.5">
          <ArrowLeft className="w-3.5 h-3.5" />
          Back
        </Button>
        <h2 className="text-sm font-semibold truncate">{entry.title}</h2>
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-4">
        <div className="flex gap-5">
          {/* Cover */}
          <div className="w-44 shrink-0 aspect-[3/4] rounded-lg overflow-hidden border border-border/50 bg-muted/30">
            <PdfCover entry={entry} />
          </div>

          {/* Info */}
          <div className="flex-1 min-w-0 space-y-3">
            <h2 className="text-lg font-bold leading-tight">{entry.title}</h2>

            {/* Series metadata from .aio_series.json */}
            {meta && (
              <div className="space-y-1.5">
                {meta.status && (
                  <span
                    className={cn(
                      "inline-block text-[10px] font-bold uppercase px-2 py-0.5 rounded border",
                      STATUS_COLORS[meta.status] || "bg-muted text-muted-foreground border-border"
                    )}
                  >
                    {meta.status}
                  </span>
                )}
                {meta.authors?.length > 0 && (
                  <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <User className="w-3 h-3 shrink-0" />
                    <span>{meta.authors.join(", ")}</span>
                  </div>
                )}
                {meta.genres?.length > 0 && (
                  <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <Tag className="w-3 h-3 shrink-0" />
                    <span>{meta.genres.join(", ")}</span>
                  </div>
                )}
              </div>
            )}

            {/* Stats row */}
            <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
              <div className="flex items-center gap-1">
                <FileText className="w-3.5 h-3.5" />
                {entry.isImageOnly
                  ? `${entry.imageCount} image${entry.imageCount !== 1 ? "s" : ""}`
                  : `${entry.files.length} file${entry.files.length !== 1 ? "s" : ""}`}
              </div>
              <div>{formatSize(entry.totalSize)}</div>
              {meta?.chapters_downloaded?.length > 0 && (
                <div>{meta.chapters_downloaded.length} chapters</div>
              )}
              {entry.lastModified && <div>Modified {formatDate(entry.lastModified)}</div>}
            </div>

            {/* Format badges */}
            <div className="flex gap-1.5">
              {formats.map((fmt) => (
                <span
                  key={fmt}
                  className={cn(
                    "text-[10px] font-bold uppercase px-2 py-1 rounded border",
                    FORMAT_COLORS[fmt] || "bg-muted text-muted-foreground border-border"
                  )}
                >
                  {fmt}
                </span>
              ))}
            </div>

            {/* Action buttons */}
            <div className="flex gap-2 pt-1">
              <Button variant="outline" size="sm" onClick={handleOpenFolder} className="gap-1.5 text-xs">
                <FolderOpen className="w-3.5 h-3.5" />
                Open Folder
              </Button>
              {meta?.url && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => window.open(meta.url, "_blank")}
                  className="gap-1.5 text-xs"
                >
                  <Globe className="w-3.5 h-3.5" />
                  Source
                </Button>
              )}
              {/* Embedded-metadata editing writes into a ComicInfo.xml inside
                  the archive; image-only (--format none) series have no
                  archive, so the editor has nothing to act on. Hide it there
                  (MetadataEditorPanel also self-guards by returning null). */}
              {entry.files.length > 0 && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setShowMetadataEditor((value) => !value)}
                  className="gap-1.5 text-xs"
                >
                  <PencilLine className="w-3.5 h-3.5" />
                  Metadata
                </Button>
              )}
              <Button
                variant="outline"
                size="sm"
                onClick={handleDelete}
                onBlur={() => setConfirmDelete(false)}
                disabled={deleting}
                className={cn(
                  "gap-1.5 text-xs",
                  confirmDelete
                    ? "text-destructive bg-destructive/10 border-destructive/50 hover:bg-destructive/20 hover:text-destructive hover:border-destructive/50"
                    : "text-destructive hover:text-destructive hover:border-destructive/50"
                )}
              >
                <Trash2 className="w-3.5 h-3.5" />
                {deleting ? "Deleting…" : confirmDelete ? "Are you sure?" : "Delete"}
              </Button>
            </div>
            {deleteError && (
              <p className="text-[10px] text-destructive mt-1">{deleteError}</p>
            )}
            {showMetadataEditor && (
              <MetadataEditorPanel
                entry={entry}
                onClose={() => setShowMetadataEditor(false)}
                onSaved={onRefresh}
              />
            )}
          </div>
        </div>

        {/* Update checking section */}
        <div className="mt-5">
          <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
            Updates
          </h3>
          <UpdateSection
            entry={entry}
            onStartDownload={onStartDownload}
            onSwitchTab={onSwitchTab}
            settings={settings}
          />
        </div>

        {/* File list (archives) — or per-chapter image folders for image-only
            (--format none) series, which have no archive to open. Each chapter
            row opens its images/Chapter_<n>/ folder in the OS file explorer
            (handleOpenChapter). imageChapters comes from library.js. */}
        <div className="mt-5">
          <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
            {entry.isImageOnly ? "Chapters" : "Files"}
          </h3>
          <div className="space-y-1">
            {entry.isImageOnly
              ? (entry.imageChapters || []).map((chap) => (
                  <button
                    key={chap.path}
                    onClick={() => handleOpenChapter(chap.path)}
                    className={cn(
                      "w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left",
                      "bg-card/40 border border-border/30",
                      "hover:bg-card/80 hover:border-primary/30",
                      "transition-all duration-100 group"
                    )}
                  >
                    <span
                      className={cn(
                        "text-[9px] font-bold uppercase px-1.5 py-0.5 rounded border shrink-0",
                        FORMAT_COLORS.images
                      )}
                    >
                      IMG
                    </span>
                    <span className="text-xs font-medium truncate flex-1">
                      {chap.name.replace(/_/g, " ")}
                    </span>
                    <span className="text-[10px] text-muted-foreground shrink-0">
                      {chap.imageCount} img &middot; {formatSize(chap.size)}
                    </span>
                    <FolderOpen className="w-3.5 h-3.5 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity shrink-0" />
                  </button>
                ))
              : entry.files.map((file) => (
                  <button
                    key={file.path}
                    onClick={() => handleOpenFile(file.path)}
                    className={cn(
                      "w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left",
                      "bg-card/40 border border-border/30",
                      "hover:bg-card/80 hover:border-primary/30",
                      "transition-all duration-100 group"
                    )}
                  >
                    <span
                      className={cn(
                        "text-[9px] font-bold uppercase px-1.5 py-0.5 rounded border shrink-0",
                        FORMAT_COLORS[file.ext] || "bg-muted text-muted-foreground border-border"
                      )}
                    >
                      {file.ext}
                    </span>
                    <span className="text-xs font-medium truncate flex-1">{file.name}</span>
                    <span className="text-[10px] text-muted-foreground shrink-0">
                      {formatSize(file.size)}
                    </span>
                    <ExternalLink className="w-3.5 h-3.5 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity shrink-0" />
                  </button>
                ))}
          </div>
        </div>
      </div>
    </div>
  );
}

// ============================================================
// EMPTY STATE
// ============================================================
function EmptyState() {
  return (
    <div className="flex-1 flex flex-col items-center justify-center text-center py-20 px-8">
      <div className="w-16 h-16 rounded-full bg-muted/50 flex items-center justify-center mb-4">
        <BookOpen className="w-7 h-7 text-muted-foreground/50" />
      </div>
      <h3 className="text-sm font-semibold mb-1">No manga yet</h3>
      <p className="text-xs text-muted-foreground max-w-xs">
        Downloaded manga will appear here. Go to the{" "}
        <span className="inline-flex items-center gap-0.5 text-primary">
          <Download className="w-3 h-3" /> New
        </span>{" "}
        tab to start downloading.
      </p>
    </div>
  );
}

// ============================================================
// LIBRARY TAB (main export)
// ============================================================
export default function LibraryTab({
  onStartDownload, onSwitchTab, settings, onSaveSettings,
  // Lifted state from useDownloader. libraryEntries is null until first
  // load completes (so we know whether to trigger an initial fetch on
  // mount). loadLibrary forces a fresh scan; setLibraryEntries is exposed
  // so handleCheckAll can splice updatedMeta back into the entries list
  // without round-tripping through the IPC scan again.
  libraryEntries, libraryLoading, loadLibrary, setLibraryEntries,
}) {
  const entries = libraryEntries || [];
  const loading = libraryLoading || libraryEntries === null;
  // setEntries shim so the existing handleCheckAll / detail-edit code reads
  // naturally without diverging from the upstream pattern. Calling
  // setLibraryEntries with a non-null value is safe — null is reserved
  // exclusively for the "not yet loaded" sentinel that loadLibrary clears
  // by setting an array (even an empty one).
  const setEntries = setLibraryEntries;

  const [searchQuery, setSearchQuery] = useState("");
  // Lazy-init from persisted settings.libraryOpts.sortBy. Falls back to "title"
  // for first run / older settings dicts. Sync below via useEffect when the
  // settings prop hydrates asynchronously from disk on app launch.
  const [sortBy, setSortBy] = useState(() => settings?.libraryOpts?.sortBy ?? "title");

  // Sync once when settings.libraryOpts.sortBy arrives from disk (history.json
  // load is async). Same shape as SearchTab's settings.searchOpts hydration.
  useEffect(() => {
    const persisted = settings?.libraryOpts?.sortBy;
    if (persisted && persisted !== sortBy) {
      setSortBy(persisted);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings?.libraryOpts?.sortBy]);

  // Wrap the setter so picking a new sort persists to settings.libraryOpts.
  // Spread merge preserves any future libraryOpts fields without listing them.
  const updateSort = (value) => {
    setSortBy(value);
    onSaveSettings?.({
      libraryOpts: { ...(settings?.libraryOpts || {}), sortBy: value },
    });
  };
  const [selectedEntry, setSelectedEntry] = useState(null);

  // New chapter counts per series (folderPath → count).
  // Populated by "Check All" or individual checks. Drives the orange "+N
  // new" badge on each MangaCard, so dismissing a card must mutate this so
  // the badge clears.
  const [newChapterCounts, setNewChapterCounts] = useState({});

  // ── Updates Center state ──
  // `seriesStates` is the live Map<folderPath, SeriesState> the panel
  // renders. SeriesState shape:
  //   {
  //     folderPath, title, cover, site,
  //     state: "queued" | "running" | "found" | "uptodate" | "error",
  //     newChapters?: string[],   // when state === "found"
  //     total?: number,           // total chapters on site (uptodate/found)
  //     error?: string,           // shorthand sentinel (uptodate/error)
  //     errorMessage?: string,    // full message
  //     enqueuedAt: number,       // monotonic for stable section sort
  //   }
  // We keep it as a useRef + a forced bump counter so frequent IPC events
  // don't re-mount the panel — the actual Map identity churn is throttled.
  // Frequent re-renders during 30-series scans got janky when state was a
  // plain object; the Map+bump pattern keeps each update O(1) without
  // copying the whole structure.
  const seriesStatesRef = useRef(new Map());
  const [seriesStateVersion, setSeriesStateVersion] = useState(0);
  const bumpStates = useCallback(() => setSeriesStateVersion((v) => v + 1), []);

  // Scan-level state for the panel header / progress bar.
  const [scanState, setScanState] = useState("idle"); // "idle" | "running" | "done"
  const [scanStats, setScanStats] = useState({ completed: 0, total: 0, durationMs: 0 });
  const [updatesPanelOpen, setUpdatesPanelOpen] = useState(false);

  // ── Load library on first mount only ──
  // libraryEntries is the null sentinel until the first scan completes.
  // Subsequent tab switches see an array and skip the fetch — the entries
  // and pending thumbnail-ready events are managed at the hook level.
  useEffect(() => {
    if (libraryEntries === null && !libraryLoading) {
      loadLibrary();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Listen for check-all-updates progress (richer event shape) ──
  // Events come tagged by `kind`. The Map mutation is in-place (we read the
  // ref, mutate it, then bump a version counter) so React re-renders the
  // panel without copying every entry on every event — critical when 30+
  // series each emit 3 events (queued, running, completed) in <60s.
  useEffect(() => {
    if (!window.electronAPI?.onUpdateCheckProgress) return;
    const unsub = window.electronAPI.onUpdateCheckProgress((event) => {
      const map = seriesStatesRef.current;
      switch (event.kind) {
        case "queued": {
          // Existing entry might be a previous scan's result — overwrite.
          map.set(event.folderPath, {
            folderPath: event.folderPath,
            title: event.title,
            cover: event.cover,
            site: event.site,
            state: "queued",
            enqueuedAt: Date.now(),
          });
          setScanState("running");
          setScanStats({ completed: 0, total: event.total, durationMs: 0 });
          bumpStates();
          break;
        }
        case "running": {
          const prev = map.get(event.folderPath) || {
            folderPath: event.folderPath,
            title: event.title,
            enqueuedAt: Date.now(),
          };
          map.set(event.folderPath, {
            ...prev,
            state: "running",
            site: event.site || prev.site,
          });
          setScanStats((s) => ({ ...s, completed: event.completed, total: event.total }));
          bumpStates();
          break;
        }
        case "completed": {
          const prev = map.get(event.folderPath) || {};
          const r = event.result || {};
          let state;
          let extra = {};
          if (r.error === "aborted") {
            state = "error";
            extra = { error: "aborted", errorMessage: "cancelled" };
          } else if (r.error) {
            state = "error";
            extra = { error: r.error, errorMessage: r.message || r.error };
          } else if (r.newChapters && r.newChapters.length > 0) {
            state = "found";
            extra = { newChapters: r.newChapters, total: r.total };
          } else {
            state = "uptodate";
            extra = { total: r.total };
          }
          map.set(event.folderPath, {
            ...prev,
            folderPath: event.folderPath,
            title: event.title || prev.title,
            cover: r.cover || prev.cover,
            site: r.site || prev.site,
            state,
            ...extra,
          });
          setScanStats((s) => ({ ...s, completed: event.completed, total: event.total }));

          // Mirror "found" rows into newChapterCounts so the grid badges
          // light up live as the scan progresses (matches the legacy
          // behavior where the badge only appeared post-scan).
          if (state === "found") {
            setNewChapterCounts((prev) => ({
              ...prev,
              [event.folderPath]: r.newChapters.length,
            }));
          }

          // Splice fresh metadata (status / authors / cover / genres) back
          // into the entries list so the grid card + detail view reflect
          // the live data without a manual Refresh. Same merge semantics
          // as the legacy handler — only overwrite fields the live check
          // populated, never drop chapters_downloaded etc.
          if (r.updatedMeta) {
            setEntries((entries) =>
              entries.map((e) => {
                if (e.folderPath !== event.folderPath) return e;
                const merged = { ...e.seriesMeta };
                for (const [k, v] of Object.entries(r.updatedMeta)) {
                  if (v !== undefined && v !== null) merged[k] = v;
                }
                return { ...e, seriesMeta: merged };
              })
            );
          }
          bumpStates();
          break;
        }
        case "done": {
          setScanState("done");
          setScanStats({
            completed: event.completed,
            total: event.total,
            durationMs: event.durationMs,
          });
          bumpStates();
          break;
        }
        default:
          // Unknown event shape (e.g. an old main.js firing the legacy
          // { current, total, title } payload). Ignore silently — when
          // both ends are upgraded this branch is never reached.
          break;
      }
    });
    return unsub;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Trigger a fresh scan ──
  // Opens the panel (if closed), clears any previous scan results, and
  // calls the IPC. The Promise return value is intentionally ignored — the
  // event stream is the source of truth for the UI; the Promise just tells
  // us when the sweep is fully drained (sectioned dispatch already handled
  // by the "done" event).
  const handleCheckAll = useCallback(async () => {
    if (!window.electronAPI?.checkAllUpdates) return;
    // Wipe last scan's bookkeeping so the panel reflects only this run.
    seriesStatesRef.current = new Map();
    bumpStates();
    setScanState("running");
    setScanStats({ completed: 0, total: 0, durationMs: 0 });
    setUpdatesPanelOpen(true);
    try {
      await window.electronAPI.checkAllUpdates();
    } catch (err) {
      console.error("Check all updates failed:", err);
      setScanState("done");
    }
  }, [bumpStates]);

  // ── Open the panel without rescanning ──
  // Used by the toolbar button when a previous scan's results are still
  // in memory — clicking shouldn't blow them away just to re-open the view.
  const handleOpenPanel = useCallback(() => {
    setUpdatesPanelOpen(true);
  }, []);

  // ── Cancel an in-flight scan ──
  const handleCancelScan = useCallback(async () => {
    if (!window.electronAPI?.cancelCheckAllUpdates) return;
    try {
      await window.electronAPI.cancelCheckAllUpdates();
    } catch (err) {
      console.error("Cancel check-all failed:", err);
    }
  }, []);

  // ── Per-row queue (called from the panel) ──
  // Builds the same download args as DetailView's "Download Missing
  // Chapters" path so the user gets identical behavior whether they queue
  // from the panel or the detail view. Pulls defaults from settings.
  //
  // One UpdatesCenter-specific override: seededRatingOnly is injected by
  // default. The probe phase in search_all (run when --multi-source is on)
  // costs ~30-60 s per series on MangaFire-class handlers — Playwright VRF
  // per sample chapter plus image-quality scoring. For an update download
  // that's typically 1-5 new chapters, that probe cost dominates the
  // actual download. Skipping it falls back to sites/quality_seed.json's
  // curated per-site quality priors for ranking (which is what the
  // multi-source picker uses as a tiebreaker anyway when title-match
  // scores are within 0.10 of each other). Cross-file: --seeded-rating-only
  // is defined in aio-dl.py near --enable-ml-rating; downloader.js maps it
  // via boolMap; aio_search_cli.find_alternatives_for_direct_url honors
  // it by passing img_quality_cache=None into search_all. Opt out per the
  // settings.updateChecksUseSeededRating toggle for users on stable
  // handlers who want full probe accuracy.
  const buildDownloadArgsForRow = useCallback((row, entry) => {
    const meta = entry?.seriesMeta || {};
    const rangeStr = chaptersToRangeString(row.newChapters);
    const d = settings?.defaults || {};
    const args = {
      format: meta.format || d.format || "pdf",
      quality: d.quality ?? 85,
      chapters: rangeStr,
      language: meta.language || "en",
      site: meta.site || undefined,
      verbose: settings?.verboseAlways ?? true,
    };
    if (d.scaling && d.scaling !== 100) args.scaling = d.scaling;
    if (d.keepChapters) args.keepChapters = true;
    if (d.noFinalFile) args.noFinalFile = true;
    if (d.keepImages) args.keepImages = true;
    if (d.noProcessing) args.noProcessing = true;
    if (d.noCleanup) args.noCleanup = true;
    if (d.imageWorkers && d.imageWorkers !== 3) args.imageWorkers = d.imageWorkers;
    if (d.httpTimeout && d.httpTimeout !== 30) args.httpTimeout = d.httpTimeout;
    if (d.httpMaxRetries && d.httpMaxRetries !== 6) args.httpMaxRetries = d.httpMaxRetries;
    // Default on — settings.updateChecksUseSeededRating !== false catches
    // both the explicit-true case and the default-undefined case. Has no
    // effect when --multi-source isn't on (the alternatives discovery
    // doesn't run, so there's nothing to probe in the first place).
    if (settings?.updateChecksUseSeededRating !== false) {
      args.seededRatingOnly = true;
    }
    // Multi-source lazy discovery (--multi-source-lazy) needs no injection
    // here: App.jsx's wrapper spreads settings.defaults (multiSource +
    // multiSourceLazy) under these args, and downloader.js's chokepoint
    // emits the flag whenever multiSource is on and multiSourceLazy isn't
    // an explicit false — update downloads inherit the global default.
    return { url: meta.url, args };
  }, [settings]);

  const handleQueueRow = useCallback((row) => {
    const entry = entries.find((e) => e.folderPath === row.folderPath);
    if (!entry?.seriesMeta?.url) return;
    const { url, args } = buildDownloadArgsForRow(row, entry);
    onStartDownload(url, args);
    // Clear the badge for the queued row — the user committed; if a new
    // scan finds more later, the count will repopulate.
    setNewChapterCounts((prev) => {
      const next = { ...prev };
      delete next[row.folderPath];
      return next;
    });
    // Also strip the row from seriesStates so the "Updates Found" section
    // shrinks. Keep an "uptodate" placeholder so the user sees feedback
    // that the row was actioned (rather than vanishing without a trace).
    const map = seriesStatesRef.current;
    const existing = map.get(row.folderPath);
    if (existing) {
      map.set(row.folderPath, {
        ...existing,
        state: "uptodate",
        newChapters: undefined,
      });
      bumpStates();
    }
  }, [entries, buildDownloadArgsForRow, onStartDownload, bumpStates]);

  const handleQueueAll = useCallback(() => {
    const map = seriesStatesRef.current;
    const founds = [];
    for (const row of map.values()) {
      if (row.state === "found") founds.push(row);
    }
    for (const row of founds) {
      const entry = entries.find((e) => e.folderPath === row.folderPath);
      if (!entry?.seriesMeta?.url) continue;
      const { url, args } = buildDownloadArgsForRow(row, entry);
      onStartDownload(url, args);
    }
    // Bulk-clear all queued badges + downgrade rows to up-to-date.
    setNewChapterCounts((prev) => {
      const next = { ...prev };
      for (const r of founds) delete next[r.folderPath];
      return next;
    });
    for (const r of founds) {
      const existing = map.get(r.folderPath);
      if (existing) {
        map.set(r.folderPath, {
          ...existing,
          state: "uptodate",
          newChapters: undefined,
        });
      }
    }
    bumpStates();
    onSwitchTab("queue");
    setUpdatesPanelOpen(false);
  }, [entries, buildDownloadArgsForRow, onStartDownload, onSwitchTab, bumpStates]);

  // Clear badges for the given folderPaths without queueing anything.
  // Used by the "Dismiss" buttons (per-row + bulk). The row itself stays in
  // the panel as "uptodate" so the user can see the dismiss took effect.
  const handleDismiss = useCallback((folderPaths) => {
    setNewChapterCounts((prev) => {
      const next = { ...prev };
      for (const p of folderPaths) delete next[p];
      return next;
    });
    const map = seriesStatesRef.current;
    for (const p of folderPaths) {
      const existing = map.get(p);
      if (existing) {
        map.set(p, {
          ...existing,
          state: "uptodate",
          newChapters: undefined,
        });
      }
    }
    bumpStates();
  }, [bumpStates]);

  const handleRefresh = useCallback(() => {
    setSelectedEntry(null);
    loadLibrary();
  }, [loadLibrary]);

  // ── Filter + Sort ──
  // Pre-compute lowercased titles ONCE per entries-list change. Without
  // this, every keystroke recomputes `e.title.toLowerCase()` for every
  // entry — at 200 entries × 8 keystrokes/sec that's 1600 case
  // conversions/sec just to filter on a substring search.
  const entriesIndexed = useMemo(
    () => entries.map((e) => ({ entry: e, lowerTitle: (e.title || "").toLowerCase() })),
    [entries]
  );
  const lowerQuery = useMemo(() => searchQuery.toLowerCase(), [searchQuery]);
  const filtered = useMemo(
    () => {
      if (!lowerQuery) return entries;
      return entriesIndexed
        .filter((x) => x.lowerTitle.includes(lowerQuery))
        .map((x) => x.entry);
    },
    [entriesIndexed, lowerQuery, entries]
  );

  const sorted = useMemo(() => {
    const copy = [...filtered];
    copy.sort((a, b) => {
      switch (sortBy) {
        case "title": return a.title.localeCompare(b.title);
        case "title-desc": return b.title.localeCompare(a.title);
        case "date": return (b.lastModified || "").localeCompare(a.lastModified || "");
        case "date-asc": return (a.lastModified || "").localeCompare(b.lastModified || "");
        case "size": return b.totalSize - a.totalSize;
        case "size-asc": return a.totalSize - b.totalSize;
        default: return 0;
      }
    });
    return copy;
  }, [filtered, sortBy]);

  // Count how many series are eligible for "Check All".
  // Must mirror main.js's checkable filter — see the comment there. When
  // settings.checkAllIncludeCompleted is on (default), Completed/Finished
  // series count too because aggregators (notably mangafire) lie about
  // status. If the user has opted out via Settings, restore the legacy
  // ongoing-only filter.
  const includeCompletedInCheck = settings?.checkAllIncludeCompleted !== false;
  const ongoingCount = entries.filter((e) => {
    if (!e.seriesMeta?.url) return false;
    if (includeCompletedInCheck) return true;
    const s = e.seriesMeta.status;
    return !s || s === "Ongoing" || s === "Releasing";
  }).length;

  // Total updates found in the current/last scan. Drives the badge on the
  // toolbar button so users see at a glance how many actionable updates
  // are waiting in the panel.
  // The dep on seriesStateVersion forces this to recompute when the Map
  // mutates (the Ref's reference identity never changes).
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const updatesFoundCount = useMemo(() => {
    let n = 0;
    for (const r of seriesStatesRef.current.values()) {
      if (r.state === "found") n += 1;
    }
    return n;
  }, [seriesStateVersion]);

  // Shallow-cloned copy of the series-state Map. The ref's Map identity
  // never changes (we mutate in place to avoid copy cost on every IPC
  // event), but the UpdatesCenter panel's useMemo over seriesStates needs
  // a fresh identity to recompute its grouping. Cloning once per version
  // bump here gives the panel a stable "input changed" signal at the
  // expense of one O(N) Map iteration per LibraryTab render — trivial
  // for N ≤ a few hundred.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const seriesStatesSnapshot = useMemo(
    () => new Map(seriesStatesRef.current),
    [seriesStateVersion]
  );

  // ── Detail view ──
  if (selectedEntry) {
    const current = entries.find((e) => e.folderPath === selectedEntry.folderPath) || selectedEntry;
    return (
      <DetailView
        entry={current}
        onBack={() => setSelectedEntry(null)}
        onRefresh={handleRefresh}
        onStartDownload={onStartDownload}
        onSwitchTab={onSwitchTab}
        settings={settings}
      />
    );
  }

  // ── GRID VIEW ──
  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center gap-2 px-4 py-2.5 border-b bg-card/20">
        <div className="relative flex-1 max-w-xs">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
          <Input
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search manga…"
            className="pl-8 h-8 text-xs"
          />
        </div>

        <div className="flex items-center gap-1.5">
          <ArrowUpDown className="w-3.5 h-3.5 text-muted-foreground" />
          <select
            value={sortBy}
            onChange={(e) => updateSort(e.target.value)}
            className={cn(
              "text-xs bg-transparent border border-border rounded-md px-2 py-1.5",
              "text-foreground cursor-pointer",
              "focus:outline-none focus:ring-1 focus:ring-primary"
            )}
          >
            {SORT_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </div>

        {/* Updates Center button.
            Three label states:
              - Scanning N/M  (during a sweep)
              - Updates ●N    (after a sweep with N found, distinct accent)
              - Check All     (fresh / no results to show)
            Click behavior:
              - While scanning: opens the panel (visible progress)
              - After a sweep with results: opens the panel WITHOUT
                re-scanning (the user just wants to see what's there)
              - Otherwise: triggers a new scan AND opens the panel
            Cross-file: UpdatesCenter.jsx is the rendered panel; its scan
            state is owned here in LibraryTab. */}
        {ongoingCount > 0 && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              if (scanState === "running") {
                handleOpenPanel();
              } else if (scanState === "done" && updatesFoundCount > 0) {
                handleOpenPanel();
              } else {
                handleCheckAll();
              }
            }}
            disabled={loading}
            className={cn(
              "gap-1.5 text-xs relative",
              scanState === "done" && updatesFoundCount > 0 && [
                "border-orange-500/50 text-orange-300",
                "hover:bg-orange-500/10 hover:text-orange-200 hover:border-orange-500/60",
              ]
            )}
            title={
              scanState === "running"
                ? `Scanning ${scanStats.completed} of ${scanStats.total}…`
                : updatesFoundCount > 0
                ? `${updatesFoundCount} series have new chapters — click to view`
                : `Check ${ongoingCount} ongoing series for new chapters`
            }
          >
            {scanState === "running" ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Bell className="w-3.5 h-3.5" />
            )}
            {scanState === "running"
              ? `Scanning ${scanStats.completed}/${scanStats.total}`
              : scanState === "done" && updatesFoundCount > 0
              ? `Updates ${updatesFoundCount}`
              : "Check All"}
            {scanState === "done" && updatesFoundCount > 0 && (
              <span
                aria-hidden
                className="absolute -top-0.5 -right-0.5 w-1.5 h-1.5 rounded-full bg-orange-400 shadow-[0_0_6px_rgba(249,115,22,0.8)]"
              />
            )}
          </Button>
        )}

        <Button
          variant="ghost"
          size="sm"
          onClick={handleRefresh}
          disabled={loading}
          className="gap-1.5 text-xs"
          title="Refresh library"
        >
          <RefreshCw className={cn("w-3.5 h-3.5", loading && "animate-spin")} />
        </Button>

        <Badge variant="secondary" className="text-[10px] ml-auto">
          {loading ? "…" : `${sorted.length} manga${sorted.length !== 1 ? "s" : ""}`}
        </Badge>
      </div>

      {/* Grid / Empty */}
      <div className="flex-1 overflow-y-auto">
        {!loading && sorted.length === 0 ? (
          searchQuery ? (
            <div className="flex flex-col items-center justify-center py-20 text-center">
              <Search className="w-8 h-8 text-muted-foreground/30 mb-3" />
              <p className="text-xs text-muted-foreground">
                No manga matching &ldquo;{searchQuery}&rdquo;
              </p>
            </div>
          ) : (
            <EmptyState />
          )
        ) : (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(140px,1fr))] gap-3 p-4">
            {sorted.map((entry) => (
              <MangaCard
                key={entry.folderPath}
                entry={entry}
                newCount={newChapterCounts[entry.folderPath] || 0}
                onClick={() => setSelectedEntry(entry)}
              />
            ))}

            {loading && entries.length === 0 &&
              Array.from({ length: 8 }).map((_, i) => (
                <div key={i} className="rounded-lg overflow-hidden border border-border/30">
                  <div className="aspect-[3/4] bg-muted/30 animate-pulse" />
                  <div className="p-2.5 space-y-2">
                    <div className="h-3 bg-muted/40 rounded animate-pulse w-3/4" />
                    <div className="h-2 bg-muted/30 rounded animate-pulse w-1/2" />
                  </div>
                </div>
              ))
            }
          </div>
        )}
      </div>

      {/* ── Updates Center side-sheet ──
          Rendered last so it overlays the grid + toolbar. */}
      <UpdatesCenter
        open={updatesPanelOpen}
        onClose={() => setUpdatesPanelOpen(false)}
        seriesStates={seriesStatesSnapshot}
        scanState={scanState}
        scanStats={scanStats}
        onRescan={handleCheckAll}
        onCancel={handleCancelScan}
        onQueueRow={handleQueueRow}
        onQueueAll={handleQueueAll}
        onDismiss={handleDismiss}
        hasCheckableSeries={ongoingCount > 0}
      />
    </div>
  );
}
