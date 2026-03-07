# AIO Webtoon & Light Novel Downloader 📚

> [!IMPORTANT]
> comick.io has ceased providing any media themselves. This project now targets multiple other platforms with the same feature set—thanks for sticking around!
> bato.to and all of its mirror sites have shut down, thus bato.to has been removed from the supported sites.


[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/) [![GNU GPLv3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

The most versatile, feature-rich & customizable tool to download webtoons (manhwa), manga, comics and light novels from [mangataro.org](https://mangataro.org), [mangafire.to](https://mangafire.to), [asuracomic.net](https://asuracomic.net), [weebcentral.com](https://weebcentral.com), [manganato.gg](https://www.manganato.gg), [comix.to](https://comix.to) & many others, and package them into **PDF**, **EPUB**, **CBZ**, or raw chapter bundles—available as both a **command-line tool** and a **graphical interface** (`gui.py`). Features include light-novel aware text handling, intelligent image processing, resumable downloads, parallel batch downloading, library auto-update, scanlation group prioritization, and optional chapter/file-size splitting.

![AIO Webtoon & Light Novel Downloader Banner](https://github.com/zzyil/comick.io-Downloader/blob/main/AIO.png?raw=true)


## 📑 Table of Contents

- [🚀 Features](#-features)  
- [🛠️ Requirements](#%EF%B8%8F-requirements)  
- [🌐 Supported Sites](#-supported-sites)
- [🧰 Installation](#-installation)  
- [🚀 Usage](#-usage)  
- [⚙️ Options](#%EF%B8%8F-options)  
- [🔐 Cookie Setup](#-cookie-setup)  
- [📖 Examples](#-examples)  
- [💡 Tips & Tricks](#-tips--tricks)  
- [📁 Output Structure](#-output-structure)  
- [⚖️ Disclaimer](#️-disclaimer)
- [🤝 Contributing](#-contributing)  
- [🙏 Acknowledgements](#-acknowledgements)  
- [📄 License](#-license)  

---

## 🚀 Features

- 💻 **Command-Line Interface**
  Full-featured CLI with extensive options—scriptable, automation-friendly, and composable with shell workflows.
- 🖥️ **Graphical User Interface**
  Launch `gui.py` for a full Tkinter GUI—manage downloads, browse your library, track chapter counts & file sizes, and trigger updates—without touching the command line.
- 📥 **Flexible Chapter Selection**
  Download specific chapters, ranges (`1-5`), lists (`1,3,5-7`), or open-ended ranges (`-10` for up to ch 10, `5-` for ch 5 onwards).
- ⚡ **Parallel Batch Downloads**
  Pass multiple URLs at once or use `--jobs N` to download several series concurrently across coordinated worker processes. Workers share a polite request pacing layer to avoid hammering sites.
- 🔁 **Library Auto-Update**
  Use `--save-params` once to record your download settings per series. Later, run `--update-all` to automatically fetch new chapters for every tracked series in your library—no need to remember flags or URLs.
- 🏷️ **Scanlation Group Control**
  Prioritize your favorite scanlation group(s), or choose by highest upvotes.
- 🖼️ **Smart Page Processing**
  Resize, scale, recombine, and compress image chapters. Webtoon strips stay continuous; page-style scans keep their page layout.
- 🚫 **No-Processing Mode**
  Package raw pages as-is into PDF/EPUB/CBZ (skip all resizing/recombining/scaling) via `--no-processing`.
- 📁 **Multiple Formats**
  Export as PDF, fixed-layout EPUB, vertical-scroll EPUB, or CBZ.
- 📚 **Light-Novel Friendly**
  Detects prose chapters automatically—EPUB embeds true XHTML pages, PDF outputs selectable text, CBZ renders text blocks as images, and `--format none` saves plain `.txt` chapters.
- 🧼 **Clean Chapter Content**
  Automatically strips Mangataro boilerplate (group avatars, site banners, copyright notices) so only true page content lands in your book.
- 🛡️ **Hardened Rate Limiting**
  Per-domain request throttling with configurable gaps (page, AJAX, image), Cloudflare challenge detection, and exponential backoff with jitter—shared across parallel workers via file locks.
- 🔄 **Resumable Downloads**
  Automatically picks up where it left off if interrupted.
- 🩹 **Missed Chapter Retry**
  After a run completes, automatically retries any chapters that failed to download or process, with configurable retry count (`--missed-retries`).
- ✂️ **Book Splitting**
  Split large downloads by file size (e.g., `400MB`) or chapter count (e.g., `10ch`).
- 💾 **Keep Originals**
  Optionally retain raw images and per-chapter files.

---

## 🛠️ Requirements


- Python
  - Officially supported: 3.8+
  - Best-effort (tested): 3.7 (may require building Pillow from source on some platforms)
  - Not supported: 3.6 and Python 2.x
- OS: macOS, Linux, Windows
- Python packages (installed via requirements.txt)
  - beautifulsoup4
  - cloudscraper
  - lxml
  - Pillow
  - pypdf
  - requests
- Notes for macOS (Apple Silicon), Python 3.7 only:
  - Building Pillow from source may require system libraries. If you see build errors,
    install via Homebrew:
    - brew install jpeg-turbo libpng freetype libtiff webp little-cms2 zlib
  - Ensure Xcode command line tools are installed: xcode-select --install

Feature support by Python version

- Legend:
  - ✅ Supported
  - ⚠️ Supported with caveats (see notes)
  - ❌ Not supported

| Python | PDF | EPUB (page) | EPUB (vertical) | CBZ | Resume/Restore | Split (size/ch) | Keep images | Group priority/mix-by-upvote | Cloudscraper | lxml parser |
| ------ | --- | ----------- | --------------- | --- | -------------- | --------------- | ----------- | ---------------------------- | ------------ | ----------- |
| 3.13   | ✅  | ✅          | ✅              | ✅  | ✅             | ✅              | ✅          | ✅                           | ✅           | ✅          |
| 3.12   | ✅  | ✅          | ✅              | ✅  | ✅             | ✅              | ✅          | ✅                           | ✅           | ✅          |
| 3.11   | ✅  | ✅          | ✅              | ✅  | ✅             | ✅              | ✅          | ✅                           | ✅           | ✅          |
| 3.10   | ✅  | ✅          | ✅              | ✅  | ✅             | ✅              | ✅          | ✅                           | ✅           | ✅          |
| 3.9    | ✅  | ✅          | ✅              | ✅  | ✅             | ✅              | ✅          | ✅                           | ✅           | ✅          |
| 3.8    | ✅  | ✅          | ✅              | ✅  | ✅             | ✅              | ✅          | ✅                           | ✅           | ✅          |
| 3.7    | ✅  | ✅          | ✅              | ✅  | ✅             | ✅              | ✅          | ✅                           | ✅          | ✅ (fallback to html.parser if lxml unavailable) |
| 3.6    | ❌  | ❌          | ❌              | ❌  | ❌             | ❌              | ❌          | ❌                           | ❌          | ❌          |
| 2.7    | ❌  | ❌          | ❌              | ❌  | ❌             | ❌              | ❌          | ❌                           | ❌          | ❌          |

Notes:
- Cloudscraper is optional. If it’s not available or fails to initialize, the downloader
  automatically falls back to requests.Session.
- The HTML parser prefers lxml if installed, and falls back to the built-in html.parser.
- On macOS (Apple Silicon), Python 3.7 may need system libraries to build Pillow.

---



## 🧰 Installation

```bash
git clone https://github.com/zzyil/AIO-Webtoon-Downloader.git
cd AIO-Webtoon-Downloader
python3 -m pip install -r requirements.txt
```

---

## 🌐 Supported Sites

For all supported sites, check [docs/SUPPORTED_SITES.MD](docs/SUPPORTED_SITES.MD)

| Site | Notes | Integration Status |
| ----- | ----- | ----- |
| [asuracomic.net](https://asuracomic.net) | Alternative URLs: [asurascans.net](https://asurascans.net) | recent check 11.25.25 ✅  |
| [Atsumaru / atsu.moe](https://atsu.moe) |  | recent check 11.26.25 ✅ |
| [**BATO.to / mpark.to**](https://bato.to) | See [mirror list](docs/SUPPORTED_SITES.MD) | ⛓️‍💥 bato.to and mangapark shut down; mirrors may still work |
| [**comix.to**](https://comix.to) | Supports multiple scanlation groups | recent check 11.26.25 ✅  |
| [fanfox.net](https://fanfox.net) | Mirrors: [mangafox.la](https://mangafox.la) and [mangahere.cc](https://mangahere.cc) | recent check 11.26.25 ✅  |
| [flamecomics.xyz](https://flamecomics.xyz) |  | recent check 11.25.25 ✅  |
| [galaxymanga.io](https://galaxymanga.io) | | recent check 11.25.25 ✅ |
| [kagane.org](https://kagane.org) | DRM-protected, requires Widevine `.wvd` file. See [docs/Widevine.md](docs/Widevine.md) | currently untested due to .wvd requirement ⚠️ |
| [kingofshojo.com](https://kingofshojo.com) | | recent check 11.25.25 ✅ |
| [mangabuddy.com](https://mangabuddy.com) | See [MangaBuddy mirrors](docs/SUPPORTED_SITES.MD) | recent check 11.27.25 ✅  |
| [**mangadex.org**](https://mangadex.org) | Supports scanlation group selection | recent check 11.25.25 ✅ |
| [**mangafire.to**](https://mangafire.to) | Supports multiple languages | recent check 11.25.25 ✅  |
| [mangahub.io](https://mangahub.io) |  | recent check 11.25.25 ✅ |
| [MangaKakalot / MangaNato](https://www.manganato.gg) | See [mirror list](docs/SUPPORTED_SITES.MD) | recent check 11.25.25 ✅  |
| [mangataro.org](https://mangataro.org) | Supports scanlation group selection | recent check 11.25.25 ✅ |
| [manhuafast.net](https://manhuafast.net) | Mirror: [manhuafast.com](https://manhuafast.com) | recent check 11.25.25 ✅ |
| [manhuaplus.com](https://manhuaplus.com) |  | recent check 11.25.25 ✅  |
| [manhuaus.com](https://manhuaus.com) |  | recent check 11.25.25 ✅  |
| [razure.org](https://razure.org) | light-novel support | recent check 11.25.25 ✅  |
| [utoon.net](https://utoon.net) | |recent check 11.26.25 ✅ |
| [violetscans.org](https://violetscans.org) |  | recent check 11.26.25 ✅  |
| [**weebcentral.com**](https://weebcentral.com) | Supports scanlation group selection | recent check 11.27.25 ✅  |


---

## 🚀 Usage

**Command-line:**
```bash
python3 aio-dl.py [OPTIONS] COMIC_URL [COMIC_URL ...]
```

**Graphical interface:**
```bash
python3 gui.py
```

Run `python3 aio-dl.py --help` for the full option list.

---

## ⚙️ Options

| Option                        | Description                                                                                                                                                                                                                  | Default     |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- |
| `COMIC_URL [...]`             | One or more comic/manga URLs.                                                                                                                                                                                                | _required_  |
| `--prompt-urls`               | Prompt for multiple URLs on stdin (one per line). Finish with an empty line.                                                                                                                                                 | `False`     |
| `--jobs INT`                  | Download multiple URLs concurrently using separate worker processes.                                                                                                                                                          | `1`         |
| `--save-params`               | Save download parameters to the series output folder for future `--update-all` runs.                                                                                                                                         | `False`     |
| `--update-all`                | Scan `--output-dir` for previously saved parameters and download new chapters for every tracked series.                                                                                                                       | `False`     |
| `-o, --output-dir PATH`       | Directory to place the final outputs.                                                                                                                                                                                         | `comics`    |
| `--epub-dir PATH`             | Optional override directory specifically for EPUB outputs.                                                                                                                                                                    | _none_      |
| `--temp-dir PATH`             | Optional override base directory for temporary processing folders.                                                                                                                                                            | _none_      |
| `--cookies STRING`            | HTTP cookie string for restricted content (e.g. `key1=val1; key2=val2`)                                                                                                                                                     | `""`        |
| `--group STRING...`           | Preferred scanlation groups (comma-separated or multiple args).                                                                                                                                                              | `[]`        |
| `--mix-by-upvote`             | When multiple `--group` entries exist, pick the version with the highest upvotes among them.                                                                                                                                 | `False`     |
| `--no-partials`               | Skip fractional chapters (e.g. 1.5).                                                                                                                                                                                          | `False`     |
| `--chapters STRING`           | Chapter filter: `all`, single (`5`), range (`1-10`), list/ranges (`1,3,5-7`), or open-ended (`-10`, `5-`).                                                                                                                   | `all`       |
| `--language STRING`           | Language code (e.g. `en`, `ja`).                                                                                                                                                                                              | `en`        |
| `--format {pdf,epub,cbz,none}`     | Output format.                                                                                                                                                                                                                | `epub`      |
| `--epub-layout {page,vertical}` | EPUB layout: `page` (fixed), `vertical` (scroll).                                                                                                                                                                             | `vertical`  |
| `--width INT`                 | Base image width (px). Defaults vary by format/layout.                                                                                                                                                                        | _auto_      |
| `--aspect-ratio STRING`       | Target W:H ratio (e.g. `4:3` or `2.5`). Not used for PDF.                                                                                                                                                                     | _auto_      |
| `--quality INT`               | JPEG quality `%` (1–100).                                                                                                                                                                                                     | `85`        |
| `--scaling INT`               | Final image scale `%` (1–100).                                                                                                                                                                                                | `100`       |
| `--no-processing`             | Skip all image post-processing (resize, recombine, scaling). Build formats directly from the raw downloaded images.                                                                                                           | `False`     |
| `--split STRING`              | Split by size (`400MB`) or chapters (`10ch`).                                                                                                                                                                                | `None`      |
| `--restore-parameters`        | Reuse saved processing settings (for format-only reassembly).                                                                                                                                                                 | `False`     |
| `--keep-images`               | Retain raw downloaded images under `<output-dir>/<Title>/Chapter_<n>/`.                                                                                                                                                       | `False`     |
| `--keep-chapters`             | Save each chapter as a separate file (`.pdf`, `.epub`, or `.cbz`).                                                                                                                                                            | `False`     |
| `--no-cleanup`                | Do not delete the temporary directory (`tmp_<hid>/`) after completion.                                                                                                                                                        | `False`     |
| `--no-retry-missed-chapters`  | Disable the end-of-run retry pass for chapters that failed to download or process.                                                                                                                                            | `False`     |
| `--missed-retries INT`        | Number of retry attempts per missed chapter at end of run.                                                                                                                                                                    | `2`         |
| `--missed-log PATH`           | Path for the temporary missed-chapter log (default: `tmp_<hid>/missed_chapters.json`).                                                                                                                                        | _auto_      |
| `--http-timeout FLOAT`        | HTTP timeout in seconds for HTML/AJAX requests.                                                                                                                                                                               | `30`        |
| `--http-max-retries INT`      | Max retries for HTTP requests.                                                                                                                                                                                                | `6`         |
| `--http-backoff-base FLOAT`   | Base seconds for exponential backoff.                                                                                                                                                                                         | `1.0`       |
| `--http-backoff-cap FLOAT`    | Maximum seconds for a single backoff sleep.                                                                                                                                                                                   | `45`        |
| `--coord-dir PATH`            | Directory for cross-process coordination locks (used with `--jobs`).                                                                                                                                                          | `comics/.aio_coord` |
| `--net-min-gap FLOAT`         | Minimum delay (seconds) between request starts across parallel workers.                                                                                                                                                       | `0.25`      |
| `--job-stall-timeout INT`     | Kill & retry a worker if its heartbeat stalls for this many seconds.                                                                                                                                                          | `900`       |
| `--job-hard-timeout INT`      | Kill & retry a worker if total runtime exceeds this many seconds (`0` = disabled).                                                                                                                                            | `0`         |
| `--job-retries INT`           | Number of times to retry a failed/stalled URL in batch mode.                                                                                                                                                                  | `3`         |
| `--job-spawn-gap FLOAT`       | Delay between launching worker processes to avoid burst request patterns.                                                                                                                                                     | `1.5`       |
| `-v, --verbose`               | Enable detailed logging.                                                                                                                                                                                                      | `False`     |
| `-d, --debug`                 | Enable debug-level logging (image processing details).                                                                                                                                                                        | `False`     |

---

## 🔐 Cookie Setup

If authentication is required, export your cookies as a single quoted string:

```bash
export COOKIES='session_token=…; another_cookie=…'
```

Then run:

```bash
python3 aio-dl.py --site mangataro --cookies "$COOKIES" \
  "https://awesome-webtoon.url/series/name"
```

---

## 📖 Examples

1. **Page-layout EPUB**, chapters 1–2, preferred group **Asura**, verbose:

   ```bash
   python3 aio-dl.py \
     --group Asura \
     --chapters "1-2" \
     --format epub \
     --epub-layout page \
     --verbose \
     "https://awesome-webtoon.url/series/name"
   ```

2. **Vertical EPUB**, chapters 1–20, split every 5 chapters, verbose & debug:

   ```bash
   python3 aio-dl.py \
     --chapters "1-20" \
     --format epub \
     --epub-layout vertical \
     --split 5ch \
     --verbose --debug \
     "https://awesome-webtoon.url/series/name"
   ```

3. **CBZ**, chapters 1–2, one chapter per file:

   ```bash
   python3 aio-dl.py \
     --chapters "1-2" \
     --format pdf \
     "https://awesome-webtoon.url/series/name"
   ```

4. **CBZ**, chapters 1–2, one chapter per file:

   ```bash
   python3 aio-dl.py \
     --chapters "1-2" \
     --format cbz \
     --keep-chapters \
     "https://awesome-webtoon.url/series/name"
   ```

4. **PDF**, chapters 1–2, save each chapter separately:

   ```bash
   python3 aio-dl.py \
     --chapters "1-2" \
     --format pdf \
     --keep-chapters \
     "https://awesome-webtoon.url/series/name"
   ```

5. **Raw CBZ**, chapters 1–50 (no resizing/recombining/scaling):

   ```bash
   python3 aio-dl.py \
    --chapters "1-5" \
     --format epub \
     --epub-layout page \
     "https://awesome-webtoon.url/series/name"
   ```

7. **Raw CBZ** (pages copied as-is):

   ```bash
   python3 aio-dl.py \
     --chapters "1-50" \
     --format cbz \
     --no-processing \
     "https://awesome-webtoon.url/series/name"
   ```

8. **Batch download** 3 series at once with 2 parallel workers:

   ```bash
   python3 aio-dl.py \
     --jobs 2 \
     --format epub \
     --save-params \
     "https://site.com/series/alpha" \
     "https://site.com/series/beta" \
     "https://site.com/series/gamma"
   ```

9. **Auto-update** all previously saved series (fetch only new chapters):

   ```bash
   python3 aio-dl.py --update-all --jobs 2
   ```

10. **Open-ended range** — download from chapter 50 to the latest:

    ```bash
    python3 aio-dl.py \
      --chapters "50-" \
      --format epub \
      "https://awesome-webtoon.url/series/name"
    ```

---

## 💡 Tips & Tricks

Here are some handy pointers to get the most out of your workflow:

- 🖥️ **GUI for Everything**
  • Run `python3 gui.py` to open the graphical interface—download, monitor progress, browse your library, and trigger updates without the command line.

- 🔁 **Ongoing Series Auto-Update**
  • Download with `--save-params` once: `python3 aio-dl.py --save-params --format epub "https://..."`.
  • Later, run `python3 aio-dl.py --update-all` (optionally with `--jobs N`) to fetch new chapters for every tracked series automatically.

- ⚡ **Parallel Batch Downloads**
  • Provide multiple URLs and set `--jobs 2` (or more) to download several series at the same time.
  • Workers share a coordinated pacing layer—no extra configuration needed for polite multi-site behavior.

- 🔄 **Ongoing Series Updates (manual)**
  • Use `--no-cleanup` to keep your temporary data around and reuse the same settings.
  • Run again whenever new chapters appear, only new content will be downloaded & processed.
  • You can use `--restore-parameters` to restore input parameters to exactly match the previous one.
 
- 🗂️ **Archiving Originals**  
  • Add `--keep-images` to save every raw page under `comics/<Title>/Chapter_<n>/`.  
  • Useful if you want to re-process images later (different layout, quality, etc.).

- 🧾 **Raw Packaging (No Processing)**  
  • Use `--no-processing` to package pages exactly as downloaded into CBZ/EPUB/PDF.  
  • Skips resize/recombine/scaling; great if you prefer untouched originals inside the final file.  
  • Combine with `--keep-chapters` to generate one raw file per chapter.

- 📑 **Precise Chapter Selection**  
  • Use `--chapters "1-20,21,23-100"` to include exactly the chapters you want (skip fillers or extras).  
  • Supports single numbers, ranges, and comma-separated lists.    
  • Use `--no-partials` to skip fractional chapters such as 1.5, 30.1 or other non decimal exclusive chapters in order to prevent duplicates within your final export.

- ⚙️ **Recover from Failures**  
  • After a crash or network hiccup, rerun with `--restore-parameters`, which will restore all parameters except for your desired file format. 
  *(Please remember that PDFs don't adhere to aspect ratios the same way as EPUBs & CBZs do, so going from pdf to either of them isn't recommended, the other way around works fine.)*

- 🍏 **Apple Books Friendly**  
  • Apple Books can choke on massive EPUBs— personal recommendation: split into ~10-chapter chunks: `--split 10ch`.  
  • Alternatively, split by size: `--split 200MB`.

- 📖 **EPUB Layout Recommendations**  
  • For standard e-readers (Kindle, Kobo): `--epub-layout page`.  
  • For continuous scroll (Apple Books, PocketBook): `--epub-layout vertical`.

- 🎨 **Control Output Quality & Size**  
  • `--quality 60` to drop JPEG quality for smaller files.  
  • `--scaling 80` to downscale pages to 80% of the original processed size.  
  • Combine with `--split` to keep individual file sizes manageable.

- 🏷️ **Best Scanlation Version**  
  • `--group "YourFavGroup"` to prefer a specific scanlation team or even teams by using  `--group "YourFavGroup1, YourFavGroup2"`.  
  • Add `--mix-by-upvote` to pick the highest-upvoted release among your chosen groups.

- 🔍 **Debugging & Verbose Logs**  
  • `-v` / `--verbose` for step-by-step progress.  
  • `-d` / `--debug` for deep image-processing insights (resizing, recombining).

- 📦 **Per-Chapter Files**  
  • Use `--keep-chapters` to save each chapter as its own PDF/EPUB/CBZ alongside the main book.

---

## 📁 Output Structure

*   **Temporary Data**: `tmp_<hid>/` (or `<temp-dir>/tmp_<hid>/` if `--temp-dir` is set)
    *   `run_params.json`: Stores the processing settings for resume functionality.
*   **Final Files**: `<output-dir>/` (default: `comics/`)
    *   Named `<Title>[_Groups]_Ch_<start>-<end>.<format>` (e.g., `My_Awesome_Comic_Asura_Ch_1-5.epub`)
*   **Series tracking** (if `--save-params` is used):
    *   `<output-dir>/<Title>/download_params.json` — saved settings for `--update-all`
    *   `<output-dir>/<Title>/.series_hid` — internal marker used to avoid folder collisions in parallel runs
*   **Raw Images** (if `--keep-images` is used):
    *   `<output-dir>/<Title>/Chapter_<n>/`

```
tmp_<hid>/                              # Temporary workspace
└── run_params.json                     # Saved settings (resume/restore)
comics/
├── <Title>[_Groups]_Ch_a-b.epub/pdf/cbz   # Final build(s)
├── <Title>/
│   ├── download_params.json            # Saved params (--save-params)
│   ├── .series_hid                     # Internal folder marker
│   └── Chapter_<n>/                    # Raw images (--keep-images)
└── .aio_coord/                         # Cross-process coordination (--jobs)
```

- Final files → `comics/` (override with `-o/--output-dir`)
- Use `--no-cleanup` to inspect `tmp_<hid>/` after completion
- Re-run with `--restore-parameters` + new `--format` to reassemble without re-downloading

---

## ⚖️ Disclaimer

This tool is provided strictly for educational purposes and to help you create personal, offline backups of manga to which you have legal access. Please respect the rights of content creators and publishers—unauthorized sharing, piracy, or redistribution of material is prohibited.

---

## 🤝 Contributing

1. Fork the repo  
2. Create a feature branch (`git checkout -b feature/foo`)  
3. Test your update (where possible)  
   - macOS (Apple Silicon): [`test_scripts/python_version_tester.sh`](test_scripts/python_version_tester.sh) automatically spins up per-version virtualenvs after installing missing `python@X.Y` formulae via Homebrew (pyenv is optional but supported).  
   - Otherwise: run targeted unit/download tests that exercise your change.
4. Commit (`git commit -m "Add foo"`)  
5. Push (`git push origin feature/foo`)  
7. Open a Pull Request  

Please follow the existing style and include tests where applicable.

---

## 📖 Changelog

### 03.07.26

**New features:**
- **GUI** (`gui.py`): full Tkinter-based graphical interface with Download, Library, and Output tabs; shows chapter counts, file sizes, and cover images; supports triggering updates from the UI
- **Parallel batch downloads** (`--jobs N`): download multiple series concurrently across coordinated worker processes with shared request pacing, stall detection, and automatic retry
- **Multi-URL CLI**: `comic_url` now accepts multiple URLs in a single invocation
- **`--prompt-urls`**: enter URLs interactively on stdin
- **Library auto-update** (`--save-params` + `--update-all`): save download settings per series and later update all tracked series in one command
- **Output directory control**: `-o/--output-dir`, `--epub-dir`, `--temp-dir` for flexible file placement
- **Missed chapter retry**: automatic end-of-run retry for failed chapters (`--missed-retries`, `--no-retry-missed-chapters`, `--missed-log`)
- **Hardened rate limiting** (`sites/hardening.py`): per-domain configurable request throttling (page, AJAX, image), Cloudflare challenge detection, exponential backoff with jitter
- **HTTP tuning flags**: `--http-timeout`, `--http-max-retries`, `--http-backoff-base`, `--http-backoff-cap`
- **Cross-process coordination flags**: `--coord-dir`, `--net-min-gap`, `--job-stall-timeout`, `--job-hard-timeout`, `--job-retries`, `--job-spawn-gap`
- **Open-ended chapter ranges**: `--chapters "50-"` (ch 50 to latest) or `--chapters "-10"` (up to ch 10)
- **Safe series folder allocation**: prevents parallel workers from mixing files for same-titled series via `.series_hid` markers

**Site fixes & improvements:**
- Removed bato.to and mangapark.net (both shut down); added `mpark.to` mirror
- Asura: per-domain throttling to reduce Cloudflare Turnstile triggers
- FlameComics: fixed image dict ordering, improved build-ID refresh error handling
- Madara-based sites: fallback `ajax/chapters/` POST for sites that render chapter links as `href="#"` (e.g. utoon.net)
- Mangafire: improvements prototyped by [Thundia2](https://github.com/Thundia2)
- Manhuaplus, Manhuaus, Mangadex: various fixes
- Fixed cookie passthrough

## 🙏 Acknowledgements

This project stands on the shoulders of many wonderful open-source tools and libraries—thank you to all the maintainers and contributors who make these possible, thank you:

- **Python** ([CPython](https://github.com/python/cpython)) – The language powering this script  
- **requests** ([psf/requests](https://github.com/psf/requests)) – HTTP for Humans  
- **cloudscraper** ([VeNoMouS/cloudscraper](https://github.com/VeNoMouS/cloudscraper)) – Seamless Cloudflare anti-bot bypass  
- **Beautiful Soup** ([BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/)) – HTML/XML parsing made easy  
- **lxml** ([lxml/lxml](https://github.com/lxml/lxml)) – Fast, feature-rich XML/HTML processing  
- **Pillow** ([python-pillow/Pillow](https://github.com/python-pillow/Pillow)) – The friendly PIL fork for image manipulation  
- **pypdf** ([py-pdf/pypdf](https://github.com/py-pdf/pypdf)) – PDF generation and manipulation  

---

## 📄 License

This project is licensed under the [GNU GPLv3](LICENSE).

---

