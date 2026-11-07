# Android port

Runs the existing downloader — `aio-dl.py` and all of `sites/` — unmodified,
inside an Android app, with CPython embedded by [Chaquopy](https://chaquo.com/chaquopy/).

**The desktop app is not affected by anything in this directory.** Nothing under
`android/` is imported by the desktop code; the dependency arrow only points
inward. The Python-side seams that make this possible (`aio_android.py`,
`sites/browser_backend.py`) are additive and inert unless an embedder activates
them — see "Python-side seams" below.

## Current state: M7 (settings, resume, metadata)

Five screens — **Library**, **Search**, **New**, **Queue**, **Logs** — over the
foreground download service, plus a **Settings** destination reached from the top
bar. A download configured in the app runs to completion with the screen locked,
reports chapter progress and an ETA in its notification and on the Queue card,
and can be cancelled. What lands on disk then shows up in a cover grid that can
be searched, sorted, filtered by genre/tag/status/source, checked for new
chapters, and pruned. **Search** fans out across every supported site at once,
groups the hits by series, rates each site's copy, and hands the chosen URL to
the New screen.

A cancelled or killed download **keeps every chapter it finished** and comes back
as an *Unfinished* card at the top of the Queue tab, resumable in any output
format. The **library folder can be moved** to shared storage so Komikku or Mihon
can read it, and the **metadata embedded in each archive** — the fields those
readers display — can be edited from the series detail screen.

The app **opens on Library**, which is also where the desktop puts it — there the
library hangs off the app icon above the rail rather than in it, because a 64px
column already had five entries. Landing there also warms the interpreter behind
a skeleton grid, since the scan is the first Python call either way.

Search sits between Library and New, following the flow it belongs to: see what
you already have, find something you do not, then download it. It hands its
winner to the New screen rather than downloading directly, because format,
chapter range and the rest still have to be chosen and duplicating that form
would be two places to keep correct.

**MangaFire works, and so does any Cloudflare-challenged site.** `browser/`
implements `sites/browser_backend.py`'s protocol over Android's WebView, so the
two paths that needed a real browser now have one: MangaFire's `vrf=` signer
(which can only be run, never reimplemented — see the invariant in the repo
CLAUDE.md) and interactive challenge solving. The latter is the one place this
port beats the desktop: zendriver has to drive a headless browser at a check
designed to reject exactly that, while here the page is simply shown to the
person holding the device.

### Browser architecture

```
Python                          Kotlin
──────                          ──────
mangafire_vrf ─┐
playwright_utils─┼─▶ browser_backend.custom_backend(profile)
crawlee_utils ─┘         │
                         ▼
              aio_android._WebViewBackend ──JNI──▶ WebViewBridge.forProfile(name)
                                                        │
                                          ┌─────────────┴──────────────┐
                                     offscreen WebView          ChallengeActivity
                                     (goto/evaluate/…)          (visible, tappable)
                                          └──── shared CookieManager ───┘
```

**One WebView per profile, not one per process.** MangaFire bootstraps
`window.__aioMfSign` into a live page and reuses it for the whole run, so an
unrelated `fetch_html_playwright` navigation sharing that WebView would wipe it —
intermittently, depending on which sites a run touched. Cookies stay shared
regardless (`CookieManager` is process-global), which is what lets one solved
challenge count everywhere.

Two things the bridge must get right, both of which fail silently otherwise:

- **Promises.** `evaluateJavascript` does *not* await a returned promise, so an
  async signer comes back as `{}` and every token reads as null. `buildEvalScript`
  wraps the call and settles through a one-method JavaScript interface.
- **The User-Agent.** Read off `settings.userAgentString`, never
  `navigator.userAgent` — the moment anything overrides the UA the page reports
  the override back, so a wrong value looks self-consistent forever. Nothing here
  sets an override, so the reported string is the wire string, which is what the
  clearance is bound to.

`browser/BrowserJs.kt` holds the JS construction and the challenge rule with no
Android imports, so both are covered by plain JVM tests.

### Library architecture

The scan is **not** reimplemented. `library_state.scan_library` is pure stdlib,
already shared with the desktop, and already does the whole job — series
metadata, chapter numbers, cover discovery, the `next_update` range. Kotlin
parses it. That is why `electron/library.js` (923 lines) has no counterpart here.

`aio_android` adds only the four things that scan deliberately does not do:

| | |
|---|---|
| `library_cover` | pulls a cover out of a CBZ/EPUB. `library_state` can do this, but only by writing a `.cover.*` **into the series folder** — and that folder is the one Komikku/Mihon read, so on a phone it stays exactly as the download left it. This writes to the app cache instead |
| `series_files` | the per-series file listing, which would multiply the grid's payload by the chapter count for data only the detail screen wants |
| `check_series_updates` | the update diff — a behavioural port of `main.js:_checkSeriesUpdates` |
| `delete_series` | the only destructive call in the module, guarded on containment in the configured library root |

A cover resolves through four places, and only one is a plain file: `cover.jpg`
at the series root (what `--komikku` writes), the first page of a raw
`Chapter_N/` dir, a member inside the archive, or page 1 of a PDF. The first two
come free from the scan, the third is `library_cover`, and the fourth is
`CoverStore`'s `PdfRenderer` — unzipping is Python's strength, rasterising is
Android's. `CoverStore`'s memo is keyed on the folder's **mtime**, so a
re-download invalidates it with nothing to clear.

**`_ENGINE_LOCK` is the one new invariant.** An update check runs the engine —
`--list-chapters` is a full `aio-dl.py` run — and `aio-dl.py` keeps per-run state
in module globals, *including the cancellation flag, which every run clears on
entry*. Unserialized, a check started while a download was being cancelled would
un-cancel it. The download path waits for the lock; a library check does not, and
reports `engine_busy` instead, because someone tapping "check for updates" wants
an answer or a reason rather than a UI that hangs until a 40-chapter download
finishes. That is also why a whole-library sweep is **serial** where the desktop
runs a four-slot worker pool: there each check is its own OS process.

### Search architecture

`aio_android.search` runs `--search <query> --search-json` the same way
`list_chapters` runs `--list-chapters`: in-process, stdout captured, engine lock
try-acquired. So the whole orchestrator — fan-out, the 60s soft barrier, quality
probing, cross-site merging — is reused as-is, and the JSON contract is the one
`electron/searcher.js` already consumes.

Two departures from the desktop's arguments, both deliberate:

| | |
|---|---|
| **comix is force-excluded** | It is the one handler that never went through the browser seam (grep `_comix_worker_loop`), so it can only spend fan-out budget failing. Its `search()` is required to swallow errors to `[]`, so leaving it in would not break a run — just slow it. Excluding it also keeps it out of the quality probe, the expensive half |
| **Lower fan-out** | 4 by default against the desktop's 6. Fan-out opens one TLS connection per site and a phone radio handles that worse than an ethernet NIC. Resource Limits tightens it further (2/3/5), as a hard override |

`--enable-ml-rating` is **never** emitted: the ML rating was removed from the
desktop too (2026-10-21) and aio-dl.py no longer accepts the flag, so an old
saved `enableMlRating` setting is ignored. The search ranker is numpy + Pillow
only, the same on both platforms.

Results are grouped by SERIES, not by site, because the orchestrator has already
merged ~40 sites' hits into one candidate per series and ranked the sources
inside it; a flat list would throw that away and make the user dedupe by eye.
Each source shows a rating **only when it was measured**: `quality_basis`
"matched" or "partial" means its pages were compared with other sites' copies of
the same pages. Every other basis ("solo", "unmatched", "pending", "unreachable",
"seed") shows a reason label such as "not compared" instead of a number, because
a seed prior next to measured ratings reads as a measurement. A source of a
different edition (e.g. fan-colored) says "Other edition", and copies of the same
scan say "Same file as …" / "Same scan as …". The desktop follows the same rules
(`UI-source/src/lib/qualityDisplay.js`); the Kotlin side is `SearchModels.kt`
(grep `measuredRating`).

A search cannot be cancelled: `--search` has no cancellation path, and the
fan-out already stops waiting at its own barrier. The UI therefore offers no
Cancel button rather than one that does not cancel.

### Settings, storage, and resume

**Settings is a destination, not a sixth tab.** Five entries is already the
practical maximum for a bottom bar before the labels stop being readable, and
Settings is somewhere you visit rarely and leave — so it opens over the shell
from the top bar and takes the system Back gesture. Diagnostics moved *inside*
it: one grey glyph in a corner gets found, two get ignored.

**`AppSettings` vs `DownloadForm` is the split that matters.** The form is what
the user is composing for one series (format, chapter range, groups). `AppSettings`
is how the app behaves on this device, and three separate paths read it — a
download, a search, and a library update check. The resource limits moved there
in M7 for exactly that reason; before it, the Search screen reached into a
half-composed download form to find them.

`AppSettings` persists to **SharedPreferences, where the plan said DataStore** —
a deliberate deviation. Every consumer needs a *synchronous* read from a
non-composable context (`DownloadService`'s worker thread resolving an argv,
MainActivity's intent harness, `AioViewModel.queueLibraryUpdate`). DataStore's
only synchronous read is `runBlocking`, which on the main thread is the ANR it
exists to prevent; collecting a Flow instead leaves a window at process start
where a job would silently run with the settings ignored. DataStore's real
advantages — cross-process Flow observation, atomic multi-writer updates — buy
nothing for a few hundred bytes written by one screen.

**The library folder can be moved to shared storage**, which is the whole point
of the `MANAGE_EXTERNAL_STORAGE` opt-in: Komikku and Mihon cannot see inside an
app-scoped directory, so `--komikku` output lands where no reader can open it.
SAF is not the mechanism because it returns `content://` URIs and Python cannot
`open()` one — the same constraint that shaped the storage decision from the
start. Three rules:

- The verdict comes from **`aio_android.probe_library_root`, which decides by
  writing a file**. A granted permission is not a writable directory; under
  scoped storage the permission bits and the sandbox routinely disagree, and the
  "Use this folder" button only enables after a probe passes.
- **A change takes effect on the next app start.** `configure()` sets the process
  CWD and environment once, and aio-dl.py bakes constants in at import — so the
  screen says so rather than pretending otherwise.
- **It fails safe.** `Aio.resolveLibraryDir` falls back to the app's own folder
  when a configured path is not writable and records why, because a revoked grant
  would otherwise throw out of `Aio.module()` and take *every* screen down.

**Resume** is `--restore-parameters` over the `tmp_<hid>/` folders a cancelled
run leaves behind, surfaced as an *Unfinished* section at the top of the Queue
tab and counted in that tab's badge — it is the only work in the app that will
sit there forever if nobody looks. `--format` is the one setting a resume can
change (aio-dl.py deliberately omits it from `run_params.json` so it can be), so
the card offers a format picker; it is also **always emitted**, because argparse
defaults that flag to `epub` and an omitted one would silently convert a CBZ
library. The throttle flags come from the *current* settings rather than the
saved run.

**The metadata editor** writes the fields Komikku and Mihon actually display,
through the same `metadata_editor.py` the desktop uses. It defaults to the first
archive with an explicit "apply to all" opt-in (a komikku series is one archive
per chapter, so all-files is a 300-archive rewrite), and refuses while a download
holds the engine — that download is writing archives into the same library. There
is no cover picker, unlike the desktop: choosing an image needs a system picker
returning a `content://` URI that Python cannot open.

### UI architecture

```
MainActivity ──setContent──▶ AioTheme ──▶ AioApp ──▶ Library / Search / New / Queue / Logs
     │                                       │              │            └▶ Settings (overlay)
     │ enqueue                               │ reads        │ reads
     ▼                                       ▼              ▼
DownloadRepository ◀──writes──── DownloadService    LibraryRepository
   (StateFlow)                   (the ONLY poller) ──▶ aio_android (Python)
```

**One rule holds the whole thing together: `poll_events()` DRAINS the Python
event queue, so exactly one component may call it.** That is
`DownloadService`. It folds what it reads into `DownloadRepository`, and the UI
only ever *reads* those flows. A second poller — say the Queue screen wanting
its own copy — would take a random half of the events and both consumers would
render nonsense. Nothing under `ui/` may call `poll_events`.

`DownloadRepository` also owns a **serial queue**. Downloads have to run one at a
time (aio-dl.py keeps per-run state in process-wide module globals, and Chaquopy
gives one interpreter per process), which M2 enforced by *refusing* a second
request. On a phone that reads as a button that does nothing, so the surplus now
waits in the queue and the service drains it.

### Design language

Ported from the desktop (`UI-source/`), not reinvented — the two apps are meant
to read as one product:

| | |
|---|---|
| `ui/theme/Color.kt` | every `--token` from `src/styles/globals.css`, HSL→RGB, light + dark |
| `ui/theme/Type.kt` | the desktop's dense 10/11/12/13/14sp Tailwind ramp, **not** Material's (whose smallest body size is 14sp) |
| `ui/theme/Motion.kt` | the three named animations and two durations from `tailwind.config.js` |
| `ui/components/Primitives.kt` | `primitives.jsx` — SectionHeader, Collapsible, SwitchRow, StatusCallout, Pill, ProgressTrack … |
| `core/LogFilter.kt` | `electron/log-filter.js`, with JVM unit tests |
| `ui/screens/LibraryScreen.kt` | `LibraryTab.jsx` + `LibraryFilterPanel.jsx` — grid, six sorts, four facet groups with library-wide counts, three distinct empty states |

Two places the library screen reshapes rather than copies, both because a phone
is not a 1400px window: the desktop's **Updates Center** side sheet becomes a
strip above the grid (a panel would cover the thing it is describing), and the
**format badges** on a card would not fit beside the title at ~120dp, so the
status rides a scrim on the cover instead and the badges live on the detail
screen. Those badges are derived from the files actually on disk, not from the
recorded `format` — a run that was cancelled or changed format leaves the two
disagreeing, and the folder is the one telling the truth about what you can open.

**No Material You dynamic color**, though the plan originally called for it: it
derives the palette from the wallpaper, which would replace the blue that carries
the product's identity — active tab, progress bar, focus ring, primary button.
Light/dark still follows the system.

**Fonts are the platform faces, not DM Sans / JetBrains Mono.** Downloadable
Google Fonts need Play Services *and* a certificate resource array and fall back
silently when either is missing; bundling the TTFs means vendoring ~800 KB of
binaries plus their OFL license. The type SCALE is what carries the design
language, and `Type.kt` has exactly two constants to change if the real faces are
ever bundled.

The intent-driven test harness from M0 **still works** — `android/TESTING.md`
depends on it — but the extras now feed the same queue the Start button does, so
a test exercises the real path.

## Building

```bash
cd android && ./gradlew assembleDebug
```

The build needs a **JDK 17–21** (AGP 8.13). If your default `java` is outside
that range — e.g. this machine has JDK 23 on `PATH` and `JAVA_HOME` pointing at
JDK 18 — set it for the invocation only rather than changing your environment.
Android Studio's bundled runtime is a known-good 21:

```bash
JAVA_HOME="/c/Program Files/Android/Android Studio/jbr" ./gradlew assembleDebug
```

`local.properties` (gitignored) holds `sdk.dir`. Recreate it with:

```bash
echo "sdk.dir=C\\:\\\\Users\\\\<you>\\\\AppData\\\\Local\\\\Android\\\\Sdk" > local.properties
```

## Why these versions

Every pin comes from Chaquopy 17.0's published compatibility table
(`product/runtime/docs/sphinx/versions.rst`): **Python 3.10–3.14, AGP 7.3–9.3,
minSdk 24.**

| Component | Pinned | Why |
|---|---|---|
| Chaquopy | 17.0.0 | Latest; the only line supporting Python 3.13+ |
| Python | 3.13 | Matches the desktop interpreter (3.13.12), so behaviour gaps can't come from a version gap |
| AGP | 8.13.2 | **Deliberately not the newest.** 9.3.1 is the exact top of Chaquopy's range, and AGP 9 is a major with its own breaking changes — a bad place to start a spike. 8.13.2 is mid-range for Chaquopy 17 *and* inside 16.1's range, so Chaquopy can be downgraded without touching AGP |
| Gradle | 8.14.5 | Highest 8.x; AGP 8.13 wants Gradle 8.13+ |
| Kotlin | 2.2.21 | Mature, pairs with AGP 8.13 |
| compileSdk / targetSdk | 36 | The only platforms installed locally (`android-36`, `android-36.1`) |
| minSdk | 26 | Chaquopy's floor is 24; 26 costs no meaningful device coverage |
| ABIs | `arm64-v8a`, `x86_64` | Chaquopy's Python 3.12+ builds are 64-bit only. `x86_64` is emulator-only and can be dropped to halve the APK |

## Dependency triage

Chaquopy serves native packages from its own Android wheel index; pure-Python
ones come from PyPI. The four that the whole port depended on **all exist** for
cp313/arm64, which was the riskiest assumption in the plan:

| Package | Android wheel | Note |
|---|---|---|
| Pillow | 11.0.0 | Hard dep, and **no WebP** — see below — imported at module scope by `aio-dl.py` and `sites/mangareader.py`. No fallback exists |
| numpy | 1.26.2 | Satisfies `numpy>=1.24` |
| lxml | 5.3.0 | BeautifulSoup's fast parser |
| cryptography | 42.0.8 | Needed by kagane / mangago / mangareader |

Native versions are **pinned exactly** in `app/build.gradle.kts`. Unpinned, pip
prefers PyPI's newer releases, finds no Android wheel, falls back to the sdist,
and fails to compile it. Check `https://chaquo.com/pypi-13.1/<name>/` before bumping.

### Chaquopy's Pillow has no WebP decoder

Found on device during M6. `PIL/_webp.so` is simply **not in the wheel**, and
11.0.0 is the *only* Pillow Chaquopy publishes, so there is no version to bump
to. Symptom in logcat:

```
UserWarning: image file could not be identified because WEBP support not installed
```

What it does and does not affect:

- **Downloads are fine.** The default CBZ path is raw byte passthrough — pages
  are never decoded, so a WebP-serving site downloads and stores correctly.
- **Image-quality probing degrades for WebP-serving sites.** Their sampled pages
  cannot be decoded, so the rating falls back to the cover or the per-site seed.
  This is *visible and honest* rather than silent: those sources render with the
  "rating not measured" triangle. Measured live — atsumaru came back `Quality 0%`
  **with** a triangle, while mangafire 88%, weebcentral 86%, mangapill 81% and
  mangakatana 78% probed for real and carried none.
- **The lossy transform paths would fail on WebP sources** — `--webtoon-recompress`
  (which also *writes* WebP), EPUB/PDF conversion, `--width`, `--scaling`,
  `--quality < 100`. None are wired into the app yet; whoever wires them owes
  this a capability check.

### rapidfuzz is vendored, and it is NOT the native wheel the plan called for

`rapidfuzz` gates cross-site search **and** AniList enrichment (both lazy-import
it, so downloads never depended on it). Chaquopy's index has no rapidfuzz and
PyPI publishes no platform-independent build, so `android/wheels/` carries one
we build ourselves — **rapidfuzz's own pure-Python wheel**, from the official
sdist, whose 37 `.py` files are byte-identical to a desktop install.

This is not the substitute matcher the plan ruled out; it is the same library.
rapidfuzz ships two backends and imports whichever is available, and the
pure-Python fallback is upstream's supported configuration (`pyproject.toml`
defaults to `wheel.cmake = false`, printing *"CMake unavailable, falling back to
pure Python Extension"*). No NDK, no cross-compilation, no per-ABI artifact, and
a Chaquopy Python bump cannot invalidate a `py3-none-any` wheel.

The native build was measured, not dismissed: compiled is **16x faster**, which
is 4.9 ms vs 79 ms per full search-and-enrich operation — inside a search that
spends 60+ seconds on the network. See `android/wheels/README.md`.

**The two backends are not bit-identical**, which matters because every
threshold is a specific number (WRatio 75 admission gate, `token_set_ratio` 85
author match, `_TITLE_BAND_DELTA` 8). An exhaustive sweep of all 1,114,112
codepoints found two causes, and `sites/fuzzy_match.py` — a **shared** module,
used by desktop too — closes the reachable one:

- **3 separator codepoints** (`_`, U+0085, U+00A0). Python's `\W` treats `_` as a
  word character and `str.split()` splits on Unicode whitespace where the C++
  tokenizer splits only on ASCII. U+00A0 is the one that bites: scraped HTML
  titles carry `&nbsp;`. Normalizing all three to a space makes both backends
  see the identical string.
- **5536 Unicode-skew codepoints** (4824 of them CJK extensions), where
  rapidfuzz's bundled C++ tables are older than Python 3.13's Unicode database.
  Not fixable by normalization — a CJK ideograph is not a separator — and
  deliberately left alone. Measured worst case 4.35 points, and 1 of 130 real
  series contains any such codepoint at all.

`tests/test_fuzzy_match.py` re-derives both sets from the installed rapidfuzz,
so an upgrade that widens the gap fails a test instead of silently changing
which series the phone matches. On device, `diagnostics()` reports
`rapidfuzz_backend` and a `rapidfuzz_fingerprint` that **must equal desktop's**.

**Not installed**, each already behind an existing capability flag so it
degrades rather than crashes:

- `curl_cffi` / `impit` — fast-download fast paths; `sites/base.py` degrades.
- `pyvips` — WebP save fast path; falls back to PIL.
- `patchright` / `zendriver` — desktop browsers, replaced by the WebView backend (M4).
- `fastapi` / `uvicorn` — the REST server; Android calls Python in-process.
- `pillow-jxl-plugin` — `--modernize` only. (Note Pillow 11, not 12, so no native AVIF either.)
- torch / pyiqa / piq / torchmetrics / easyocr — not needed by anything any more:
  the opt-in ML rating that used them was removed (2026-10-21).

## Python source staging

The downloader's Python lives at the **repo root**, not here. `stagePythonSources`
(a `Sync` task in `app/build.gradle.kts`, wired to `preBuild`) mirrors the
shipping subset into `app/build/generated/python`, which is Chaquopy's source
dir. It is the Android analogue of `UI-source/scripts/prepare-src.js` — **when a
new root-level module appears, both lists need it** (grep `ROOT_PYTHON_MODULES`).

Two things that list gets right and a naive copy would not:

- It is an **include-list**. The repo root also holds one-off dev scripts
  (`investigate_comix.py`, `probe_mangafire_vrf.py`, `calibrate_quality_probe.py`,
  `_diag_*.py`). An exclude-list would ship them the moment someone adds another.
- It copies `sites/**/*.json`, not just `*.py`. `sites/` carries two data files —
  `quality_seed.json` (comix image-quality probe) and `official_publishers.json`
  (metadata layer) — that a `*.py` filter drops silently, surfacing much later as
  a wrong rating.

`api.py`, `gui.py` and `metadata_dialog.py` are excluded on purpose: the first is
the FastAPI server, the other two are the tkinter desktop GUI.

## Python-side seams

Two files in the repo root / `sites/` exist for this port and are **inert on desktop**:

- **`aio_android.py`** — the Chaquopy entry point, and the only module Kotlin
  talks to. It drives `aio-dl.py:main()` by building an `argv`, exactly as the
  CLI does, rather than refactoring 5,500 lines of argparse-driven code. It fixes
  the four things that assumption breaks on Android: CWD, cancellation
  (cooperative, since there's no process to kill), progress (a structured event
  sink instead of stdout scraping), and the browser.
- **`sites/browser_backend.py`** — a `BrowserBackend` protocol plus a registry.
  Desktop installs no factory, so `custom_backend()` returns `None` and every
  existing Patchright/zendriver path runs byte-identically. Android installs a
  WebView-backed one from `core/Aio.kt`, right after `configure()`.

The JNI boundary is deliberately **positional and primitives-only**, with
everything structured crossing as a JSON string — Python kwargs and dicts do not
survive the trip from a Kotlin object. See `aio_android.py`'s module header for
the exact interface Kotlin must implement.

`core/Aio.kt` is the only place Kotlin starts the interpreter, so `Python.start`
and `configure()` happen exactly once and in that order. Every call through it
blocks; callers are on background threads.

## Roadmap

| | |
|---|---|
| **M0** | Gradle + Chaquopy build, `configure()` + `diagnostics()` on device — **done** |
| **M1** | Python portability layer — **done** (registry guard, cancellation, event sink, browser seam) |
| **M2** | Download foreground service, progress + ETA, cancel — **done** |
| **M3** | Compose UI — Download / Queue / Logs — **done** |
| **M4** | WebView `BrowserBackend` — MangaFire vrf signing + interactive challenges — **done** |
| **M5** | Library — cover grid, facets, update checks, delete — **done** |
| **M6** | Cross-site search — vendored rapidfuzz, results grouped by series — **done** |
| **M7** | Settings + `AppSettings`, resume, metadata editor, `MANAGE_EXTERNAL_STORAGE` library path ← *here* |
| M8 | Packaging / CI |

Downloads are serialized (concurrency 1) because `aio-dl.py`'s module globals
(`_HOST_CONCURRENCY_CAP`, `_image_prefetch_queue`, `_RATE_LIMIT_SCHEDULE`) are
process-wide state.

**comix is the one site that still cannot work here.** It drives its own
Patchright session directly (grep `_comix_worker_loop` in `sites/comix.py`)
instead of going through the backend seam, so the WebView bridge does not reach
it. Everything else that needed a browser — MangaFire, violetscans, and any
Cloudflare-challenged Madara/MangaThemesia site — routes through
`custom_backend()` and works. The New screen says which of the two a pasted URL
is, rather than letting the run fail ten seconds in.
