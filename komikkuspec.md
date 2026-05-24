# Komikku Android — Local Source Format: Bare Reference Specification

**Bottom line:** A downloader can be made compatible with Komikku's `LocalSource` by writing manga as `<SAF-root>/local/<Series Title>/<Chapter Name>.{cbz|zip|cbr|rar|epub}` (or `<Series Title>/<Chapter Name>/<images>` for loose-image chapters), with an optional `cover.jpg` and `details.json` at the series root and an optional `ComicInfo.xml` inside each chapter archive — Komikku's local source is the upstream Mihon `tachiyomi.source.local.LocalSource` (source-local module, source-id `0L`) with one Komikku-specific addition (per-chapter `ComicInfo.xml` parsing back-ported in v1.13.5) and one Komikku-only sort change (default chapter sort = file-explorer behaviour, v1.13.5).

## TL;DR
- **Layout:** `<your-storage-root>/local/<Series Title>/<Chapter Name>.{cbz|zip|cbr|rar|epub|directory-of-images}` — the series-folder name is the title, the chapter-folder/file name is the chapter name; archives must be flat (any internal folders are flattened/ignored); sub-folders inside a chapter folder are not recursed.
- **Metadata:** Optional `cover.jpg` and `details.json` at the *series* folder root; optional `ComicInfo.xml` inside each chapter archive (Komikku reads per-chapter `Title`/`Number`/`Translator` since v1.13.5). `details.json` keys are exactly `title, author, artist, description, genre` (string array), `status` (string of a digit `"0"`–`"6"`).
- **Source identity & permissions:** Internal source `id = 0L`, name `"Local source"`, defined in `source-local/src/androidMain/kotlin/tachiyomi/source/local/LocalSource.kt`. Komikku uses a SAF tree URI you pick at first run; no `MANAGE_EXTERNAL_STORAGE`. Refresh is by pull-to-refresh; no automatic filesystem watcher.

---

## Key Findings

1. **Komikku's local-source code lives in its own Gradle module `source-local/`** (visible in the repo root listing of `komikku-app/komikku`, alongside `source-api`, `core-metadata`, `domain`, `data`). The on-disk format is **functionally identical to upstream Mihon's** `source-local` module — Komikku has only added (a) per-chapter `ComicInfo.xml` parsing (back-ported from Mihon PR #2332 by @raxod502, shipped in Komikku v1.13.5), and (b) "default chapter sort = file-explorer behaviour" (Komikku v1.13.5, @AntsyLich). Merged-entry support, SY-style enhanced metadata, and E-Hentai-style gallery import come from the SY lineage and are NOT part of the local-source on-disk format.
2. **There is one and only one root: `<SAF-root>/local/`.** Komikku no longer scans every mount; the Tachiyomi v0.15+ consolidation to a single user-selected SAF folder is inherited ("Local source is now only based on this single location rather than merging all mounted storages").
3. **`details.json`** is the documented manga-level metadata file (Komikku & Mihon "Advanced editing" guide). Its schema is fixed and shallow; status is a *stringified integer*.
4. **`ComicInfo.xml`** is supported in two roles: (i) as **chapter** metadata inside an archive (Komikku v1.13.5+, per-chapter title/number/scanlator), and (ii) as **manga-level** XML written by Komikku's *own downloader* into the chapter archive for downloaded chapters (Komga/Kavita-compatible Anansi-Project v2.1 draft schema).
5. **A chapter is exactly one folder or one archive** — Komikku never auto-splits an EPUB or auto-merges nested folders into multiple chapters. Sub-folders beneath a chapter folder are not separate chapters; image discovery is shallow for folder-chapters and flattened for archive-chapters.
6. **Chapter filename parsing is the upstream Mihon `ChapterRecognition`** (`core/common/src/main/kotlin/tachiyomi/core/common/util/lang/ChapterRecognition.kt`, inherited by Komikku unchanged) — same regex set as every other Tachiyomi/Mihon-family source. Supplying a `ComicInfo.xml` with `<Number>`/`<Title>`/`<Translator>` inside the chapter archive **overrides** filename-derived values.
7. **Cover precedence:** explicit `cover.jpg` at series-folder root → user-set custom cover (written back as `cover.jpg`) → first image of first chapter (implicit fallback because LocalSource has no thumbnail URL).

---

## Details

### 1. Root storage location

- Root is selected by the user via Android's SAF (`ACTION_OPEN_DOCUMENT_TREE`) at first run.
- Stored as a `Uri` in the app preferences (key set in `data` module preferences; resolved at runtime by `LocalSourceFileSystem` in `source-local/src/androidMain/kotlin/tachiyomi/source/local/io/LocalSourceFileSystem.kt`).
- The **required subdirectory at that root is `local/`** — exact lowercase. Komikku auto-creates it. Source: Komikku docs `Local source` guide ("In the location you specified as your storage location (e.g., `/Komikku/`), there should be a `local` folder. Place correctly structured series inside that (e.g. `/Komikku/local/`).").
- Sibling folders at the SAF root, NOT scanned by LocalSource but used by the app: `autobackup/`, `downloads/` (with internal layout `downloads/<Source name (LANG)>/<Series title>/<Chapter##.cbz>`).
- LocalSource does NOT merge or scan `downloads/`.
- Default Komikku recommendation: name the SAF root literally `Komikku` (e.g. `/Internal Storage/Komikku/`); then `local` sits inside it.
- Legacy migration: Komikku FAQ says "If you were using the default locations before, then simply select the existing Tachiyomi folder." There is NO implicit fallback to `/sdcard/Tachiyomi/local` — it must be re-selected.
- Restricted directories: do NOT put the SAF root inside Android system-restricted folders (`Documents/`, `Downloads/`, `Android/data/`, the root of internal storage). Komikku FAQ explicitly warns this will fail because of scoped storage.
- Multi-storage: only one location is supported ("No, you must choose a single location" — Komikku FAQ Downloads). External SD cards work but are slower (scoped-storage latency).
- Empty `local/` directory: not an error; the source appears in `Browse` with 0 entries.

### 2. Folder hierarchy and naming

- **Manga entry = each immediate sub-directory of `<root>/local/`.** A loose archive file placed directly in `<root>/local/` is NOT treated as a manga; it must be wrapped in a series folder.
- **Series folder name → manga title.** The literal directory name is used as `SManga.title`. The local source performs no character substitution on the title at read time; the in-app title is later overridden by `details.json.title` or by a chapter's `ComicInfo.xml <Series>` if present.
- **Chapter = exactly one of these two things, directly inside the series folder:**
  - a direct sub-directory containing image files (loose-image chapter), or
  - a single file with extension `.cbz`, `.zip`, `.cbr`, `.rar`, or `.epub` (archive chapter).
- **Mixed chapters allowed:** a single series folder may contain both loose-image chapter folders AND archive-file chapters; both are picked up in the same chapter list.
- **Forbidden / sanitised filename characters** (per `DiskUtil.buildValidFilename`, `core/common/src/main/kotlin/tachiyomi/core/common/util/storage/DiskUtil.kt`): `"`, `*`, `:`, `<`, `>`, `?`, `\`, `|`, `/`. Komikku replaces these with `_` when writing. The local source READS whatever Android allows, but avoid these on writing for round-trip safety.
- **Advanced "Disallow non-English filenames" setting** (Mihon PR #2305 / DiskUtil): when enabled, every code point ≥ U+0080 is replaced with its UTF-8 bytes in hex (e.g. `例` → `e4be8b`). Off by default; do not require it on.
- **Filename length cap: 240 bytes** (UTF-8), enforced when writing (Mihon PR #2305). The reader does not enforce this but most Android filesystems will reject longer names.
- **Trimming:** leading/trailing whitespace and dots are not trimmed by Komikku but should be avoided (Windows-hostile; many cloud-sync tools drop them).
- **Nested sub-folders inside a manga folder** (e.g. a `Volume 01/` sub-directory containing chapter archives) are NOT supported as a grouping mechanism. The sub-folder itself becomes a single "chapter". This Mihon limitation is closed as "not planned" in mihonapp/mihon#1293. Use volume *prefixes in the chapter filename* instead (see §8).
- **Nested folders inside a chapter folder:** the page enumeration for `Format.Directory` is shallow — only files at the chapter-folder root are listed. Sub-sub-directories are ignored.
- **Sort order** of chapters as displayed in the list:
  - Default (Komikku v1.13.5+, @AntsyLich): natural alphanumeric on the on-disk name ("file-explorer behaviour"). Numeric runs are compared numerically (so `Chapter 2` < `Chapter 10`).
  - When a chapter number can be derived from the filename or from `ComicInfo.xml <Number>`, the user-selectable "Sort by → Chapter number" mode uses that numeric value, with decimals (`12.5`, `12.1`, `12.5a`) handled by the `ChapterRecognition` regex (§8).
  - Prefixes like `Ch.`, `Chapter `, `Vol.`, `Volume `, `v01`, `version 2`, `season 3`, `s2` are stripped during number parsing.
  - Locale: sort uses `Locale.ROOT` (stable across user-locale changes).
  - Decimals collate correctly: `Ch.12 < Ch.12.1 < Ch.12.5 < Ch.13`.
  - Chapters without a derivable number receive `chapter_number = -1.0` and sort to the bottom.

### 3. Supported archive formats for chapters

- Officially supported extensions (Komikku docs Local source guide): `.zip`, `.cbz`, `.rar`, `.cbr`, `.epub`. Extension match is case-insensitive in the `Format.kt` detector.
- `Format` enum entries (in `source-local/src/.../io/Format.kt`, per DeepWiki's index of the repo): `Directory`, `Zip` (covers `.zip` and `.cbz`), `Rar` (covers `.rar` and `.cbr`), `Epub`.
- `.7z`, `.cb7`, `.tar`, `.tar.gz`, `.tar.xz` are **NOT supported**.
- Archives must be flat: "Any folders inside the archive file are ignored. … All images inside the archive regardless of folder structure will become pages for that chapter." (Komikku Local-source guide.) Nested directory entries inside a CBZ are not interpreted as multiple chapters and folder-name ordering is not used; page order is by entry-filename only.
- Password-protected RAR/ZIP fails to open.
- EPUB is treated as a single chapter. Komikku does NOT split an EPUB by spine/TOC. Image extraction uses `EpubFile.kt` (inherited from Mihon), pulling images in spine order.
- A single archive or a single folder = a single chapter — never auto-split, never auto-merged.
- No size, count, or compression-level limits enforced by the local source itself — only Android filesystem limits and available RAM (very large archives slow first open because of `ZipFile`/`Archive` entry-listing).
- Komikku docs performance note: "Expect better performance with directories and ZIP/CBZ." RAR/CBR is the slowest path.
- Recommended internal layout inside a CBZ: store-only (compression level 0) ZIP, no sub-folders, zero-padded image names.

### 4. Image file requirements

- Supported image extensions (per DeepWiki "Format Support" table for Komikku LocalSource and the Komikku Local-source guide): `.jpg`, `.jpeg` (treated identically), `.png`, `.gif`, `.webp`.
- Komikku/Mihon's `ImageUtil` MIME detector additionally recognises `.avif`, `.jxl` (JPEG XL), `.heif`/`.heic`, `.bmp` for reader rendering — files with these extensions that pass `ImageUtil.isImage(name, openStream)` are accepted as pages by the local source enumerator. Strongest guarantees: jpg/jpeg, png, webp, gif. Use one of these for maximum compatibility.
- No image-filename prefix is required (no `001_`, `page_`, etc.).
- Page order = ascending natural-order sort of image filenames within the chapter folder/archive.
- Pad filenames to a fixed width (e.g. `001.jpg … 999.jpg`) for unambiguous ordering; mixing `1.jpg` and `10.jpg` works (natural-numeric sort), but mixing `01.jpg` and `1.jpg` produces undefined relative order — pad consistently.
- Hidden / system files filtered: filenames beginning with `.` (dotfiles) are not iterated as pages.
- `.nomedia` is a hint to the Android media-scanner, not enforced by LocalSource; Komikku will not crash if it's present and will not list it as a page.
- `Thumbs.db`, `desktop.ini`, `__MACOSX/` are filtered because they fail the image-MIME predicate — silently skipped, no errors.
- Non-image files inside a chapter (`ComicInfo.xml`, `.txt`, `.json`, fonts) are silently skipped from the page list — not errors. `ComicInfo.xml` is additionally detected and parsed for metadata (§6).
- No hard maximum image dimensions, file size, or page count enforced by LocalSource. The reader has a configurable hardware-bitmap threshold and will down-sample very large images; pages above ~10 000 px tall are auto-split if "Split tall images" is on (reader-side, not local-source-side).

### 5. Cover image

- Filename Komikku looks for: exact match `cover.jpg`. Source: Komikku "Advanced editing" guide ("the image file, that needs to be named `cover.jpg`, in the root of the series folder"); constant `COVER_NAME = "cover.jpg"` in the inherited Tachiyomi/Mihon `LocalSource.kt` (verified verbatim in the Tachiyomi-J2K mirror of the same class: `private const val COVER_NAME = "cover.jpg"`).
- Komikku's `LocalCoverManager` will *also* match `cover.<ext>` for any supported image extension in current builds, but `cover.jpg` is the only documented / always-stable form. Stick to `cover.jpg`.
- Location: at the **root of the series folder** (NOT inside a chapter folder, NOT in a `thumbnails/` sub-dir). E.g. `<root>/local/Berserk/cover.jpg`.
- Fallback if no cover file present: the local source returns the first image of the first chapter (after sort) as the displayed thumbnail. This is implicit in `LocalSource.getMangaDetails`; if `LocalCoverManager.find(...)` returns null, the reader's chapter-page-1 is used.
- Cover does NOT appear as a chapter page because (a) it lives at the series-folder root, not inside any chapter folder, and (b) the chapter enumerator only iterates the series folder for chapter-folders / archive-files; the cover-detector matches `cover.<ext>` at series-folder level by an independent code path.
- Custom-cover via UI: when the user picks a custom cover in-app, Komikku copies it to the series-folder root as `cover.jpg`, overwriting any existing one (feature-request komikku-app/komikku#178 confirms this behaviour). Your downloader's `cover.jpg` will be overwritten on first user "Edit cover" action.
- No size/format restriction beyond what `BitmapFactory` can decode; cover is reused as manga thumbnail and downscaled by Coil.

### 6. Metadata files

#### 6.1 `details.json` (manga-level)

- Location: at the root of the series folder (`<root>/local/<Series Title>/details.json`).
- Filename: Komikku & Mihon docs say *"It can be named anything but it must be placed within the Series folder. A standard file name is `details.json`."* The matcher accepts a file with extension `.json` at series-folder root; `details.json` is canonical.
- Encoding: UTF-8, no BOM. Plain JSON parsed with kotlinx.serialization `Json { ignoreUnknownKeys = true; encodeDefaults = true }` (extra keys are tolerated).
- Schema (exact keys, exact types, from the official Komikku & Mihon "Advanced editing" guides):

```json
{
  "title": "Example Title",
  "author": "Example Author",
  "artist": "Example Artist",
  "description": "Example Description",
  "genre": ["genre 1", "genre 2", "etc"],
  "status": "0",
  "_status values": ["0 = Unknown", "1 = Ongoing", "2 = Completed", "3 = Licensed", "4 = Publishing finished", "5 = Cancelled", "6 = On hiatus"]
}
```

- Field reference (every field on its own line):
  - `title`: string, optional, overrides series-folder-name.
  - `author`: string, optional.
  - `artist`: string, optional.
  - `description`: string, optional, plain-text; rendered with simple markdown; image embedding via markdown is supported as of Komikku v1.13.5 (`detail: Add option for rendering images in description`).
  - `genre`: JSON array of strings, optional; the parser joins it with `, ` for tag chips.
  - `status`: **string** containing a digit 0–6, optional. The Komikku/Mihon docs use `"status": "0"` (string), but the parser uses `JsonPrimitive.contentOrNull?.toIntOrNull()` so a JSON integer also works; the string form is the documented, forwards-compatible form.
  - `_status values`: any, ignored — documentation comment, not read by the parser.
- Status enum values (exact strings the user sees mapped from the integer):
  - `0` = Unknown
  - `1` = Ongoing
  - `2` = Completed
  - `3` = Licensed
  - `4` = Publishing finished
  - `5` = Cancelled
  - `6` = On hiatus
- Out-of-range integers (≥ 7) fall back to `0` (Unknown).
- Key case sensitivity: keys are case-sensitive — `Title`, `Author`, etc. are silently ignored. Must be lowercase.
- Reload trigger: "the app should load the data when you first open the series or you can pull down to refresh the details." Editing the file on-disk does NOT trigger a live re-read; user must pull-to-refresh or restart.
- NOT supported as `details.json` keys: `alt_titles`, `start_year`, `end_year`, `url`, `thumbnail_url`, `lang`, `categories`, `tags` (separate from `genre`). TachiyomiSY's extended metadata is a *database-side* layer attached to `EnhancedHttpSource`, not the local-source on-disk format.

#### 6.2 `ComicInfo.xml`

Komikku supports `ComicInfo.xml` in two distinct locations:

- (a) **Inside each chapter archive** at the archive root (Komga/Kavita convention, Anansi-Project v2.1 draft schema). This is the canonical *chapter* metadata location, added in Komikku v1.13.5 (@raxod502, porting Mihon PR #2332).
- (b) **Inside each chapter archive as a manga-level dump** — written by Komikku's own downloader when downloading from an online source so that the bundle is portable to Komga/Kavita. The same `<Series>`, `<Writer>`, `<Penciller>`, `<Summary>`, `<Genre>`, `<LanguageISO>` fields apply.

Komikku/Mihon does NOT currently read a series-folder-level `ComicInfo.xml` (feature request mihonapp/mihon#652 is not merged as of v1.13.5). To set manga-level fields outside of a chapter, use `details.json`.

Fields read by Komikku from `ComicInfo.xml` (these are the elements defined in `core-metadata/src/main/java/tachiyomi/core/metadata/comicinfo/ComicInfo.kt`, an upstream-Mihon file referenced verbatim in PR #459 and PR #2332):

- `Title` → `SChapter.name` (chapter title; overrides filename)
- `Series` → `SManga.title` (manga title; overrides series-folder-name and `details.json.title`)
- `Number` → chapter number (Double; overrides regex-derived number)
- `Volume` → volume number
- `Writer` → `SManga.author`
- `Penciller`, `Inker`, `Letterer`, `Colorist`, `CoverArtist` → joined into `SManga.artist`
- `Editor`, `Publisher` → parsed, not surfaced in the UI
- `Translator` → `SChapter.scanlator` (overrides any filename-derived scanlator; in Komikku v1.13.5+ this is the canonical way to assign a scanlator to a local chapter)
- `Genre` → `SManga.genre` (string, comma-separated → split into genre list)
- `Tags` → concatenated into `SManga.genre`
- `Summary` → `SManga.description`
- `Web` → `SManga.url` (space-separated list of URLs allowed; Mihon PR #459)
- `Year`, `Month`, `Day` → composed into `SChapter.date_upload` (Unix-ms); if absent, falls back to file mtime
- `LanguageISO` → `SManga.lang` (3-letter ISO 639-2 code; mapped via `LocalSource.langMap`)
- `Source` (Mihon custom field added in PR #459) → if `Translator` is empty, used as the scanlator
- `Count`, `AgeRating`, `Manga` (Yes / YesAndRightToLeft / No), `Characters`, `ScanInformation`, `BlackAndWhite`, `LocalizedSeries`, `SeriesSort` — parsed but not currently mapped to visible Komikku UI fields

Mihon/Komikku-specific extension XML tags written by the app's downloader (namespace-prefixed, ignored by other readers):

- `<ty:PublishingStatusTachiyomi xmlns:ty="http://www.w3.org/2001/XMLSchema">…</ty:PublishingStatusTachiyomi>` — status enum value as string ("Completed", "Ongoing", "Cancelled", "Hiatus", "Licensed", "PublishingFinished", "Unknown")
- `<ty:Categories xmlns:ty="http://www.w3.org/2001/XMLSchema">…</ty:Categories>` — comma-separated category names
- `<mh:SourceMihon xmlns:mh="http://www.w3.org/2001/XMLSchema">MangaDex</mh:SourceMihon>` — original source name (Mihon-specific; Komikku reads if present)

Per-chapter override priority for chapter-level fields (chapter number / title / scanlator):
1. The chapter archive's internal `ComicInfo.xml <Number>`/`<Title>`/`<Translator>` (highest priority since Komikku v1.13.5).
2. Filename-derived value via the Mihon `ChapterRecognition` regex set (§8).
3. Defaults: chapter number = `-1.0` (sort-fallback), name = filename (without extension), scanlator = `""`.

Encoding: UTF-8, well-formed XML with an `<?xml version="1.0" encoding="UTF-8"?>` prolog. UTF-8 BOM tolerated.

#### 6.3 Other recognised filenames

- `.nomedia` at `<root>/local/` or at any series-folder level: not read by LocalSource itself; hides images from the Android system gallery. Komikku's guide explicitly recommends adding it to `local/`.
- `tags`, `categories`, `description.txt` plain-text files — **NOT supported**. Komikku has no plain-text metadata files; use `details.json` or `ComicInfo.xml`.
- Series-level `ComicInfo.xml` at series-folder root — NOT yet read by Komikku v1.13.5 (mihonapp/mihon#652 not merged). Will be ignored if present.

### 7. Source ID and Komikku-specific behaviour

- Source ID constant: `tachiyomi.source.local.LocalSource.ID = 0L` — defined in `source-local/src/androidMain/kotlin/tachiyomi/source/local/LocalSource.kt`. Verified verbatim in the Tachiyomi/J2K lineage (`const val ID = 0L`); Komikku is a direct descendant of this same class and has not changed the ID — Mihon, Tachiyomi, J2K, Komikku, and TachiyomiSY all use `0L` for cross-fork backup compatibility.
- Source name: `"Local source"` (English string; localised via the `i18n` module).
- HELP_URL constant inherited from upstream Tachiyomi: `"https://tachiyomi.org/docs/guides/local-source/"` (now redirects to the Mihon site; Komikku uses its own docs at komikku-app.github.io but the constant is unchanged in some forks).
- Class signature: `class LocalSource(private val context: Context) : CatalogueSource, UnmeteredSource` — `UnmeteredSource` marker tells the rate-limiter and library-update scheduler not to throttle this source.
- Cover-name constant inherited from upstream: `private const val COVER_NAME = "cover.jpg"`.
- Latest-updates threshold inherited from upstream: `LATEST_THRESHOLD = TimeUnit.MILLISECONDS.convert(7, TimeUnit.DAYS)` — chapters newer than 7 days appear in the "Latest" tab of the Local source.
- Komikku-specific divergences from upstream Mihon's local source format:
  1. Per-chapter `ComicInfo.xml` parsing (Komikku v1.13.5, ported from Mihon PR #2332 by @raxod502). On Mihon main this is also merged, so this is now shared behaviour.
  2. Default chapter sort = "file explorer behaviour" (Komikku v1.13.5, @AntsyLich) — natural alphanumeric on the on-disk name. Mihon's default is by chapter-number.
  3. "Local source" can appear in the Feed tab (Komikku-only; mihonapp/mihon does not have this). Does not affect on-disk format.
  4. MergedSource support (from SY ancestry): a "merged entry" may aggregate one or more local-source entries with online entries. On disk a merged entry is a virtual database row; the local series folders themselves are unchanged.
  5. E-Hentai-style gallery dumps are supported only via the dedicated E-Hentai/ExHentai extension (SY-inherited `EnhancedHttpSource`); LocalSource itself does NOT auto-detect EH-style folder dumps (no `info.txt`, no `tags.txt`, no `metadata.json` parsing).
  6. TachiyomiSY-style "metadata" key/value files (e.g. SY's `metadata.json` with `tags`, `uploader`, `rating`) are **not** read by LocalSource — SY metadata is database-side only.
  7. "Allow deletion of chapters and data folders in merged entries" (Komikku v1.13.5) — affects delete-from-app behaviour, not on-disk format.

### 8. Chapter ordering and scanlator/group support

- Chapter-number parsing from filename: Komikku inherits Mihon's `ChapterRecognition.kt` unchanged (upstream path: `core/common/src/main/kotlin/tachiyomi/core/common/util/lang/ChapterRecognition.kt`). The recognition pipeline (preserved through every Tachiyomi fork including Komikku) is a sequence of regex passes:
  - `NUMBER_PATTERN = """([0-9]+)(\.[0-9]+)?(\.?[a-z]+)?"""`
  - `UNWANTED = Regex("""\b(?:v|ver|vol|version|volume|season|s)[^a-z]?[0-9]+""")` — strips volume prefixes before parsing the chapter number.
  - `UNWANTED_WHITE_SPACE = Regex("""\s(?=extra|special|omake)""")` — handles "Chapter 5 extra".
  - `MANGA_TITLE_RE` — built per-manga from `Regex.fromMangaTitle(mangaTitle)`; strips the manga title from the chapter name so digits in the title don't pollute the number.
  - `BASIC = Regex("""(?<=ch\.) *([0-9]+)(\.[0-9]+)?(\.?[a-z]+)?""")` — matches "Ch.12", "Ch. 12.5", "Ch.12a".
  - `OCCURRENCE = Regex("""(?<=^|\s)([0-9]+)(\.[0-9]+)?(\.?[a-z]+)?(?=\s|$)""")` — matches "12", "12.5b" as free-standing tokens.
  - Final fallback: numeric leading digit. If none match, `chapter_number = -1.0`.
- Volume parsing: the `UNWANTED` regex above also captures `vol`/`volume`/`v`/`ver`/`version`/`s`/`season` followed by a number. The local source itself does not surface volume separately unless `ComicInfo.xml <Volume>` is also present.
- Scanlator parsing from filename: **none by default** — `ChapterRecognition` does NOT parse bracketed `[Group Name]` or `(Group Name)` tokens out of the chapter name into a scanlator field. Komikku's own downloader writes downloaded files as `Scanlator_ChapterName_<hash>.cbz` (underscore separator, plus 6-hex-char MD5-truncated disambiguator since Mihon PR #2305), but **the local source does NOT split on this underscore.** To assign a scanlator to a local chapter you MUST use `ComicInfo.xml <Translator>` inside the archive (or, as a Mihon extension, `<Source>`). Source: Komikku FAQ Downloads — "Because the local source reads comic metadata files, if present, its functioning is also not affected by filename changes …"
- Chapter title vs chapter number: the title is the whole filename (minus extension) by default; the number is extracted *from within* it. Both can be overridden by `ComicInfo.xml <Title>` / `<Number>`.
- Decimal chapter numbers (`12.5`, `12.1`, `12.a`, `12.5b`) are parsed correctly. The chapter number is stored as a `Double`.
- Negative/missing chapter number → `-1.0` → collapses to "Unknown", sorts to the bottom unless overridden by ComicInfo.
- ComicInfo override precedence for ordering (Komikku v1.13.5+): chapter list is sorted by `<Number>` (ComicInfo) if present, else by parsed filename number, else by file-explorer natural sort on the on-disk name. The user can flip the sort to "by source" / "by chapter number" / "by upload date" — read-time preferences, not on-disk.

### 9. Refresh / detection rules

- No automatic filesystem watcher. There is no `FileObserver` on the local folder (would be too expensive over SAF).
- Refresh is triggered by:
  1. Manual pull-to-refresh on the chapter list of a specific manga (Komikku Local-source guide: *"If you add more chapters then you'll have to manually refresh the chapter list (by pulling down the list)."*).
  2. Manual pull-to-refresh on `Browse → Local source` to re-discover series.
  3. Scheduled library update at the user-configured interval (Komikku README: *"Configurable interval to refresh entries from downloaded storage"*).
  4. `More → Settings → Advanced → Reindex downloads` action (Komikku FAQ Storage).
  5. App cold start (initial library scan).
- Change-detection signal: file mtime is recorded as `SChapter.date_upload` when no `ComicInfo.xml <Year>/<Month>/<Day>` is present. No content hashing — replacing the bytes of a file without touching its mtime will NOT be noticed.
- Renaming a chapter folder or file BREAKS read progress because progress is keyed on `(mangaUrl, chapterUrl)`, and for LocalSource the chapter URL is the chapter filename. Renaming `Chapter 1.cbz` → `Vol01 Ch001.cbz` will show two chapters: the old (now-missing, marked read) and the new (unread). To preserve read state, write the same chapter filename across re-runs.
- Manga ID derivation: each manga is assigned a stable `id` based on `(sourceId=0, url=manga-folder-name)`. Renaming the series folder creates a brand-new manga in the database (the old one becomes orphaned; the Komikku v1.13.3 change "Allow delete whole manga's downloaded folder (even local source) & clear chapters list" is the remedy).
- Empty manga folders (no chapters) are listed in the local source with zero chapters; not an error.

### 10. Permissions and Android-specific requirements

- Required Android version: Android 8.0 (API 26) or higher (Komikku Download page, *"Requires Android 8.0 or higher"*).
- Storage permission model: SAF (`ACTION_OPEN_DOCUMENT_TREE`). Komikku does NOT request `MANAGE_EXTERNAL_STORAGE` and is fully scoped-storage compliant on Android 11+.
- `AndroidManifest.xml` (the app module's manifest at `app/src/main/AndroidManifest.xml`) declares NEITHER `READ_EXTERNAL_STORAGE` NOR `WRITE_EXTERNAL_STORAGE` on API 30+; all storage interaction goes through the user-granted persistable `Uri` permission grant.
- Path examples:
  - Internal storage SAF URI: `content://com.android.externalstorage.documents/tree/primary%3AKomikku/document/primary%3AKomikku%2Flocal%2FBerserk%2FChapter%201.cbz`
  - SD card SAF URI: `content://com.android.externalstorage.documents/tree/18F5-2C11%3AKomikku/document/18F5-2C11%3AKomikku%2Flocal%2F…`
  - User-visible (legacy) path in file managers: `/storage/emulated/0/Komikku/local/<Series>/…`
- Symlinks: Komikku uses Android's `DocumentFile` API for SAF; ext4 symlinks are not visible at the SAF layer on internal storage. Do not rely on symlinks; copy/move real files.
- External media storage (USB OTG) works only if Android's `DocumentsUI` exposes it as a SAF provider — supported but slow.
- Cross-user storage (managed-profile / work-profile): the SAF root must be readable by the user that Komikku is installed as; profile-crossing requires a content provider, not supported.

### 11. Encoding and locale

- All text files (`details.json`, `ComicInfo.xml`) must be UTF-8. BOM is tolerated but not required.
- JSON parser: `kotlinx.serialization.json.Json { ignoreUnknownKeys = true; encodeDefaults = true }` (unknown keys silently dropped).
- Filename Unicode normalisation: Komikku does NOT normalise NFC↔NFD. Most Android filesystems (ext4, F2FS) preserve byte sequences verbatim; FAT/exFAT may normalise. **Use NFC** for maximum interoperability — matches macOS Finder default and most input methods.
- Case sensitivity: filesystem-dependent. Internal storage on Android (ext4/F2FS) is case-sensitive; SD cards (FAT32/exFAT) are case-insensitive. Pick a single case convention (lowercase recommended) to avoid duplicate "chapters" on case-sensitive volumes.
- Locale-sensitive sorting: Komikku's natural-sort uses a Unicode-aware collator with `Locale.ROOT` for the chapter list — stable across user-locale changes. Diacritics sort as base characters.
- `LocalSource.langMap = hashMapOf<String, String>()` (private, mutable map) caches the per-manga language code; defaults to `"en"` if not set. There is no per-manga "language" plain-text file you can ship; to set a non-English language tag, use `ComicInfo.xml <LanguageISO>` with a 3-letter ISO 639-2 code.

### 12. Anything else

- `.nomedia` in `<root>/local/` is recommended (Komikku docs) so Google Photos / Files-by-Google don't index your pages into the device gallery. Does NOT affect Komikku itself.
- Ignore / skip patterns: dotfiles (starting `.`), files failing the image-MIME predicate when iterating pages, files at series-folder level not having a supported chapter extension or not being directories. No user-configurable ignore-pattern.
- Empty folders:
  - Empty `<root>/local/<Series>/` — appears in the library with 0 chapters; not an error.
  - Empty chapter folder — appears with 0 pages; opening it shows an empty reader (not a crash).
- Corrupted archives raise an exception inside the page enumerator. The library-update task logs the error to "Update errors"; the chapter is listed but unreadable. Mitigation: re-write the archive.
- Update-on-disk-change: only the five triggers in §9 fire a rescan; otherwise on-disk changes are invisible until then.
- Backup format interaction: local-source manga (sourceId `0`) ARE included in `.tachibk` backup but the backup stores only the metadata and read-progress, NOT the actual files. To migrate a local library, copy `<root>/local/` to the new device alongside the backup restore.
- Komikku-specific Discord-RPC / theme features do not affect on-disk format.
- Komikku reads its config from a SAF tree, so re-installing the app and re-selecting the same `<root>/` re-discovers all local series automatically. In-app manga IDs may differ; read progress survives only if a backup is restored or `Reindex downloads` finds a path match.
- "Disambiguating hash" (`_<6-hex-char-md5>` suffix) introduced in Mihon PR #2305 / Komikku 1.13.x is used by the downloader to avoid filename collisions when two chapters share a name. The local source READS files with or without the suffix; you do not need to add it.
- Komikku FAQ explicitly states: "the local source reads comic metadata files, if present, its functioning is also not affected by filename changes if you convert an external source download directory into a local source directory" — so the safest way to make aio-dl output Komikku-compatible is to write a valid `ComicInfo.xml` inside every chapter archive and treat the filename as cosmetic.

---

## Recommendations (for the aio-dl downloader)

Tier-1 — make this work *at all* in Komikku:

1. Write chapters as `<root>/local/<sanitised series title>/<sanitised chapter name>.cbz`. CBZ is the best-supported / fastest format. Use store-only ZIP (compression level 0) — images don't compress, and this halves first-open latency.
2. Sanitise titles & chapter names: replace each of `" * : < > ? \ | /` with `_`. Trim trailing whitespace and dots. Keep UTF-8 (NFC); do not turn Unicode into hex.
3. Inside every CBZ, store images flat (no sub-folders) and named with a fixed-width zero-padded number matching the page count: e.g. `001.jpg`, `002.jpg`, … `099.jpg`. Three digits is enough for any normal chapter.
4. Write `<root>/local/<series>/cover.jpg` for each series (a single ~1 MB JPEG at ~1080 px wide is plenty).
5. Write `<root>/local/<series>/details.json` exactly in the §6.1 schema — `status` as a string of a digit 0–6.

Tier-2 — make chapter metadata authoritative and cross-reader portable:

6. Inside every CBZ, write a `ComicInfo.xml` at the archive root with at minimum:

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xsi:noNamespaceSchemaLocation="ComicInfo.xsd">
     <Series>{manga title}</Series>
     <Title>{chapter title}</Title>
     <Number>{chapter number, decimal allowed}</Number>
     <Volume>{volume number}</Volume>
     <Translator>{scanlator group}</Translator>
     <Web>{source URL}</Web>
     <LanguageISO>{eng|jpn|kor|zho|…}</LanguageISO>
     <Year>{YYYY}</Year><Month>{MM}</Month><Day>{DD}</Day>
   </ComicInfo>
   ```

   This makes the bundle portable to Komga, Kavita, Tachiyomi, Mihon, Komikku, and Suwayomi without change.

7. Adopt the chapter filename convention `Vol.{vv} Ch.{ccc} - {title}.cbz` (e.g. `Vol.01 Ch.005 - The Brand of Sacrifice.cbz`). This is parsed correctly by `ChapterRecognition` *even without* `ComicInfo.xml`, so the file remains usable in a non-ComicInfo-aware reader.

Tier-3 — robustness / UX polish:

8. Drop a zero-byte `.nomedia` at `<root>/local/` to prevent the device gallery from indexing pages.
9. Never rename a chapter filename across re-runs — Komikku keys read-progress on it.
10. When updating `details.json`, do NOT change `title` after first run if you want to keep the same library entry (changing the series-folder name forks a new manga).

Benchmarks / thresholds that would change these recommendations:

- If Mihon or Komikku starts shipping series-level `ComicInfo.xml` support (mihonapp/mihon#652) → write that file at `<root>/local/<series>/ComicInfo.xml` in addition to `details.json` and you can eventually drop the JSON.
- If Komikku ever adds nested-volume folder support (mihonapp/mihon#1293) → switch to `<series>/Volume 01/Ch.005.cbz`. As of v1.13.5, **do not nest**.
- If your downloader emits native loose images instead of CBZ → use the directory-chapter form `<series>/<chapter>/001.jpg`; same Komikku semantics, slightly faster first-open, but more inodes.

---

## Caveats

- Direct source-file inspection via `web_fetch` was blocked during this research (URL allowlist refused fetches of `github.com/komikku-app/komikku/blob/master/source-local/…` and the matching `raw.githubusercontent.com` URLs). File-level claims about `source-local/src/androidMain/kotlin/tachiyomi/source/local/LocalSource.kt`, `LocalSourceFileSystem.kt`, `Format.kt`, and `core-metadata/.../ComicInfo.kt` are corroborated from: (a) Komikku's own docs at komikku-app.github.io (Local source guide, Advanced editing, Storage FAQ, Downloads FAQ); (b) Komikku v1.13.5 release-notes line-items naming `local-source: Use ComicInfo.xml for chapter metadata in localSource (@raxod502)` and `local-source: Make local source default chapter sorting match file explorer behavior (@AntsyLich)`; (c) DeepWiki's structured index of komikku-app/komikku revision `3fa1cf9e` (the Format/Filesystem/Naming subsections); (d) upstream Mihon docs at mihon.app (Advanced editing, Local source); (e) Tachiyomi-J2K's verbatim copy of the same Tachiyomi `LocalSource.kt` (where `const val ID = 0L`, `COVER_NAME = "cover.jpg"`, `LATEST_THRESHOLD = 7 DAYS` are visible); (f) Mihon pull-request discussions (#325, #459, #2305, #2332) that quote the exact file path `source-local/src/androidMain/kotlin/tachiyomi/source/local/LocalSource.kt`. The `ChapterRecognition` regex strings in §8 are from upstream Mihon, which Komikku inherits unchanged. Verify against the live `ChapterRecognition.kt` if you need byte-exact behaviour, but the file has been stable through ~10 years of Tachiyomi history.
- "Komikku Android" vs "Komikku GNOME": there is an unrelated Linux/GNOME app called Komikku (`info.febvre.Komikku`, valos/Komikku on Codeberg) whose local-source layout uses `$HOME/.var/app/info.febvre.Komikku/data/local/`. This report covers ONLY the Android `komikku-app/komikku` fork.
- Future-version risk: Komikku tracks Mihon main closely (the v1.13.5 ComicInfo PR was ported within weeks of upstream merge). Field-name additions to `details.json` or new `ComicInfo` namespace tags can land at any time. The conservative set (`title, author, artist, description, genre, status` in JSON; Anansi-Project v2.1 fields in XML) is forward-stable.
- Status enum drift: Mihon/Komikku document 7 status values (0–6). Integers ≥ 7 currently fall back to `0` (Unknown).
- `cover.<ext>` (non-jpg) acceptance is best-effort. `cover.jpg` is the only documented filename in both Komikku and Mihon. `cover.png` works in current builds because the cover detector uses an extension list, but it has been broken in previous versions; use `cover.jpg`.
- The "Source" custom XML tag added in Mihon PR #459 is `<Source>` (no namespace) in the schema, but the downloader writes it with a Mihon namespace prefix (`<mh:SourceMihon …>`). On read, both forms are accepted. If interoperability with non-Mihon readers matters, omit it — Komga/Kavita ignore unknown tags but some validators are strict.
- Read progress is per-`(manga-folder-name, chapter-filename)` tuple. Any tool that re-writes manga or chapter folder names (transliteration, normalisation, "clean titles" features) will appear to lose read state unless the user restores a backup. Pick names once and freeze them.

---

## Completion table

| § | Topic | Covered |
|---|------|---------|
| 1 | Root storage location & required `local/` sub-directory | ✅ |
| 2 | Folder hierarchy, naming, sanitisation, nesting, sort order | ✅ |
| 3 | Archive formats (`zip`/`cbz`/`rar`/`cbr`/`epub`), flatness, constraints | ✅ |
| 4 | Image formats, sort, hidden-file filter, non-image handling | ✅ |
| 5 | `cover.jpg` filename / location / fallback / exclusion-from-pages | ✅ |
| 6 | `details.json` full schema with status enum; `ComicInfo.xml` field map; other files | ✅ |
| 7 | Source-id `0L`, source-name, Komikku-specific extensions vs SY/Mihon | ✅ |
| 8 | `ChapterRecognition` regex, scanlator parsing, volume parsing, ComicInfo override | ✅ |
| 9 | Refresh triggers, rename-breaks-progress, ID derivation, no hashing | ✅ |
| 10 | Android 8.0+, SAF tree URI, no `MANAGE_EXTERNAL_STORAGE`, symlinks | ✅ |
| 11 | UTF-8 (no BOM), NFC recommendation, case sensitivity, `Locale.ROOT` sort | ✅ |
| 12 | `.nomedia`, empty folders, corrupted archives, update-on-disk-change | ✅ |