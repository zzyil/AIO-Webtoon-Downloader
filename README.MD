# AIO Webtoon & Light Novel Downloader 📚

> [!IMPORTANT]
> **May 2026 update:** cross-site search, multi-source fallback, source-quality ranking, stricter chapter recovery, expanded site handlers, tests, and the Electron desktop app have been integrated.


[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/) [![GNU GPLv3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE) [![Sites](https://img.shields.io/badge/sites-250%2B-green)](docs/SUPPORTED_SITES.MD)

The most versatile, feature-rich & customizable tool to download webtoons (manhwa), manga, comics and light novels from [mangataro.org](https://mangataro.org), [mangafire.to](https://mangafire.to), [asuracomic.net](https://asuracomic.net), [weebcentral.com](https://weebcentral.com), [manganato.gg](https://www.manganato.gg), [comix.to](https://comix.to) & many others, and package them into **PDF**, **EPUB**, **CBZ**, or raw chapter bundles—available as a **command-line tool**, the original Tkinter **graphical interface** (`gui.py`), and a full **Electron desktop app** under `UI-source/`. Features include light-novel aware text handling, intelligent image processing, resumable downloads, parallel batch downloading, library auto-update, cross-site search, multi-source fallback, image-quality source ranking, scanlation group prioritization, and optional chapter/file-size splitting.

![AIO Webtoon & Light Novel Downloader Banner](AIO.png)

### [Follow me on 𝕏](https://x.com/listentovv)  

## 📑 Table of Contents

- [🚀 Features](#-features)  
- [🛠️ Requirements](#%EF%B8%8F-requirements)  
- [🌐 Supported Sites](#-supported-sites)
- [🧰 Installation](#-installation)  
- [🚀 Usage](#-usage)  
- [🔌 REST API](#-rest-api)
- [🔎 Cross-Site Search](#-cross-site-search)
- [🌉 Multi-Source Mode](#-multi-source-mode)
- [🖥️ Desktop UI](#%EF%B8%8F-desktop-ui)
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
- 🖥️ **Electron Desktop App**
  Use the React/Vite/Tailwind app under `UI-source/` for Download, Search, Queue, Logs, Library, and Settings workflows, with packaging scripts for Windows, macOS, and Linux.
- 🔎 **Cross-Site Search**
  Search across search-capable handlers with `--search`, fuzzy title scoring via `rapidfuzz`, quality-seed ranking, language filtering, and JSON output for automation or UI integrations.
- 🌉 **Multi-Source Fallback**
  Pair `--multi-source` with direct URLs or `--search --auto-pick` to pre-fetch alternative chapter lists and transparently fall back per chapter when the primary source is missing, hollowed out, or failing.
- 🏛️ **Official-Publisher Detection**
  MangaDex official-publisher metadata is recognized and used during chapter merging so licensed/official releases are surfaced correctly.
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
- 📊 **Image-Quality Source Ranking**
  Curated quality priors in `sites/quality_seed.json` help search and multi-source fallback prefer visually stronger sources when title matches are close.
- 🛡️ **Hardened Rate Limiting**
  Per-domain request throttling with configurable gaps (page, AJAX, image), Cloudflare challenge detection, and exponential backoff with jitter—shared across parallel workers via file locks.
- ⚡ **Parallel Image Downloads**
  `--image-workers` controls per-chapter image concurrency, while `--prefetch-image-workers` can download chapter N+1 while chapter N is being encoded.
- 🔄 **Resumable Downloads**
  Automatically picks up where it left off if interrupted.
- 🎯 **Per-Chapter Strict Mode**
  Missing-page detection, chapter deadlines, host-poison thresholds, and inline chapter retries prevent silently incomplete chapters from landing in final books.
- 🩹 **Missed Chapter Retry**
  After a run completes, automatically retries any chapters that failed to download or process, with configurable retry count (`--missed-retries`).
- ✂️ **Book Splitting**
  Split large downloads by file size (e.g., `400MB`) or chapter count (e.g., `10ch`).
- 💾 **Keep Originals**
  Optionally retain raw images and per-chapter files.
- 🧪 **Regression Tests**
  Pytest coverage now includes chapter merging, search orchestration, and key site handlers under `tests/`.

---

## 🛠️ Requirements


- Python
  - Officially supported: 3.8+
  - Best-effort (tested): 3.7 (may require building Pillow from source on some platforms)
  - Not supported: 3.6 and Python 2.x
- OS: macOS, Linux, Windows
- Node.js 20+ (only for the Electron desktop UI)
- Python packages (installed via requirements.txt)
  - beautifulsoup4
  - cloudscraper
  - lxml
  - Pillow
  - pypdf
  - requests
  - fastapi / uvicorn — REST API server
  - impit — Chrome/Firefox TLS impersonation for bot-protected sites (handles zstd/brotli)
  - zendriver — Cloudflare Managed Challenge solver for CF-protected sites
  - patchright — Playwright-compatible browser automation for MangaFire VRF generation and site probes (`python -m patchright install chromium` after pip install)
  - rapidfuzz — cross-site title matching
  - pywidevine / cryptography — Kagane DRM support
  - curl_cffi — MangaFire fast image-download path with HTTP/2 + Chrome-like TLS fingerprints
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

| Python | PDF | EPUB (page) | EPUB (vertical) | CBZ | Resume/Restore | Split (size/ch) | Keep images | Group priority/mix-by-upvote | Search | Multi-source | Browser automation |
| ------ | --- | ----------- | --------------- | --- | -------------- | --------------- | ----------- | ---------------------------- | ------ | ------------ | ------------------ |
| 3.13   | ✅  | ✅          | ✅              | ✅  | ✅             | ✅              | ✅          | ✅                           | ✅     | ✅           | ✅                 |
| 3.12   | ✅  | ✅          | ✅              | ✅  | ✅             | ✅              | ✅          | ✅                           | ✅     | ✅           | ✅                 |
| 3.11   | ✅  | ✅          | ✅              | ✅  | ✅             | ✅              | ✅          | ✅                           | ✅     | ✅           | ✅                 |
| 3.10   | ✅  | ✅          | ✅              | ✅  | ✅             | ✅              | ✅          | ✅                           | ✅     | ✅           | ✅                 |
| 3.9    | ✅  | ✅          | ✅              | ✅  | ✅             | ✅              | ✅          | ✅                           | ✅     | ✅           | ✅                 |
| 3.8    | ✅  | ✅          | ✅              | ✅  | ✅             | ✅              | ✅          | ✅                           | ✅     | ✅           | ✅                 |
| 3.7    | ✅  | ✅          | ✅              | ✅  | ✅             | ✅              | ✅          | ✅                           | ⚠️     | ⚠️           | ⚠️                 |
| 3.6    | ❌  | ❌          | ❌              | ❌  | ❌             | ❌              | ❌          | ❌                           | ❌     | ❌           | ❌                 |
| 2.7    | ❌  | ❌          | ❌              | ❌  | ❌             | ❌              | ❌          | ❌                           | ❌     | ❌           | ❌                 |

Notes:
- Cloudscraper is optional. If it’s not available or fails to initialize, the downloader
  automatically falls back to requests.Session.
- The HTML parser prefers lxml if installed, and falls back to the built-in html.parser.
- Search and multi-source mode require `rapidfuzz`; browser automation paths require Python 3.8+ in practice.
- On macOS (Apple Silicon), Python 3.7 may need system libraries to build Pillow.

---



## 🧰 Installation

### CLI

```bash
git clone https://github.com/zzyil/AIO-Webtoon-Downloader.git
cd AIO-Webtoon-Downloader
python3 -m pip install -r requirements.txt
python3 -m patchright install chromium    # one-time, for MangaFire/search probes
```

Development/test dependencies:

```bash
python3 -m pip install -r requirements-dev.txt
```

### Desktop UI (Electron)

```bash
cd UI-source
npm install
npm run electron:dev       # development mode with hot reload
```

Build installers:

```bash
npm run dist:win           # Windows NSIS installer
npm run dist:mac           # macOS DMG/ZIP
npm run dist:linux         # Linux AppImage
```

The `electron:build` and `dist:*` scripts run `scripts/prepare-src.js`, bundle the Python backend as Electron `extraResources`, and write installers under `UI-source/release/`.

---

## 🌐 Supported Sites

For all supported sites, check [docs/SUPPORTED_SITES.MD](docs/SUPPORTED_SITES.MD)

| Site | Notes | Integration Status |
| ----- | ----- | ----- |
| [asurascans.com](https://asurascans.com) | Alternative URLs: [asuracomic.net](https://asuracomic.net), [asurascans.net](https://asurascans.net) | recent check 05.20.26 ✅ |
| [Atsumaru / atsu.moe](https://atsu.moe) | WebP-native, custom PDF encoding | recent check 05.20.26 ✅ |
| [**comix.to**](https://comix.to) | Supports multiple scanlation groups | recent check 05.20.26 ✅ |
| [fanfox.net](https://fanfox.net) | Mirrors: [mangafox.la](https://mangafox.la) and [mangahere.cc](https://mangahere.cc) | recent check 05.20.26 ✅ |
| [flamecomics.xyz](https://flamecomics.xyz) | | recent check 05.20.26 ✅ |
| [galaxymanga.io](https://galaxymanga.io) | | recent check 05.20.26 ✅ |
| [kagane.org](https://kagane.org) | DRM-protected, requires Widevine `.wvd` file. See [docs/Widevine.md](docs/Widevine.md) | updated API (v2) 03.08.26 ⚠️ |
| [kappabeast.com](https://kappabeast.com) | Dedicated Strapi API handler; CF-protected when needed | recent check 05.20.26 ✅ |
| [kingofshojo.com](https://kingofshojo.com) | CF-protected; solved via zendriver with cached session reuse | recent check 05.20.26 ✅ |
| [likemanga.ink](https://likemanga.ink) / [likemanga.in](https://likemanga.in) | CF-protected; solved via zendriver where needed | `.ink` chapter filter broken; `.in` recent check 11.26.25 ⚠️ |
| [mangabuddy.com](https://mangabuddy.com) | Platform went down May 2026 | offline/deprecated ⚠️ |
| [**mangadex.org**](https://mangadex.org) | Supports scanlation group selection and official-publisher detection | recent check 05.20.26 ✅ |
| [**mangafire.to**](https://mangafire.to) | Supports multiple languages; Patchright VRF bridge; async image path | recent check 05.20.26 ✅ |
| [mangahub.io](https://mangahub.io) | | recent check 05.20.26 ✅ |
| [MangaKakalot / MangaNato](https://www.manganato.gg) | JSON API + impit for zstd; See [mirror list](docs/SUPPORTED_SITES.MD) | recent check 05.20.26 ✅ |
| [mangataro.org](https://mangataro.org) | Supports scanlation group selection | recent check 05.20.26 ✅ |
| [manhuafast.net](https://manhuafast.net) | Mirror: [manhuafast.com](https://manhuafast.com) | recent check 05.20.26 ✅ |
| [manhuaplus.com](https://manhuaplus.com) | | recent check 05.20.26 ✅ |
| [manhuaus.com](https://manhuaus.com) | | recent check 05.20.26 ✅ |
| [manhwaread.com / mgread.io](https://mgread.io) | Unique Base64 image encoding support; NSFW content | recent check 05.20.26 ✅ |
| [ragescans.com](https://ragescans.com) | Platform marked offline May 2026 | offline ⚠️ |
| [razure.org](https://razure.org) | light-novel support | recent check 11.26.25 ✅ |
| [utoon.net](https://utoon.net) | | recent check 05.20.26 ✅ |
| [violetscans.org](https://violetscans.org) | | recent check 05.20.26 ✅ |
| [**weebcentral.com**](https://weebcentral.com) | Supports scanlation group selection; impit fallback for zstd | recent check 05.20.26 ✅ |
| [webtoon.xyz](https://www.webtoon.xyz) | CF-protected; solved via zendriver | recent check 05.20.26 ✅ |
| [zeroscann.com](https://zeroscann.com) | Migrated from zscans.com REST API to HTML scraping; intermittent 525 upstream errors | recent check 05.20.26 ⚠️ |
| ~~bato.to / mangapark~~ | Shut down permanently | ⛓️‍💥 removed |


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

**Electron desktop UI:**
```bash
cd UI-source
npm run electron:dev
```

**Cross-site search with automatic fallback:**
```bash
python3 aio-dl.py --search "Witch Hat Atelier" --auto-pick --multi-source
```

**List chapters as JSON without downloading:**
```bash
python3 aio-dl.py --list-chapters "https://awesome-webtoon.url/series/name"
```

Run `python3 aio-dl.py --help` for the full option list.

---

## 🔌 REST API

The downloader includes a FastAPI-based REST backend (`api.py`) allowing you to interact with supported sites programmatically to retrieve metadata, active chapters, and image contents. (Thanks to [@norphiil](https://github.com/norphiil) for the REST API implementation!).

**Start the API Server:**
```bash
uvicorn api:app --host 0.0.0.0 --port 8000
# or:
python3 aio-dl.py --serve --api-host 0.0.0.0 --api-port 8000
```

**Available Endpoints:**

- `GET /api/handlers`  
  List all supported site handlers and their corresponding domains.
- `GET /api/info?url=URL[&site=NAME]`  
  Fetch comic metadata (title, author, cover image, status).
- `GET /api/chapters?url=URL[&site=NAME][&language=en]`  
  Retrieve a JSON list of available chapters for a comic.
- `GET /api/chapter_images?url=URL&chapter_id=ID[&site=NAME]`  
  Get the exact image URLs for a specific chapter ID.
- `GET /api/download_image?url=IMG_URL`  
  Downloads an image behind site protections and returns a local cache URL (`/api/temp/...`) for safe consumption.

> **Note**: Cross-Origin Resource Sharing (CORS) is enabled by default for all origins, and intermediate images older than 1 hour are auto-purged from the temp directory.

---

## 🔎 Cross-Site Search

Use `--search "<title>"` to query search-capable handlers, score title matches with `rapidfuzz`, and return ranked JSON candidates with source site, URL, language, match score, and image-quality prior.

```bash
python3 aio-dl.py --search "Witch Hat Atelier"
```

Add `--auto-pick` to continue into the normal download pipeline with the top-ranked result:

```bash
python3 aio-dl.py --search "Witch Hat Atelier" --auto-pick --format epub
```

Useful search flags:

| Flag | Default | Purpose |
| ---- | ------- | ------- |
| `--search-language CODE` | `--language` | Filter supported sites by language; use `all` to disable |
| `--search-parallelism INT` | `6` | Number of sites searched in parallel |
| `--search-timeout FLOAT` | `20.0` | Per-site timeout |
| `--search-min-match FLOAT` | `0.55` | Drop fuzzy title matches below this score |
| `--search-json` | `False` | Print candidate JSON even when `--auto-pick` is used |
| `--seeded-only` | `False` | Search only handlers listed in `sites/quality_seed.json` |

---

## 🌉 Multi-Source Mode

Some sources have incomplete catalogs, regional gaps, DMCA removals, or intermittent CDN failures. `--multi-source` pre-fetches alternative chapter lists, aligns them through `sites/chapter_merger.py`, and falls back per chapter when the primary source fails.

```bash
python3 aio-dl.py --search "Witch Hat Atelier" --auto-pick --multi-source
```

You can also use it with a direct URL:

```bash
python3 aio-dl.py --multi-source "https://mangadex.org/title/..."
```

Useful multi-source flags:

| Flag | Default | Purpose |
| ---- | ------- | ------- |
| `--multi-source` | `False` | Enable cross-site fallback |
| `--multi-source-quality-min FLOAT` | `0.65` | Minimum source-quality seed/probe score for fallback candidates |
| `--no-collapse-splits` | `False` | Keep decimal split chapters counted separately in diagnostics |
| `--multi-source-prefetched FILE` | _none_ | Reuse alternatives already discovered by the UI/search layer |

---

## 🖥️ Desktop UI

The original Tkinter GUI remains available as `python3 gui.py`. The merged desktop app adds a full Electron + React + Vite + Tailwind interface under `UI-source/`.

```bash
cd UI-source
npm install
npm run electron:dev
```

Electron tabs:

- **Download** — single URL or paste-multiple download setup.
- **Search** — cross-site search results with score, source rank, and coverage information.
- **Queue** — running and queued jobs with pause/resume/cancel controls.
- **Logs** — per-job stdout/stderr streaming from `aio-dl.py`.
- **Library** — manage downloaded books on disk.
- **Settings** — Python interpreter, default paths, group preferences, and theme.

Installer builds use `npm run dist:win`, `npm run dist:mac`, or `npm run dist:linux`, and output to `UI-source/release/`.

---

## ⚙️ Options

| Option                        | Description                                                                                                                                                                                                                  | Default     |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- |
| `COMIC_URL [...]`             | One or more comic/manga URLs.                                                                                                                                                                                                | _required_  |
| `--prompt-urls`               | Prompt for multiple URLs on stdin (one per line). Finish with an empty line.                                                                                                                                                 | `False`     |
| `--jobs INT`                  | Download multiple URLs concurrently using separate worker processes.                                                                                                                                                          | `1`         |
| `--search QUERY`              | Search across search-capable sites and print ranked JSON candidates; with `--auto-pick`, download the top result.                                                                                                           | _none_      |
| `--auto-pick`                 | With `--search`, select the top-ranked candidate and run the normal download pipeline.                                                                                                                                        | `False`     |
| `--search-language STRING`    | Language filter for `--search`; use `all` to disable.                                                                                                                                                                        | `en`        |
| `--search-parallelism INT`    | Number of sites probed in parallel during search.                                                                                                                                                                            | `6`         |
| `--search-timeout FLOAT`      | Per-site search timeout in seconds.                                                                                                                                                                                          | `20.0`      |
| `--search-min-match FLOAT`    | Drop search hits below this fuzzy title-match score (`0.0`–`1.0`).                                                                                                                                                           | `0.55`      |
| `--search-json`               | Force JSON candidate output even with `--auto-pick`.                                                                                                                                                                         | `False`     |
| `--multi-source`              | Enable cross-site alternative chapter fallback.                                                                                                                                                                              | `False`     |
| `--multi-source-quality-min FLOAT` | Minimum source-quality score for multi-source alternatives.                                                                                                                                                              | `0.65`      |
| `--seeded-only`               | Restrict search and direct-URL alternative discovery to handlers listed in `sites/quality_seed.json`.                                                                                                                        | `False`     |
| `--multi-source-prefetched PATH` | Use pre-discovered alternative sources from JSON, usually from the Electron Search tab.                                                                                                                                   | _none_      |
| `--save-params`               | Save download parameters to the series output folder for future `--update-all` runs.                                                                                                                                         | `False`     |
| `--update-all`                | Scan `--output-dir` for previously saved parameters and download new chapters for every tracked series.                                                                                                                       | `False`     |
| `--scan-library`              | Scan `--output-dir` and print library state as JSON.                                                                                                                                                                         | `False`     |
| `--serve`                     | Start the FastAPI REST server instead of downloading.                                                                                                                                                                        | `False`     |
| `--api-host STRING`           | Host for `--serve`.                                                                                                                                                                                                          | `127.0.0.1` |
| `--api-port INT`              | Port for `--serve`.                                                                                                                                                                                                          | `8000`      |
| `-o, --output-dir PATH`       | Directory to place the final outputs.                                                                                                                                                                                        | `manga`     |
| `--epub-dir PATH`             | Optional override directory specifically for EPUB outputs.                                                                                                                                                                    | _none_      |
| `--temp-dir PATH`             | Optional override base directory for temporary processing folders.                                                                                                                                                            | _none_      |
| `--site STRING`               | Explicitly select the site handler (auto-detected by URL when omitted).                                                                                                                                                      | _auto_      |
| `--cookies STRING`            | HTTP cookie string for restricted content (e.g. `key1=val1; key2=val2`)                                                                                                                                                     | `""`        |
| `--group STRING...`           | Prefer the specified scanlation groups (comma-separated or multiple args). If a chapter has no matching group, the downloader falls back to the highest-upvoted available release and reports it at the end by default.    | `[]`        |
| `--mix-by-upvote`             | When multiple specified `--group` entries exist for the same chapter, pick the matched version with the highest upvotes.                                                                                                    | `False`     |
| `--no-group-fallback`         | When used with `--group`, disable fallback and skip chapters that do not contain any requested group.                                                                                                                       | `False`     |
| `--no-partials`               | Skip fractional chapters (e.g. 1.5).                                                                                                                                                                                          | `False`     |
| `--chapters STRING`           | Chapter filter: `all`, single (`5`), range (`1-10`), list/ranges (`1,3,5-7`), or open-ended (`-10`, `5-`).                                                                                                                   | `all`       |
| `--list-chapters`             | Fetch the chapter list and series metadata as JSON, then exit.                                                                                                                                                               | `False`     |
| `--download-volumes`          | Download volumes instead of chapters (if supported by the site).                                                                                                                                                             | `False`     |
| `--language STRING`           | Language code (e.g. `en`, `ja`).                                                                                                                                                                                              | `en`        |
| `--metadata-source {none,anilist}` | Enrich tags + description + country/format from an external API. `anilist` queries the free [AniList GraphQL API](https://graphql.anilist.co) (90 req/min, no auth) and writes ranked, categorized, spoiler-aware tags + a normalized description into ComicInfo.xml (`<Tags>`, `<SpoilerTags>`, `<TagsExtended>`, `<CountryOfOrigin>`, `<MediaFormat>`, `<AnilistId>`, `<MalId>`) and `.aio_series.json` for the library UI. The matched AniList + MAL IDs are ID-cached so resume / `--update-all` skip the fuzzy title-match step. Honors `AIO_METADATA_SOURCE` env var. | `none`      |
| `--metadata-tag-min-rank INT` | When `--metadata-source=anilist`, only include tags whose AniList relevance rank (0-100) meets this threshold. `0` includes everything; `80` keeps only the very-relevant.                                                    | `50`        |
| `--metadata-refresh`          | Force re-fetch from the configured `--metadata-source` even when an external ID is already cached in `.aio_series.json`. Use to backfill an existing library or after AniList retags a series.                                | `False`     |
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
| `--no-final-file`             | With `--keep-chapters`, skip building the combined book.                                                                                                                                                                      | `False`     |
| `--build-final-file`          | Build a final book from existing per-chapter files without downloading.                                                                                                                                                       | `False`     |
| `--no-cleanup`                | Do not delete the temporary directory (`tmp_<hid>/`) after completion.                                                                                                                                                        | `False`     |
| `--image-workers INT`         | Parallel image downloads inside one chapter.                                                                                                                                                                                  | `3`         |
| `--prefetch-image-workers INT` | Prefetch chapter N+1 while encoding chapter N; `0` disables.                                                                                                                                                                 | `-1`        |
| `--mangafire-image-concurrency INT` | Concurrent MangaFire image fetches for the `curl_cffi` async downloader.                                                                                                                                              | `8`         |
| `--mangafire-vrf-prefetch-depth INT` | Number of MangaFire chapters ahead to queue VRF prefetching.                                                                                                                                                         | `4`         |
| `--mangafire-vrf-parallel INT` | Opt-in concurrent MangaFire VRF capture.                                                                                                                                                                                     | `1`         |
| `--no-retry-missed-chapters`  | Disable the end-of-run retry pass for chapters that failed to download or process.                                                                                                                                            | `False`     |
| `--missed-retries INT`        | Number of retry attempts per missed chapter at end of run.                                                                                                                                                                    | `2`         |
| `--missed-log PATH`           | Path for the temporary missed-chapter log (default: `tmp_<hid>/missed_chapters.json`).                                                                                                                                        | _auto_      |
| `--chapter-deadline-seconds INT` | Per-chapter wall-clock budget before retry handling.                                                                                                                                                                      | `90`        |
| `--chapter-host-poison-threshold INT` | Treat a chapter as failed if too many URLs to one host fail during that chapter.                                                                                                                                  | `5`         |
| `--inline-chapter-retries INT` | Retry a chapter inline when pages are missing before giving up.                                                                                                                                                              | `2`         |
| `--inline-chapter-backoff FLOAT` | Base backoff for inline chapter retries.                                                                                                                                                                                   | `30`        |
| `--http-timeout FLOAT`        | HTTP timeout in seconds for HTML/AJAX requests.                                                                                                                                                                               | `30`        |
| `--http-max-retries INT`      | Max retries for HTTP requests.                                                                                                                                                                                                | `6`         |
| `--http-backoff-base FLOAT`   | Base seconds for exponential backoff.                                                                                                                                                                                         | `1.0`       |
| `--http-backoff-cap FLOAT`    | Maximum seconds for a single backoff sleep.                                                                                                                                                                                   | `45`        |
| `--coord-dir PATH`            | Directory for cross-process coordination locks (used with `--jobs`).                                                                                                                                                          | `manga/.aio_coord` |
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

1. **Find and download via cross-site search** with fallback enabled:

   ```bash
   python3 aio-dl.py \
     --search "Witch Hat Atelier" \
     --auto-pick \
     --multi-source \
     --format epub \
     --epub-layout page \
     --quality 92
   ```

2. **Page-layout EPUB**, chapters 1–2, preferred group **Asura**, verbose:

   ```bash
   python3 aio-dl.py \
     --group Asura \
     --chapters "1-2" \
     --format epub \
     --epub-layout page \
     --verbose \
     "https://awesome-webtoon.url/series/name"
   ```

3. **Vertical EPUB**, chapters 1–20, split every 5 chapters, verbose & debug:

   ```bash
   python3 aio-dl.py \
     --chapters "1-20" \
     --format epub \
     --epub-layout vertical \
     --split 5ch \
     --verbose --debug \
     "https://awesome-webtoon.url/series/name"
   ```

4. **PDF**, chapters 1–2, combined into one file:

   ```bash
   python3 aio-dl.py \
     --chapters "1-2" \
     --format pdf \
     "https://awesome-webtoon.url/series/name"
   ```

5. **CBZ**, chapters 1–2, one chapter per file:

   ```bash
   python3 aio-dl.py \
     --chapters "1-2" \
     --format cbz \
     --keep-chapters \
     "https://awesome-webtoon.url/series/name"
   ```

6. **PDF**, chapters 1–2, save each chapter separately:

   ```bash
   python3 aio-dl.py \
     --chapters "1-2" \
     --format pdf \
     --keep-chapters \
     "https://awesome-webtoon.url/series/name"
   ```

7. **Page-layout EPUB**, chapters 1–5:

   ```bash
   python3 aio-dl.py \
     --chapters "1-5" \
     --format epub \
     --epub-layout page \
     "https://awesome-webtoon.url/series/name"
   ```

8. **Raw CBZ** (pages copied as-is):

   ```bash
   python3 aio-dl.py \
     --chapters "1-50" \
     --format cbz \
     --no-processing \
     "https://awesome-webtoon.url/series/name"
   ```

9. **Batch download** 3 series at once with 2 parallel workers:

   ```bash
   python3 aio-dl.py \
     --jobs 2 \
     --format epub \
     --save-params \
     "https://site.com/series/alpha" \
     "https://site.com/series/beta" \
     "https://site.com/series/gamma"
   ```

10. **Auto-update** all previously saved series (fetch only new chapters):

   ```bash
   python3 aio-dl.py --update-all --jobs 2
   ```

11. **Open-ended range** — download from chapter 50 to the latest:

    ```bash
    python3 aio-dl.py \
      --chapters "50-" \
      --format epub \
      "https://awesome-webtoon.url/series/name"
    ```

12. **List chapters as JSON** without downloading:

    ```bash
    python3 aio-dl.py --list-chapters "https://awesome-webtoon.url/series/name"
    ```

---

## 💡 Tips & Tricks

Here are some handy pointers to get the most out of your workflow:

- 🖥️ **GUI for Everything**
  • Run `python3 gui.py` to open the graphical interface—download, monitor progress, browse your library, and trigger updates without the command line.
  • Run `npm run electron:dev` inside `UI-source/` for the newer desktop app with Search, Queue, Logs, Library, and Settings tabs.

- 🔎 **Search Before Downloading**
  • Use `--search "Series Title"` when you are not sure which site has the best source.
  • Add `--auto-pick --multi-source` to let the downloader pick the strongest match and pre-fetch alternatives before downloading.

- 🌉 **DMCA or Missing Chapter Recovery**
  • Use `--multi-source` for hollowed-out catalogs. The downloader compares chapter lists across sites and falls back per chapter when the primary source is missing content or fails.

- 🔁 **Ongoing Series Auto-Update**
  • Download with `--save-params` once: `python3 aio-dl.py --save-params --format epub "https://..."`.
  • Later, run `python3 aio-dl.py --update-all` (optionally with `--jobs N`) to fetch new chapters for every tracked series automatically.

- ⚡ **Parallel Batch Downloads**
  • Provide multiple URLs and set `--jobs 2` (or more) to download several series at the same time.
  • Workers share a coordinated pacing layer—no extra configuration needed for polite multi-site behavior.
  • For image-level parallelism, tune `--image-workers`; if a CDN starts throwing 5xx storms, lower it or set `--prefetch-image-workers 0`.

- 🔄 **Ongoing Series Updates (manual)**
  • Use `--no-cleanup` to keep your temporary data around and reuse the same settings.
  • Run again whenever new chapters appear, only new content will be downloaded & processed.
  • You can use `--restore-parameters` to restore input parameters to exactly match the previous one.
 
- 🗂️ **Archiving Originals**  
  • Add `--keep-images` to save every raw page under `manga/<Title>/Chapter_<n>/`.
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
  • `--group "YourFavGroup"` to prefer that scanlation team, or use `--group "YourFavGroup1, YourFavGroup2"` to allow several teams in priority order. If a chapter is missing from those groups, the downloader falls back to the highest-upvoted available release and reports it at the end by default.  
  • Add `--mix-by-upvote` to pick the highest-upvoted release among the groups you allowed for that chapter.
  • Add `--no-group-fallback` if you want strict group-only downloads and would rather skip missing chapters.

- 🔍 **Debugging & Verbose Logs**  
  • `-v` / `--verbose` for step-by-step progress.  
  • `-d` / `--debug` for deep image-processing insights (resizing, recombining).

- 📦 **Per-Chapter Files**  
  • Use `--keep-chapters` to save each chapter as its own PDF/EPUB/CBZ alongside the main book.
  • Add `--no-final-file` if you only want those per-chapter files and do not want a combined book.

---

## 📁 Output Structure

*   **Temporary Data**: `tmp_<hid>/` (or `<temp-dir>/tmp_<hid>/` if `--temp-dir` is set)
    *   `run_params.json`: Stores the processing settings for resume functionality.
*   **Final Files**: `<output-dir>/` (default: `manga/`)
    *   Named `<Title>[_Groups]_Ch_<start>-<end>.<format>` (e.g., `My_Awesome_Comic_Asura_Ch_1-5.epub`)
*   **Series tracking** (if `--save-params` is used):
    *   `<output-dir>/<Title>/download_params.json` — saved settings for `--update-all`
    *   `<output-dir>/<Title>/.series_hid` — internal marker used to avoid folder collisions in parallel runs
*   **Raw Images** (if `--keep-images` is used):
    *   `<output-dir>/<Title>/Chapter_<n>/`

```
tmp_<hid>/                              # Temporary workspace
└── run_params.json                     # Saved settings (resume/restore)
manga/
├── <Title>[_Groups]_Ch_a-b.epub/pdf/cbz   # Final build(s)
├── <Title>/
│   ├── download_params.json            # Saved params (--save-params)
│   ├── .series_hid                     # Internal folder marker
│   └── Chapter_<n>/                    # Raw images (--keep-images)
└── .aio_coord/                         # Cross-process coordination (--jobs)
```

- Final files → `manga/` (override with `-o/--output-dir`)
- Use `--no-cleanup` to inspect `tmp_<hid>/` after completion
- Re-run with `--restore-parameters` + new `--format` to reassemble without re-downloading

---

## ⚖️ Disclaimer

This tool is provided strictly for educational purposes and to help you create personal, offline backups of manga to which you have legal access. Many supported sites are unauthorized aggregators of licensed content; MangaDex official-publisher detection helps surface licensed English releases when metadata is available. Please respect the rights of content creators and publishers—unauthorized sharing, piracy, or redistribution of material is prohibited.

---

## 🤝 Contributing

1. Fork the repo  
2. Create a feature branch (`git checkout -b feature/foo`)  
3. Test your update (where possible)  
   - macOS (Apple Silicon): [`test_scripts/python_version_tester.sh`](test_scripts/python_version_tester.sh) automatically spins up per-version virtualenvs after installing missing `python@X.Y` formulae via Homebrew (pyenv is optional but supported).  
   - Otherwise: run targeted unit/download tests that exercise your change (`python3 -m pytest tests/` when dev dependencies are installed).
   - For new site handlers, add or update focused tests under `tests/` where practical.
4. Commit (`git commit -m "Add foo"`)  
5. Push (`git push origin feature/foo`)  
7. Open a Pull Request  

Please follow the existing style and include tests where applicable.
New handlers should be registered in `sites/__init__.py`; if they support title search, implement `search()` so they join cross-site discovery and multi-source fallback.

---

## 📖 Changelog

> Full changelog: [docs/CHANGELOG.md](docs/CHANGELOG.md)

### 05.24.26

**May 2026 integration:**
- Integrated the Electron desktop UI, cross-site search, multi-source fallback, image-quality ranking, official-publisher metadata, expanded site handlers, regression tests, and packaging/release tooling.

## 🙏 Acknowledgements

This project stands on the shoulders of many wonderful open-source tools and libraries—thank you to all the maintainers and contributors who make these possible, thank you:

- **Python** ([CPython](https://github.com/python/cpython)) – The language powering this script  
- **requests** ([psf/requests](https://github.com/psf/requests)) – HTTP for Humans  
- **cloudscraper** ([VeNoMouS/cloudscraper](https://github.com/VeNoMouS/cloudscraper)) – Seamless Cloudflare anti-bot bypass  
- **Beautiful Soup** ([BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/)) – HTML/XML parsing made easy  
- **lxml** ([lxml/lxml](https://github.com/lxml/lxml)) – Fast, feature-rich XML/HTML processing  
- **Pillow** ([python-pillow/Pillow](https://github.com/python-pillow/Pillow)) – The friendly PIL fork for image manipulation  
- **pypdf** ([py-pdf/pypdf](https://github.com/py-pdf/pypdf)) – PDF generation and manipulation
- **FastAPI** ([fastapi/fastapi](https://github.com/fastapi/fastapi)) – REST API backend
- **impit** – Chrome/Firefox TLS fingerprint impersonation; handles zstd/brotli compression without launching a browser
- **zendriver** – CDP-based Chrome automation with built-in Cloudflare challenge solving
- **Patchright / Playwright** ([microsoft/playwright-python](https://github.com/microsoft/playwright-python)) – Browser automation for MangaFire VRF generation and site probes
- **rapidfuzz** ([rapidfuzz/RapidFuzz](https://github.com/rapidfuzz/RapidFuzz)) – Fast fuzzy matching for cross-site search
- **curl_cffi** ([lexiforest/curl_cffi](https://github.com/lexiforest/curl_cffi)) – HTTP/2 + browser-like TLS fingerprints for fast image paths
- **pywidevine** ([devine-dl/pywidevine](https://github.com/devine-dl/pywidevine)) – Widevine DRM support for Kagane
- **Electron** ([electron/electron](https://github.com/electron/electron)) – Desktop app shell
- **React** ([facebook/react](https://github.com/facebook/react)), **Vite** ([vitejs/vite](https://github.com/vitejs/vite)), **Tailwind CSS** ([tailwindlabs/tailwindcss](https://github.com/tailwindlabs/tailwindcss)), and **lucide-react** ([lucide-icons/lucide](https://github.com/lucide-icons/lucide)) – Electron renderer stack

---

## 📄 License

This project is licensed under the [GNU GPLv3](LICENSE).

---
