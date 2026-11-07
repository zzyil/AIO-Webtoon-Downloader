# Desktop → Android parity: authoritative report

**What this file owns:** the complete inventory of what the Electron desktop app does that the
Android port does not — defects, gaps, and deliberate omissions alike — each anchored to a
`file:line`. Read it before claiming a feature "works on Android", and before porting anything
new: §4 lists the places the existing docs assert capabilities the code does not have.

**Who reads from it:** a cold session picking up the port. Its companions are `android/README.md`
(toolchain + dependency rationale) and `android/TESTING.md` (on-device recipes). **Where this
file and those disagree, this file was derived from source and they were not** — §4 is exactly
that disagreement, enumerated.

**Derived:** 2026-08-13, static analysis of the working tree at branch
`fix/comix-waf-session-and-list-integrity`. Paths are repo-relative. No device was in the loop;
§6 lists what that leaves unsettled.

Evidence grades used throughout:
**[EXEC]** = confirmed by executing code · **[READ]** = confirmed by reading source ·
**[DEV]** = rests on an on-device observation that could not be executed here.

> **Staleness rule.** Every row is a claim about code that changes. The `file:line` anchors are
> the point — re-check the anchor before trusting a row, and delete rows whose defect is fixed
> rather than leaving them to rot. §2 defects D1–D9 are the rows most worth re-testing after any
> change to `aio-dl.py`'s end-of-run block, `sites/madara.py`, or the image pipeline.

---

## 1. Verdict

**The default Android configuration is proven safe.** `build_argv` was executed on the stock
form and emits `['--format','cbz','--quality','100','--language','en','<URL>']` **[EXEC]**;
every term of `cbz_fast_path` (`aio-dl.py:11582-11590`) holds, so pages are archived as raw
wire bytes with no PIL decode (`aio-dl.py:11638-11639`) and the download half never decodes
either (`sites/_image_io.py:33-37`, magic-byte sniffing). A stock download of a fully
WebP-serving site archives byte-for-byte correctly.

The exceptions are four, and three of them are one tap away from that default. A cancelled
or delta run rebuilds the series archive **in place** over the complete one
(`aio-dl.py:12917`, `:12939`, `:4941`), and `.aio_series.json` then conceals the loss
(`:13041-13047`). The Cloudflare rescue that 244 of 303 handlers depend on is gated on a
desktop-only flag and never fires on Android (`sites/madara.py:757`), failing with an error
that names the wrong cause. `--webtoon-recompress` is offered in the UI with no WebP encoder
behind it. Five image controls route into a decode path Chaquopy's Pillow cannot service.

Everything else is absence, not breakage — and most of the absence is argued in source
comments. 91 distinct settings, 101 engine flags, 35 IPC channels, 303 handlers were
enumerated; the asymmetry is entirely one-directional (no Android control fails to reach
Python).

---

## 2. Severity-ranked defects

Ranked by likelihood × damage. "Android-only" vs "cross-platform" is stated for each.

### D1 — A partial run overwrites the complete series archive, and the metadata hides it

**CROSS-PLATFORM** (core hazard) **+ Android-only second door** (cooperative cancel).
Evidence grade: **[READ]** throughout — the path was traced line by line but never executed.

| Element | Evidence |
|---|---|
| Final-file gate has no `run_cancelled()` term | `aio-dl.py:12917` |
| Tail `run_cancelled()` calls are all *after* the build | `aio-dl.py:13234`, `:13237`, `:13262` |
| CBZ write truncates | `aio-dl.py:4941` (`ZipFile(out_path,"w")`); PDF `:5386` (`open(...,"wb")`) |
| Same `final_path` as the complete run | `aio-dl.py:12939`; folder reuse via `.series_hid` `:485-519`; `base_filename` `:9581-9583` |
| Floor is **one page**, not one chapter | `aio-dl.py:10472-10476` seeds `current_book_content` with the cover for CBZ → guard true with **zero** chapters; loop breaks at `ch_idx=0` (`:12540-12542`) |
| `chapters_downloaded` is a UNION with the prior file | `aio-dl.py:13041-13047`, stored `:13087` |
| Delta runs hit it on **both** platforms | `src/lib/downloadArgs.js:37-72` passes only the missing range as `chapters` and sets `noFinalFile` only from user defaults (`:54-55`); Android twin `core/DownloadForm.kt:198-214` |
| Android cancel returns normally through the tail | `DownloadService.kt:283-294` → `aio_android.py:623-630` → `aio-dl.py:1039-1046`; loop break `:12540-12542` |
| Desktop cancel is a process kill, so it never reaches `:12917` | `UI-source/electron/downloader.js:933-953` |

**Immune modes** (branch order at `aio-dl.py:12918-12922`): `--komikku` (coerces
`no_final_file=True`, `aio-dl.py:8799-8809`), `--format none`, any manual
`--keep-chapters --no-final-file`. Split mode writes a differently-named part file
(`:5429-5430`), not the main archive. **Android's default is `format=cbz`, `komikku=false`
(`core/DownloadForm.kt:47`, `:51`) — squarely in the exposed `else` branch.**

**User-visible symptom.** A 40-chapter `Series X.cbz` becomes a 3-chapter (or 1-cover) file.
The phase indicator reads "Building final file…" (`core/Models.kt:124`); the terminal
notification reads "Download cancelled / Finished chapters kept — resume to continue"
(`DownloadService.kt:211-217`); the Queue history pill shows `3/40 ch`
(`ui/screens/QueueScreen.kt:497-508`). Nothing states the archive was replaced — and
"Finished chapters kept" reads as reassurance about exactly what was destroyed. Because
`.aio_series.json` still lists 1–40, the library's update-check finds nothing missing, so
**nothing will ever offer to repair it**. No backup, no rename-aside, no versioning
(grep over `android/app/src/main/java/` for `overwrit|backup|final file` finds only the
cover cache `core/CoverStore.kt:183` and UI labels).

Why #1: the delta-run path is routine on both platforms, the Android cancel path is a
one-tap notification action, the damage is irreversible, and the concealment removes the
normal recovery route.

### D2 — Cloudflare rescue is dead for 244 of 303 handlers, and fails with the wrong error

**ANDROID-ONLY.** Grade: **[READ]** + **[EXEC]** for the registry figures.

`sites/madara.py:757` is `if ZENDRIVER_AVAILABLE and is_cf_challenge(...)`, inside
`except Exception: pass` (`:760-761`). `ZENDRIVER_AVAILABLE` is False on Android, so `and`
short-circuits and `is_cf_challenge` is **never evaluated** — the interstitial HTML is parsed
as the page. The working branch at `:749` (which does reach `get_cf_session` → the WebView)
is dead code, because **0 of 244 Madara handlers set `use_zendriver`** **[EXEC]**.
`sites/manhwaread.py:111` repeats the pattern on the image path.
No subclass overrides `MadaraSiteHandler._fetch_html` **[EXEC]** — `:757` is the single
chokepoint for all 244.

The seam works everywhere else: `crawlee_utils.py:312-317` diverts to the backend, and
`sync_cf_cookies` (`:453-462`) was fixed for this exact failure mode with a comment naming
Android. The sweep stopped two files short.

**Nothing else rescues it** — verified exhaustively: the three `custom_backend()` consumers
are `get_cf_session` (unreachable from Madara), `fetch_html_playwright` (MangaThemesia-only;
zero hits in `sites/madara.py`), and the mangafire signer. `ProfileBridge.goto`'s
auto-interstitial handler (`WebViewBridge.kt:205-226`) only runs inside the WebView, which a
cloudscraper `scraper.get()` never touches. Cookie sharing does not help: `cf_clearance` is
per-origin, and `sync_cf_cookies` is keyed on the fetched netloc (`crawlee_utils.py:463-467`).

**Two user-visible symptoms, neither naming Cloudflare:**
- *403 / 200 interstitial* → `_classify_response_failure` inspects only the first 250 chars
  (`aio-dl.py:1255-1310`), where a CF page has only doctype + `<title>Just a moment...</title>`
  → classed "permanent" → `make_request` logs "Got status 403 but response has content,
  continuing…" (`:718-720`) and returns the challenge page → title falls back to the URL slug
  (`madara.py:768-771`) → empty chapter list → **`sys.exit("No chapters selected.")`**
  (`aio-dl.py:10335-10336`). The user is told their chapter filter matched nothing.
- *503 interstitial* → classed "retryable" → 6-retry storm → **"Failed to fetch comic data:
  Retryable server error (503)"** (`aio-dl.py:9330-9333`). The user is told the site is down.

Whether a given Madara site works on Android is a property of the target's WAF configuration,
not of the code — which is why this is content-conditional rather than a flat 244 outage.

### D3 — `--webtoon-recompress` is offered on Android with no WebP encoder behind it

**ANDROID-ONLY, LINE-Webtoon-scoped.** Grade: **[EXEC]** for the Pillow mechanics
(simulated), **[DEV]** for the premise that Chaquopy's `Pillow==11.0.0` ships no `_webp.so`.

Pillow registers WebP *open* unconditionally but *save* only `if SUPPORTED`
(`PIL/WebPImagePlugin.py:312-315`), so `Image.save(format="WebP")` reaches
`SAVE[format.upper()]` and raises **`KeyError('WEBP')`** — reproduced **[EXEC]**; note it is
**not** an OSError. Both outcomes below are scoped by
`_webp_source_is_lossy = args.webtoon_recompress and handler.name == "linewebtoon"`
(`aio-dl.py:11764-11766`), so **neither can occur on a non-LINE-Webtoon series.**

**Outcome A — default CBZ path: silent no-op.** `KeyError` swallowed by the broad
`except Exception` at `aio-dl.py:3547`, original path returned `:3559`, notice emitted via
`log_verbose` at `:3551-3554`, which prints only under `--verbose`/`--debug`
(`:626-629`). Android **never emits either** — both keys sit in `_BOOL_FLAGS`
(`aio_android.py:967-968`) with **no Kotlin writer**, and resume deliberately omits
`--verbose` (`:1936-1939`). The download succeeds, the CBZ holds untouched originals, and
`[recompress] Done in 0.4s` prints (`aio-dl.py:11418-11421`). The switch's own help text
promises "around 90% smaller" (`ui/screens/DownloadScreen.kt:554-556`). **Unconditionally
invisible on device** — there is no user-accessible way to surface it.

**Outcome B — combined with `--quality < 100` or `--scaling < 100`: hard chapter failure.**
Both are first-class sliders one collapsible away (`DownloadScreen.kt:439-452`, `:453-459`).
Either falsifies `cbz_fast_path` → slow `save_final_images` → `_webp_source_is_lossy` is True
(it keys on *the flag having been passed*, so Outcome A's silent failure is invisible here) →
`_output_format = "webp_q85"` (`aio-dl.py:11777-11792`) → `_save_one` calls
`src_img.save(dst, format="WebP", …)` at `:3320` with **no try/except anywhere in
`:3287-3322`** → propagates out of `fut.result()` (`:3357`). The chapter fails.

`--webtoon-recompress-method` has no Android writer at all (`aio_android.py:1103-1104` reads
a key nothing sets), so encoder effort is pinned to the Python default — moot while the
codec is missing.

### D4 — Five image controls route into a decode path Chaquopy's Pillow cannot service

**ANDROID-ONLY.** Grade: **[EXEC]** for the Pillow behaviour and the default-safety proof.

Chaquopy's Pillow has no WebP **decode** either: `Image.open()` emits
`UserWarning: image file could not be identified because WEBP support not installed` then
raises `UnidentifiedImageError` (⊂ OSError) — reproduced verbatim **[EXEC]**, and the string
matches the device observation at `android/README.md:338`.

**Blast radius is 5 UI controls, not 7.** `--aspect-ratio` and `--no-cbz-preserve-originals`
have **zero Kotlin writers** (`aspectRatio` hits under `android/…/java/` are Compose layout
modifiers only), so they are unreachable from the UI. The five that are reachable —
`--format epub`, `--format pdf`, `--width` (`DownloadScreen.kt:463-466`), `--scaling < 100`
(`:453-459`), `--quality < 100` (`:439-451`) — are exactly the set
`android/README.md:351-353` already names.

**Two outcomes, and the worse one is louder than the census claimed:**
- *Mixed-format chapter* → each WebP page returns `None` (`aio-dl.py:3000-3002`), `_assemble`
  skips it (`:3015-3016`) → **silently short output**. The completeness gate cannot see it:
  `pages_total`/`pages_ok` are captured during **download** (`:11168-11169`) and the gate runs
  at `:11226`, while the decode happens at `:11571`+. The chapter logs N/N and ships short.
- *All-WebP chapter* (the common case on a WebP-native CDN) → every page drops,
  `chapter_content` is empty → **caught**: recorded as `empty_content` and surfaced in the
  missed-chapter report + end-of-run retry (`aio-dl.py:12775-12778`). Loud, never silent.

**Not fully silent even in the mixed case:** `aio-dl.py:3001` does a plain `print("  Warning:
Skipping corrupted image …")`, which under Chaquopy reaches logcat and the in-app Logs screen
(`core/LogTail.kt:11-33`). What is silent is the **accounting**, not the event.

### D5 — External "open with" is broken for any custom library root

**ANDROID-ONLY.** Grade: **[READ]**.

`res/xml/file_paths.xml:17-19` declares exactly one element,
`<external-files-path name="library" path="manga/"/>`, which maps to
`getExternalFilesDir(null)/manga` = `Aio.defaultLibraryDir` (`core/Aio.kt:49-50`). The file's
own comment at `:12-15` warns that a user-chosen root needs a `root-path` entry "or every
'open with' will throw IllegalArgumentException". **M7 shipped the user-chosen root**
(`core/AppSettings.kt:58`, `core/Aio.kt:77-97`, `ui/screens/SettingsScreen.kt:266`); the
`root-path` entry did not.

Symptom: tapping a book row (`ui/screens/SeriesDetailScreen.kt:592`) yields the caught
message **"Couldn't share that file (IllegalArgumentException)."** (`:877-879`), rendered as a
warning `HelpText` at `:606-610`. Not a crash. Affects only users who set a custom root —
which is precisely the `MANAGE_EXTERNAL_STORAGE` audience the storage flow courts.

### D6 — weebcentral can win a search and then hard-fail at download

**ANDROID-ONLY.** Grade: **[READ]**.

`sites/weebcentral.py:224-231` and `:280-287` fall back to `fetch_html_impit` on any
exception or 403/429/503, which raises `RuntimeError("impit not available")` → rewrapped as
`"WeebCentral chapter list fetch failed: …"`. impit is not installed. The site works while
cloudscraper succeeds (probed ~86% live per `android/README.md:344`) and hard-raises the
moment it does not.

`_UNAVAILABLE_SEARCH_SITES = ("comix",)` (`aio_android.py:1235-1238`) — and the docstring
immediately above it **already names weebcentral and kagane as known future entries**. So a
weebcentral hit stays search-eligible, can be picked, and then fails at download time.

### D7 — A pasted comix URL is not blocked in Python; only an advisory callout stands in the way

**ANDROID-ONLY.** Grade: **[READ]**.

comix is excluded in `build_search_argv` only (`aio_android.py:1289-1292`); `build_argv`
forwards just the user's `disabledSites` (`:1092-1094`). And `--disable-sites` explicitly
exempts direct URLs — "A directly-downloaded URL is never filtered"
(`aio-dl.py:7869-7872`). comix drives its own Patchright lifecycle and **never calls the
browser seam** (`sites/comix.py:2038-2046`, `:4876`; zero `custom_backend|browser_backend`
hits in that file **[EXEC]** grep). The only guard is a regex-matched Compose callout
(`ui/screens/DownloadScreen.kt:169`, `:247-262`) — advisory text, not a block.

### D8 — `diagnostics()` is blind to the one Pillow limitation that matters

**ANDROID-ONLY.** Grade: **[READ]**.

The capability probe tests module **importability** only (`aio_android.py:175-192`:
`importlib.import_module("PIL")` → True) and never codec registration. On device it reports
`capabilities.pillow: true` while `"WEBP" not in Image.SAVE`. The app's own "fastest answer to
*why did every site fail*" affordance (`ui/screens/SettingsScreen.kt:461-464`) cannot see D3
or D4. One call — `PIL.features.check("webp")` — would settle both the blind spot and the
**[DEV]** premise underneath them.

### D9 — The `imageWorkers` field displays a number the run will not use

**ANDROID-ONLY, cosmetic-but-misleading.** Grade: **[READ]**.

`apply_network_limit` (`aio_android.py:827-842`, esp. `:840-841`) unconditionally overwrites
`imageWorkers` for any non-unlimited preset, while `ui/screens/DownloadScreen.kt:584-589`
keeps rendering the user's typed value with no lock or disabled state. The desktop disables
the input, badges it, and shows the *effective* value (`SettingsTab.jsx:98-118`, `:2031-2034`;
helpers `src/lib/resourceLimits.js:56-93`). Same runtime behaviour, but the Android UI can lie
about what will run.

---

## 3. Feature parity tables

Classification is {PARITY, DELIBERATE, GAP, DEGRADED, N/A}. PARITY rows are summarised rather
than enumerated (they carry no action); every non-PARITY row is listed.

### 3.1 Settings — 93 table rows / **91 distinct settings**

Counts are **verify-a's corrected figures**; the census's five summary numbers were wrong in
four places and its FULL/ABSENT tally conflated "PARITY" with "present on Android".

| Class | Census | Authoritative |
|---|---|---|
| PARITY | 46 | **46** |
| DELIBERATE | 17 | **18** (+1 hybrid) |
| GAP | 20 | **20** |
| DEGRADED | 5 | **5** ᵃ |
| N/A-DESKTOP-ONLY | 5 | **4** |
| **Rows** | 93 | **93** (91 distinct ᵇ) |

Android status: **FULL 40 · ABSENT 53** (census claimed FULL 46 / ABSENT 47). Of the 53
ABSENT: 18 deliberate, 4 degraded-to-preset, 4 desktop-only, 10 wire-only-on-*both*
platforms, 17 genuine gaps ᶜ. **UI-MISSING 0 and WIRE-MISSING 0 are CONFIRMED** — all 8
`AppSettings` and all 28 `DownloadForm` fields render a control and reach Python, and two unit
tests pin the two deliberately Kotlin-only fields (`AppSettingsTest.kt:80-86`, `:88-91`).

> ᵃ verify-a proposed a sixth class, HAZARD, for `webtoonRecompress`. Folded into DEGRADED
> here (control present, cannot do what it says) and escalated to **Defect D3**;
> `webtoonRecompressQuality` is inert by consequence.
> ᵇ `noPartials` (§4+§5) and `seededOnly` (§3+§5) are each one setting counted twice.
> ᶜ The 20-GAP and 17-genuine-gap figures are different slicings in verify-a and it did not
> reconcile them; both are reported as given. Minor residual uncertainty.

**DELIBERATE (18 + 1 hybrid) — with the rationale the reader asked to see**

| Setting | Rationale | Evidence |
|---|---|---|
| `appAutoUpdate`, `appUpdateDelayDays` | "Paths & Python, App Updates, Modernize. The first two are Electron concerns" — an APK updates via store/sideload | `ui/screens/SettingsScreen.kt:97-98` |
| `useFileBasedChapterCheck` | "ONE DELIBERATE DIVERGENCE … This UNIONS them" — strictly better than either desktop mode | `aio_android.py:1591-1598`, union `:1638-1640` |
| `checkAllConcurrency` | "SERIAL BY CONSTRUCTION, not by choice" — all checks contend for one interpreter behind `_ENGINE_LOCK` | `core/LibraryRepository.kt:180-190`, loop `:201-222` |
| `disabledSites` (block list half) | The one site that cannot work (comix) is already force-excluded in Python | `ui/screens/SettingsScreen.kt:93-96`; `aio_android.py:1238` |
| `jobs` | "parallel runs inside one Chaquopy interpreter would corrupt aio-dl.py's process-wide backoff and prefetch globals" | `ui/AioViewModel.kt:63-67` |
| `modernize` ×8 (`modernize`, `…Reversible`, `…Format`, `…Quality`, `…Distance`, `…MinSaving`, `…Effort`, `…AvifSpeed`) | "neither has an Android wheel behind it (no pillow-jxl, no torch)" | `core/DownloadForm.kt:20-23`; `ui/screens/DownloadScreen.kt:75-77`; `ui/screens/SettingsScreen.kt:97-98`; `android/README.md:404` |
| `enableMlRating` | removed on desktop too (2026-10-21); aio-dl.py no longer accepts `--enable-ml-rating`, so a stale saved value is ignored | grep `enableMlRating` in `aio_android.py` |
| `mangafireImageConcurrency` | Pre-2026-05-13 back-compat alias; Android has no settings predating the rename | `aio_android.py:953-956` |
| `promptUrls` | "there is no stdin under Chaquopy" | `aio_android.py:979-980` |
| `verbose` on resume | "Resuming a 200-chapter series would otherwise bury the Logs screen" | `aio_android.py:1936-1939` |
| `searchParallelism` **(hybrid)** | *Baseline* 4-vs-6 is DELIBERATE ("a phone radio handles [one TLS connection per site] worse than an ethernet NIC"); the *control* is a GAP | `aio_android.py:1230-1232`, `:1240-1243`, honoured-if-supplied `:1258-1262` |

**GAP (20)** — `prefetchImageWorkers` · `noFastDownload` · `metadataSource` ·
`metadataTagMinRank` · `metadataRefresh` · `verboseAlways` ᵈ · `noCleanup` ·
`cbzPreserveOriginals` · `webtoonRecompressMethod` · `searchOpts.searchLanguage` ᵉ ·
`searchOpts.multiSource` · `searchOpts.multiSourceQualityMin` · `searchParallelism`
(control half) · `aspectRatio` · `split` · `mixByUpvote` · `httpBackoffBase` ·
`httpBackoffCap` · `netMinGap` · `multiSourcePrefetched`.

> ᵈ Reclassified from DELIBERATE: the only cited rationale (`aio_android.py:1936-1939`)
> covers the **resume** path; nothing justifies the download path's omission. The census
> itself wrote "Doc/code gap" in the Notes and then labelled it DELIBERATE.
> ᵉ Key-name divergence as well: `build_search_argv` reads `language`
> (`aio_android.py:1252-1254`), the desktop key is `searchLanguage`, and
> `AppSettings.putInto` emits neither (`core/AppSettings.kt:100-107`). Dead on both counts.

**Notable GAPs by consequence:** `split` (a 4 GB single file matters most on FAT32/exFAT SD
cards), `netMinGap`/`httpBackoff*` (throttling levers on a phone radio), the whole AniList
trio (see §3.4), and `multiSourcePrefetched` (moot while search-`multiSource` is unreachable).

**DEGRADED (5)** — `imageConcurrency`, `imagePrefetchDepth`, `imagePrefetchParallel`
(reachable only through the coarse 4-step Network preset, `aio_android.py:802-812`; "0 =
disable prefetch" is inexpressible) · `checkAllIncludeCompleted` (pinned to the desktop's
default: `core/LibraryModels.kt:142` `checkable = url.isNotBlank()`) · `webtoonRecompress`
(→ **D3**).

**N/A-DESKTOP-ONLY (4)** — `pythonCmd`, `scriptPath` (Chaquopy imports the interpreter
in-process; nothing to locate), `logUpdateInterval` (renderer IPC flush cadence),
`isPackaged`.

**Structural divergences that change what "parity" means**

1. **No global-defaults / per-job split.** Desktop has `SettingsTab.defaults.*`
   (`SettingsTab.jsx:294-403`) spread onto a per-job form (`DownloadTab.jsx:345-347`);
   Android has ONE object that is both (`core/DownloadForm.kt:219-252`). You cannot set a
   default that survives a per-job override — every edit sticks.
2. **No Save button / dirty-diff.** Desktop drafts and commits (`SettingsTab.jsx:469-494`,
   `:544-657`); Android writes through immediately. Both have Reset.
3. **Category taxonomy.** Desktop 7 categories / 17 sections (`SettingsTab.jsx:419-437`,
   `:2770-2788`); Android 5 (`ui/screens/SettingsScreen.kt:115-119`) with
   Output/Compression/Multi-source on the Download screen. Whole desktop sections with no
   Android counterpart: Paths & Python, Logging, App Updates, Modernize, AniList Enrichment,
   Search Sources, and 4 of the 5 Image-Prefetch knobs.
4. **Resume throttle parity is exact** — `resume_throttle_flags` (`aio_android.py:857-886`)
   is a faithful port of `resumeThrottleFlags` (`UI-source/electron/resource-limits.js:101-119`).

### 3.2 Providers — 303 handlers

Registry **[EXEC]**: 303 registered / 42 base; Madara **244** (237 generic + 7 dedicated),
MangaThemesia **28** (24 + 4), bespoke **31**; `_OPTIONAL_HANDLER_ERRORS == {}`.
`use_zendriver=True` count is **1** (`kingofshojo`), `use_playwright=True` is **1**
(`violetscans`), `SUPPORTS_FAST_DOWNLOAD` is **2** (`mangafire`, `linewebtoon`).
Search-capable: 298.

**Availability partition (sums to 303)**

| Tier | N | Members | Android behaviour | Class |
|---|---|---|---|---|
| A. Works | **55** | 27 MangaThemesia + 28 bespoke ᶠ | Plain HTTP, or a browser path that reaches the seam | PARITY |
| B. Works **iff the target is not actively CF-challenged** | **244** | every Madara handler | On a clear site, fine. On a challenged one → **D2** | DEGRADED |
| C. Works on the happy path, hard-raises on the fallback | **1** | `weebcentral` | → **D6** | DEGRADED |
| D. Broken — loud, accurate error | **1** | `kingofshojo` | `RuntimeError("zendriver is not installed.")` (`crawlee_utils.py:567-570`) → `"Failed to fetch comic data: zendriver is not installed."` (`aio-dl.py:9333`) | GAP |
| E. Broken for downloads, metadata OK | **1** | `kagane` | `pywidevine` absent (`sites/kagane.py:18-26`, `:97-98`); not search-capable on either platform | DELIBERATE |
| F. Broken — no seam at all | **1** | `comix` | Own Patchright lifecycle; excluded from search, **not** blocked for a pasted URL → **D7** | DELIBERATE |

> ᶠ Corrected arithmetic. census-b's "297 pure-HTTP" is not derivable (it silently reuses the
> wrong `SettingsScreen.kt:94` figure), and verify-b's tier-A "56" double-counts
> `weebcentral` inside its bespoke subtotal (31 − comix − kagane − **weebcentral** = 28, not
> 29). Family totals 244 + 28 + 31 = 303 are live-verified by both agents.

**Non-availability buckets (not mutually exclusive)**

| Bucket | N | Notes |
|---|---|---|
| CF rescue silently dead | **244 handlers / 2 call sites** | `sites/madara.py:757` (all 244, shared `_fetch_html`, 0 subclass overrides **[EXEC]**) + `sites/manhwaread.py:111` (images). census-b's "245" double-counts `manhwaread`, which *is* a `MadaraSiteHandler` |
| Browser through the seam — works | 2 | `violetscans` (`sites/playwright_utils.py:29-41`), `mangafire` vrf (`sites/mangafire_vrf.py:356-365`) |
| Fast-download degraded (speed only) | 2 | `SUPPORTS_FAST_DOWNLOAD = _CURL_CFFI_AVAILABLE` → False (`sites/mangafire.py:127`, `sites/linewebtoon.py:193`); auto fallback `aio-dl.py:6687-6689` |
| WebP-probe degraded | ≥1 | `atsumaru` (`sites/atsumaru.py:17`) and any WebP CDN — rating falls back to cover/seed and **the UI says so**: `Icons.Filled.Warning`, `contentDescription = "Rating not measured from chapter pages"` (`ui/screens/SearchScreen.kt:71`, `:399-408`). The one degradation surfaced honestly |
| CF recovers via the WebView (ungated) | 2 | `likemanga` (`sites/likemanga.py:73-77`, `:156-161`, `:221-223`), `kappabeast` (`sites/kappabeast.py:102-107`) |

**Per-site-feature rows**

| Feature | Android | Class | Evidence |
|---|---|---|---|
| tapas locked-chapter multi-source rescue | WORKS + dedicated Compose callout | PARITY | `sites/tapas.py:447`, `:514`; `ui/screens/DownloadScreen.kt:189-228` |
| tapas BGM / LINE Webtoon motion + AudioCloud aux (`_aio/` in CBZ) | WORKS — plain HTTP + stdlib `zipfile`, no PIL, no curl_cffi, no browser | PARITY | `aio-dl.py:4525-4570`, `:4573-4605`, `:11511-11558`; `sites/linewebtoon.py:700-753` |
| `--no-sidecar-assets` opt-out | **MISSING** | GAP | Not in `_BOOL_FLAGS` (`aio_android.py:958-977`); desktop `aio-dl.py:8490`. Matters more than cosmetic: `_fetch_binary_asset_bytes` is **watchdog-EXEMPT** by design (`aio-dl.py:4541-4549`), so a slow audio CDN extends per-chapter wall time on a metered radio with no way to decline |
| mangafire vrf signing | WORKS through the seam | PARITY | `sites/mangafire_vrf.py:356-365`, `:419-438`; profile pin `aio_android.py:136-138` |
| AniList enrichment (module capability) | Module works (only `requests` + vendored pure-Python rapidfuzz) — **but unreachable**, see §3.4 | GAP | `sites/external_metadata.py:41-51`; `android/app/build.gradle.kts:217` |
| comix `/@waf/` CAPTCHA handoff | BROKEN (moot) | DELIBERATE | `sites/comix.py:2581`, `:5028` |
| comix sign-in (`--comix-login`) | MISSING (zero Kotlin matches) | DELIBERATE | `aio-dl.py:8528`; `UI-source/electron/main.js:916-943` |
| Disabled-sites manager ("Search Sources") | MISSING (UI only — Python reads the key on both paths) | DELIBERATE (list) + GAP (perf lever) | `ui/screens/SettingsScreen.kt:93-96`; `aio_android.py:1092-1094`, `:1283-1292` |

### 3.3 App features — 35 IPC channels + screens

35 `ipcMain.handle` registrations; **all 35 census line citations verified exact**.

| Class | N | Channels |
|---|---|---|
| PARITY | 10 | `get-settings`, `save-settings` ᵍ, `resume-download`, `delete-temp`, `scan-resumable`, `metadata:read`, `delete-series` (Android strictly safer — containment vs bare `fs.rmSync`), `check-for-updates`, `cancel-check-all-updates`, `get-theme` |
| PARTIAL | 7 | `start-download` (serial), `cancel-download` (cooperative → **D1**), `search:run` (comix excluded, no ML, no live stream), `metadata:update` (no cover), `scan-library` (never downloads the remote cover URL), `open-file` (→ **D5**), `check-all-updates` (serial; no completed-filter) |
| GAP | 7 ʰ | `get-history`, `queue:get`, `queue:save`, `open-folder`, `app-update:get-status`, `app-update:check-now`, `app-update:apply-now` |
| DELIBERATE | 5 | `search:cancel`, `pick-folder`, `pick-file`, `comix:login`, `save-series-meta` |
| N/A-PLATFORM | 6 | `retry-setup`, `get-resolved-paths`, `quit:confirm`, `quit:cancel`, `quit-app`, `reinstall-python` |

> ᵍ Promoted from PARTIAL: the settings round-trip is at parity; the only missing half is
> `appUpdater.applySettings` (`main.js:686-693`), already counted as three `app-update:*` GAP
> rows. Counting it a fourth time inflates PARTIAL.
> ʰ verify-c's tally of 6 omitted `open-folder`, which census-c did label GAP. It also argues
> `get-history` should split into a **cosmetic** GAP and a derived **functional** one (the
> desktop back-fills a resumable's `url`/`title` from history, `main.js:819-827`; Android has
> only `run_meta.json`, `aio_android.py:1910-1914`, so a pre-`run_meta` tmp folder shows as
> non-resumable, `ui/screens/QueueScreen.kt:251-259`). Counted as 7 with that annotation.

**DELIBERATE rationales:** `search:cancel` — "WHY THERE IS NO CANCEL": `--search` has no
cancellation path, so `clear()` drops the result rather than showing a Cancel that doesn't
(`core/SearchRepository.kt:30-35`). `pick-folder`/`pick-file` — SAF returns `content://` URIs
Python's `open()` cannot take (`AndroidManifest.xml:28-31`, `ui/screens/SettingsScreen.kt:127-138`,
`core/Aio.kt:36-45`, `ui/screens/SeriesDetailScreen.kt:663-667`). `comix:login` — comix cannot
run on Android at all. `save-series-meta` — declined inline because a re-download restores
every other field too (`ui/screens/SeriesDetailScreen.kt:465-475`).

**Screen-level non-PARITY**

| Feature | Android | Class |
|---|---|---|
| DetailView folder reveal + per-chapter folder | Path printed as plain text; `Chapter_N/` rows `onClick = null` with the reason inline (`ui/screens/SeriesDetailScreen.kt:227-243`, `:600-603`) | GAP |
| Metadata **cover picker** | `write_book_metadata` passes `None` always (`aio_android.py:2148`) | DELIBERATE (`content://`) |
| Manual-URL entry for an orphan series | NONE | DELIBERATE |
| UpdatesCenter slide-out | No consolidated panel / no section grouping. **Per-series state and a per-series queue action DO exist**, distributed: `+N` badge on cards (`ui/screens/LibraryScreen.kt:832-838`, fed `:241`/`:728`/`:752`) and a per-series "Download N" (`ui/screens/SeriesDetailScreen.kt:521-550`) | PARTIAL |
| SearchTab chapter-map / slow-sites callout / pre-queue options dialog | NONE (`SearchChapterMap` `SearchTab.jsx:1272`, `SlowSitesCallout` `:821`, `SearchDownloadOptionsDialog` `:971`) | GAP |
| Prefetched multi-source alternatives | `multiSourcePrefetched` declared (`aio_android.py:936`), **no writer anywhere** | GAP |
| QueueTab "start alongside" (`startQueuedNow`) | NONE — serial by constraint (`UI-source/src/hooks/useDownloader.js:946`) | DELIBERATE (constraint) |
| Log text search / copy-to-clipboard | NONE — level chips + Clear only (`ui/screens/LogsScreen.kt:215-252`); no clipboard call anywhere under `android/app/src/main/java/` | GAP |
| Log capture lifetime | Only while the Logs screen is composed (`ui/screens/LogsScreen.kt:82-85`), following logcat from now (`core/LogTail.kt:92`, `-T 1`), bounded 1500 lines (`:45`) | GAP |
| `logUpdateInterval` | `FLUSH_MS = 220L` hardcoded (`core/LogTail.kt:52`); no `AppSettings` field | GAP |
| History persistence | In-memory, 20 entries, "deliberately NOT persisted across process death" (`core/DownloadRepository.kt:33-34`, `:41`, `:189`) vs desktop's 200 on disk (`UI-source/electron/history.js:166-168`) | GAP (cosmetic + derived) |
| Queue persistence | `_queue` in-memory (`core/DownloadRepository.kt:43-44`); `START_NOT_STICKY` in all branches (`DownloadService.kt:67`, `:86`, `:94`, rationale `:91-93`). **Started** jobs survive via `tmp_<hid>/` + resume; **never-started** jobs are lost | GAP (functional, bounded) + DELIBERATE (START_NOT_STICKY) |
| App self-update | No updater source file exists under `android/app/src/main/java/com/aio/downloader/`; M8 (Packaging/CI) unshipped | GAP ("not yet", not "decided against") |
| Cover `Referer` injection for `*.pstatic.net` / `*.webtoons.com` | NONE — zero "Referer" occurrences under `android/app/src/main/java/` (desktop `main.js:1625-1631`) | GAP |
| Multi-URL paste | Same gesture, one job per URL, serial (`core/DownloadForm.kt:108`, `ui/AioViewModel.kt:69-93`) | DELIBERATE |
| `--prompt-urls` | NONE | DELIBERATE (no stdin) |
| First-run setup wizard, ConfirmQuitDialog, Python repair | N/A — Chaquopy embeds the interpreter (`core/Aio.kt:110`) | N/A |
| ResumeBar format override | **PARITY** — the desktop has a per-item format dropdown (`ResumeBar.jsx:37-39`, `:220-236`, `:82-88` → `main.js:774-799` → `downloader.js:654`, `:681`) | PARITY ⁱ |

> ⁱ Conflict resolved by direct read this session: verify-a listed the resume-format control
> as Android-only, citing `downloader.js:682`/`:699` — but those lines are
> `--restore-parameters` and `--verbose`, not `--format`. census-c was right.

**Explicitly-checked non-gaps** (the desktop has none of these either, so Android is not
behind): drag-and-drop, tray icon / minimize-to-tray, global keyboard shortcuts, custom
application menu, window-state persistence, zoom control, log export. Theme is OS-driven on
both. `TapasPremiumCallout` and `ComixBrowserCallout` **do** exist on Android
(`ui/screens/DownloadScreen.kt:194-209`, `:253-259`), so they are not screen gaps.
`--serve` / `--update-all` / `--scan-library` / `--refresh-library-metadata` are reachable
from **neither** app: a repo-wide grep over `UI-source/electron/*.js` and `UI-source/src/**`
returns zero hits. That is PARITY, not an Android gap.

### 3.4 Engine flags — 101 options

Arithmetic **[EXEC]** (AST parse of `main()` at `aio-dl.py:7597`): 102 `p.add_argument` =
1 positional (`comic_url` `:7599`) + 101 options; 2 `argparse.SUPPRESS`ed
(`--mangafire-image-concurrency` `:7755`, `--no-collapse-splits` `:7982`) → **99 visible**.

**Two different numbers, and the distinction is the point:**

| Quantity | Value | Meaning |
|---|---|---|
| Literal presence in `aio_android.py`'s flag tables | **74** | What the Python arg-builder *could* emit |
| **Truly reachable** (a Kotlin writer or a dedicated builder exists) | **43** | What the shipped app can *actually* emit |
| Unreachable | **58** | Of which 27 have no table entry at all |

census-d reported 74/27 by grepping for the flag literal in `aio_android.py`. verify-d
re-derived reachability **[EXEC]** by executing `build_argv` / `build_search_argv` /
`build_resume_argv` against the union of every key any Kotlin file writes via `put("…")`,
counting resource-limit presets as live (they inject inside `build_argv` itself,
`aio_android.py:840-841`, `:1028-1031`). **31 flags sit in the tables with no emitting code
path.** Separately, census-d's header split of its own 27 ("12 DELIBERATE / 15 GAP") is not
reproducible from its rows, which yield 7 DELIBERATE / 7 N/A / 13 GAP.

**The 31 dead wires** — `--aspect-ratio` · `--coord-dir` · `--http-backoff-base` ·
`--http-backoff-cap` · `--job-hard-timeout` · `--job-retries` · `--job-spawn-gap` ·
`--job-stall-timeout` · `--jobs` · `--metadata-refresh` · `--metadata-source` ·
`--metadata-tag-min-rank` · `--missed-log` · `--mix-by-upvote` · `--modernize` ·
`--modernize-avif-speed` · `--modernize-distance` · `--modernize-effort` ·
`--modernize-format` · `--modernize-min-saving` · `--modernize-quality` ·
`--multi-source-prefetched` · `--net-min-gap` · `--no-cbz-preserve-originals` ·
`--no-cleanup` · `--no-fast-download` · `--prefetch-image-workers` · `--split` ·
`--webtoon-recompress-method` · `-v` · `-d`.

**GAP — reachable on desktop, not on Android**

| Flag(s) | Consequence | Evidence |
|---|---|---|
| `--metadata-source` / `--metadata-tag-min-rank` / `--metadata-refresh` | **AniList enrichment is OFF and cannot be turned on.** The flag defaults to `"none"` and `configure()` never sets `AIO_METADATA_SOURCE` — the only other route | `aio-dl.py:7907-7918` (`default=os.environ.get("AIO_METADATA_SOURCE","none")`); keys `aio_android.py:949`, `:950`, `:976`; `configure()` `:130-144` |
| `--refresh-library-metadata` / `--refresh-rewrite-cbz` | No in-place library repair path **of any kind** — combined with the row above, a poisoned or unenriched library cannot be fixed on device | `aio-dl.py:8122-8144` |
| `--no-group-fallback` | Cannot skip chapters missing the preferred group | `aio-dl.py:8061-8065` |
| `--download-volumes` | Volume mode unreachable | `aio-dl.py:8107-8111` |
| `--build-final-file` | Cannot recombine existing chapter files standalone | `aio-dl.py:8287-8292` |
| `--chapter-deadline-seconds`, `--chapter-host-poison-threshold`, `--inline-chapter-retries`, `--inline-chapter-backoff` | Watchdog runs at defaults; **doubly** unreachable — their env twins are not set either | `aio-dl.py:8177`, `:8185`, `:8193`, `:8202`; `configure()` `aio_android.py:130-144` |
| `--epub-dir` | Falls back to `out_dir` | `aio-dl.py:8218-8223`, fallback `:5462` |
| `--split`, `--mix-by-upvote`, `--no-cleanup`, `--missed-log`, `--http-backoff-*`, `--net-min-gap`, `--prefetch-image-workers`, `--no-sidecar-assets` | See §3.1 / §3.2 | — |
| `--save-params` | GAP but **moot** — its only consumer `--update-all` is itself blocked | `aio-dl.py:8293-8297` |
| `--group` / `--exclude-group` | **DEGRADED, not absent**: `argv.extend([flag, str(value)])` emits one token, so the CLI's `action="extend"` repeat/multi-value forms are lost. A comma string still works (`main()` splits on `,`) — but group names can contain commas, which is the exact ambiguity the CLI comment warns about | `aio_android.py:1045`; `aio-dl.py:8035-8036`, `:8042-8051`, `:8085-8095` |

**DELIBERATE — with rationale**

| Flag | Rationale | Evidence |
|---|---|---|
| `--serve` (+ `--api-host`/`--api-port`) | fastapi/uvicorn not installed; `api.py` is **not even staged into the APK** | `android/app/build.gradle.kts:33`, `:224`; `aio-dl.py:8612-8618` |
| `--prompt-urls` | No stdin under Chaquopy | `aio_android.py:979-980` |
| `--enable-ml-rating` | removed from aio-dl.py (2026-10-21); not a parity gap any more | grep `enableMlRating` in `aio_android.py` |
| `--modernize` ×7 | No pillow-jxl wheel; Pillow pinned 11.0.0 so no native AVIF either | `build.gradle.kts:186`, `:225-226` |
| `--comix-headless` / `--comix-allow-gapped-chapters` / `--comix-login` | comix cannot run at all | `aio_android.py:1224-1229`, `:1238` |
| `--mangafire-image-concurrency` | Suppressed back-compat alias | `aio_android.py:953-956` |
| `--search-parallelism` baseline 4 vs 6 | Phone radio vs ethernet NIC | `aio_android.py:1230-1232`, `:1240-1243` |
| `-v` on resume | Would bury the Logs screen | `aio_android.py:1936-1939` |

**INERT (emitted or emittable, but does nothing)** — `--image-concurrency` (only read by
`fast_download_images`, which needs curl_cffi; exactly 2 use sites, both fast-path-gated:
`aio-dl.py:11046-11047`, `:11506`) · `--no-fast-download` (the path is already off) ·
`--jobs` + the 4 `--job-*` + `--coord-dir` + `--net-min-gap` (batch-supervisor and
`AIO_COORD_ENABLED`-gated, `aio-dl.py:9002-9008`, `:9103`).
**Important non-consequence:** the "Low data" preset is *not* compromised by
`--image-concurrency` being inert — the preset also emits `--image-workers 1`,
`--image-prefetch-depth 1`, `--image-prefetch-parallel 1`, `--max-cpu-percent 25`
(`aio_android.py:802-806` **[EXEC]**), and those are live on the ThreadPool path
(`aio-dl.py:6749`).

**Subprocess-bound modes — refused cleanly, not silently.** `aio-dl.py` re-invokes itself in
exactly two places, both guarded by `_self_spawn_unavailable()` (`:1059-1077`), whose
docstring names Chaquopy as the live case. `--update-all` → `sys.exit("--update-all cannot
run here: …")` (`:8626-8629`); the multi-URL supervisor → `sys.exit("Multiple URLs cannot be
run here: …")` (`:9108-9110`), unreachable anyway since `build_argv` appends exactly one URL
(`aio_android.py:1134-1136`). `_run_engine` maps the non-int `SystemExit.code` to **1**
(`aio_android.py:732-736`). Android's replacement for `--update-all` is `check_series_updates`
(`aio_android.py:1580-1669`) — per-series, not a bulk driver.

**Modernize: deliberate omission + latent-if-ever-wired, NOT a live defect.** census-d framed
it as a live hard-error GAP. It is **inert** — four settings-blob producers exist
(`core/DownloadForm.kt:125-179`, `:198-214`; `core/AppSettings.kt:100-107`; the literal `"{}"`
in `core/ResumeRepository.kt:139`) and none can emit the key; a whole-tree grep for
`modernize` returns **comments only**; there is no persistence vector
(`DownloadForm.persistJson:219-248`, `AppSettings.persistJson:121-130`). And it is
**fail-safe if reached**: every policy hard-errors at parse time — `auto`/`jxl`/`jxl+avif` →
`p.error("… needs the JXL encoder")` (`aio-dl.py:8934-8942`), `avif` → `p.error("… needs AVIF
write support")` (`:8943-8962`) → `SystemExit(2)` → exit 2 **before the first chapter**, and
even past that the per-page catch keeps originals (`:3931-3944`). The latent trap worth
recording: `modernize_blocked` (`aio_android.py:1058-1066`) mirrors only aio-dl.py's seven
format/transform checks and **never tests encoder availability** — so if a future blob ever
carries the key, the guard will pass it straight through to the parse error.

**One escape hatch that makes every "unreachable" claim UI-scoped:** the adb intent harness
passes an arbitrary argv fragment through — `MainActivity.kt:152` reads the `extra` string,
`:160` splits it on spaces, `DownloadService.kt:197-201` splices it in, bypassing `build_argv`
entirely. Via `adb am start … -e extra "--aspect-ratio 3:4"` all 101 flags are reachable,
including the two that hard-error. Developer surface, not a shipped control.

**Dependency table (Android)**

| Dep | Present | Gates | Failure mode |
|---|---|---|---|
| Pillow 11.0.0 **without `_webp.so`** | partial | everything decoding | → **D3**, **D4**. `build.gradle.kts:186` |
| pillow-jxl / AVIF | no | `--modernize` | parse-time `p.error` → exit 2 |
| curl_cffi | no | fast-download, `--image-concurrency` | auto-degrades to ThreadPool (`sites/base.py:46-51`, `:806-810`) |
| impit | no | weebcentral CF/zstd rescue | `RuntimeError` → **D6** |
| pyvips | no | `webp_q85` fast path | falls back to PIL, which also cannot encode WebP |
| torch/pyiqa | no | nothing (the ML rating that used them was removed 2026-10-21) | not needed |
| fastapi/uvicorn | no | `--serve` | `api.py` not staged |
| patchright/zendriver | **replaced** by `_WebViewBackend` (`aio_android.py:294-377`) | CF, vrf, playwright | works except comix and the two gated Madara call sites |
| pywidevine | no | kagane downloads | guarded raise |
| rapidfuzz | **yes** — vendored pure-Python wheel | search, AniList scoring | scores pinned to desktop by `sites/fuzzy_match.py` + `rapidfuzz_fingerprint` |
| numpy / lxml / cryptography / cloudscraper / requests / bs4 / pypdf | yes | — | — |

---

## 4. Doc-vs-code drift

| # | Claim | Reality | Location |
|---|---|---|---|
| 1 | `android/README.md:351-354`: the lossy transform paths "**None are wired into the app yet**; whoever wires them owes this a capability check" | **All five are wired and shipped as UI controls**, and no capability check exists. This is the strongest doc error found — it asserts the absence of a control that shipped, and it is why **D3**/**D4** were never caught. (The README's *list* of affected paths is correct, and correctly omits `--aspect-ratio`/`--no-cbz-preserve-originals`, which census-d wrongly added) | quality `ui/screens/DownloadScreen.kt:439-451`, scaling `:453-459`, width `:463-466`, format `:265-303`, recompress `:548-576` |
| 2 | `android/README.md:400`: impit is a "fast-download fast path; `sites/base.py` degrades" | impit is **not** a fast-download path — `sites/base.py` has zero impit references **[EXEC]** grep; its only two consumers are weebcentral's CF/zstd rescues, which **raise** | `sites/base.py`; `sites/weebcentral.py:224-231`, `:280-287` |
| 3 | `android/README.md:473-475` + `core/Aio.kt:127-128` + `WebViewBridge.kt:39-41`: "any Cloudflare-challenged Madara/MangaThemesia site … routes through `custom_backend()` and works" | False on both halves. **Madara**: true only for `use_zendriver=True` handlers, of which there are **0** → **D2**. **MangaThemesia**: 27 of 28 have *no CF handling at all* (bare `make_request` + parse, no detector) — that is desktop parity, but the doc claims a capability neither platform has | `sites/madara.py:746-762`; `sites/mangathemesia.py:277-280`, `:444-448`, `:553-556` |
| 4 | `ui/screens/SettingsScreen.kt:94`: **"297 handlers"** | Wrong twice. (a) The registry is **303** **[EXEC]**; 297 = 298 search-capable − comix, hardcoded in a KDoc, computed by nothing at runtime. (b) It describes the desktop as "a scrolling table over 297 handlers with per-site health" — `renderSearchSources` renders **no such table**: it is disabled-site chips, this-session slow/down rows (in-memory, empty after restart), and a free-text add. The Android omission is argued against a desktop UI that does not exist. The runtime-truthful surface is `DiagnosticsSheet.kt:121-130` | `ui/screens/SettingsScreen.kt:93-96`; `UI-source/src/components/SettingsTab.jsx:2549-2620` |
| 5 | `res/xml/file_paths.xml:12-15` predicts that a user-chosen root without a `root-path` entry breaks every "open with" | The prediction came true — M7 shipped the root, the entry did not → **D5** | `res/xml/file_paths.xml:17-19`; `core/AppSettings.kt:58` |
| 6 | `android/app/build.gradle.kts:222`: "pyvips — WebP save fast path; **falls back to PIL**" | Stale — the PIL fallback also cannot encode WebP on this build | `aio-dl.py:3307`, `:602-617` |
| 7 | `aio_android.py:1058-1066` `modernize_blocked` described as mirroring aio-dl.py's hard errors | Mirrors only the six/seven format-transform checks (`aio-dl.py:8889-8928`), never the two encoder-availability checks (`:8933-8962`) — the only two that always fire on Android | — |
| 8 | `aio_android.py:1235-1238` docstring names weebcentral and kagane as known-unavailable | Neither is in the tuple; `_UNAVAILABLE_SEARCH_SITES = ("comix",)` → **D6** | — |
| 9 | `aio_android.py:376-380`: the in-app WebView solve is "the one place Android beats desktop" | Overstated — the desktop also opens a headed browser for a **human** to solve comix's `/@waf/` challenge (`sites/comix.py:2581`, `:5028`, invoked `:2537`). The real Android advantage is **scope and location** (in-app, across many handlers), not existence | — |
| 10 | `core/DownloadRepository.kt:34` implies resume (M7) owns the killed-process story | True for a *started* job (a `tmp_` folder exists); false for a *queued* one, which leaves nothing on disk | `core/DownloadRepository.kt:43-44` |
| 11 | Root `CLAUDE.md` has no Android section | Imprecise: there is no dedicated section, but Android **is** described — the fuzzy-match invariant covers the Chaquopy/rapidfuzz split at length and the pointer table names `android/wheels/README.md`. Still: nothing tells a cold session that a second UI with a different settings surface exists | root `CLAUDE.md` |
| 12 | `aio_android.py:967` carries a `verbose` wire | No Kotlin writer, and unlike the resume path, no comment says why. The census's own note said "Doc/code gap" | `core/DownloadForm.kt:125-179`; `core/AppSettings.kt:100-107` |
| 13 | Desktop-side: `useDownloader.js:731-733` reads `s.defaults.excludeGroup` | `DEFAULT_SETTINGS` never defines it and no control writes it — a desktop dead injection path, not an Android gap | `UI-source/src/hooks/useDownloader.js:731-733` |

---

## 5. Android-only advantages (parity in the other direction)

15 entries, after striking two false credits and adding one the census missed.

1. **Foreground service** + determinate progress notification with a Cancel action, wake +
   wifi locks released in `finally` with a 6h cap (`DownloadService.kt:298-361`, `:107-134`).
   Zero `Notification`/`Tray` usage anywhere in `UI-source/electron/`.
2. **Structured `_emit` progress events** instead of the desktop's 14 stdout regexes
   (`UI-source/electron/downloader.js:381-497`); ETA computed at the emit site in Python on a
   monotonic clock (`aio_android.py:485-501`, `:549-620`).
3. **`diagnostics()` + the Diagnostics sheet** — handler counts, optional-import errors,
   9-library capability probe, `rapidfuzz_backend` + a cross-platform fingerprint
   (`aio_android.py:152-254`). *Caveat: blind to codec registration → **D8***.
4. **Resume-folder size reporting**; Discard names what it reclaims
   (`aio_android.py:1820-1861`, `ui/screens/QueueScreen.kt:296`). Desktop counts chapter
   markers only (`downloader.js:1054-1067`).
5. **`probe_library_root`** — proves writability by creating and deleting a file, reports free
   bytes + existing series count (`aio_android.py:2173-2226`). No `freeSpace`/`statfs` anywhere
   in `UI-source/electron/`.
6. **Containment guards on every destructive path** (`aio_android.py:1737-1774`, `:1980-2015`,
   `:2047-2070`) vs the desktop's bare `fs.rmSync` (`main.js:1044-1053`).
7. **Single-call batch metadata write** holding `_ENGINE_LOCK` once (`aio_android.py:2103-2154`)
   vs the desktop's per-file loop (`LibraryTab.jsx:665-668`).
8. **Update check UNIONS** the two on-disk modes rather than forcing an either/or
   (`aio_android.py:1591-1598`) — strictly fewer false "new chapter" reports.
9. **Conservative `--chapters` range collapse** that refuses to join `5` and `6` across a
   possible `5.5` (`aio_android.py:1673-1690`) — no equivalent in `src/lib/downloadArgs.js`.
10. **Clear cover cache** (`ui/screens/SettingsScreen.kt:466-477`). *Reset settings is **not**
    Android-only — the desktop has it at `SettingsTab.jsx:918`, `:2884`.*
11. **Library-tab "updates found" badge** (`ui/AioApp.kt:145`, `:392-396`); the desktop badges
    only Queue and Logs (`src/App.jsx:134-153`).
12. **`autoCheckUpdates`** — sweep the whole library on first open of the Library tab
    (`core/AppSettings.kt:84-85`). No desktop analogue exists (grep for
    `autoCheck|checkAllOnLaunch|autoUpdateCheck` across `UI-source/` returns nothing).
13. **Intent-driven test harness** (`--es url/chapters/format/extra`, `--ez cancel`,
    `--es browsertest`, `--es solvetest`) feeding the same queue as the Start button
    (`MainActivity.kt:101-172`, `:221-256`).
14. **In-app WebView bot-check solve** — reframed: the advantage is **scope and location**
    (in-app, reachable for Cloudflare across many handlers), not existence; the desktop has a
    comix-scoped headed-browser equivalent (`sites/comix.py:2537`, `:2581`, `:5028`).
15. **Constraint mitigations, not desktop deficits** (recorded, credited softly): engine-busy
    pre-flight that disables and explains buttons before the tap
    (`ui/screens/SearchScreen.kt:119-128`, `ui/screens/SeriesDetailScreen.kt:489-494`,
    `:810-817`) and the `MANAGE_EXTERNAL_STORAGE` flow with ON_RESUME re-check and fail-safe
    fallback (`ui/screens/SettingsScreen.kt:519-564`, `core/Aio.kt:60-97`). Both solve
    Android-only problems.

---

## 6. Unresolved / needs a device test

| # | Question | Why static analysis cannot settle it | Cheapest resolution |
|---|---|---|---|
| 1 | Does Chaquopy's `Pillow==11.0.0` wheel really ship without `_webp.so`? | **Everything in D3 and D4 rests on this premise.** The failure *mechanics* are certain **[EXEC]** on a simulated webp-less Pillow, but the premise is device-observed (`android/README.md:338`, `android/TESTING.md:478` report the exact Pillow-internal string, which is self-authenticating but still second-hand) | On device: `PIL.features.check("webp")` and `"WEBP" in PIL.Image.SAVE`. The same call closes **D8** |
| 2 | Does the D1 overwrite actually fire end to end? | The path was traced line by line **[READ]** and never executed. Every link is individually verified; the composition is not | Download a 5-chapter CBZ series, re-run it with `--chapters 1-2`, and check the archive's page count and `.aio_series.json` |
| 3 | Which of the three routes did M4's device verification actually exercise? | M4's success is real and does not contradict **D2**, but it can only have gone through (a) a hand-constructed `use_zendriver=True` handler, (b) `WebViewBridge.forceNextChallenge()` (`:122-135`, which forces the detector inside `goto` and therefore exercises only the playwright/vrf paths), or (c) `likemanga`/`kappabeast`, which are ungated and genuinely work. None is the generic-Madara path | Point a stock build at a known CF-challenged generic Madara site and read the failure text |
| 4 | Settings GAP arithmetic: 20 GAP rows vs "17 genuine gaps" in the ABSENT-53 breakdown | verify-a produced both figures from different slicings and did not reconcile them | Recount from the row labels; low stakes |
| 5 | Is the D4 mixed-vs-all-WebP split representative of real CDNs? | The all-WebP case is caught loudly (`aio-dl.py:12775-12778`); the silently-short case needs a chapter that mixes WebP with JPEG/PNG. How common that is, is an empirical property of each site | Run an EPUB build against `atsumaru` and compare page counts |

---

## 7. Methodology

**What was executed vs read.** Live-interpreter or executed-code results **[EXEC]**: the
handler registry and every family/flag split (303/42, 244/28/31, `use_zendriver`=1,
`use_playwright`=1, `SUPPORTS_FAST_DOWNLOAD`=2, 298 search-capable, `_OPTIONAL_HANDLER_ERRORS`
empty, 0 overrides of `MadaraSiteHandler._fetch_html`); the argparse inventory (AST parse:
102/1/101/2/99); `build_argv` / `build_search_argv` / `build_resume_argv` driven against the
real Kotlin key set, which produced the 43-reachable figure and the default-config safety
proof; the Pillow webp-less simulation (decode → `UserWarning` + `UnidentifiedImageError` ⊂
OSError; encode → `KeyError('WEBP')`, **not** an OSError; the magic-byte sniffer unaffected);
and a large set of negative greps. Everything else is **[READ]** — including all of D1, which
is the most consequential unexecuted claim in this report and is marked as such in §6.

**Doc handling.** `android/README.md`, `android/TESTING.md`, `CLAUDE.md` and the memory files
were treated as claims under test, never as evidence. That rule produced §4 and upgraded two
DELIBERATE rationales from doc-only to source-comment (`core/LibraryRepository.kt:180-190`,
`aio_android.py:1230-1232`) — material, because a doc-only rationale is exactly what an audit
rule demotes to "undocumented gap".

**Conflicts resolved**

| Conflict | Resolution | Why |
|---|---|---|
| AniList: census-d PARITY vs census-a/verify-d unreachable | **UNREACHABLE.** Spine + verify-d **[EXEC]** | census-d graded the feature by its **dependency** (rapidfuzz is vendored → "works") and never asked whether any code path emits `--metadata-source`. The dependency was necessary, not sufficient. See the lesson below |
| Flag arithmetic: 74 vs 43/58 | **Both, with the framing made explicit** (§3.4) | 74 = literal presence in a flag table; 43 = a Kotlin writer or builder exists. Different questions. Also noted: census-d's own 12/15 header split of its 27 is not reproducible from its rows (7/7/13) |
| WebP blast radius: 7 vs 5 controls | **5** | `--aspect-ratio` and `--no-cbz-preserve-originals` have zero Kotlin writers; the `aspectRatio` hits in the app are Compose layout modifiers |
| Modernize: live GAP vs inert | **Deliberate omission + latent-if-ever-wired, not a live defect** | verify-a proved zero writers, zero persistence vector, and a deterministic parse-time `p.error` → `SystemExit(2)` on every policy. The missing codec check in `modernize_blocked` is recorded as a latent trap only |
| Settings counts: 46/17/20/5/5 vs verify-a | **verify-a's** (46/18/20/5/4; FULL 40 / ABSENT 53; 91 distinct) | The census conflated PARITY with Android-FULL (10 wire-only-on-both-sides rows are PARITY but ABSENT), double-counted two settings, and mislabelled `verboseAlways` |
| Overwrite scope: Android-specific vs cross-platform | **Cross-platform core, komikku-immune, one-page floor; Android cancel is a second door** | Spine + verify-c. `downloadArgs.js:37-72` passes the delta as `chapters` on both platforms; `:12918` branch order makes `--komikku`/`--format none` immune |
| Android-only list: 15 | **13 clean + 1 half + 1 reframed + 1 added = 15 entries, differently composed** | Desktop *has* settings-reset (`SettingsTab.jsx:918`) and *has* an interactive WAF solve (`sites/comix.py:5028`); `autoCheckUpdates` was missing from the list |
| IPC PARTIAL: 8 vs 7 | **7** | `save-settings` is PARITY; its only missing half is already three `app-update:*` GAP rows. Separately, verify-c's GAP tally of 6 omitted `open-folder` → **7** |
| EPUB + WebP outcome | **Both branches reported** | All-WebP chapter → `empty_content`, caught loudly (`aio-dl.py:12775-12778`); mixed chapter → silently short. Softens the worst case materially |
| Resume format override: Android-only (verify-a) vs PARITY (census-c) | **PARITY** — settled by direct read this session | verify-a cited `downloader.js:682`/`:699`, which are `--restore-parameters` and `--verbose`. The desktop's per-item format dropdown is at `ResumeBar.jsx:37-39`, `:220-236`, threaded `main.js:774-799` → `downloader.js:654`, `:681` |
| Provider tier-A count: 56 (verify-b) | **55** | verify-b's bespoke subtotal (31 − comix − kagane = 29) left `weebcentral` inside tier A while also listing it as tier C. 31 − 3 = 28; 27 + 28 = 55; 55 + 244 + 4 = 303 ✓ |
| CF-dead bucket: 245 vs 244 | **244 handlers / 2 call sites** | `manhwaread` subclasses `MadaraSiteHandler`, so counting it separately double-counts |

**The census-d lesson, recorded as a caution.** census-d graded AniList enrichment PARITY
because its hard dependency (rapidfuzz) is vendored and its flags appear in
`_VALUED_FLAGS`/`_BOOL_FLAGS`. Both facts are true. The feature is nonetheless **off and
unreachable**, because no Kotlin caller sets `metadataSource` and `configure()` never sets
`AIO_METADATA_SOURCE` — so the flag always takes its `"none"` default. **Grading a feature by
its dependency, or by the presence of a wire, answers "could this work?" and not "does the
shipped app do it?"** The same error class produced 31 flags counted reachable that nothing
emits, and it is worth noting the failure is symmetric: `libraryPath` reaches Python with *no*
flag at all, via `configure()` (`core/Aio.kt:120-124`), so a flag-only lens under-counts as
well as over-counts. The only reliable test is the one verify-d ran: execute the arg builder
against the real key set, then check the env-var routes the flag table cannot see.
