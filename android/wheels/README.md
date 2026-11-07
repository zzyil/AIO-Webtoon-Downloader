# Vendored wheels

Wheels no index serves for Android, built here and pinned in
`app/build.gradle.kts`. They fall into **two classes with completely different
failure modes**, and conflating them is the mistake this file exists to prevent:

| File | Version | SHA-256 | Size |
|---|---|---|---|
| `rapidfuzz-3.14.5-py3-none-any.whl` | 3.14.5 | `a81737316526ca70207424ce9942f628efb358e6d641dd932cff2c9395b1ca3e` | 64 KB |
| `pillow-11.0.0-1-cp313-cp313-android_24_arm64_v8a.whl` | 11.0.0 build 1 | `1ada85e390fc257e0f594127d0780a28d9b95fd409dd1575436d92c8e80c872c` | 539 KB |
| `pillow-11.0.0-1-cp313-cp313-android_24_x86_64.whl` | 11.0.0 build 1 | `80e3005b7abb5beeb0c7a03bcde8a493269d3a20c52e72b66b1d4b6d1bbc367e` | 537 KB |
| `chaquopy_libwebp-1.6.0-0-py3-none-android_24_arm64_v8a.whl` | 1.6.0 | `b9c5db8d44a35c2b08ce7ba222caa1076fb9899edc66ad7eaf435709ce35cd8a` | 296 KB |
| `chaquopy_libwebp-1.6.0-0-py3-none-android_24_x86_64.whl` | 1.6.0 | `f5769f3404f7c1083319e151ce15592d9dd712a210f3a244b03df96a13c92c21` | 326 KB |

**`py3-none-any` (rapidfuzz)** installs on any Python 3 and cannot be
invalidated by a toolchain move.

**Per-ABI native (Pillow, chaquopy-libwebp)** is compiled against one Python
minor version and one ABI. **Bumping `chaquopy { version = "3.13" }` in
`app/build.gradle.kts` silently invalidates the Pillow wheels** — `cp313` stops
matching, pip falls back to Chaquopy's own index, and the only symptom is that
WebP quietly starts going through the Kotlin bridge again. Adding an ABI to
`abiFilters` has the same effect for that ABI. Rebuild per "Regenerating"
below whenever either moves. (`chaquopy_libwebp` is tagged `py3-none` because
it contains no Python at all — but it is still per-ABI, so an ABI change
invalidates it too.)

---

## rapidfuzz — the pure-Python wheel

Chaquopy's package index publishes no `rapidfuzz`
(`https://chaquo.com/pypi-13.1/rapidfuzz/` → 404), and PyPI publishes no
platform-independent one. Without it, cross-site search and AniList enrichment
raise `ImportError` on device while downloads keep working — see
`sites/external_metadata.py:_load_rapidfuzz`.

This is rapidfuzz's own **pure-Python build**, produced from the official PyPI
sdist. Not a fork, not a substitute matcher, not a repackage — the 37 `.py`
files in it are byte-identical to the ones in a normal desktop
`pip install rapidfuzz==3.14.5`.

rapidfuzz ships two backends and picks whichever imports: a compiled `*_cpp`
extension, or the pure-Python `*_py` reference modules it falls back to. That
fallback is upstream's own supported configuration — `pyproject.toml` defaults
to `wheel.cmake = false` and prints *"CMake unavailable, falling back to pure
Python Extension"* — and it is what piwheels and conda-less builds use. So this
needs no NDK, no cross-compilation and no per-ABI artifact.

**The two backends are not bit-identical.** `sites/fuzzy_match.py` normalizes
the three codepoints responsible and its header documents the residue;
`tests/test_fuzzy_match.py` re-derives the divergence from the installed
rapidfuzz and fails if it widens. Read that header before changing anything
here — every threshold in the matcher is calibrated to specific score numbers.

### Why not build the native wheel

It was the original plan. The measured case against it: the compiled backend is
**16x faster**, which sounds decisive until you attach a unit — a full
search-plus-enrichment operation is ~1950 scorer calls, i.e. **4.9 ms compiled
vs 79 ms pure-Python**, inside a search that spends 60+ seconds on network I/O.
Against that, the native route costs a per-ABI binary committed to the repo and
a rebuild every time Chaquopy's Python version moves.

The toolchain to do it now exists (see "Regenerating" below) and rapidfuzz is a
plausible `build-wheel.py` target — Cython plus header-only C++ over
`rapidfuzz-cpp`, no external native deps. `fuzzy_match`'s normalization is
correct either way, and its `rapidfuzz_backend()` would start reporting `"cpp"`
on device. The cost/benefit above has not changed, so it is still not done.

### Regenerating it

Any machine, no Android toolchain, but **no `cmake` on `PATH`** (with cmake
present, scikit-build-core would build a host-native extension and the wheel
would stop being portable — `--config-settings` forces the right path
regardless, but check the filename says `py3-none-any` before shipping):

```bash
pip wheel "rapidfuzz==3.14.5" --no-binary rapidfuzz --no-deps --config-settings=wheel.cmake=false -w android/wheels
```

Then verify it is what it claims to be:

```bash
python -c "import zipfile; z=zipfile.ZipFile('android/wheels/rapidfuzz-3.14.5-py3-none-any.whl'); assert not [n for n in z.namelist() if n.endswith(('.so','.pyd','.dll'))], 'native artifacts leaked in'; print(z.read('rapidfuzz-3.14.5.dist-info/WHEEL').decode())"
```

`Root-Is-Purelib: true` and `Tag: py3-none-any` are the two lines that matter.

---

## Pillow + chaquopy-libwebp — a WebP codec inside Pillow

**Chaquopy's own Pillow has no WebP codec.** Confirmed by unpacking
`app/build/python/pip/debug/{arm64-v8a,x86_64}/PIL/`: the five other extensions
are there and `_webp.so` is not, in either ABI (`common/PIL/` carries only
`_webp.pyi`, a type stub). 11.0.0 is the only Pillow that index publishes, so
there was no version to bump to. The cause is upstream's recipe — Chaquopy's
`server/pypi/packages/pillow/meta.yaml` lists exactly two host requirements,
`chaquopy-libjpeg` and `chaquopy-freetype`, and **there is no
`chaquopy-libwebp` recipe in the whole repository**, so Pillow's `setup.py`
finds no libwebp and drops the extension.

These wheels add it: a new `chaquopy-libwebp` recipe, plus Pillow rebuilt
against it.

### This is an optimization, not a rescue

**Nothing is broken without these wheels.** `sites/image_codec.py` plus the
Kotlin `ImageCodecBridge` already give Pillow a WebP opener and saver over
`BitmapFactory` / `Bitmap.compress`, and that is verified working on device.
What the rebuild buys:

- **The shim self-disables.** `install_pillow_shims()` registers nothing when
  `PIL.features.check("webp")` is True, so `Image.SAVE`/`Image.OPEN` are
  Pillow's own again with no code change anywhere. That gating-on-capability
  rather than on-platform is exactly what it was written for.
- **No PNG round-trip.** The bridge's interchange format is PNG, so every WebP
  page costs a `Bitmap.compress(PNG)` plus a PIL PNG decode on the way in, and
  the reverse on the way out. At ~1600x2400 per page that is real work the
  native codec does not do.
- **Real libwebp encoder controls.** `Bitmap.compress` exposes a quality int
  and a lossy/lossless format choice, and nothing else.

So if these wheels are ever a maintenance problem, **deleting them is a safe
retreat**: pip falls back to the index's webp-less build 0 and the bridge picks
the work back up. Nothing else needs to change. That is deliberate, and it is
why the pip block resolves Pillow by name rather than by path.

### How pip picks these over Chaquopy's index

`app/build.gradle.kts` passes `options("--find-links", vendoredWheelsDir…)` and
still says `install("Pillow==11.0.0")`. Both wheels satisfy that specifier, and
**pip sorts candidates by build tag**, so build `1` here beats build `0` on the
index. `install(<path>)` would not work: Chaquopy runs pip once per ABI, and a
literal path would pin one ABI's wheel for both.

`--find-links` also does something the version pin cannot: the rebuilt Pillow's
METADATA carries `Requires-Dist: chaquopy-libwebp (>=1.6.0)`, a package that
exists on **no** index. Without the find-links entry, resolution fails outright.

That `Requires-Dist` line is also the cheapest way to tell which wheel a build
actually used — the index's build 0 requires only freetype and libjpeg:

```bash
grep chaquopy android/app/build/python/pip/debug/common/pillow-11.0.0.dist-info/METADATA
```

Three lines including `chaquopy-libwebp` means these wheels are in force; two
means pip fell back to the index.

### Verifying the wheels

Native wheels can lie in a way `py3-none-any` ones cannot: the ABI is in the
filename but the truth is in the ELF header, and a build for the wrong ABI
loads fine on the build machine and fails only on the phone. So check the
machine type, not just the name:

```bash
python -c "
import io, sys, zipfile
from elftools.elf.elffile import ELFFile          # pip install pyelftools
EXPECT = {'arm64_v8a': 'EM_AARCH64', 'x86_64': 'EM_X86_64'}
for name in sys.argv[1:]:
    z = zipfile.ZipFile(name)
    abi = next(a for a in EXPECT if name.endswith(a + '.whl'))
    sos = [n for n in z.namelist() if n.endswith('.so')]
    for so in sos:
        got = ELFFile(io.BytesIO(z.read(so))).header.e_machine
        assert got == EXPECT[abi], f'{name}: {so} is {got}, filename says {abi}'
    print(f'{name}\n  {len(sos)} ELF objects, all {EXPECT[abi]}\n  {sorted(sos)}')
" android/wheels/pillow-11.0.0-1-cp313-cp313-android_24_arm64_v8a.whl \
  android/wheels/pillow-11.0.0-1-cp313-cp313-android_24_x86_64.whl \
  android/wheels/chaquopy_libwebp-1.6.0-0-py3-none-android_24_arm64_v8a.whl \
  android/wheels/chaquopy_libwebp-1.6.0-0-py3-none-android_24_x86_64.whl
```

Each Pillow wheel must list **six** objects including `PIL/_webp.so` — five is
the webp-less build. Each libwebp wheel must list four:
`libwebp.so`, `libwebpmux.so`, `libwebpdemux.so`, `libsharpyuv.so`. Pillow's
`_webp.so` records the first three as `DT_NEEDED`; `libsharpyuv.so` is
libwebp's own dependency and is easy to drop by accident, at which point
`import PIL._webp` fails on device with an unhelpful linker error.

On device, the one-line answer is already logged. `Aio.kt` logs the snapshot
`set_image_codec_bridge` returns, so `adb logcat -s AioCore:*` reports
`pillow_webp_decode` / `pillow_webp_encode` true and `bridge_installed` empty
when these wheels are working — the bridge correctly buying nothing.

### Regenerating them

**Linux x86-64 only** — Chaquopy's `build-wheel.py` says so explicitly, and WSL
counts. (Chaquopy's `server/pypi/README.md` now recommends cibuildwheel for
Python 3.13+, but that route has no answer for the native dependency: nothing
cross-compiles libwebp for you, and `build-wheel.py`'s `requirements.host`
mechanism does exactly that. `build-wheel.py` still accepts `--python 3.13`.)

Setup, once:

```bash
sudo apt install build-essential patch patchelf unzip zip curl wget git \
                 autoconf automake libtool pkg-config openjdk-21-jdk-headless

git clone --depth 1 https://github.com/chaquo/chaquopy.git
cd chaquopy

# build-wheel must RUN on the Python minor version it builds FOR. Ubuntu 26.04
# ships 3.14 and has no python3.13 package; uv's standalone build is the least
# invasive source (Chaquopy's own docs suggest Miniconda for the same reason).
uv python install 3.13
~/.local/share/uv/python/cpython-3.13-*/bin/python3.13 -m venv ~/bw-venv
~/bw-venv/bin/pip install -r server/pypi/requirements.txt
export PATH=~/bw-venv/bin:$PATH        # provides `python3.13`

# Android SDK. build-wheel installs NDK 27.3.13750724 itself on first use
# (the version is pinned in chaquopy/target/android-env.sh) — but only if the
# SDK licences are already accepted; copy them from an existing SDK rather
# than blanket-accepting.
export ANDROID_HOME=~/android-sdk
# ...unzip commandlinetools-linux-*.zip to $ANDROID_HOME/cmdline-tools/latest
cp /path/to/existing/Sdk/licenses/* $ANDROID_HOME/licenses/

# The interpreter Pillow links against.
./target/download-target.sh maven/com/chaquo/python/target/3.13.9-0
```

Then apply this repo's recipe overlay and build. `recipes/` here holds only the
files that **differ** from Chaquopy's — `pillow/meta.yaml` replaces upstream's,
`pillow/patches/chaquopy-webp.patch` is added alongside upstream's
`chaquopy.patch` (build-wheel applies every file in `patches/`), and
`chaquopy-libwebp/` is entirely new:

```bash
AIO=/path/to/AIO-Webtoon-Downloader
cp -r $AIO/android/wheels/recipes/chaquopy-libwebp server/pypi/packages/
cp $AIO/android/wheels/recipes/pillow/meta.yaml     server/pypi/packages/pillow/
cp $AIO/android/wheels/recipes/pillow/patches/*     server/pypi/packages/pillow/patches/
chmod +x server/pypi/packages/chaquopy-libwebp/build.sh

# Pillow's other two host requirements are prebuilt and build-wheel will not
# fetch them for you; it only tells you where they live.
for pkg_file in \
  "chaquopy-libjpeg  chaquopy_libjpeg-1.5.3-1-py3-none-android_21_arm64_v8a.whl" \
  "chaquopy-libjpeg  chaquopy_libjpeg-1.5.3-1-py3-none-android_21_x86_64.whl" \
  "chaquopy-freetype chaquopy_freetype-2.9.1-2-py3-none-android_21_arm64_v8a.whl" \
  "chaquopy-freetype chaquopy_freetype-2.9.1-2-py3-none-android_21_x86_64.whl"; do
    set -- $pkg_file
    mkdir -p server/pypi/dist/$1
    curl -fsSL -o server/pypi/dist/$1/$2 "https://chaquo.com/pypi-13.1/$1/$2"
done

cd server/pypi
for abi in arm64-v8a x86_64; do ./build-wheel.py --python 3.13 --abi $abi chaquopy-libwebp; done
for abi in arm64-v8a x86_64; do ./build-wheel.py --python 3.13 --abi $abi pillow; done
cp dist/chaquopy-libwebp/*.whl dist/pillow/*.whl $AIO/android/wheels/
```

**Delete the superseded wheels from `android/wheels/` by hand.** A stale
`cp313` Pillow left beside a new `cp314` one is inert, but a stale one left
beside a *rebuilt* `cp313` with a **higher build number** would win — so bump
`build: number:` in `recipes/pillow/meta.yaml` only when you mean to, and
remove what it replaces.

---

## What was tried and rejected: pillow-jxl-plugin

`--modernize`'s JPEG XL path. **Not attempted past the feasibility check**, and
the reason is that it is not one cross-compile but three stacked:

- It is **Rust, not C**. PyPI's own summary is "Pillow plugin for JPEG-XL, using
  Rust for bindings", and `Cargo.toml` (1.3.8) declares
  `crate-type = ["cdylib"]` over `pyo3 0.29` and `jpegxl-rs 0.15`. So it needs
  a Rust toolchain with the `aarch64-linux-android` and `x86_64-linux-android`
  targets linking through the NDK.
- **pyo3 must cross-compile** against Chaquopy's `libpython3.13.so`, which
  means getting `PYO3_CROSS_*` pointed at the unpacked `target-3.13.9-0-<abi>`
  zip. build-wheel does not do this for you; nothing in `packages/` is a pyo3
  crate.
- `jpegxl-rs` is FFI over **libjxl** (C++17), so there is still a native
  library to produce — either by adding `chaquopy-libjxl` (libjxl pulls brotli
  and highway) or via the crate's `vendored` feature, which builds libjxl from
  source with CMake inside the Rust build, under the NDK toolchain.

Against that, the payoff is narrow: `--modernize` is opt-in, it is **not wired
into the Android UI at all**, and it is an archival re-encode of an existing
library rather than anything a download needs. `build-wheel.py` does support
`rust` as a build requirement (`rustup` on `PATH`), so this is not impossible —
it is just badly priced. Revisit only if `--modernize` becomes a shipped
Android feature.

Two related notes for whoever picks this up:

- **AVIF is not a Pillow-11 option either.** Chaquopy's index has no
  `pillow-avif-plugin`, and native AVIF only arrives in Pillow 12. Android's
  `Bitmap.CompressFormat` has no AVIF at any API level, so
  `image_capabilities()`'s `effective_avif_encode` is false regardless; decode
  is the platform's on API 31+.
- **The Pillow version ceiling is gone.** "Chaquopy publishes at most Pillow
  11.0.0" stopped being a constraint the moment this directory started building
  its own — a Pillow 12 recipe would get native AVIF for free. It is not done
  here because it would make Android's Pillow a different major version from
  every desktop install, which is a parity question, not a packaging one.
