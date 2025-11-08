# AIO Webtoon & Light Novel Downloader 📚

> [!IMPORTANT]
> comick.io has ceased providing any media themselves. This project now targets multiple other platforms with the same feature set—thanks for sticking around!


[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/) [![GNU GPLv3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

The most versatile, feature-rich & customizable command-line tool to download webtoons (manhwa), comics and light novels from [mangataro.org](https://mangataro.org), [bato.to](https://bato.to), [asuracomic.net](https://asuracomic.net) & many others, and package them into **PDF**, **EPUB**, **CBZ**, or raw chapter bundles—complete with light-novel aware text handling, intelligent image processing, resumable downloads, scanlation group prioritization, and optional chapter/file-size splitting.

![Comick.io Downloader Banner](https://github.com/zzyil/comick.io-Downloader/blob/main/comick_downloader.png?raw=true)


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

- 📥 **Flexible Chapter Selection**  
  Download specific chapters, ranges (`1-5`), or lists (`1,3,5-7`).
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
- 🔄 **Resumable Downloads**  
  Automatically picks up where it left off if interrupted.
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
git clone https://github.com/zzyil/comick.io-Downloader.git
cd comick.io-Downloader
python3 -m pip install -r requirements.txt
```

---

## 🌐 Supported Sites

For supported NSFW sites, check docs/NSFW.MD

| Site | Notes |
| ----- | ----- |
| [anisascans.in](https://anisascans.in) | tested ✅ | 
| [apcomics.org](https://apcomics.org) | tested ✅ |
| [aquamanga.com](https://aquareader.net) | tested ✅ |
| [arc-relight.com](https://arc-relight.com) | tested ✅ |
| [arcanescans.org](https://arcanescans.org) | tested ✅ |
| [aryascans.com/brainrotcomics.com](https://brainrotcomics.com/) | tested ✅ |
| [artlapsa.com](https://artlapsa.com) |  |
| [asmotoon.com](https://asmotoon.com) |  |
| [assortedscans.com](https://assortedscans.com) |  |
| [asuracomic.net](https://asuracomic.net) |  |
| [asurascans.net](https://asurascans.net) |  |
| [atsu.moe](https://atsu.moe) |  |
| [bato.to](https://bato.to) |  |
| [boratscans.com](https://boratscans.com) |  |
| [cocomic.co](https://cocomic.co) |  |
| [coffeemanga.io](https://coffeemanga.io) |  |
| [darkscans.net](https://darkscans.net) |  |
| [decadencescans.com](https://reader.decadencescans.com) |  |
| [dragontea.ink](https://dragontea.ink) |  |
| [dynasty-scans.com](https://dynasty-scans.com) | Supports scanlation group selection |
| [evilflowers.com](https://evilflowers.com) |  |
| [fanfox.net](https://fanfox.net) |  |
| [firescans.xyz](https://firescans.xyz) |  |
| [freecomiconline.me](https://freecomiconline.me) |  |
| [freemanga.me](https://freemanga.me) |  |
| [freemangatop.com](https://freemangatop.com) |  |
| [galaxydegenscans.com](https://gdscans.com) |  |
| [gedecomix.com](https://gedecomix.com) |  |
| [gourmetscans.com](https://gourmetsupremacy.com) |  |
| [harimanga.me](https://harimanga.me) |  |
| [hentai4free.net](https://hentai4free.net) |  |
| [hentaimanga.me](https://hentaimanga.me) |  |
| [hentairead.com](https://hentairead.com) |  |
| [hentaiwebtoon.com](https://hentaiwebtoon.com) |  |
| [hentaixcomic.com](https://hentaixcomic.com) |  |
| [hentaixdickgirl.com](https://hentaixdickgirl.com) |  |
| [hentaixyuri.com](https://hentaixyuri.com) |  |
| [hiperdex.com](https://hiperdex.com) |  |
| [hm2d.com](https://doujindistrict.com) |  |
| [inkreads.com](https://inkreads.com) |  |
| [isekaiscanmanga.com](https://isekaiscanmanga.com) |  |
| [isekaiscantop.com](https://isekaiscan.top) |  |
| [jimanga.com](https://s2manga.io) |  |
| [kagane.org](https://kagane.org) | DRM-protected, requires Widevine `.wvd` file. See [docs/Widevine.md](docs/Widevine.md) |
| [kissmangain.com](https://kissmanga.in) |  |
| [ksgroupscans.com](https://ksgroupscans.com) |  |
| [kunmanga.com](https://kunmanga.com) |  |
| [lhtranslation.net](https://lhtranslation.net) |  |
| [likemanga.ink](https://likemanga.ink) |  |
| [likemanga.in](https://likemanga.in) |  |
| [lilymanga.net](https://lilymanga.net) |  |
| [madaradex.org](https://madaradex.org) |  |
| [manga18free.com](https://manga18free.com) |  |
| [manga18fx.com](https://manga18fx.com) |  |
| [manga18x.net](https://manga18x.net) |  |
| [manga3s.com](https://manga3s.com) |  |
| [mangabuddy.com](https://mangabuddy.com) |  |
| [mangaclash.com](https://toonclash.com) |  |
| [mangadass.com](https://mangadass.com) |  |
| [mangadistrict.com](https://mangadistrict.com) |  |
| [mangaforfree.com](https://mangaforfree.com) |  |
| [mangafoxfull.com](https://mangafoxfull.com) |  |
| [mangafreakonline.com](https://mangafreak.online) |  |
| [mangagg.com](https://mangagg.com) |  |
| [mangahentai.me](https://mangahentai.me) |  |
| [mangahub.io](https://mangahub.io) |  |
| [mangakakalot.gg](https://mangakakalot.gg) |  |
| [mangakiss.org](https://mangakiss.org) |  |
| [mangaleveling.com](https://mangaleveling.com) |  |
| [mangamaniacs.org](https://mangamaniacs.org) |  |
| [mangaonlineteam.com](https://mangaonlineteam.com) |  |
| [mangaowl.io](https://mangaowl.io) |  |
| [mangapark.net](https://mangapark.net) |  |
| [mangaread.co](https://mangaread.co) |  |
| [mangaread.org](https://www.mangaread.org) |  |
| [mangareader.to](https://mangareader.to) |  |
| [mangasushi.org](https://mangasushi.org) |  |
| [mangatone.com](https://mangatone.com) |  |
| [mangatxunoriginal.com](https://mangaempress.com) |  |
| [mangatyrant.com](https://mangatyrant.com) |  |
| [mangatyun.com](https://mangatoto.com) |  |
| [mangadex.org](https://mangadex.org) | Supports scanlation group selection |
| [mangataro.org](https://mangataro.org) | Supports scanlation group selection |
| [manhuaaz.com](https://manhuaaz.com) |  |
| [manhuaes.com](https://manhuaes.com) |  |
| [manhuafast.com](https://manhuafast.com) |  |
| [manhuafast.net](https://manhuafast.net) |  |
| [manhuaga.org](https://manhua-ga.org) |  |
| [manhuahot.com](https://manhuahot.com) |  |
| [manhuanext.com](https://manhuanext.com) |  |
| [manhuaplus.com](https://manhuaplus.com) |  |
| [manhuatop.org](https://manhuatop.org) |  |
| [manhuaus.com](https://manhuaus.com) |  |
| [manhuazonghe.com](https://www.manhuazonghe.com) |  |
| [manhwa18.org](https://manhwa18.org) |  |
| [manhwa68.com](https://manhwa68.com) |  |
| [manhwaclan.com](https://manhwaclan.com) |  |
| [manhwaden.com](https://www.manhwaden.com) |  |
| [manhwahentai.to](https://manhwahentai.to) |  |
| [manhwahentai.me](https://manhwahentai.me) |  |
| [manhwamanhua.com](https://manhwamanhua.com) |  |
| [manhwas.men](https://manhwas.men) |  |
| [manhwatoon.me](https://www.manhwatoon.me) |  |
| [manhwatop.com](https://manhwatop.com) |  |
| [manycomic.com](https://manycomic.com) |  |
| [manytoon.com](https://manytoon.com) |  |
| [manytoon.me](https://manytoon.me) |  |
| [milftoon.xxx](https://milftoon.xxx) |  |
| [murimscan.com](https://www.deatte5.com) |  |
| [novelcrow.com](https://novelcrow.com) |  |
| [novelmic.com](https://novelmic.com) |  |
| [paritehaber.com](https://www.paritehaber.com) |  |
| [pawmanga.com](https://pawmanga.com) |  |
| [petrotechsociety.org](https://www.petrotechsociety.org) |  |
| [platinumcrown.com](https://platinumscans.com) |  |
| [plutoscans.com](https://flamescans.lol) |  |
| [pomanga.com](https://pomanga.com) |  |
| [porncomix.online](https://porncomix.online) |  |
| [rdscans.com](https://rdscans.com) |  |
| [readfreecomics.com](https://readfreecomics.com) |  |
| [readmangafree.net](https://readmangafree.net) |  |
| [resetscans.org](https://reset-scans.org) |  |
| [retsu.org](https://retsu.org) |  |
| [rosesquadscans.com](https://rosesquadscans.aishiteru.org) |  |
| [s2manga.com](https://s2manga.com) |  |
| [sectscans.com](https://sectscans.com) |  |
| [setsuscans.com](https://setsuscans.com) |  |
| [shibamanga.com](https://shibamanga.com) |  |
| [shootingstarscans.com](https://shootingstarscans.com) |  |
| [sitemanga.com](https://sitemanga.com) |  |
| [sleepytranslations.com](https://sleepytranslations.com) |  |
| [spiderscans.xyz](https://spiderscans.xyz) |  |
| [spmanhwa.online](https://spmanhwa.online) |  |
| [stonescape.xyz](https://stonescape.xyz) |  |
| [summanga.com](https://summanga.com) |  |
| [tappytoon.net](https://tappytoon.net) |  |
| [theblank.net](https://theblank.net) |  |
| [toonchill.com](https://toonchill.com) |  |
| [toongod.org](https://www.toongod.org) |  |
| [toonily.com](https://toonily.com) |  |
| [toonizy.com](https://toonizy.com) |  |
| [topmanhua.net](https://topmanhua.net) |  |
| [topreadmanhwa.com](https://topreadmanhwa.com) |  |
| [tritiniascans.com](https://tritinia.org) |  |
| [utoon.net](https://utoon.net) |  |
| [vyvymanga.org](https://vyvymanga.org) |  |
| [wearehunger.site](https://www.wearehunger.site) |  |
| [webdexscans.com](https://webdexscans.com) |  |
| [webniichan.online](https://webniichan.online) |  |
| [webtoonscan.com](https://webtoonscan.com) |  |
| [webtoonxyz.com](https://www.webtoon.xyz) |  |
| [weebcentral.com](https://weebcentral.com) | Supports scanlation group selection |
| [whalemanga.com](https://whalemanga.com) |  |
| [woopread.com](https://woopread.com) |  |
| [wuxiaworld.site](https://wuxiaworld.site) |  |
| [yakshacomics.com](https://yakshacomics.com) |  |
| [yakshascans.com](https://yakshascans.com) |  |
| [yaoihub.com](https://yaoihub.com) |  |
| [yaoiscan.com](https://yaoiscan.com) |  |
| [yaoitoon.com](https://yaoitoon.com) |  |
| [zandynofansub.com](https://zandynofansub.aishiteru.org) |  |
| [zazamanga.com](https://www.zazamanga.com) |  |
| [zinchanmanga.net](https://zinchangmanga.net) |  |
| [zinchanmangacom.com](https://zinchangmanga.com) |  |
| [zinmanga.org](https://mangazin.org) |  |
| [zinmangacom.com](https://zin-manga.com) |  |
| [zinmanganet.com](https://zinmanga.net) |  |

---

## 🚀 Usage

```bash
python3 comick_downloader.py [OPTIONS] COMIC_URL
```

Run `python3 comick_downloader.py --help` for the full option list.

---

## ⚙️ Options

| Option                        | Description                                                                                                                                                                                                                  | Default     |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- |
| `COMIC_URL`                   | URL of the manga’s main page (e.g. `https://mangataro.org/manga/shadow-slave`)                                                                                                                                               | _required_  |
| `--cookies STRING`            | HTTP cookie string for restricted content (e.g. `key1=val1; key2=val2`)                                                                                                                                                     | `""`        |
| `--group STRING...`           | Preferred scanlation groups (comma-separated or multiple args).                                                                                                                                                              | `[]`        |
| `--mix-by-upvote`             | When multiple `--group` entries exist, pick the version with the highest upvotes among them.                                                                                                                                 | `False`     |
| `--no-partials`               | Skip fractional chapters (e.g. 1.5).                                                                                                                                                                                          | `False`     |
| `--chapters STRING`           | Chapter filter: `all`, single (`5`), range (`1-10`), or list/ranges (`1,3,5-7`).                                                                                                                                             | `all`       |
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
| `--keep-images`               | Retain raw downloaded images under `comics/<Title>/Chapter_<n>/`.                                                                                                                                                             | `False`     |
| `--keep-chapters`             | Save each chapter as a separate file (`.pdf`, `.epub`, or `.cbz`).                                                                                                                                                            | `False`     |
| `--no-cleanup`                | Do not delete the temporary directory (`tmp_<hid>/`) after completion.                                                                                                                                                        | `False`     |
| `-v, --verbose`               | Enable detailed logging.                                                                                                                                                                                                      | `False`     |
| `-d, --debug`                 | Enable debug-level logging (image processing details).                                                                                                                                                                        | `False`     |

---

## 🔐 Cookie Setup

If authentication is required, export your cookies as a single quoted string:

```bash
export COOKIES='session_token=…; another_cookie=…'
```

Then run (adding `--site` if auto-detection ever fails):

```bash
python3 comick_downloader.py --site mangataro --cookies "$COOKIES" \
  "https://mangataro.org/manga/shadow-slave"
```

---

## 📖 Examples

1. **Page-layout EPUB**, chapters 1–2, preferred group **Asura**, verbose:

   ```bash
   python3 comick_downloader.py \
     --site mangataro \
     --group Asura \
     --chapters "1-2" \
     --format epub \
     --epub-layout page \
     --verbose \
     "https://mangataro.org/manga/re-zero-kara-hajimeru-isekai-seikatsu"
   ```

2. **Vertical EPUB**, chapters 1–20, split every 5 chapters, verbose & debug:

   ```bash
   python3 comick_downloader.py \
     --site mangataro \
     --chapters "1-20" \
     --format epub \
     --epub-layout vertical \
     --split 5ch \
     --verbose --debug \
     "https://mangataro.org/manga/shadow-slave"
   ```

3. **Asura PDF**, chapters 1–2 from a series page:

   ```bash
   python3 comick_downloader.py \
     --site asura \
     --chapters "1-2" \
     --format pdf \
     "https://asuracomic.net/series/regression-of-the-yong-clan-heir-9c9e0572"
   ```

4. **CBZ**, chapters 1–2, one chapter per file:

   ```bash
   python3 comick_downloader.py \
     --site mangataro \
     --chapters "1-2" \
     --format cbz \
     --keep-chapters \
     "https://mangataro.org/manga/shadow-slave"
   ```

5. **PDF**, chapters 1–2, save each chapter separately:

   ```bash
   python3 comick_downloader.py \
     --site mangataro \
     --chapters "1-2" \
     --format pdf \
     --keep-chapters \
     "https://mangataro.org/manga/re-zero-kara-hajimeru-isekai-seikatsu"
   ```

6. **Light-novel EPUB** (prose converted to XHTML, no image slicing):

   ```bash
   python3 comick_downloader.py \
    --site mangataro \
    --chapters "1-5" \
     --format epub \
     --epub-layout page \
     "https://mangataro.org/manga/re-zero-kara-hajimeru-isekai-seikatsu"
   ```

7. **Raw CBZ** (pages copied as-is):

   ```bash
   python3 comick_downloader.py \
     --site mangataro \
     --chapters "1-50" \
     --format cbz \
     --no-processing \
     "https://mangataro.org/manga/shadow-slave"
   ```

---

## 💡 Tips & Tricks

Here are some handy pointers to get the most out of your workflow:

- 🔄 **Ongoing Series Updates**  
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

*   **Temporary Data**: `tmp_<hid>/`
    *   `run_params.json`: Stores the processing settings for resume functionality.
*   **Final Files**: `comics/`
    *   Named `<Title>[_Groups]_Ch_<start>-<end>.<format>` (e.g., `My_Awesome_Comic_Asura_Ch_1-5.epub`)
*   **Raw Images** (if `--keep-images` is used):
    *   `comics/<Title>/Chapter_<n>/` (e.g., `comics/My_Awesome_Comic/Chapter_1/`)

```
tmp_<hid>/                   # Temporary workspace
└── run_params.json          # Saved settings (resume/restore)
comics/
├── <Title>[_Groups]_Ch_a-b.epub/pdf/cbz   # Final build(s)
└── <Title>/Chapter_<n>/                  # Raw images (if --keep-images)
```

- Final files → `comics/`  
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

> Tip: The multi-version test script expects Homebrew to provide `python@X.Y`. If you're on another platform, mimic that matrix with your package manager or pyenv so we keep coverage consistent.

---

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
