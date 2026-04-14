# 📖 Changelog

All notable changes to AIO Webtoon & Light Novel Downloader are documented here.
Only the latest entry is shown in the [README](../README.MD); the full history lives in this file.

---

### 04.14.26

**Volume downloading (`sites/`, `aio-dl.py`, `gui.py`, `api.py`):**
- Added a `--download-volumes` flag to the CLI and a matching checkbox in the GUI to fetch volumes instead of chapters on supported sites (like MangaFire)
- The existing `--chapters` range filter applies natively to volumes when this mode is toggled (e.g. `--chapters 1-5` downloads volumes 1-5)
- Expanded the `/api/chapters` REST endpoint to support an optional `?type=volume` parameter

---

### 04.09.26

**Group selection safety (`sites/base.py`, `aio-dl.py`):**
- Normalized scanlation-group matching so case, spacing, punctuation, and common `official` label variants no longer cause false mismatches
- If a requested scanlation group is not present for a chapter, the downloader now falls back to the highest-upvoted available release and reports the affected chapters at the end instead of switching silently
- Added `--no-group-fallback` for users who want strict group-only downloads with skipped missing chapters
- Updated `--mix-by-upvote` so it only compares releases from the groups explicitly allowed by the user
- Added verbose reporting plus an end-of-run notice for chapters that fell back to another scanlation group

**Tests & docs:**
- Added focused unit tests covering normalized group matching, `official` aliases, fallback behavior, strict no-fallback behavior, and `mix-by-upvote` filtering
- Updated CLI/README help text to document both the default fallback behavior and the new strict no-fallback option

---

### 04.04.26

**New features:**
- **REST API** (`api.py`): Implemented FastAPI-based REST backend allowing users to retrieve comic metadata, active chapters, and image contents programmatically. Includes auto-cleanup logic and Cloudflare bypass capability sharing. (Thanks to [@norphiil](https://github.com/norphiil)!)
- **Comix token capture & API fix**: Resolved the "0 chapters" download failure by implementing a Playwright-based URL token capture. This automatically bypasses `comix.to`'s newly obfuscated JavaScript API requirements by generating the correct `time` and `_` query parameters, ensuring chapter lists and images load reliably again.

**Fixes:**
- **MangaFire VRF**: Fixed a crash caused by stale running event loops blocking Playwright's Sync API initialization in the `mangafire_vrf_simple.py` generator. (Thanks to XCSTech!)

---

### 03.21.26

**AsuraScans (`sites/asura.py`) — full rewrite:**
- Rewrote handler for the new asurascans.com site structure; added `asurascans.com` domain
- Switched URL path from `/series/` to `/comics/`; old `self.__next_f.push` flight-data parsing removed
- Chapter list now extracted from HTML `<a>` tags; images from embedded RSC (React Server Components) props JSON with regex CDN fallback
- Verified image extraction matches Chrome browser (21, 17, 14 images across 3 test chapters — all exact matches)

**zendriver Cloudflare cookie fix (`sites/crawlee_utils.py`):**
- Fixed `KeyError: 'sameParty'` crash in zendriver's CDP cookie retrieval — Chrome removed the deprecated `sameParty` field, but zendriver's `Cookie.from_json()` still required it. This caused `browser.cookies.get_all()` to silently hang, returning zero cookies and leaving the Chrome process open indefinitely
- Added module-level monkey-patch for `Cookie.from_json` that defaults `sameParty` to `False`
- Added overall `asyncio.wait_for` timeout (45s) and Chrome process force-kill to `_solve_cf_async` to prevent abandoned browser windows

**Comix (`sites/comix.py`):**
- Fixed "No chapters selected" bug — the language filter used `item.get("language") != language`, which silently dropped chapters that had no `language` field (since `None != "en"`)
- Language comparison is now case-insensitive and matches long-form names (e.g. `"English"` matches `"en"`)
- Added CF-aware requests (`_cf_aware_request`) — all API and page fetches now auto-fall back to a zendriver CF session on Cloudflare 403, since comix.to added Cloudflare protection

---

### 03.08.26

**Anti-bot infrastructure (`sites/crawlee_utils.py` — new module):**
- Added `fetch_html_impit()` — Chrome/Firefox TLS impersonation via `impit`; handles zstd/brotli compression transparently without launching a browser
- Added `get_cf_session()` — zendriver-based Cloudflare challenge solver; launches visible Chrome once, solves CF Managed Challenge, captures `cf_clearance` cookie (~10s), then reuses a lightweight `requests.Session` for all subsequent calls (cached per domain, 25-min TTL)
- Added `fetch_html_with_cf_cookies()` — convenience wrapper around `get_cf_session` for one-shot HTML fetches
- Added `impit` and `zendriver` to `requirements.txt`

**Cloudflare bypass rollout:**
- **Madara** (`madara.py`): `MadaraSiteHandler.__init__` now accepts `use_zendriver=True`; all three fetch paths (`fetch_comic_context`, `_parse_chapters` / `_load_ajax_chapters`, `get_chapter_images`) transparently route through `get_cf_session` when enabled; added automatic CF-detection fallback for non-zendriver sites (short HTML, "just a moment" strings, 403/429/503 status)
- **MangaThemesia** (`mangathemesia.py`): added `use_zendriver` parameter mirroring the Playwright path; all three fetch points use `fetch_html_with_cf_cookies`; `_fetch_series_metadata_via_api` uses CF session when `use_zendriver=True`
- **WebtoonXYZ** (`webtoonxyz.py`): switched from the plain Madara entry in `madara_extra_sites.py` to the dedicated `WebtoonXYZSiteHandler` with `use_zendriver=True`; removed the duplicate generic Madara entry that was shadowing the custom handler
- **KingOfShojo, KappaBeast, RageScans** (`mangathemesia_sites.py`): added `use_zendriver: True` to their site configs
- **LikeManga** (`likemanga.py`): all three fetch paths now go directly through `fetch_html_with_cf_cookies` / `get_cf_session`; removed the old cloudscraper-based paths
- **MangaFire** (`mangafire.py`): `fetch_comic_context` uses `fetch_html_with_cf_cookies`; chapter-list and image AJAX calls use `get_cf_session`-backed session instead of `make_request`
- **`__init__.py`**: forwards `use_zendriver` from site config dict to `MangaThemesiaSiteHandler`; removed duplicate `MangaFireSiteHandler` import

**Site handler rewrites & fixes:**
- **ZeroScans** (`zeroscans.py`): full rewrite from REST API (zscans.com) to HTML scraping on the new domain `zeroscann.com`; HTML pagination replaces API pagination; chapter links scraped from `<a href*="/chapter-">` anchors; image extraction from `<img>` tags filtered by CDN URL pattern
- **Manganato** (`manganato.py`): added `_impit_get()` helper for Chrome-impersonated fetches; `fetch_comic_context` and `get_chapter_images` now use impit with cloudscraper fallback; added `_fetch_chapters_api()` using the `/api/manga/{slug}/chapters?limit=-1` JSON endpoint as primary chapter source (handles zstd); HTML fallback retained for legacy domains
- **WeebCentral** (`weebcentral.py`): chapter-list and image-list fetches now fall back to `fetch_html_impit` when cloudscraper returns 403/429/503 or raises (zstd compression fix)
- **AssortedScans** (`assortedscans.py`): primary image fetch path uses `fetch_html_impit` for all per-page requests; cloudscraper loop retained as fallback
- **Kagane** (`kagane.py`): updated API base to `yuzuki.kagane.org`, endpoint paths to `/api/v2/`; added integrity-token fetch (`/api/integrity`) and `x-integrity-token` header injection into DRM challenge POST; updated thumbnail URL template
- **ErosScans** (`mangathemesia_sites.py`): migrated primary domain from `erosvoid.xyz` to `erosxsun.xyz`

**MangaThemesia enhancements:**
- Added `/read/` + `chapter-` anchor fallback for sites (e.g. nikatoons) that place chapter links outside standard MangaThemesia containers; deduplication by chapter number applied
- Added `img[src*='/uploads/']` fallback image selector for Next.js-injected pages (e.g. nikatoons)
- Extended reader selectors to include `#readerArea` (capital A) variant
- Removed stale `import re` / `from urllib.parse import urljoin` inline imports; moved to top-level

**Removals:**
- Deleted `sites/batoto.py` and `sites/bato_mirrors.py` (bato.to and all mirrors shut down permanently)
- Deleted `sites/mangapark.py` (mangapark domain bounces endlessly; confirmed offline)
- Removed `BatoToSiteHandler` and `MangaParkSiteHandler` from `__init__.py`

---

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
