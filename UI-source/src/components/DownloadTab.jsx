// ============================================================
// DOWNLOAD TAB
//
// The main form for starting new downloads. Every CLI argument
// from aio-dl.py has a corresponding UI control.
//
// SCROLL FIX: The form area uses flex-1 + overflow-y-auto so
// it scrolls when content is taller than the window. The Start
// button stays pinned at the bottom with flex-shrink-0.
// ============================================================

import React, { useState, useEffect } from "react";
import {
  Button, Input, Textarea, Label, Switch, Slider, Select,
  Checkbox, SectionHeader, Collapsible, Badge,
} from "@/components/ui/primitives";
import { Download } from "lucide-react";
import { cn } from "@/lib/utils";

// ── DEFAULT VALUES ──
// Match aio-dl.py's defaults so you can start a basic download
// without changing anything.
//
// NOTE on quality=100 vs aio-dl.py's argparse default of 85:
// Phase G4 in aio-dl.py (~line 4272) reads `_user_set_quality` from sys.argv;
// any explicit --quality < 100 disables the CBZ byte-preserving fast-path.
// The UI always emits --quality from form state (line 111), so a default
// of 85 would force every CBZ download into the slow decode/re-encode path.
// Keep this at 100 unless you also revisit Phase G4. Direct CLI users still
// get aio-dl.py's argparse default of 85 — the divergence is intentional.
const DEFAULT_FORM = {
  urls: "",
  format: "pdf",
  epubLayout: "vertical",
  quality: 100,
  scaling: 100,
  width: "",
  aspectRatio: "",
  chapters: "all",
  language: "en",
  noPartials: false,
  keepChapters: false,
  noFinalFile: false,
  keepImages: false,
  noProcessing: false,
  noCleanup: false,
  splitMode: "none",
  splitValue: "",
  group: "",
  mixByUpvote: false,
  cookies: "",
  jobs: 1,
  imageWorkers: 3,
  httpTimeout: 30,
  httpMaxRetries: 6,
  httpBackoffBase: 1.0,
  httpBackoffCap: 45,
  netMinGap: 0.25,
  missedRetries: 2,
  noRetryMissedChapters: false,
  // Multi-source fallback (also exposed in Search tab; applies here for
  // direct-URL downloads). When enabled, aio-dl.py pre-fetches chapter
  // lists from cross-site alternatives so per-chapter download failures
  // can fall over to another source instead of aborting. Quality floor
  // gates which alternatives qualify (mirror of --multi-source-quality-min).
  multiSource: false,
  multiSourceQualityMin: 0.65,
  // CBZ byte-preservation (Phase F, 2026-05-07). True = use original wire
  // bytes when --format cbz with --scaling 100 and no --width/--quality
  // override. False emits --no-cbz-preserve-originals and forces the
  // legacy decode/recombine/re-encode path. Settings tab provides the
  // global default; per-job overrides aren't surfaced in this form
  // (the setting is essentially "trust the bytes" yes/no, not per-job).
  cbzPreserveOriginals: true,
  // LINE Webtoon WebP recompression (Phase 1, 2026-05-11). Re-encodes
  // each downloaded page from PNG/JPEG to lossy WebP at the chosen quality
  // before packaging. Only fires on webtoons.com (Python checks
  // handler.name === "linewebtoon"); silently no-ops elsewhere, so this is
  // safe to leave on as a global default for users whose library is mostly
  // webtoons. Target: ~45GB Eleceed library → ~5GB at q85. Same useEffect
  // at line ~110 spreads settings.defaults onto the form, so the Settings
  // tab counterparts pre-populate these on a fresh New tab open.
  webtoonRecompress: false,
  webtoonRecompressQuality: 85,
  webtoonRecompressMethod: 4,
  // Komikku-compatible per-chapter CBZ output (2026-05-12, Komikku LocalSource format).
  // When on, Python force-coerces format=cbz / keep-chapters / no-final-file,
  // writes per-chapter ComicInfo.xml inside each CBZ, plus cover.jpg +
  // details.json at the series-folder root. settings.defaults.komikku
  // propagates here via the useEffect at line ~120-124 below.
  komikku: false,
};

const FORMATS = [
  { value: "pdf", label: "PDF", desc: "Tablet reading" },
  { value: "epub", label: "EPUB", desc: "E-reader" },
  { value: "cbz", label: "CBZ", desc: "Comic archive" },
  // The "Images only" promise is honored by the format-button onClick
  // below (and SettingsTab's Format select), which auto-enables
  // keepImages whenever format=none is picked. aio-dl.py:6196 silently
  // `pass`es on format==none — without the auto-enable, this label
  // produced an empty manga folder. See the warning below the format
  // grid for the user-explicitly-unchecked edge case.
  { value: "none", label: "None", desc: "Images only" },
];

const LANGUAGES = [
  { value: "en", label: "English" },
  { value: "ja", label: "Japanese" },
  { value: "ko", label: "Korean" },
  { value: "zh", label: "Chinese" },
  { value: "es", label: "Spanish" },
  { value: "fr", label: "French" },
  { value: "pt-br", label: "Portuguese (BR)" },
  { value: "de", label: "German" },
  { value: "it", label: "Italian" },
  { value: "ru", label: "Russian" },
  { value: "ar", label: "Arabic" },
  { value: "tr", label: "Turkish" },
];

function formDefaults(settings) {
  return { ...DEFAULT_FORM, ...(settings?.defaults || {}) };
}

export default function DownloadTab({
  onStartDownload,
  settings,
  draft,
  onDraftChange,
  onDraftConsumed,
}) {
  const [form, setFormState] = useState(() => ({
    ...formDefaults(settings),
    ...(draft || {}),
  }));

  // Apply saved defaults from Settings tab when they load. If the user has an
  // unsent draft, keep that draft on top so tab switches don't wipe their work.
  useEffect(() => {
    setFormState({
      ...formDefaults(settings),
      ...(draft || {}),
    });
  }, [settings?.defaults, draft]);

  const updateForm = (updater) => {
    setFormState((prev) => {
      const next = typeof updater === "function" ? updater(prev) : updater;
      onDraftChange?.(next);
      return next;
    });
  };

  // Helper to update one form field
  const set = (key, value) => updateForm((prev) => ({ ...prev, [key]: value }));

  // Build CLI args from form state and trigger download
  const handleStart = () => {
    const urls = form.urls.split("\n").map((u) => u.trim()).filter(Boolean);
    if (urls.length === 0) return;

    const args = {
      format: form.format,
      quality: form.quality,
      language: form.language,
      verbose: settings?.verboseAlways !== false,
    };

    // Only include changed-from-default values
    if (form.scaling !== 100) args.scaling = form.scaling;
    if (form.format === "epub") args.epubLayout = form.epubLayout;
    if (form.width) args.width = parseInt(form.width, 10);
    if (form.aspectRatio) args.aspectRatio = form.aspectRatio;
    if (form.chapters !== "all") args.chapters = form.chapters;
    if (form.noPartials) args.noPartials = true;
    if (form.keepChapters) args.keepChapters = true;
    if (form.noFinalFile) args.noFinalFile = true;
    if (form.keepImages) args.keepImages = true;
    if (form.noProcessing) args.noProcessing = true;
    if (form.noCleanup) args.noCleanup = true;
    if (form.noRetryMissedChapters) args.noRetryMissedChapters = true;
    if (form.missedRetries !== 2) args.missedRetries = form.missedRetries;
    if (form.multiSource) {
      args.multiSource = true;
      // Only emit --multi-source-quality-min when it differs from the CLI
      // default (0.65). Keeps the spawn args cleaner.
      if (form.multiSourceQualityMin !== 0.65) {
        args.multiSourceQualityMin = form.multiSourceQualityMin;
      }
    }
    // Phase F (2026-05-07): forward the CBZ byte-preservation toggle. Only
    // emit when the user has it OFF — buildCliArgs's negative-form handler
    // turns this into --no-cbz-preserve-originals. Default (true) is the
    // implicit Python-side default, so omitting from args keeps spawn lines
    // clean.
    if (form.cbzPreserveOriginals === false) {
      args.cbzPreserveOriginals = false;
    }
    // Webtoon WebP recompression (Phase 1, 2026-05-11). Emit the master
    // toggle so downloader.js's boolMap picks it up; pass quality/method
    // through too so buildCliArgs can decide whether to forward them
    // (it only does when the master toggle is on AND the value differs
    // from the Python-side default, to keep the spawn line clean).
    if (form.webtoonRecompress) {
      args.webtoonRecompress = true;
      args.webtoonRecompressQuality = form.webtoonRecompressQuality;
      args.webtoonRecompressMethod = form.webtoonRecompressMethod;
    }
    // Komikku output (2026-05-12, Komikku LocalSource format). One boolean — Python
    // does all the coercion. downloader.js boolMap emits --komikku when
    // this is true.
    if (form.komikku) {
      args.komikku = true;
    }
    if (form.splitMode === "size" && form.splitValue) args.split = form.splitValue;
    if (form.splitMode === "chapters" && form.splitValue) args.split = `${form.splitValue}ch`;
    if (form.group.trim()) args.group = form.group.trim();
    if (form.mixByUpvote) args.mixByUpvote = true;
    if (form.cookies.trim()) args.cookies = form.cookies.trim();
    if (form.jobs > 1) args.jobs = form.jobs;
    if (form.imageWorkers !== 3) args.imageWorkers = form.imageWorkers;
    if (form.httpTimeout !== 30) args.httpTimeout = form.httpTimeout;
    if (form.httpMaxRetries !== 6) args.httpMaxRetries = form.httpMaxRetries;
    if (form.httpBackoffBase !== 1.0) args.httpBackoffBase = form.httpBackoffBase;
    if (form.httpBackoffCap !== 45) args.httpBackoffCap = form.httpBackoffCap;
    if (form.netMinGap !== 0.25) args.netMinGap = form.netMinGap;

    // ── Multi-URL handling ──
    // Single URL: start one download, no --jobs needed
    // Multiple URLs: pass them all to one aio-dl.py process with --jobs N
    //   The Python script handles parallelism natively (spawns workers, retries, etc.)
    if (urls.length === 1) {
      onStartDownload(urls[0], args);
    } else {
      // Automatically set --jobs to the number of URLs
      // (user can still override via the Parallel Workers section)
      args.jobs = form.jobs > 1 ? form.jobs : urls.length;
      onStartDownload(urls, args);
    }

    onDraftConsumed?.();
    setFormState((prev) => ({ ...prev, urls: "" }));
  };

  return (
    // This outer flex container fills the tab content area.
    // flex-col makes the scrollable area and the sticky button stack vertically.
    <div className="flex flex-col h-full">

      {/* ── Scrollable form area ──
          flex-1: takes all available space
          min-h-0: allows flexbox to shrink this below its content height
          overflow-y-auto: adds a scrollbar when content overflows
      */}
      <div className="flex-1 min-h-0 overflow-y-auto px-5 py-4 space-y-1">

        {/* URL Input */}
        <div>
          <Label htmlFor="urls" className="text-sm font-semibold">
            Manga URL(s)
          </Label>
          <p className="text-xs text-muted-foreground mt-0.5 mb-2">
            Paste one or more MangaFire URLs, one per line
          </p>
          <Textarea
            id="urls"
            value={form.urls}
            onChange={(e) => set("urls", e.target.value)}
            placeholder="https://mangafire.to/manga/title.id"
            className="font-mono text-sm min-h-[70px]"
          />
        </div>

        {/* Output Format */}
        <SectionHeader>Output Format</SectionHeader>
        <div className="grid grid-cols-4 gap-2">
          {FORMATS.map((f) => (
            <button
              key={f.value}
              onClick={() => {
                // Honor the "Images only" label promise on the None button:
                // aio-dl.py treats --format none as "skip the final book
                // build" and produces nothing unless --keep-images or
                // --keep-chapters is also set. Auto-enable keepImages here
                // so the label is truthful out of the box. update form in one
                // pass so the next render sees both fields consistent
                // (avoids a brief frame where format=none but keepImages
                // is still false, which would flash the warning below).
                updateForm((prev) => ({
                  ...prev,
                  format: f.value,
                  ...(f.value === "none" ? { keepImages: true } : {}),
                }));
              }}
              className={cn(
                "flex flex-col items-center gap-1 rounded-lg border p-3 transition-all text-sm",
                form.format === f.value
                  ? "border-primary bg-primary/5 text-primary ring-1 ring-primary/30"
                  : "border-border hover:border-primary/40 hover:bg-accent/30"
              )}
            >
              <span className="font-semibold">{f.label}</span>
              <span className="text-[10px] text-muted-foreground">{f.desc}</span>
            </button>
          ))}
        </div>
        {/* Edge-case warning: only fires if the user explicitly unchecks
            both Keep chapters and Keep images while format=none. The
            format-button onClick above auto-enables keepImages, so the
            normal "select None" path never trips this. Without the
            warning, an unchecked-then-launched run would silently produce
            an empty manga folder (aio-dl.py:6196 `pass`es with no log). */}
        {form.format === "none" && !form.keepImages && !form.keepChapters && (
          <p className="text-[10px] text-yellow-500 dark:text-yellow-400 mt-2 leading-snug animate-slide-up">
            Format = None with neither "Keep chapters" nor "Keep images"
            checked produces nothing in the manga folder (only metadata).
            Re-enable one of those toggles to keep raw images or
            per-chapter files.
          </p>
        )}

        {/* Komikku-mode per-job toggle (2026-05-12, Komikku LocalSource format).
            Pre-filled from settings.defaults.komikku via the useEffect at
            line ~120-124 (which spreads settings.defaults into the form).
            Python-side --komikku auto-coerces format=cbz / keep-chapters /
            no-final-file, so the format buttons above are effectively
            ignored when this is on — that's intentional, and the helper
            text below tells the user. */}
        <div className={cn(
          "flex items-center justify-between gap-3 mt-2 rounded-md border p-3 transition-colors",
          form.komikku
            ? "border-primary/50 bg-primary/5"
            : "border-border bg-background"
        )}>
          <div className="flex-1">
            <Label className="text-xs cursor-pointer">
              Komikku-compatible output
            </Label>
            <p className="text-[10px] text-muted-foreground mt-0.5 leading-snug">
              {form.komikku ? (
                <>
                  Overrides format → <span className="font-mono">CBZ</span>. Writes
                  per-chapter CBZs with their own{" "}
                  <span className="font-mono">ComicInfo.xml</span>, plus{" "}
                  <span className="font-mono">cover.jpg</span> +{" "}
                  <span className="font-mono">details.json</span> at the
                  series-folder root. Output stays at{" "}
                  <span className="font-mono">manga/&lt;Series&gt;/</span>.
                </>
              ) : (
                <>
                  Enable for Mihon/Tachiyomi/Komikku readers. Each chapter
                  becomes its own CBZ with proper per-chapter metadata, plus
                  series-level cover.jpg + details.json. Forces format=CBZ.
                </>
              )}
            </p>
          </div>
          <Switch
            checked={!!form.komikku}
            onCheckedChange={(v) => set("komikku", v)}
          />
        </div>

        {/* EPUB layout — only shown when EPUB is selected */}
        {form.format === "epub" && (
          <div className="flex items-center gap-4 pt-2 animate-slide-up">
            <Label className="text-xs text-muted-foreground">EPUB Layout:</Label>
            <div className="flex gap-2">
              {["vertical", "page"].map((layout) => (
                <button
                  key={layout}
                  onClick={() => set("epubLayout", layout)}
                  className={cn(
                    "px-3 py-1 rounded-md text-xs font-medium border transition-colors",
                    form.epubLayout === layout
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border hover:bg-accent"
                  )}
                >
                  {layout.charAt(0).toUpperCase() + layout.slice(1)}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Quality & Processing */}
        <SectionHeader>Quality &amp; Processing</SectionHeader>
        <div className="grid grid-cols-2 gap-x-6 gap-y-3">
          <div>
            <div className="flex items-center justify-between mb-1">
              <Label className="text-xs">Quality</Label>
              <Badge variant="secondary">{form.quality}</Badge>
            </div>
            <Slider value={form.quality} onValueChange={(v) => set("quality", v)} min={1} max={100} />
          </div>
          <div>
            <div className="flex items-center justify-between mb-1">
              <Label className="text-xs">Scaling</Label>
              <Badge variant="secondary">{form.scaling}%</Badge>
            </div>
            <Slider value={form.scaling} onValueChange={(v) => set("scaling", v)} min={1} max={100} />
          </div>
          <div>
            <Label htmlFor="width" className="text-xs">Width (px)</Label>
            <Input id="width" type="number" value={form.width} onChange={(e) => set("width", e.target.value)} placeholder="auto" className="mt-1" />
          </div>
          <div>
            <Label htmlFor="aspect" className="text-xs">Aspect Ratio</Label>
            <Input id="aspect" value={form.aspectRatio} onChange={(e) => set("aspectRatio", e.target.value)} placeholder="e.g. 4:3 or leave empty" className="mt-1" />
          </div>
        </div>

        {/* Chapters */}
        <SectionHeader>Chapters</SectionHeader>
        <div className="grid grid-cols-2 gap-x-6 gap-y-3">
          <div>
            <Label htmlFor="chapters" className="text-xs">Chapter Range</Label>
            <Input
              id="chapters"
              value={form.chapters}
              onChange={(e) => set("chapters", e.target.value)}
              onFocus={(e) => {
                if (e.target.value === "all") e.target.select();
              }}
              placeholder="all, or 1-50, 75, 80-100"
              className="mt-1"
            />
          </div>
          <div>
            <Label htmlFor="language" className="text-xs">Language</Label>
            <Select id="language" value={form.language} onChange={(e) => set("language", e.target.value)} className="mt-1">
              {LANGUAGES.map((l) => (
                <option key={l.value} value={l.value}>{l.label}</option>
              ))}
            </Select>
          </div>
        </div>
        <div className="flex items-center gap-2 pt-2">
          <Checkbox id="noPartials" checked={form.noPartials} onCheckedChange={(v) => set("noPartials", v)} />
          <Label htmlFor="noPartials" className="text-xs cursor-pointer">Skip partial chapters (1.5, 60.1, etc.)</Label>
        </div>

        {/* Output Options */}
        <SectionHeader>Output Options</SectionHeader>
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <Checkbox id="keepChapters" checked={form.keepChapters} onCheckedChange={(v) => { set("keepChapters", v); if (!v) set("noFinalFile", false); }} />
            <Label htmlFor="keepChapters" className="text-xs cursor-pointer">Keep individual chapter files</Label>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox id="noFinalFile" checked={form.noFinalFile} onCheckedChange={(v) => set("noFinalFile", v)} disabled={!form.keepChapters} />
            <Label htmlFor="noFinalFile" className={cn("text-xs cursor-pointer", !form.keepChapters && "opacity-40")}>
              Skip combined final file (chapters only)
            </Label>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox id="keepImages" checked={form.keepImages} onCheckedChange={(v) => set("keepImages", v)} />
            <Label htmlFor="keepImages" className="text-xs cursor-pointer">Keep original unprocessed images</Label>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox id="noProcessing" checked={form.noProcessing} onCheckedChange={(v) => set("noProcessing", v)} />
            <Label htmlFor="noProcessing" className="text-xs cursor-pointer">Skip all image processing (use raw images)</Label>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox id="noCleanup" checked={form.noCleanup} onCheckedChange={(v) => set("noCleanup", v)} />
            <Label htmlFor="noCleanup" className="text-xs cursor-pointer">Don't clean up temp folder after completion</Label>
          </div>
        </div>

        {/* LINE Webtoon WebP recompression (Phase 1, 2026-05-11).
            Master toggle + quality/method sliders. Only fires when the
            active handler is `linewebtoon` (Python-side gating; the flag
            is a no-op for other sites, so leaving it on as a global
            default is safe for mixed-site libraries). Target case:
            webtoons.com archival-quality PNGs (~2-3 MB/page) recompress
            to WebP q85 at ~80-130 KB/page — ~90% disk reduction on
            verified Eleceed Ch 57 PNGs. Settings tab carries the global
            default; this block is the per-job override surface, mirroring
            how Multi-source fallback exposes both per-job and global. */}
        <SectionHeader>LINE Webtoon recompression</SectionHeader>
        <div className="space-y-3">
          <div className="flex items-start gap-3">
            <Switch
              id="webtoonRecompress"
              checked={form.webtoonRecompress}
              onCheckedChange={(v) => set("webtoonRecompress", v)}
              className="mt-0.5"
            />
            <div className="flex-1">
              <Label htmlFor="webtoonRecompress" className="text-xs cursor-pointer">
                Recompress lossless PNG pages to WebP (webtoons.com only)
              </Label>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                Re-encodes lossless PNG pages to lossy WebP before packaging.
                JPEG-served chapters (early/low-popularity series) are skipped
                automatically — they're already small and re-encoding would be
                generation-loss for tiny gains. Verified: ~90% smaller CBZ
                files at q85 with no visible quality loss on phone-screen
                viewing of color webtoons. Requires CBZ or EPUB output.
              </p>
            </div>
          </div>
          {form.webtoonRecompress && (
            <div className="pl-12 animate-slide-up grid grid-cols-2 gap-x-6 gap-y-3">
              <div>
                <div className="flex items-center justify-between mb-1">
                  <Label className="text-xs">Quality</Label>
                  <Badge variant="secondary" className="font-mono tabular-nums">
                    {form.webtoonRecompressQuality}
                  </Badge>
                </div>
                <Slider
                  value={form.webtoonRecompressQuality}
                  onValueChange={(v) => set("webtoonRecompressQuality", v)}
                  min={1}
                  max={100}
                />
                <p className="text-[10px] text-muted-foreground mt-1 leading-snug">
                  85 = storage-optimized (default). 90 = archival-safe
                  (~60% larger). 95+ = wasted bytes on color webtoons.
                </p>
              </div>
              <div>
                <div className="flex items-center justify-between mb-1">
                  <Label className="text-xs">Encoder effort</Label>
                  <Badge variant="secondary" className="font-mono tabular-nums">
                    {form.webtoonRecompressMethod}
                  </Badge>
                </div>
                <Slider
                  value={form.webtoonRecompressMethod}
                  onValueChange={(v) => set("webtoonRecompressMethod", v)}
                  min={0}
                  max={6}
                />
                <p className="text-[10px] text-muted-foreground mt-1 leading-snug">
                  0 = fastest, 6 = smallest. Default 4. method=6 trades
                  ~2-3× encode time for ~5% smaller files.
                </p>
              </div>
            </div>
          )}
        </div>

        {/* Multi-source fallback (cross-site per-chapter retry).
            Hidden behind a default-off Switch since it adds 30-60s of search
            overhead at job start (the alt-discovery phase) for the long-tail
            benefit of per-chapter source fallback when the primary CDN
            poisons. The same toggle is exposed in the Search tab; this
            duplicate lives here for direct-URL pastes that bypass Search. */}
        <SectionHeader>Multi-source fallback</SectionHeader>
        <div className="space-y-3">
          <div className="flex items-start gap-3">
            <Switch
              id="multiSource"
              checked={form.multiSource}
              onCheckedChange={(v) => set("multiSource", v)}
              className="mt-0.5"
            />
            <div className="flex-1">
              <Label htmlFor="multiSource" className="text-xs cursor-pointer">
                Use alternate sources for chapters the primary fails to fetch
              </Label>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                Adds ~30-60s of cross-site discovery before downloading.
                When the primary CDN throttles or 404s a page, the chapter
                falls over to the next source automatically.
              </p>
            </div>
          </div>
          {form.multiSource && (
            <div className="pl-12 animate-slide-up">
              <div className="flex items-center justify-between mb-1">
                <Label className="text-xs">Alternative quality floor</Label>
                <Badge variant="secondary" className="font-mono tabular-nums">
                  {form.multiSourceQualityMin.toFixed(2)}
                </Badge>
              </div>
              <Slider
                value={form.multiSourceQualityMin}
                onValueChange={(v) => set("multiSourceQualityMin", v)}
                min={0.3}
                max={0.95}
                step={0.05}
              />
              <p className="text-[10px] text-muted-foreground mt-1">
                Sources below this seed/measured quality won't be used as
                fallbacks. Default 0.65 keeps unknown-language Madara extras out.
              </p>
            </div>
          )}
        </div>

        {/* Splitting */}
        <SectionHeader>Splitting</SectionHeader>
        <div className="flex items-center gap-3">
          <Select value={form.splitMode} onChange={(e) => { set("splitMode", e.target.value); set("splitValue", ""); }} className="w-40">
            <option value="none">No splitting</option>
            <option value="size">By file size</option>
            <option value="chapters">By chapter count</option>
          </Select>
          {form.splitMode !== "none" && (
            <Input
              value={form.splitValue}
              onChange={(e) => set("splitValue", e.target.value)}
              placeholder={form.splitMode === "size" ? "e.g. 400MB" : "e.g. 10"}
              className="w-32 animate-slide-in"
            />
          )}
        </div>

        {/* Advanced */}
        <SectionHeader>Advanced</SectionHeader>
        <div className="space-y-2">
          <Collapsible title="Scanlation Groups">
            <div className="space-y-2">
              <Label htmlFor="group" className="text-xs">Preferred groups (comma-separated, priority order)</Label>
              <Input id="group" value={form.group} onChange={(e) => set("group", e.target.value)} placeholder='e.g. "Official, GroupA"' />
              <div className="flex items-center gap-2">
                <Checkbox id="mixByUpvote" checked={form.mixByUpvote} onCheckedChange={(v) => set("mixByUpvote", v)} disabled={!form.group.trim()} />
                <Label htmlFor="mixByUpvote" className={cn("text-xs cursor-pointer", !form.group.trim() && "opacity-40")}>
                  Pick by most upvotes instead of priority order
                </Label>
              </div>
            </div>
          </Collapsible>

          <Collapsible title="Network Tuning">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label className="text-xs">HTTP Timeout (sec)</Label>
                <Input type="number" value={form.httpTimeout} onChange={(e) => set("httpTimeout", Number(e.target.value))} className="mt-1" />
              </div>
              <div>
                <Label className="text-xs">Max Retries</Label>
                <Input type="number" value={form.httpMaxRetries} onChange={(e) => set("httpMaxRetries", Number(e.target.value))} className="mt-1" />
              </div>
              <div>
                <Label className="text-xs">Backoff Base (sec)</Label>
                <Input type="number" step="0.1" value={form.httpBackoffBase} onChange={(e) => set("httpBackoffBase", Number(e.target.value))} className="mt-1" />
              </div>
              <div>
                <Label className="text-xs">Backoff Cap (sec)</Label>
                <Input type="number" value={form.httpBackoffCap} onChange={(e) => set("httpBackoffCap", Number(e.target.value))} className="mt-1" />
              </div>
              <div>
                <Label className="text-xs">Net Min Gap (sec)</Label>
                <Input type="number" step="0.05" value={form.netMinGap} onChange={(e) => set("netMinGap", Number(e.target.value))} className="mt-1" />
              </div>
            </div>
          </Collapsible>

          <Collapsible title="Parallel Workers">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label className="text-xs">Concurrent Jobs</Label>
                <Input type="number" min={1} max={8} value={form.jobs} onChange={(e) => set("jobs", Number(e.target.value))} className="mt-1" />
                <p className="text-[10px] text-muted-foreground mt-1">Parallel processes for multi-URL batches</p>
              </div>
              <div>
                <Label className="text-xs">Image Workers</Label>
                <Input type="number" min={1} max={10} value={form.imageWorkers} onChange={(e) => set("imageWorkers", Number(e.target.value))} className="mt-1" />
                <p className="text-[10px] text-muted-foreground mt-1">Threads per chapter for image downloads</p>
              </div>
            </div>
          </Collapsible>

          <Collapsible title="Missed Chapter Retries">
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <Checkbox id="noRetryMissed" checked={form.noRetryMissedChapters} onCheckedChange={(v) => set("noRetryMissedChapters", v)} />
                <Label htmlFor="noRetryMissed" className="text-xs cursor-pointer">Disable end-of-run retry</Label>
              </div>
              <div>
                <Label className="text-xs">Retry Attempts</Label>
                <Input type="number" min={0} max={10} value={form.missedRetries} onChange={(e) => set("missedRetries", Number(e.target.value))} disabled={form.noRetryMissedChapters} className="mt-1 w-24" />
              </div>
            </div>
          </Collapsible>

          <Collapsible title="Cookies">
            <div>
              <Label className="text-xs">Cookie String</Label>
              <Input value={form.cookies} onChange={(e) => set("cookies", e.target.value)} placeholder="key1=value1;key2=value2" className="mt-1 font-mono text-xs" />
            </div>
          </Collapsible>
        </div>

        {/* Bottom spacer so last collapsible isn't jammed against the button */}
        <div className="h-4" />
      </div>

      {/* ── Start Button (sticky at bottom, never scrolls) ── */}
      <div className="flex-shrink-0 p-4 border-t bg-background/80 backdrop-blur-sm">
        {(() => {
          // Count valid URLs for the button label
          const urlCount = form.urls.split("\n").map((u) => u.trim()).filter(Boolean).length;
          return (
            <Button
              onClick={handleStart}
              disabled={!form.urls.trim()}
              className="w-full h-11 text-sm font-semibold gap-2"
            >
              <Download className="w-4 h-4" />
              {urlCount > 1
                ? `Start ${urlCount} Downloads (--jobs ${form.jobs > 1 ? form.jobs : urlCount})`
                : "Start Download"}
            </Button>
          );
        })()}
      </div>
    </div>
  );
}
