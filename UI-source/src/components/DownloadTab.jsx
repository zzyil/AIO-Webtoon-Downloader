// ============================================================
// DOWNLOAD TAB
//
// The main form for starting new downloads. Every CLI argument
// from aio-dl.py has a corresponding UI control.
//
// FORM STATE IS SPARSE EDITS (lib/downloadForm.js header has the model). The
// tab keeps only the fields the user changed; what it shows is
// {...formDefaults(settings), ...edits}, so unedited fields always show the
// current Settings value. Start queues a "newTab" recipe carrying those edits
// (lib/queueRecipe.js), and the args are built when the download STARTS:
// a waiting New-tab item keeps exactly the fields the user changed here and
// follows Settings for the rest. The draft App.jsx holds across tab switches
// is the same edits object (plus the URL text).
//
// CHAPTERS | VOLUMES. A single URL on a site with native volumes (MangaFire;
// lib/siteCapabilities.js) shows a switch under the URL box. Volume mode is
// job data (downloadForm.js DEFAULT_FORM.downloadVolumes), is greyed out while
// Komikku is on (Python turns volumes off under --komikku), and disables the
// chapter-only controls the engine would override anyway (grep "[Volumes]
// Forcing" in aio-dl.py): keep/skip chapter files, multi-source, splitting,
// scanlation groups. The format choice is shared with chapters (user decision
// 2026-11-06).
//
// SCROLL FIX: The form area uses flex-1 + overflow-y-auto so
// it scrolls when content is taller than the window. The Start
// button stays pinned at the bottom with flex-shrink-0.
// ============================================================

import React, { useState } from "react";
import {
  Button, Input, Textarea, Label, Switch, Slider, Select,
  Checkbox, SectionHeader, Collapsible, Badge,
} from "@/components/ui/primitives";
import { Download, Lock, Sparkles, MonitorPlay, LogIn } from "lucide-react";
import { cn } from "@/lib/utils";
import { LANGUAGES } from "@/lib/constants";
import {
  formDefaults,
  applyEdits,
  recompressAllowed as isRecompressAllowed,
} from "@/lib/downloadForm";
import { useSiteCapabilities, volumesSupportedForUrl } from "@/lib/siteCapabilities";
import ItemKindSwitch from "./ItemKindSwitch";

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

// LANGUAGES moved to @/lib/constants (shared with SearchTab + SettingsTab).

// ── Tapas premium-episode → multi-source callout ──────────────────────────
// Tapas serves premium / wait-to-unlock episodes we can't fetch logged-out.
// The downloader now emits them as `_locked` placeholders so --multi-source
// fills each from the highest-rated alternative site (grep _locked in
// aio-dl.py + sites/tapas.py; _PERMANENT_SKIP_REASONS is the no-alt fallback).
// With multi-source OFF that whole feature is a SILENT no-op — the placeholders
// are dropped before download — so the moment a tapas.io URL is pasted we
// surface the situation and offer a one-click enable. Amber = "premium/gated";
// the panel flips to a calm green "handled" state once multi-source is on
// (color/icon/copy cross-fade via the duration-300 transitions).
const TAPAS_URL_RE = /\btapas\.io/i;
function hasTapasUrl(urls) {
  return typeof urls === "string" && TAPAS_URL_RE.test(urls);
}

function TapasPremiumCallout({ active, onEnable }) {
  return (
    <div
      className={cn(
        "relative mt-2 overflow-hidden rounded-lg border px-4 py-3",
        "animate-slide-up transition-colors duration-300",
        active
          ? "border-success/40 bg-gradient-to-br from-success/10 to-success/[0.02]"
          : "border-warning/40 bg-gradient-to-br from-warning/10 to-warning/[0.02]"
      )}
    >
      {/* Left accent bar — the spine that reads instantly as a status band. */}
      <div
        className={cn(
          "absolute inset-y-0 left-0 w-1 transition-colors duration-300",
          active ? "bg-success" : "bg-warning"
        )}
      />
      <div className="flex items-start gap-3 pl-1.5">
        {/* Icon chip — ringed for a touch of depth against the flat form. */}
        <div
          className={cn(
            "mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg",
            "ring-1 transition-colors duration-300",
            active
              ? "bg-success/15 text-success ring-success/30"
              : "bg-warning/15 text-warning ring-warning/30"
          )}
        >
          {active ? (
            <Sparkles className="h-4 w-4" />
          ) : (
            <Lock className="h-4 w-4" />
          )}
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <p className="text-xs font-semibold text-foreground">
              {active
                ? "Premium episodes will be filled in"
                : "Tapas locks premium episodes"}
            </p>
            <Badge
              variant="secondary"
              className="h-4 px-1.5 font-mono text-[9px] uppercase tracking-wider"
            >
              tapas.io
            </Badge>
          </div>
          <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
            {active ? (
              <>
                Paid / wait-to-unlock episodes will be pulled from the
                highest-rated alternative site, so your download stays gap-free.
              </>
            ) : (
              <>
                Paid / wait-to-unlock episodes can&rsquo;t be fetched directly
                and would leave gaps. Multi-source pulls each from the
                highest-rated alternative site instead.
              </>
            )}
          </p>
          {!active && (
            <div className="mt-2.5 flex items-center gap-2.5">
              <Button
                size="sm"
                onClick={onEnable}
                className="h-7 gap-1.5 px-2.5 text-[11px] shadow-sm transition-transform active:scale-[0.97]"
              >
                <Sparkles className="h-3 w-3" />
                Enable Multi-source
              </Button>
              <span className="text-[10px] text-muted-foreground">
                otherwise premium episodes are skipped
              </span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── comix.to browser callout ──────────────────────────────────────────────
// comix is the only site whose DOWNLOAD path drives a real browser: its chapter
// list, page URLs and search all sit behind a signed + encrypted API, so pages
// are captured out of the rendered reader (grep fetch_chapter_images_via_dom).
//
// Two consequences the user has to know about BEFORE pressing Start, because
// both look like bugs otherwise:
//   1. A visible Chromium window opens and scrolls itself. It runs headed on
//      purpose — comix's reader defers roughly every 10th page until it scrolls
//      into view, which needs a live rendering lifecycle; headless chapters come
//      out short (grep _COMIX_HEADLESS_ENV).
//   2. Occasionally the site asks for human verification, and the run pauses on
//      that window until it's completed.
// Multi-source is the safety net for any chapter that still comes up short, so
// this offers the same one-click enable as the tapas callout.
const COMIX_URL_RE = /\bcomix\.to/i;
function hasComixUrl(urls) {
  return typeof urls === "string" && COMIX_URL_RE.test(urls);
}

function ComixBrowserCallout({ active, onEnable, onLogin }) {
  return (
    <div
      className={cn(
        "relative mt-2 overflow-hidden rounded-lg border px-4 py-3",
        "animate-slide-up transition-colors duration-300",
        active
          ? "border-success/40 bg-gradient-to-br from-success/10 to-success/[0.02]"
          : "border-info/40 bg-gradient-to-br from-info/10 to-info/[0.02]"
      )}
    >
      {/* Left accent bar — same status-band idiom as the tapas callout. */}
      <div
        className={cn(
          "absolute inset-y-0 left-0 w-1 transition-colors duration-300",
          active ? "bg-success" : "bg-info"
        )}
      />
      <div className="flex items-start gap-3 pl-1.5">
        <div
          className={cn(
            "mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg",
            "ring-1 transition-colors duration-300",
            active
              ? "bg-success/15 text-success ring-success/30"
              : "bg-info/15 text-info ring-info/30"
          )}
        >
          <MonitorPlay className="h-4 w-4" />
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <p className="text-xs font-semibold text-foreground">
              A browser window will open
            </p>
            <Badge
              variant="secondary"
              className="h-4 px-1.5 font-mono text-[9px] uppercase tracking-wider"
            >
              comix.to
            </Badge>
          </div>
          <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
            comix pages are read out of its own reader, so a Chromium window
            opens and scrolls through each chapter. Leave it alone while it
            works &mdash; it closes itself. If the site asks you to verify
            you&rsquo;re human, the download waits for you to do it.
          </p>
          <div className="mt-2.5 flex flex-wrap items-center gap-2.5">
            {!active && (
              <Button
                size="sm"
                onClick={onEnable}
                className="h-7 gap-1.5 px-2.5 text-[11px] shadow-sm transition-transform active:scale-[0.97]"
              >
                <Sparkles className="h-3 w-3" />
                Enable Multi-source
              </Button>
            )}
            <Button
              size="sm"
              variant="outline"
              onClick={onLogin}
              className="h-7 gap-1.5 px-2.5 text-[11px] transition-transform active:scale-[0.97]"
            >
              <LogIn className="h-3 w-3" />
              Sign in to comix.to
            </Button>
            <span className="text-[10px] text-muted-foreground">
              {active
                ? "any chapter that comes up short is refilled from another site"
                : "multi-source refills any chapter that comes up short"}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function DownloadTab({
  onStartDownload,
  settings,
  draft,
  onDraftChange,
  onDraftConsumed,
}) {
  // Only the fields the user changed (see the file header). Seeded from App's
  // draft so a tab switch keeps unsent work.
  const [edits, setEdits] = useState(() => ({ ...(draft || {}) }));
  const baseline = formDefaults(settings);
  const form = { ...baseline, ...edits };

  // Write fields. A value equal to the current default drops the edit, so the
  // field follows later Settings changes again. Side-effect writes (None →
  // keep images, etc.) go through the same rule: they're visible on screen.
  const update = (patch) => {
    setEdits((prev) => {
      const next = applyEdits(prev, patch, baseline);
      onDraftChange?.(next);
      return next;
    });
  };

  // Helper to update one form field
  const set = (key, value) => update({ [key]: value });

  // UIR-3: clamp numeric inputs whose raw Number() read is forwarded straight
  // to the CLI (Network Tuning). Empty/NaN (e.g. the user cleared the field)
  // falls back to the field's current default instead of emitting 0/null;
  // `min` enforces a sane floor so e.g. --http-timeout can't go to 0.
  const setNum = (key, raw, min) => {
    const n = Number(raw);
    const value = raw === "" || Number.isNaN(n) ? baseline[key] : Math.max(min, n);
    update({ [key]: value });
  };

  // Format gates for the recompress / modernize toggles. DERIVED from the
  // format, never written into the form — see lib/downloadForm.js.
  const recompressAllowed = isRecompressAllowed(form);

  // Volume mode is offered for exactly one URL on a site with native volumes.
  // `volumeMode` is what the job will actually do; a leftover downloadVolumes
  // edit from an earlier URL is ignored (and stripped at Start) once the URL
  // no longer qualifies.
  const sites = useSiteCapabilities();
  const urlList = form.urls.split("\n").map((u) => u.trim()).filter(Boolean);
  const volumesOffered = urlList.length === 1 && volumesSupportedForUrl(urlList[0], sites);
  const volumeMode = volumesOffered && !!form.downloadVolumes && !form.komikku;

  // Queue a "newTab" recipe: the args are built when the download starts
  // (lib/queueRecipe.js), from these edits over the settings at that moment.
  const handleStart = () => {
    const urls = form.urls.split("\n").map((u) => u.trim()).filter(Boolean);
    if (urls.length === 0) return;

    // The URL text is the job, not a setting. downloadVolumes rides only when
    // the switch is actually in effect (see volumeMode).
    const { urls: _urls, downloadVolumes: _dv, ...formEdits } = edits;
    if (volumeMode) formEdits.downloadVolumes = true;
    const recipe = { kind: "newTab", input: { formEdits, urlCount: urls.length } };

    // Single URL: one download. Multiple URLs: one aio-dl.py process with
    // --jobs N (the builder sets it); Python handles the parallelism.
    onStartDownload(urls.length === 1 ? urls[0] : urls, { recipe });

    onDraftConsumed?.();
    setEdits({});
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
            placeholder="https://mangafire.to/title/id-slug"
            className="font-mono text-sm min-h-[70px]"
          />
          {/* Tapas-only: premium episodes need multi-source (grep hasTapasUrl /
              _locked). Slides in the instant a tapas.io URL is present. */}
          {hasTapasUrl(form.urls) && (
            <TapasPremiumCallout
              active={form.multiSource}
              onEnable={() =>
                // Mirror the Multi-source switch: enabling force-resets the
                // nested lazy toggle ON (opt-out inside the opt-in).
                update({ multiSource: true, multiSourceLazy: true })
              }
            />
          )}
          {/* comix-only: a visible browser window is part of how this site is
              downloaded at all (grep hasComixUrl / fetch_chapter_images_via_dom).
              Warn before Start so the window isn't mistaken for a bug. */}
          {hasComixUrl(form.urls) && (
            <ComixBrowserCallout
              active={form.multiSource}
              onEnable={() => update({ multiSource: true, multiSourceLazy: true })}
              onLogin={() => window.electronAPI?.comixLogin?.()}
            />
          )}
          {volumesOffered && (
            <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 animate-slide-up">
              <ItemKindSwitch
                volumes={volumeMode}
                disabled={!!form.komikku}
                onChange={(v) => set("downloadVolumes", v)}
              />
              <p className="text-[10px] leading-snug text-muted-foreground flex-1 min-w-[12rem]">
                {form.komikku ? (
                  <span className="text-yellow-500 dark:text-yellow-400">
                    Volumes are unavailable with Komikku output, which is per chapter.
                    Turn Komikku off to download volumes.
                  </span>
                ) : volumeMode ? (
                  <>
                    One file per volume (<span className="font-mono">Title Vol.01</span>) in
                    the series folder, next to any chapters. Chapter-only options
                    below are off.
                  </>
                ) : (
                  <>This site also offers whole volumes.</>
                )}
              </p>
            </div>
          )}
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
                // so the label is truthful out of the box. One update so the
                // next render sees both fields consistent (avoids a frame
                // where format=none but keepImages is still false, which would
                // flash the warning below).
                //
                // Recompress and modernize are NOT cleared here any more: an
                // incompatible format gates them off where they're read
                // (recompressAllowed / modernizeAllowed in lib/downloadForm.js).
                // Writing false would pin "off" as an edit, so going PDF→CBZ
                // would lose the Settings value.
                update({
                  format: f.value,
                  ...(f.value === "none" ? { keepImages: true } : {}),
                });
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
            Shows settings.defaults.komikku until the user flips it here
            (formDefaults in lib/downloadForm.js).
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

        {/* Chapters (volume numbers in volume mode: --chapters selects
            volumes then, grep "--chapters" help in aio-dl.py) */}
        <SectionHeader>{volumeMode ? "Volumes" : "Chapters"}</SectionHeader>
        <div className="grid grid-cols-2 gap-x-6 gap-y-3">
          <div>
            <Label htmlFor="chapters" className="text-xs">{volumeMode ? "Volume Range" : "Chapter Range"}</Label>
            <Input
              id="chapters"
              value={form.chapters}
              onChange={(e) => set("chapters", e.target.value)}
              onFocus={(e) => {
                if (e.target.value === "all") e.target.select();
              }}
              placeholder={volumeMode ? "all, or 1-5, 8" : "all, or 1-50, 75, 80-100"}
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
        {!volumeMode && (
          <div className="flex items-center gap-2 pt-2">
            <Checkbox id="noPartials" checked={form.noPartials} onCheckedChange={(v) => set("noPartials", v)} />
            <Label htmlFor="noPartials" className="text-xs cursor-pointer">Skip partial chapters (1.5, 60.1, etc.)</Label>
          </div>
        )}

        {/* Output Options */}
        <SectionHeader>Output Options</SectionHeader>
        <div className="space-y-2">
          {/* Volume runs always write one file per volume and never a combined
              file; the engine forces both, so the controls are frozen. */}
          <fieldset disabled={volumeMode} className={cn("space-y-2", volumeMode && "opacity-40")}>
          <div className="flex items-center gap-2">
            <Checkbox id="keepChapters" checked={form.keepChapters} onCheckedChange={(v) => update({ keepChapters: v, ...(!v ? { noFinalFile: false } : {}) })} />
            <Label htmlFor="keepChapters" className="text-xs cursor-pointer">Keep individual chapter files</Label>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox id="noFinalFile" checked={form.noFinalFile} onCheckedChange={(v) => set("noFinalFile", v)} disabled={!form.keepChapters} />
            <Label htmlFor="noFinalFile" className={cn("text-xs cursor-pointer", !form.keepChapters && "opacity-40")}>
              Skip combined final file (chapters only)
            </Label>
          </div>
          </fieldset>
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
              checked={form.webtoonRecompress && recompressAllowed}
              onCheckedChange={(v) => recompressAllowed && set("webtoonRecompress", v)}
              disabled={!recompressAllowed}
              className="mt-0.5"
            />
            <div className="flex-1">
              <Label htmlFor="webtoonRecompress" className={cn("text-xs cursor-pointer", !recompressAllowed && "opacity-40")}>
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
              {!recompressAllowed && (
                <p className="text-[10px] text-yellow-500 dark:text-yellow-400 mt-1 leading-snug animate-slide-up">
                  Unavailable with <span className="font-mono">{form.format.toUpperCase()}</span>{" "}
                  output — recompression needs a CBZ or EPUB archive to write
                  into. Switch to CBZ/EPUB (or enable Komikku) to use it.
                </p>
              )}
            </div>
          </div>
          {form.webtoonRecompress && recompressAllowed && (
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
        {/* Other sites list chapters, not this site's volume scans: nothing to
            fall back to in volume mode (item_kinds.py VOLUMES.multi_source). */}
        <fieldset disabled={volumeMode} className={cn("space-y-3", volumeMode && "opacity-40")}>
          <div className="flex items-start gap-3">
            <Switch
              id="multiSource"
              checked={form.multiSource}
              onCheckedChange={(v) =>
                // Enabling multi-source force-resets the nested lazy toggle
                // to ON (opt-out inside the opt-in), mirroring the Settings
                // master switch. One update so both fields land in the same
                // render.
                update({ multiSource: v, ...(v ? { multiSourceLazy: true } : {}) })
              }
              className="mt-0.5"
            />
            <div className="flex-1">
              <Label htmlFor="multiSource" className="text-xs cursor-pointer">
                Use alternate sources for chapters the primary fails to fetch
              </Label>
              <p className="text-[10px] text-muted-foreground mt-0.5">
                When the primary CDN throttles or 404s a page, the chapter
                falls over to the next source automatically.
              </p>
            </div>
          </div>
          {form.multiSource && (
            <div className="pl-12 animate-slide-up space-y-3">
              {/* Lazy discovery — opt-out nested in the opt-in. Mirrors the
                  Settings → Default Multi-source Fallback nested toggle;
                  this is the per-job override surface (doesn't save back). */}
              <div className="flex items-start gap-3">
                <Switch
                  id="multiSourceLazy"
                  checked={form.multiSourceLazy !== false}
                  onCheckedChange={(v) => set("multiSourceLazy", v)}
                  className="mt-0.5"
                />
                <div className="flex-1">
                  <Label htmlFor="multiSourceLazy" className="text-xs cursor-pointer">
                    Only search alternatives after a chapter fails (recommended)
                  </Label>
                  <p className="text-[10px] text-muted-foreground mt-0.5">
                    <strong>On:</strong> start downloading immediately; the ~30-80s
                    cross-site discovery only runs if a chapter actually needs a
                    fallback. <strong>Off:</strong> discover up front — slower
                    start, but split-collapse gets cross-source consensus and
                    ghost detection has alignment data from chapter one.
                  </p>
                </div>
              </div>
              <div>
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
            </div>
          )}
        </fieldset>

        {/* Splitting */}
        <SectionHeader>Splitting</SectionHeader>
        {/* --split is an error with --download-volumes (each volume is
            already its own file). */}
        <fieldset disabled={volumeMode} className={cn("flex items-center gap-3", volumeMode && "opacity-40")}>
          <Select value={form.splitMode} onChange={(e) => update({ splitMode: e.target.value, splitValue: "" })} className="w-40">
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
        </fieldset>

        {/* Advanced */}
        <SectionHeader>Advanced</SectionHeader>
        <div className="space-y-2">
          <Collapsible title="Scanlation Groups">
            {/* A volume is the site's own scan; the engine clears group,
                exclude-group and the MTL policy in volume mode. */}
            <fieldset disabled={volumeMode} className={cn("space-y-3", volumeMode && "opacity-40")}>
              <div className="space-y-1.5">
                <Label htmlFor="group" className="text-xs">Preferred groups (comma-separated, priority order)</Label>
                <Input id="group" value={form.group} onChange={(e) => set("group", e.target.value)} placeholder='e.g. "Official, GroupA"' />
                <p className="text-[11px] text-muted-foreground">
                  Naming a group here forces it, overriding every automatic signal
                  below. Use <code>Official</code> to match any licensed/publisher release.
                </p>
              </div>

              <div className="flex items-center gap-2">
                <Checkbox id="mixByUpvote" checked={form.mixByUpvote} onCheckedChange={(v) => set("mixByUpvote", v)} disabled={!form.group.trim()} />
                <Label htmlFor="mixByUpvote" className={cn("text-xs cursor-pointer", !form.group.trim() && "opacity-40")}>
                  Rank across all my groups instead of using priority order
                </Label>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="excludeGroup" className="text-xs">Groups to avoid (comma-separated)</Label>
                <Input id="excludeGroup" value={form.excludeGroup} onChange={(e) => set("excludeGroup", e.target.value)} placeholder='e.g. "SomeGroup"' />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="mtl" className="text-xs">Machine translations (MTL)</Label>
                <Select id="mtl" value={form.mtl} onChange={(e) => set("mtl", e.target.value)}>
                  <option value="avoid">Avoid — use only if it's the only version</option>
                  <option value="allow">Allow — treat like any other version</option>
                  <option value="exclude">Exclude — skip the chapter entirely</option>
                </Select>
                <p className="text-[11px] text-muted-foreground">
                  Detected from the group's name and self-description. "Avoid" never
                  costs you a chapter; "Exclude" leaves gaps where only MTL exists.
                </p>
              </div>
            </fieldset>
          </Collapsible>

          <Collapsible title="Network Tuning">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label className="text-xs">HTTP Timeout (sec)</Label>
                <Input type="number" min={1} value={form.httpTimeout} onChange={(e) => setNum("httpTimeout", e.target.value, 1)} className="mt-1" />
              </div>
              <div>
                <Label className="text-xs">Max Retries</Label>
                <Input type="number" min={0} value={form.httpMaxRetries} onChange={(e) => setNum("httpMaxRetries", e.target.value, 0)} className="mt-1" />
              </div>
              <div>
                <Label className="text-xs">Backoff Base (sec)</Label>
                <Input type="number" step="0.1" min={0.1} value={form.httpBackoffBase} onChange={(e) => setNum("httpBackoffBase", e.target.value, 0.1)} className="mt-1" />
              </div>
              <div>
                <Label className="text-xs">Backoff Cap (sec)</Label>
                <Input type="number" min={1} value={form.httpBackoffCap} onChange={(e) => setNum("httpBackoffCap", e.target.value, 1)} className="mt-1" />
              </div>
              <div>
                <Label className="text-xs">Net Min Gap (sec)</Label>
                <Input type="number" step="0.05" min={0} value={form.netMinGap} onChange={(e) => setNum("netMinGap", e.target.value, 0)} className="mt-1" />
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
                <Input type="number" min={1} max={10} value={form.imageWorkers} onChange={(e) => setNum("imageWorkers", e.target.value, 1)} className="mt-1" />
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
                <Input type="number" min={0} max={10} value={form.missedRetries} onChange={(e) => setNum("missedRetries", e.target.value, 0)} disabled={form.noRetryMissedChapters} className="mt-1 w-24" />
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
