# On-device testing runbook (for agents)

**Read this before touching a device. Follow it literally.** It exists so a
testing agent can verify the Android port without reading the Kotlin, the Gradle
files, or the Python — and without flooding its context with logcat.

## Rule zero: never dump raw logcat

`adb logcat` on this device emits thousands of unrelated lines a second. **Always
filter with `-s <tag>` and always bound with `Select-Object -Last N`.** One
unfiltered `adb logcat -d` can burn an entire context window.

The only four tags that matter:

| tag | what it carries |
|---|---|
| `AioM0` | the harness's own report: diagnostics JSON, `[run] queued`, `[run] exit=`, resulting file tree |
| `AioService` | the run lifecycle: the built argv, `worker already running`, `download finished exit=` |
| `python.stdout` | everything `aio-dl.py` prints — the download log |
| `python.stderr` | Python tracebacks and the hardening layer's cooldown/error lines |
| `AioLibrary` / `AioCovers` | library scan / cover resolution, and **only on failure** — silence here is the healthy state, not a missing run |

**`python.stdout` is EMPTY during an update check, by design.**
`aio_android.list_chapters` wraps the run in `redirect_stdout` so an 800-chapter
payload cannot bury the log. Do not read that silence as "the check never ran" —
it cost a wrong diagnosis once. To prove a check happened, use `python.stderr`
(not redirected — aio-dl.py's import warnings appear there) or open the series
and look for a pre-populated result panel with the button reading **Check
again**.

The app's **Logs screen shows `python.stdout` / `python.stderr` / `AioService`
in-app**, already classified. For a quick "what is it doing", that beats adb.

## Setup (once per session)

```powershell
$adb = "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe"
& $adb devices -l
```

Expect one device in state `device`. If it says `unauthorized`, the tablet is
showing a "Allow USB debugging?" prompt — **ask the user to accept it**; you
cannot dismiss it yourself.

## Build and install

The build needs **JDK 17–21**. This machine's `JAVA_HOME` is JDK 18 and its
`PATH` java is JDK 23, so **set it per-invocation** — do not change the user's
environment:

```powershell
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
Set-Location "C:\Users\legoc\OneDrive\Belgeler\Scripts\AIO-Webtoon-Downloader\android"
.\gradlew.bat --console=plain assembleDebug 2>&1 | Select-Object -Last 12
& "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe" install -r app\build\outputs\apk\debug\app-debug.apk
```

Rebuilds are incremental (~20s). A no-op build reports `47 actionable tasks: 47
up-to-date` — if it re-runs everything instead, something touched the staged
Python; that is expected after editing any `.py` at the repo root or in `sites/`.

## Test 1 — diagnostics only (fast, ~25s)

Proves Python boots and the handler registry imported. **Run this first after
any change to Python or Gradle**; it catches most breakage in half a minute.

```powershell
$adb = "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe"
& $adb logcat -c
& $adb shell am force-stop com.aio.downloader
& $adb shell am start -n com.aio.downloader/.MainActivity
Start-Sleep -Seconds 25
& $adb logcat -d -s AioM0:* 2>&1 | Select-Object -Last 40
```

**Pass criteria — all four:**

- `[ok] configure()  library=/storage/emulated/0/Android/data/…/manga`
  (M3 folded the separate `Python.start()` / `import aio_android` lines into
  `core/Aio.kt`, which does both or throws; the old three-line report is gone)
- `"registered_handlers": 303` and `"base_handlers": 42` — **must match desktop exactly**
- `"optional_handler_errors": {}` — a non-empty dict means a native wheel failed;
  the named handlers (kagane/mangago/mangareader) dropped out. Report the dict verbatim.
- capabilities `pillow`, `cryptography`, `lxml`, `numpy`, `cloudscraper`, `pypdf` all `true`

`curl_cffi` and `impit` are `false` **on purpose** — they are not installed.
That is not a failure. See `README.md` "Dependency triage".

`rapidfuzz` **must be `true`** as of M6 (it is vendored in `android/wheels/`).
Two more fields come with it, and both matter:

- `"rapidfuzz_backend": "python"` — expected on device. `"cpp"` would mean
  someone dropped a native wheel in; `null` means the wheel went missing and
  search is dead.
- `"rapidfuzz_fingerprint"` — **must equal desktop's, byte for byte.** Get
  desktop's with:

  ```powershell
  python -c "import aio_android; print(aio_android._match_fingerprint())"
  ```

  A mismatch means the phone's matcher scores differently from the desktop's,
  which shows up as *wrong series matched* and never as an error. Report both
  strings verbatim; do not try to interpret the numbers.

## Test 2 — a real download

The harness is driven by intent extras, so you can change site, chapters, format
and flags **without rebuilding**:

| extra | meaning | default |
|---|---|---|
| `url` | series URL. Omit for diagnostics only | — |
| `chapters` | `--chapters` spec: `all`, `1`, `-3` (last 3), ranges | `1` |
| `format` | `cbz` \| `epub` \| `pdf` \| `none` | `cbz` |
| `extra` | extra CLI args, space-separated, appended verbatim | — |
| `service` | accepted and ignored since M3 — everything runs through the service | — |
| `cancel` | cancel the running download | — |
| `browsertest` | URL: exercise the WebView backend only, no download (Test 5) | — |
| `solvetest` | URL: open the interactive challenge window directly (Test 5) | — |
| `forcechallenge` | with `browsertest`: pretend the page is bot-checked | `false` |

**Firing a second intent while one is running QUEUES it** (M3). Look for
`AioService: worker already running; queued for it to pick up`, then a second
`[run] exit=` when it drains. The queue de-duplicates on the exact URL string, so
two intents with the identical URL log `[run] already queued` and only one runs —
to test queueing you need two genuinely different URLs.

```powershell
$adb = "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe"
& $adb logcat -c
& $adb shell am force-stop com.aio.downloader
& $adb shell am start -n com.aio.downloader/.MainActivity `
    --es url "https://mangadex.org/title/db84bc96-05db-4022-9b61-11982f67c18e" `
    --es chapters "1" --es format "cbz"
```

Then **wait for a terminal state instead of sleeping a fixed time** — a download
can take anywhere from 30s to several minutes:

```bash
ADB="/c/Users/legoc/AppData/Local/Android/Sdk/platform-tools/adb.exe"
for i in $(seq 1 150); do
  if "$ADB" logcat -d -s AioM0:* 2>/dev/null | grep -qE "\[run\] exit=|\[FAILED\]"; then echo DONE; exit 0; fi
  sleep 5
done
echo "TIMED OUT"
```

Read the result:

```powershell
& $adb logcat -d -s AioM0:* 2>&1 | Select-Object -Last 30      # exit code + file tree
& $adb logcat -d -s python.stdout:* 2>&1 | Select-Object -Last 40   # download log
& $adb logcat -d -s python.stderr:* 2>&1 | Select-Object -Last 20   # only if it failed
```

**Pass criteria:** `[run] exit=0`, plus a `.cbz` in the tree AND the entry check
below. Size alone proves nothing in either direction — **a few KB means empty
images, but a large file is not automatically suspicious**: MangaDex serves
original-quality PNGs, so the reference run below is legitimately 62 MB.

### Verify the CBZ is real, not just present

A zero-byte or HTML-filled CBZ still appears in the tree, so open it:

```powershell
$adb = "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe"
$cbz = "$env:TEMP\check.cbz"
& $adb pull "/storage/emulated/0/Android/data/com.aio.downloader/files/manga/Anne Shirley/Anne Shirley.cbz" $cbz
Add-Type -AssemblyName System.IO.Compression.FileSystem
$z = [System.IO.Compression.ZipFile]::OpenRead($cbz)
"entries: $($z.Entries.Count)"
foreach ($e in ($z.Entries | Where-Object { $_.Length -gt 0 } | Select-Object -First 5)) {
  $s = $e.Open(); $b = New-Object byte[] 8; $null = $s.Read($b,0,8); $s.Close()
  "{0,-16} {1}" -f $e.FullName, (($b | ForEach-Object { $_.ToString('X2') }) -join ' ')
}
$z.Dispose()
```

Magic bytes must be `FF D8 FF` (JPEG), `89 50 4E 47` (PNG) or `52 49 46 46`
(WebP). **`3C ...` is ASCII `<` — an HTML error page saved as an image**, which
is the classic silent failure. `ComicInfo.xml` should also be present.

**Reference result (verified 2026-08-08, Android 15 / arm64):** *Anne Shirley*
chapter 1 → `exit=0` in ~48s, **59 entries** = 1 JPEG cover + 57 PNG pages +
`ComicInfo.xml`, 62.4 MB, alongside `.aio_series.json` and `.series_hid`.
Note the timing summary attributes nearly everything to "Image URL fetch" and
reports "Image download: 0.0s" — that is bucket accounting on this path, not a
sign the images were skipped.

## Test 3 — the foreground service (and cancel)

**Since M3 every run goes through `DownloadService`; there is no inline path.**
`--ez service true` is still ACCEPTED and ignored, so older recipes keep working
— you just don't need it. An intent enqueues exactly what the Start button
enqueues, so what you test is what a user gets.

```powershell
& $adb shell pm grant com.aio.downloader android.permission.POST_NOTIFICATIONS
& $adb shell am start -n com.aio.downloader/.MainActivity `
    --es url "https://mangadex.org/title/db84bc96-05db-4022-9b61-11982f67c18e" `
    --es chapters "1-4" --es format "cbz"
```

Watch it with `& $adb logcat -d -s AioService:* | Select-Object -Last 10`.
Confirm the notification exists:

```powershell
& $adb shell dumpsys notification --noredact | Select-String "com.aio.downloader" | Select-Object -First 2
```

Expect `channel=downloads`, `FOREGROUND_SERVICE`, and **`actions=1`** (Cancel).

### Check the progress text and ETA

`Select-String` on the raw dumpsys will not find these — the extras sit far
below the record header, so slice from the package name instead:

```powershell
$dump = & $adb shell dumpsys notification --noredact 2>&1 | Out-String
$idx = $dump.IndexOf("com.aio.downloader")
($dump.Substring($idx, 4000) -split "`n" |
  Select-String -Pattern "android.title|android.text=|android.progress") | Select-Object -First 5
```

Expect, once three chapters have started (the ETA needs two intervals):

```
android.title=String (Anne Shirley)
android.text=String (Chapter 3/4  ·  ch 1  ·  2m left)
android.progress=Integer (3) / progressMax=Integer (4) / progressIndeterminate=false
```

- **Title** is the series name, from the `series` event.
- **`Chapter x/y`** proves the `chapters_selected` event parsed. If y is stuck
  at 0 and the bar is indeterminate, Kotlin is reading the wrong field name —
  aio-dl.py emits **`total`**, not `count`.
- **`… left`** is the ETA, computed in Python at the emit site
  (`aio_android._EtaEstimator`). Absent for the first two chapters **by design**.

**`processed` counts chapters STARTED, and a retried chapter ticks again** —
so a chapter retrying three times reads as `Chapter 3/4 · ch 1`. That is not a
bug; the desktop behaves identically (both count the same repeated log line),
and the ETA clamps at zero rather than going negative.

**Cancel goes through the ACTIVITY, not the service:**

```powershell
& $adb shell am start -n com.aio.downloader/.MainActivity --ez cancel true
```

`adb shell am startservice ... DownloadService` **does not work** and never
will — the service is `exported="false"`, so adb gets *"Requires permission not
exported"*. That is correct security behaviour; do not "fix" it by exporting the
service. The Activity is `singleTop`, so this is delivered to the running
instance via `onNewIntent`.

**Cancel is COOPERATIVE and takes time** — measured 28s and 66s in two runs.
Pages already in flight finish or time out, then the chapter loop breaks and the
run finalizes normally (skipped-chapter report, missed-chapters JSON, `Done.`).
Everything already downloaded stays on disk and is resumable. Allow at least
2 minutes before calling cancel broken; look for `download finished exit=` in
`AioService`.

**A cancelled run exits `130`** (`aio_android.CANCELLED_EXIT_CODE`, the shell's
SIGINT convention) and the notification reads **"Download cancelled"**. Pass
criteria for a cancel test are therefore `exit=130` — *not* 0, and *not* 1.

**130 wins over a failure code, on purpose.** Cancelling mid-chapter usually
makes aio-dl.py take its abort branch and `sys.exit(1)`; reporting that as a
failure would send the user hunting for a site problem they caused themselves by
pressing Cancel. The real per-chapter reasons are still written to the
skipped-chapters report and the missed-chapters JSON, so nothing is lost. If you
need to know whether the run ALSO hit a genuine error, read those — not the exit
code.

## Test 4 — the UI itself (M3)

The screens are driven by the same state a real download produces, so the fastest
check is to start a run and look at it. Rail/bottom-bar coordinates below are for
the reference tablet in portrait (1440x2200); re-derive from a screenshot on any
other device.

```powershell
$adb = "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe"
$out = "$env:TEMP\aio-shots"; New-Item -ItemType Directory -Force $out | Out-Null
& $adb shell input tap 72 261        # Queue tab   (New = 72 137, Logs = 72 387)
Start-Sleep -Seconds 2
& $adb shell screencap -p /sdcard/s.png
& $adb pull /sdcard/s.png "$out\queue.png"; & $adb shell rm /sdcard/s.png
```

**Pass criteria, screen by screen:**

- **New** — every Collapsible header ("Battery & data", "Image quality", …) must
  be *readable*. If they render near-black on charcoal, a `Surface` stopped
  providing `LocalContentColor` and every unstyled `Text` in the app went black.
  That shipped once; it is the first thing to check after any theme edit.
- **Queue** — during a run: series title, `Ch. x/y`, a determinate bar, an
  elapsed clock that ticks every second, and `~Nm left` appearing from the third
  chapter on (the ETA needs two samples). A `+N queued` pill when work is waiting.
- **Logs** — open it BEFORE starting the run. `LogTail` follows logcat from
  "now", so output produced while the screen was closed is not retroactively
  shown. Expect `CBZ saved →` and `Done.` in green, indented detail dimmed,
  monospace columns aligned, and the `All / Errors / Warnings` counts non-zero.

**Both themes must read correctly.** Flipping the device theme is a system
setting, so **restore it when you are done**:

```powershell
& $adb shell "cmd uimode night no"    # ... check ... then put it back
& $adb shell "cmd uimode night yes"
```

The log-filter port has offline tests — run these before blaming the device:

```powershell
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
.\gradlew.bat --console=plain :app:testDebugUnitTest
```

Gradle prints nothing on success, so **read the report rather than trusting
"BUILD SUCCESSFUL"** — that message also appears when zero tests ran:

```powershell
Get-ChildItem -Recurse app\build\test-results -Filter *.xml | ForEach-Object {
  $x = [xml](Get-Content -Raw $_.FullName)
  "{0}: tests={1} failures={2}" -f $x.testsuite.name, $x.testsuite.tests, $x.testsuite.failures
}
```

Expect `LogFilterTest: tests=15`, `BrowserJsTest: tests=21` and
`LibraryModelsTest: tests=30`, all with `failures=0`.

The Python half of the browser bridge has its own offline coverage on the desktop
side — `python -m pytest tests/test_android_browser_backend.py -q` (34 tests). Run
it before flashing anything: it pins the JNI method names, and a rename there
fails at runtime with no compile-time warning.

## Test 5 — the WebView browser backend (M4)

This is the fastest way to answer "is the browser bridge broken, or is the site?"
— it runs every backend operation with no download in the way, in about 10s.

```powershell
& $adb logcat -c
& $adb shell am force-stop com.aio.downloader
& $adb shell am start -n com.aio.downloader/.MainActivity --es browsertest "https://mangafire.to/"
Start-Sleep -Seconds 15
& $adb logcat -d -s AioM0:* AioWeb:* 2>&1 | Select-Object -Last 12
```

**Pass criteria** — all seven lines present, and in particular:

| line | what a wrong value means |
|---|---|
| `asyncSum = 42` | **the load-bearing one.** `evaluateJavascript` does not await promises; if the wrapper in `buildEvalScript` breaks, this reads `{}` and every MangaFire token silently comes back null |
| `userAgent = …(wv)…` | the real WebView UA. It must be whatever actually goes on the wire — a pinned or guessed value invalidates any clearance bound to it |
| `title = "…"` | JSON-encoded, because `evaluate` returns JSON. `content` is the only op that returns raw text |
| `html = N chars` | > 10000 for a real site. A few hundred usually means an interstitial |

### The interactive challenge window

A live bot check is not summonable on demand, so there is a seam (modelled on
comix's `AIO_COMIX_FORCE_WAF`) that fakes the *detection* once:

```powershell
& $adb shell am start -n com.aio.downloader/.MainActivity `
    --es browsertest "https://mangafire.to/" --ez forcechallenge true
```

Expect, in order: `forced challenge consumed (debug seam)` → `… is showing a bot
check; handing it to the user` → the window opens → `challenge cleared for … (N
cookie chars)` → the original `[browser] ok in …` about 12s later (10s of that is
the grace period below).

`--es solvetest <url>` opens the same window directly, **without** the offscreen
WebView ever being created. Use it to tell a visible-WebView problem apart from a
headless-WebView one; that distinction is what isolated the layout bug below.

What the seam proves: the notification, the Activity, the detection loop, the
cookie capture, and the caller's error path when the user declines. What it
**cannot** prove: that a real clearance then satisfies the origin. That needs a
genuinely challenged site.

Two timings worth knowing before you call something hung:
- The window waits **10s** (`GRACE_MS`) before declaring an unchallenged page
  passed. It reports success immediately once a challenge it *saw* clears.
- `goto` gives the user **180s** to tap through, then fails the call.

## Test 6 — the library (M5)

The library is driven by what is on disk, so the cheapest realistic fixture is a
**synthetic series folder** rather than another download. It exercises the paths
a real library takes minutes to reach, and it can be removed by the app's own
Delete button — so cleanup is itself a test.

### Build the fixture

```powershell
$adb = "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe"
$dir  = "/storage/emulated/0/Android/data/com.aio.downloader/files/manga/Zz Cover Test"
# Any small PDF works; this one exists to be rasterized by PdfRenderer.
python -c "from PIL import Image; Image.new('RGB',(720,960),(28,40,84)).save('cover_test.pdf','PDF')"
& $adb shell "mkdir -p '$dir'"
& $adb push cover_test.pdf "$dir/Zz Cover Test Ch 1.pdf"
```

Then a `.aio_series.json` beside it. Point `url` at the known-good series below
and record **only chapter 1** as downloaded, so a check has something to find:

```json
{"title":"Zz Cover Test","url":"https://mangadex.org/title/db84bc96-05db-4022-9b61-11982f67c18e",
 "site":"mangadex","language":"en","format":"pdf","status":"Completed",
 "authors":["Test Author"],"genres":["Drama","Slice of Life"],"chapters_downloaded":["1"]}
```

### What to check

| step | pass criteria |
|---|---|
| launch (app opens on Library) | a card per folder; the count reads `N series` |
| covers | a CBZ-only series shows real art — that cover came out of the archive, into `<cacheDir>/covers`. A PDF-only series shows page 1. Neither should sit on the tinted monogram, which is the fallback *and* the placeholder |
| the series folder | **byte-identical after a scan.** `ls -la` it: no `.cover.*` may appear. Komikku/Mihon read that folder |
| status band | uppercase, on a scrim across the bottom of the cover |
| detail | genres as chips, stats row, format badges **derived from the files on disk** (not from the recorded `format`, which a cancelled run can leave stale) |
| check for updates | on the fixture: `3 new chapters`, range `2-4`, a `Download 3` button. On a complete series: `Up to date — N on the site, N here.` |
| the built argv | `AioService: run_download_json […]` must carry `--chapters 2-4` and the SERIES' format/site, not the New tab's |
| filter sheet | counts are library-wide; selecting one narrows the header to `1 of 2` and adds a removable chip |
| delete | two taps, and **the second must land within 4s** — the button auto-disarms, and a `screencap`/`uiautomator dump` between taps blows that budget |

### Cleanup

Delete the fixture through the app (Detail → Delete → Delete). Confirm the
folder is gone with `adb shell ls`.

**If you queue a download from a check, expect it to write into the folder named
after the SERIES, not the fixture** — and to honour the fixture's `format`. A
cancelled `--format pdf` run against a CBZ series therefore leaves both a stray
`.pdf` and a rewritten `.aio_series.json` whose `format` now says `pdf`. Remove
the stray file and restore the field; both are the user's real library.

## Test 7 — cross-site search (M6)

Search runs the same engine a download does, so it takes the engine lock and
takes **40-100 seconds**. That is normal, not a hang.

```powershell
$adb = "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe"
& $adb logcat -c
& $adb shell am force-stop com.aio.downloader
& $adb shell am start -n com.aio.downloader/.MainActivity
```

Tap **Search**, type `frieren`, tap Search, and wait. Then:

```powershell
& $adb logcat -d -s python.stderr:* 2>&1 | Select-Object -Last 30
```

**Pass criteria:**

| | |
|---|---|
| Results appear | one card per SERIES, not per site. A card says "N sites" |
| Expanding a card | lists each site with a quality %, chapter count and match % |
| The rating | reads "Quality N%" only for `quality_basis` "matched"/"partial"; every other source reads "Quality: not compared" / "not measured" / "timed out" / "unreachable". Some sources having a label is CORRECT — they were not compared with another site's pages |
| Edition / same-scan notes | an "Other edition: …" line on a fan-colored or otherwise different copy; "Same file as …" / "Same scan as …" under sources that serve copies of one scan |
| Tapping Get/Download | switches to **New** with the URL filled in |
| `python.stderr` | shows the fan-out (`Searching N sites`) and per-site timings |
| No comix | it is force-excluded on Android; seeing it means `--disable-sites` broke |

**Search during a download** is the case worth checking deliberately: start a
download, then go to Search. The screen should warn *before* you tap, and a tap
should return "A download is running…" rather than hanging. It must never
report the raw code `engine_busy`.

**Expect `WEBP support not installed` in `python.stderr`.** Chaquopy's Pillow
has no WebP decoder, so pages from WebP-serving sites cannot be scored. That is a
known platform gap, not a regression: those sources fall back to a seed rating
and show the warning triangle. atsumaru is the reliable example — `Quality 0%`
**with** a triangle, while mangafire/weebcentral/mangapill probe for real and
carry none. A source showing a high rating AND a triangle is also fine (seed).
What would be a bug is a 0% with NO triangle.

**Empty stdout is expected** — `search` captures it, exactly like the update
check in Test 6. See rule zero.

## Test 8 — settings, resume and metadata (M7)

Three features that share one screen change: **Settings** is now a destination
reached from the **top-bar gear**, and Diagnostics lives inside it (there is no
longer an ⓘ button in the top bar — that is the change, not a regression).

### 8a — Settings and the resource-limit move

Open the gear. Expect five sections: Storage, Battery & data, Search, Library,
About.

| | |
|---|---|
| **Battery & data** | is HERE now, not on the New tab. The New tab's advanced list must NOT still have a "Battery & data" collapsible — two copies would disagree |
| A migrated limit | if a limit was set before this build, it must still be set: it is lifted out of the old form blob on first load (`AppSettings.migratedFromForm`) |
| **About → Diagnostics** | opens the same sheet as before, ON TOP of Settings |
| **Reset these settings** | two taps, and keeps the library path |

Then prove it reaches Python — set **Network: Low**, start any download, and:

```powershell
& $adb logcat -d -s AioService:I 2>&1 | Select-String "run_download_json"
```

The argv must contain `--image-concurrency 2 --image-workers 1`. A limit that
does not appear here is the failure this whole layer exists to prevent: it is
silent everywhere else.

### 8b — Resume

**Cancel LATE and you get nothing to resume.** aio-dl.py only leaves
`tmp_<hid>/` behind when the cancel lands while chapters are still downloading;
a cancel that arrives once every chapter is done falls through to the
final-file build and the folder is cleaned up as a normal finish. On the test
series a chapter takes ~12s, so **cancel at ~16-22s**. Confirm with
`adb shell run-as com.aio.downloader ls cache/work` before going near the UI —
an empty work dir means you cancelled too late, not that resume is broken.

Resume needs an interrupted run, so make one:

```powershell
& $adb logcat -c
& $adb shell am start -n com.aio.downloader/.MainActivity `
    --es url "https://mangadex.org/title/db84bc96-05db-4022-9b61-11982f67c18e" `
    --es chapters "1-4" --es format cbz
```

Wait for **2-3 chapters** (watch `AioService:I`), then cancel:

```powershell
& $adb shell am start -n com.aio.downloader/.MainActivity --ez cancel true
```

Open **Queue**. Pass criteria:

| | |
|---|---|
| An **Unfinished** section, ABOVE Active | with the series name, "N ch kept" and a size |
| The **Queue tab badge** | counts it — unfinished work is work waiting to happen |
| **Resume as** | shows the original format selected. Picking another shows "Was CBZ." |
| **Resume** | queues a run; the card disappears from Unfinished immediately |
| The resumed run's argv | `--restore-parameters --format cbz … <url>` and **no `--verbose`** |
| The log | says `Parameters match. Resuming download.` and the kept chapters come back as "already processed" |
| **Discard** | two taps, and the button names the size it will reclaim |

The single highest-value check is the **`--format` in that argv**. argparse
defaults it to `epub`, so if it were ever omitted a resumed CBZ series would
silently become an EPUB — with no error anywhere.

### 8c — The metadata editor

Open any downloaded series → **Metadata → Edit embedded metadata**.

| | |
|---|---|
| The form | pre-fills from the first CBZ/EPUB/PDF. An empty form on a `--komikku` download is suspicious — those always carry a ComicInfo.xml |
| **Save** | is disabled until something changes, and after saving goes disabled again |
| **Apply to all N files** | appears only when the series has more than one archive |
| **During a download** | Save is disabled and the screen says why. It must never fail on tap with a raw `engine_busy` |

Verify it actually wrote, rather than trusting the toast:

```powershell
& $adb shell "cd '/sdcard/Android/data/com.aio.downloader/files/manga/<Series>' && unzip -p '<Series>.cbz' ComicInfo.xml" | Select-String "Writer|Title"
```

`<Writer>` should hold the comma-joined string you typed. **This is also the
regression test for the shared list-handling fix** — the desktop sends these
fields as JSON arrays, and before `metadata_editor._as_text` any list raised
`AttributeError` and the whole save failed.

**`unzip` failing with nothing on stdout is usually NOT a save failure.** Files
in app-scoped external storage land as `-rw-------`, so the `shell` user cannot
read them at all — a plain downloaded file behaves the same way. Read the value
back through the app's own editor instead, or check `ls -la` for a changed mtime
and size. (Quote the paths: the series folder has a space in it.)

**Two device-only traps that cost time here:**

| | |
|---|---|
| The on-screen keyboard | `uiautomator dump` reports the app's layout as if the IME were not there, so Save's coordinates look valid while the keyboard covers them. Send `KEYCODE_BACK` to dismiss it BEFORE tapping Save |
| Two-tap confirms | Discard re-arms for only 4s and the button RESIZES when armed ("Delete 283 MB?"). Tap the same coordinate twice, ~1s apart, taken from the `clickable="true"` node rather than the text node |

### 8d — The shared library folder (destructive-ish; ask first)

Only worth running deliberately, because it **moves where downloads land** and
needs an app restart. In Settings → Storage → *Use a shared folder*:

1. **Grant access** opens the system all-files screen. After granting and coming
   back, the "Needs all-files access" callout must be **gone** — it re-checks on
   resume, since the grant happens in another app with no callback.
2. **Check folder** on `/storage/emulated/0/Manga` reports free space and any
   series already there. This probe WRITES a file to decide, so a pass means the
   downloader really can use it.
3. **Use this folder** only enables after a passing probe.
4. Force-stop and relaunch, then confirm the Library is reading the new path
   (Settings → Storage shows it; a series' detail screen shows the full path).

To undo: *Back to the app's own folder*, then restart. Nothing is moved either
way — downloads already written stay where they are.

## Known-good test series

`https://mangadex.org/title/db84bc96-05db-4022-9b61-11982f67c18e` — *Anne Shirley*,
content rating `safe`, short series, ~26-page chapters.

**Do not test with a series that is officially licensed in English.** MangaDex
delists those, so the handler correctly returns **0 chapters** and the run fails
with "No chapters selected" — which looks like a bug and is not one. Frieren
(`b0b721ff…`) is exactly this trap: it returns a title and `status: hiatus` but
`total: 0`. Licensed chapters also appear as `pages: 0` + `externalUrl` in the
MangaDex API and can never download.

## Environment facts that will waste your time otherwise

- **This network's DNS resolver drops some manga hosts, INTERMITTENTLY, on both
  machines.** On the PC, `api.mangadex.org` and `dynasty-scans.com` fail to
  resolve while `mangadex.org` and `google.com` succeed. The tablet usually
  resolves all four — but a run on 2026-08-08 died mid-download with
  `NameResolutionError ... Failed to resolve 'api.mangadex.org' ([Errno 7] No
  address associated with hostname)`, and a ping two minutes later succeeded.
  **So a mid-run resolution failure is an environment flake, not a code
  regression** — re-run before investigating. Symptoms: `[!] Chapter N long
  retry k/2: waiting …s for <host>.mangadex.network to recover`, and the same
  chapter number ticking repeatedly. Do not "fix" this by editing DNS or hosts;
  for desktop-side API checks use
  `curl.exe -s -g --resolve api.mangadex.org:443:45.129.229.2 "<url>"`
  (`-g` is required, or curl reads `[]` in the query as a glob and exits 3).
- **`rapidfuzz` is not installed on Android** and has no wheel. Downloads are
  unaffected, but `--search` and AniList enrichment will raise. Do not report
  that as a regression.
- The library lives at
  `/storage/emulated/0/Android/data/com.aio.downloader/files/manga` —
  app-scoped external storage, no permission needed.

## Failure triage

| symptom | meaning |
|---|---|
| `optional_handler_errors` non-empty | a native wheel is missing/broken. Report the dict verbatim; do not "fix" by removing the handler |
| `registered_handlers` ≠ 303 | registry drift — compare against desktop: `python -c "from sites import _REGISTERED_HANDLERS as r; print(len(r))"` |
| `[FAILED] com.chaquo.python.PyException` | a Python traceback; the message is the whole diagnosis. Check `python.stderr` too |
| `No chapters selected` | usually the licensed-series trap above, not a bug |
| `exit=130` | the run was CANCELLED. Expected if you asked for it; if nobody cancelled, something called `aio_android.cancel()` |
| `Chapter N …` repeating, `long retry k/2` | the CDN or DNS is failing that host — see the intermittent-DNS note above. Re-run before investigating |
| CBZ present but tiny | images failed but the container was still written — check `python.stdout` for per-page errors |
| app dies with no `AioM0` output | native crash. `adb logcat -d -s DEBUG:* AndroidRuntime:* \| Select-Object -Last 40` |
| `INSTALL_FAILED_UPDATE_INCOMPATIBLE` | signature mismatch. `adb uninstall com.aio.downloader`, then install again |
| text renders near-black / invisible in dark mode | `AioTheme`'s `Surface` stopped providing `LocalContentColor` (it defaults to BLACK, and MaterialTheme alone does not set it). Every `Text` without an explicit colour is affected |
| second intent never runs | check for `[run] already queued` — the queue de-duplicates on the exact URL string |
| Queue bar indeterminate, `Ch. 0/0` | no `chapters_selected` event parsed. aio-dl.py emits **`total`**, not `count` |
| Logs screen empty during a run | `LogTail` follows logcat from "now" and only runs while the screen is open. Open Logs BEFORE starting the download |
| `[run] exit=` never appears | it is logged when the job lands in the repository's history. If the Activity was destroyed, fall back to `AioService: download finished exit=` |
| `asyncSum = {}` instead of `42` | the promise wrapper broke. `evaluateJavascript` returns the SYNCHRONOUS result, so an un-awaited promise stringifies to `{}` — and MangaFire then reports every token as null, which looks like a site problem |
| MangaFire: `no vrf signer among 0 module candidates` | the page never really loaded — usually an interstitial was scraped instead. Run Test 5 against `https://mangafire.to/` and look at `html = N chars` |
| MangaFire: `vrf signer ready` on EVERY run | expected, not a cache miss. The disk cache is namespaced on the signer chunk URL, which is only knowable after loading the page, so the ~800ms bootstrap always happens once per process. Tokens themselves still come from disk — confirm by checking `vrf-cache.json` is byte-identical after a second run |
| a whole Activity renders BLACK, no exception anywhere | a WebView in that window was measured to **zero height** and took the window's rendering down with it. Check the layout with `adb shell uiautomator dump` before suspecting Chromium — the semantics tree shows the real bounds. This is what `StatusCallout`'s `fillMaxHeight` spine did before it was pinned to `IntrinsicSize.Min` |
| the verification window never opens | the loop guard already offered that host once this run. `DownloadService` clears it per run and `forceNextChallenge()` clears it too; outside those, one host gets one prompt |
| library grid empty but the folder has series | a series needs a directory directly under `manga/`; dotted names are skipped. Check `AioLibrary` for a scan error, and confirm `scan_library` sees them: the payload is `aio_android.scan_library()` |
| a card shows the monogram, never the art | the cover could not be resolved. CBZ/EPUB extraction is `library_cover`; a PDF goes through `CoverStore`'s `PdfRenderer`; anything else genuinely has no cover. `AioCovers` logs the failure |
| "check for updates" is disabled | a download is running. Checks share the engine with downloads (`_ENGINE_LOCK`) — this is the pre-emptive guard, and Python answers `engine_busy` if the race is lost anyway |
| a sweep says "up to date" instantly | with a warm interpreter, one small series is a single API call. Do NOT conclude it short-circuited from empty `python.stdout` — see rule zero. Open the series: a real check leaves a result panel and a **Check again** button |
| Delete does nothing | the confirm auto-disarms after 4s. Two `adb shell input tap` calls back to back work; anything slower between them re-arms instead of confirming |
| `site verification was not completed within 180s` | the user declined, closed the window, or never saw it. If the app was backgrounded, check that the notification was postable — `POST_NOTIFICATIONS` denied means the only route back is the direct activity start, which API 29+ blocks from the background |

## What NOT to do

- Don't `adb uninstall` casually — it deletes the downloaded library and any
  browser profile, and the next run then re-solves challenges from scratch.
- Don't change the device's system settings, DNS, or developer options.
- Don't edit `local.properties` — it is machine-specific and gitignored.
- Don't commit anything. Shipping is decided by the user at end-of-work, per
  the repo's CLAUDE.md.
