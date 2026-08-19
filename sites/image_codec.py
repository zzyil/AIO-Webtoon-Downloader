"""Pluggable image codec backend for formats Pillow's own build cannot handle.

What this module owns: a tiny embedder seam (decode / encode / formats), plus
the PIL *registry shim* that routes `Image.open` and `Image.save(format="WebP")`
through that seam when — and only when — Pillow itself has no codec.

Who reads from it:
  * aio_android.py — installs the Android bridge (grep set_image_codec_bridge)
    and reports the resulting capabilities (grep image_capabilities).
  * NOTHING ELSE, and that is the entire design. aio-dl.py has ~15
    `Image.open` / `im.save(format="WebP")` sites and sites/_image_io.py sniffs
    magic bytes; none of them changed, because the shim installs itself into
    PIL's own dispatch tables instead of asking call sites to route around it.

Depends on: Pillow, imported LAZILY inside the functions that need it. A
process with no Pillow at all can still import this module, and a desktop
process pays nothing — see DESKTOP IS BYTE-IDENTICAL below.

---------------------------------------------------------------------------
WHY THIS EXISTS

Chaquopy's `Pillow==11.0.0` wheel ships **no `_webp.so`** (verified by
unpacking it: `android/app/build/python/pip/debug/{arm64-v8a,x86_64}/PIL/`
contains `_imaging`, `_imagingft`, `_imagingmath`, `_imagingmorph`,
`_imagingtk` and nothing else; the `common/PIL/_webp.pyi` beside them is a
TYPE STUB). `PIL/WebPImagePlugin.py` registers WebP *open* unconditionally but
*save* only `if SUPPORTED`, so on device:

    im.save(..., format="WebP")  -> KeyError('WEBP')          (NOT an OSError)
    Image.open(<webp bytes>)     -> UnidentifiedImageError, preceded by
                                    UserWarning: "image file could not be
                                    identified because WEBP support not
                                    installed"

That single fact is `android/PARITY.md` defects D3 (`--webtoon-recompress` is a
silent no-op), D4 (EPUB/PDF/`--width`/`--scaling`/`--quality<100` silently drop
or fail WebP pages) and D8 (`diagnostics()` could not see either). There is no
wheel to fix it with: Chaquopy's index publishes no pillow-jxl-plugin, no
pillow-avif-plugin, no pyvips, no imagecodecs, and Pillow tops out at 11.0.0.

Android's *platform* has had a WebP codec since API 14 — `BitmapFactory` and
`Bitmap.compress` — it is simply not wired to Pillow. This module is that wire.

---------------------------------------------------------------------------
DESKTOP IS BYTE-IDENTICAL

`install_pillow_shims()` no-ops unless BOTH are true: Pillow reports no codec
for the format, AND an embedder has installed a backend. Desktop satisfies
neither, so nothing is registered and PIL's compiled `_webp` keeps handling
everything exactly as before. If the "rebuild the wheel" spike ever lands,
`features.check("webp")` flips to True and the shim self-disables with no code
change — that is the intended upgrade path, not a conflict.

---------------------------------------------------------------------------
PNG IS THE INTERCHANGE FORMAT, ON PURPOSE

Both directions of the seam carry PNG bytes rather than raw RGBA. Raw pixels
would be faster and it is tempting, but it demands stride, alpha
pre-multiplication and colour-space negotiation across a JNI boundary, and
every bug in that negotiation is a SILENTLY WRONG PIXEL — the worst failure
shape available for an archival downloader. PNG is lossless, self-describing,
and both sides already have a battle-tested codec for it (PIL natively;
Android's own PNG encoder). Both directions land exclusively on the SLOW paths
(EPUB/PDF/`--quality<100`/`--webtoon-recompress`) where one extra
encode+decode is noise next to the resize and the WebP encode itself.

FUTURE OPTIMISATION, named so nobody has to rediscover it: a raw-RGBA fast
path (`Bitmap.copyPixelsToBuffer` <-> `Image.frombytes("RGBA", size, buf)`)
would remove two PNG round-trips per page. Do it only with a byte-equality
test against the PNG path across all four Bitmap configs.

MEMORY, which is the real cost on a phone: a decode holds the source bytes,
the PNG, a JVM `Bitmap` and a PIL core image at once. A stitched LINE-Webtoon
page (1500x12000 ARGB_8888) is ~72 MB of JVM heap for the Bitmap alone, and
aio-dl.py runs these in a pool sized at half the CPU budget. If a device OOMs
inside an EPUB build, that pool width is the first thing to look at, and the
Kotlin side recycling its Bitmaps promptly is the second.

---------------------------------------------------------------------------
A PYTHON-SIDE BACKEND MUST BYPASS THE PIL REGISTRY

Only relevant if somebody ever writes a backend in Python (a test double, a
pyvips or imagecodecs adapter). Once a shim is registered, `Image.open` on a
WebP dispatches to THAT SHIM — so a backend whose `decode_to_png` calls
`Image.open` re-enters the shim it is servicing and recurses. `decode_to_png`
below swallows backend exceptions, so the RecursionError surfaces as a bare
UnidentifiedImageError with nothing pointing at the cause. Such a backend has
to reach for the plugin class directly (`WebPImagePlugin.WebPImageFile(fp)`,
`WebPImagePlugin._save(im, fp, "")`) — see the round-trip test in
tests/test_image_codec.py. Android is immune: its backend is Kotlin and has
never heard of PIL. The opener additionally REQUIRES the decoded bytes to
carry the PNG signature, which turns that recursion into a clean failure.

---------------------------------------------------------------------------
THREADING

Unlike sites/browser_backend.py's WebView backend, this seam does NOT need the
Android main thread — `BitmapFactory` and `Bitmap.compress` are ordinary
thread-safe calls. That is load-bearing: aio-dl.py encodes pages from a
ThreadPoolExecutor, and a main-thread hop per page would serialize the whole
pool behind the UI. Backends must therefore be re-entrant.
"""

from __future__ import annotations

import io
import threading
from typing import Any, Dict, Optional, Protocol, Set, runtime_checkable

#: Formats this module knows how to shim, mapped to their PIL registration
#: facts. Extend by adding a row AND teaching the backend to declare the name
#: from `formats()`; nothing else in this file is format-specific.
#: `probe` is the `PIL.features.check` name used to ask "does Pillow already
#: have this?" — an unknown name there returns False with a warning, which is
#: why the probe is membership-tested before it is called.
_SHIMMABLE: Dict[str, Dict[str, str]] = {
    "WEBP": {"probe": "webp", "ext": ".webp", "mime": "image/webp"},
    "AVIF": {"probe": "avif", "ext": ".avif", "mime": "image/avif"},
}


@runtime_checkable
class ImageCodecBackend(Protocol):
    """A host image codec. Deliberately TWO operations wide plus a capability
    declaration — resist widening it, because everything added here is
    something every future backend has to reimplement.

    Implementations MUST be re-entrant (see THREADING in the module header) and
    MUST NOT raise: an empty `bytes` return is the failure signal, so a codec
    that cannot service a call degrades to "Pillow could not read/write this"
    rather than to an exception crossing a language boundary.
    """

    def decode_to_png(self, data: bytes) -> bytes:
        """Any format the host can read -> PNG bytes. b"" when it cannot."""
        ...

    def encode_webp(self, png: bytes, quality: int, lossless: bool) -> bytes:
        """PNG bytes -> WebP bytes. b"" when it cannot."""
        ...

    def formats(self) -> Set[str]:
        """Upper-case format names this backend can DECODE. `{"WEBP"}` today;
        an Android backend on API 31+ may also declare `"AVIF"`. Encoding is
        WebP-only by construction — `Bitmap.CompressFormat` has no other
        modern format."""
        ...


# ---------------------------------------------------------------------------
# Registry
#
# One process-wide backend, not a per-profile map like browser_backend's: a
# codec is stateless and there is nothing to isolate. Everything else about the
# shape (install-once, `custom_backend()` as the "did an embedder supply one?"
# question, `reset()` for tests) mirrors that module deliberately.
# ---------------------------------------------------------------------------

_BACKEND: Optional[ImageCodecBackend] = None
_LOCK = threading.RLock()

#: Per-format PIL registry state captured at install time, so [reset] can put
#: the tables back EXACTLY as they were. Absent entries are recorded as absent.
_INSTALLED: Dict[str, Dict[str, Any]] = {}


def set_backend(backend: Optional[ImageCodecBackend]) -> None:
    """Install (or with None, remove) the process-wide codec backend.

    Android calls this exactly once from aio_android.py before any handler
    runs. Desktop never calls it. Removing a backend also unregisters whatever
    shims were installed for it — leaving a shim pointing at a dead backend
    would turn every WebP page into an UnidentifiedImageError.
    """
    global _BACKEND
    with _LOCK:
        # Unconditional, not just on None: REPLACING a backend must also drop
        # the old shims, or a new backend declaring fewer formats would inherit
        # a registration it cannot service. Same rule as
        # browser_backend.set_backend_factory, which closes existing instances
        # on any replacement. Callers re-run install_pillow_shims after.
        _uninstall_locked()
        _BACKEND = backend


def custom_backend() -> Optional[ImageCodecBackend]:
    """The EMBEDDER-INSTALLED backend, or None on the built-in desktop path.

    Named to match sites/browser_backend.custom_backend, and it answers the
    same question: "has somebody explicitly supplied one?" Never raises.
    """
    return _BACKEND


def available() -> bool:
    return _BACKEND is not None


def formats() -> Set[str]:
    """Upper-case format names the installed backend declares, or an empty set.

    Swallows a misbehaving backend rather than propagating — this is read from
    the diagnostics path, and a diagnostics call that raises is worse than one
    that reports nothing.
    """
    backend = _BACKEND
    if backend is None:
        return set()
    try:
        return {str(name).upper() for name in (backend.formats() or ())}
    except Exception:
        return set()


def decode_to_png(data: bytes) -> bytes:
    """Host-decode `data` to PNG bytes. b"" when there is no backend or it
    failed — callers treat that as "not decodable", never as an error."""
    backend = _BACKEND
    if backend is None or not data:
        return b""
    try:
        return bytes(backend.decode_to_png(data) or b"")
    except Exception:
        return b""


def encode_webp(png: bytes, quality: int, lossless: bool) -> bytes:
    """Host-encode PNG bytes to WebP. b"" when there is no backend or it
    failed; the PIL saver turns that into the OSError PIL's own WebP encoder
    raises for the same condition."""
    backend = _BACKEND
    if backend is None or not png:
        return b""
    try:
        return bytes(backend.encode_webp(png, int(quality), bool(lossless)) or b"")
    except Exception:
        return b""


def reset() -> None:
    """Unregister every shim and forget the backend.

    For tests above all — a leaked WEBP registration would change how EVERY
    later test in the process opens an image. No atexit hook (unlike
    browser_backend, which has a Chromium child to kill): there is no OS
    resource here, only dictionary entries that die with the interpreter.
    """
    global _BACKEND
    with _LOCK:
        _uninstall_locked()
        _BACKEND = None


def shims_installed() -> Set[str]:
    """Formats currently shimmed. Empty on desktop, always."""
    with _LOCK:
        return set(_INSTALLED)


# ---------------------------------------------------------------------------
# Pillow capability probe
# ---------------------------------------------------------------------------


def pillow_supports(fmt: str) -> bool:
    """Does the INSTALLED Pillow have a native codec for `fmt`?

    This is the seam the tests monkeypatch: a dev machine has a working
    `_webp.so`, so the codec-less branch is otherwise unreachable offline.

    Returns False when Pillow is missing entirely, when the feature name is
    unknown to this Pillow (11.0.0 has never heard of "avif"), or when the
    probe itself throws. False means "shim it if you can", which is the safe
    direction: the shim only ever ACTIVATES on a format PIL cannot handle, and
    a wrong False on a format PIL CAN handle is caught by
    [install_pillow_shims]'s second condition — no backend, no registration.
    """
    row = _SHIMMABLE.get(fmt.upper())
    if row is None:
        return False
    try:
        from PIL import features  # noqa: PLC0415 - lazy on purpose, see header
    except Exception:
        return False
    name = row["probe"]
    try:
        # Membership-tested first: features.check warns "Unknown feature" for a
        # name this Pillow does not know, and that warning would fire on every
        # Android start for "avif".
        known = (
            name in getattr(features, "codecs", {})
            or name in getattr(features, "modules", {})
            or name in getattr(features, "features", {})
        )
        return bool(known and features.check(name))
    except Exception:
        return False


def registered_save(fmt: str) -> bool:
    """Is `fmt` in Pillow's SAVE table right now — shim included?

    This is the literal `"WEBP" in PIL.Image.SAVE` measurement PARITY.md D8
    asks for, and it exists as a function because reading `Image.SAVE`
    naively gives the WRONG answer: the table is populated LAZILY by
    `Image.init()`, so before the first image operation it is empty and every
    format reports False. Forcing the init is the whole point.
    """
    try:
        from PIL import Image  # noqa: PLC0415

        Image.init()
        return fmt.upper() in Image.SAVE
    except Exception:
        return False


def native_save(fmt: str) -> bool:
    """Is Pillow's OWN encoder registered for `fmt` (i.e. not ours)?

    Distinguishing the two is the point: once a shim is installed,
    [registered_save] is True for a Pillow that has no codec at all, and a
    diagnostics reader needs to be able to tell those apart.
    """
    try:
        from PIL import Image  # noqa: PLC0415

        Image.init()
        handler = Image.SAVE.get(fmt.upper())
    except Exception:
        return False
    return handler is not None and handler is not _save_webp


# NOTE there is deliberately no `can_open(fmt)` helper to match the two above.
# `fmt in Image.OPEN` is a LIE for WebP — `WebPImagePlugin` registers its opener
# unconditionally and its `_accept` then returns a warning STRING instead of
# True, so a codec-less Pillow has "WEBP" in OPEN and still cannot open one.
# `capabilities()["effective_webp"]` is the question worth asking.


# ---------------------------------------------------------------------------
# The PIL registry shim
# ---------------------------------------------------------------------------


#: "this key was not in the table" — distinct from a stored None, which for
#: `OPEN[fmt] = (factory, accept)` would be a legal-looking value.
_ABSENT = object()


def install_pillow_shims() -> Set[str]:
    """Register opener/saver shims for every format Pillow lacks and the
    backend supplies. Returns the set of formats newly shimmed (empty is the
    normal, healthy desktop answer).

    IDEMPOTENT: a format already shimmed is skipped, so calling this twice
    cannot double-register or corrupt the saved-state snapshot [reset] restores
    from.

    NEVER RAISES. It runs from aio_android.configure-time code on a device with
    no console; a codec upgrade path that can take the app down is worse than
    no codec.
    """
    with _LOCK:
        if _BACKEND is None:
            return set()
        try:
            from PIL import Image, ImageFile  # noqa: PLC0415
        except Exception:
            return set()

        declared = formats()
        # Force the full plugin import FIRST. Two reasons, both bugs if
        # skipped: (1) `Image.init()` imports WebPImagePlugin, which calls
        # `register_open("WEBP", ...)` — running it AFTER us would silently
        # overwrite our opener with the broken one, at whatever unrelated
        # moment something first touched an image; (2) `Image.SAVE` is empty
        # until init, so the snapshot [reset] restores would record "absent"
        # for entries that are merely not loaded yet.
        try:
            Image.init()
        except Exception:
            return set()

        installed: Set[str] = set()
        for fmt, row in _SHIMMABLE.items():
            if fmt in _INSTALLED or fmt not in declared or pillow_supports(fmt):
                continue
            try:
                _install_one_locked(Image, ImageFile, fmt, row)
            except Exception:
                continue
            installed.add(fmt)
        return installed


def _install_one_locked(Image, ImageFile, fmt: str, row: Dict[str, str]) -> None:
    """Register one format. Caller holds [_LOCK] and has run `Image.init()`."""
    _INSTALLED[fmt] = {
        "open": Image.OPEN.get(fmt, _ABSENT),
        "save": Image.SAVE.get(fmt, _ABSENT),
        "ext": Image.EXTENSION.get(row["ext"], _ABSENT),
        "mime": Image.MIME.get(fmt, _ABSENT),
        "in_id": fmt in Image.ID,
    }
    Image.register_open(fmt, _build_opener(Image, ImageFile, fmt), _accept_for(fmt))
    # DECODE-ONLY for anything but WebP: `Bitmap.CompressFormat` has JPEG, PNG
    # and WebP and nothing else, so an AVIF saver would be a promise no backend
    # can keep. A caller asking for one still gets PIL's own honest KeyError.
    if fmt == "WEBP":
        Image.register_save(fmt, _save_webp)
        Image.register_extension(fmt, row["ext"])
        Image.register_mime(fmt, row["mime"])


def _uninstall_locked() -> None:
    """Put PIL's tables back exactly as they were found."""
    if not _INSTALLED:
        return
    try:
        from PIL import Image  # noqa: PLC0415
    except Exception:
        _INSTALLED.clear()
        return
    for fmt, saved in list(_INSTALLED.items()):
        row = _SHIMMABLE[fmt]
        _restore(Image.OPEN, fmt, saved["open"])
        _restore(Image.SAVE, fmt, saved["save"])
        _restore(Image.EXTENSION, row["ext"], saved["ext"])
        _restore(Image.MIME, fmt, saved["mime"])
        if not saved["in_id"]:
            try:
                Image.ID.remove(fmt)
            except ValueError:
                pass
    _INSTALLED.clear()


def _restore(table: Dict[Any, Any], key: Any, value: Any) -> None:
    if value is _ABSENT:
        table.pop(key, None)
    else:
        table[key] = value


# --- openers ---------------------------------------------------------------


def _accept_for(fmt: str):
    """The magic-byte test PIL calls before instantiating our opener.

    Mirrors each plugin's own `_accept` rather than delegating to
    sites/_image_io.image_magic_extension: PIL passes only a 16-byte prefix,
    and this must return a plain bool — the codec-less WebP plugin's habit of
    returning a warning STRING is precisely the behaviour being replaced.
    """
    if fmt == "WEBP":

        def _accept(prefix: bytes) -> bool:
            return (
                len(prefix) >= 16
                and prefix[:4] == b"RIFF"
                and prefix[8:12] == b"WEBP"
                and prefix[12:16] in (b"VP8 ", b"VP8X", b"VP8L")
            )

        return _accept

    def _accept_avif(prefix: bytes) -> bool:
        # ISO-BMFF ftyp box; the brand list matches sites/_image_io's AVIF
        # magic table (grep image_magic_extension).
        return len(prefix) >= 12 and prefix[4:8] == b"ftyp" and prefix[8:12] in (
            b"avif",
            b"avis",
        )

    return _accept_avif


def _build_opener(Image, ImageFile, fmt: str):
    """An `ImageFile.ImageFile` subclass that decodes through the backend.

    HOW IT WORKS, since it is not the shape a normal plugin has: a normal
    plugin describes its data to PIL's C decoders via `self.tile`, which we
    cannot do — the pixels come back from the JVM, not from `self.fp`. So
    `_open` decodes eagerly, borrows the core image out of the PNG that PIL
    just parsed for us (`self.im = base.im`), and leaves `self.tile = []`.
    `ImageFile.load` then short-circuits to `Image.Image.load`, which hands
    back pixel access off that core image. Verified against Pillow 11.0.0
    (Chaquopy's) and 12.1.0 (desktop): both use `_mode` + the `im` property,
    and both accept an empty tile list once `_im` is set.
    """

    class _BridgeImageFile(ImageFile.ImageFile):
        format = fmt
        format_description = f"{fmt} (decoded by the embedder image-codec bridge)"

        def _open(self) -> None:
            try:
                self.fp.seek(0)
                data = self.fp.read()
            except Exception as exc:
                raise SyntaxError(f"{fmt}: unreadable source ({exc})") from exc

            # BOMB CHECK BEFORE THE DECODE, on dimensions read from the
            # container header. PIL runs `_decompression_bomb_check(im.size)`
            # only AFTER the opener returns, which for a normal lazy plugin is
            # fine — nothing is allocated yet. This opener is not lazy: by the
            # time PIL looks, the backend has already materialized a JVM Bitmap,
            # an interchange PNG and a PIL core image. On the memory-constrained
            # phone this shim exists for, the guard was running after the damage.
            #
            # Reusing sites/_image_io.sniff_image_dimensions rather than parsing
            # here: it already covers all three WebP codecs (VP8 / VP8L / VP8X),
            # is tested, imports no Pillow, and is what the download path
            # already trusts to size images. Unparseable header -> no check, the
            # same position PIL's own late check leaves us in.
            #
            # Deliberately NOT made lazy instead. Deferring the decode to load()
            # would move a bridge failure from Image.open (UnidentifiedImageError)
            # to load() (OSError), a semantic change for every caller that guards
            # only the open — and the reason laziness looked attractive, a
            # metadata-only reader paying a full decode, was removed at the
            # caller instead (grep _image_magic_extension in aio-dl.py).
            try:
                from ._image_io import sniff_image_dimensions

                _dims = sniff_image_dimensions(data[:64])
            except Exception:
                _dims = None
            if _dims is not None:
                Image._decompression_bomb_check(_dims)

            png = decode_to_png(data)
            # The magic check is NOT paranoia about a corrupt decode — it
            # breaks an INFINITE RECURSION. A backend that echoed its input
            # (or fell back to "return the original bytes" on an unsupported
            # feature) would hand a WebP back here, `Image.open` below would
            # dispatch to this same opener, and the process would die with a
            # RecursionError from inside a page save. The contract says PNG;
            # this enforces it.
            if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                # SyntaxError, not OSError. `Image.open` catches SyntaxError
                # per-format and falls through to UnidentifiedImageError —
                # which is exactly what a codec-less Pillow already raises, so
                # every existing caller's error handling stays correct.
                raise SyntaxError(f"{fmt}: image-codec bridge could not decode")

            base = Image.open(io.BytesIO(png))
            base.load()
            self._mode = base.mode
            self._size = base.size
            self.im = base.im
            self.tile = []

            if fmt == "WEBP" and _webp_is_animated(data):
                # The host decoder hands back frame 0 of an animated WebP with
                # no complaint. Saying so is what keeps aio-dl.py's flatten
                # guard honest — grep `_is_animated_image` and
                # `getattr(im, "is_animated", False)` there; both preserve the
                # ORIGINAL bytes when this is True, which is the right outcome
                # for a file we can only partially decode. `seek()` is left
                # inherited, so asking for frame 1 raises EOFError.
                self.n_frames = 2  # ">1", the only thing any caller tests
                self.is_animated = True

        def load(self):
            """Close the file we were handed, then defer to PIL.

            WHY THIS OVERRIDE EXISTS. `_open` borrows an already-decoded core
            image and leaves `tile` empty, and `ImageFile.load()` returns EARLY
            on an empty tile list:

                pixel = Image.Image.load(self)
                if not self.tile:
                    return pixel          # <-- exits here
                ...                       # exclusive-fp close lives past this

            So the close never ran and `Image.open(path)` leaked an open
            BufferedReader for the lifetime of the image. Measured on Windows:
            after `.load()`, `im.fp` was still an open handle and `os.remove`
            failed with WinError 32, where PIL's native opener left `fp = None`
            and the delete succeeded. On Linux (i.e. on the device this shim
            exists for) the unlink succeeds, so it presents as fd pressure
            rather than a failure — the PDF path holds every page object of a
            chapter at once, so an N-page chapter pinned N descriptors.

            PIL's own WebPImageFile solves it the same way, in load() and not in
            _open(): `_exclusive_fp` is set by Image.open AFTER the plugin's
            _open returns, so it cannot be consulted there. Only OUR fp is
            closed — a caller who passed a BytesIO keeps theirs (`_exclusive_fp`
            is False for those), matching PIL's contract exactly.
            """
            if self.fp is not None and self._exclusive_fp:
                self.fp.close()
                self.fp = None
            return super().load()

    return _BridgeImageFile


def _webp_is_animated(data: bytes) -> bool:
    """Animated-WebP detection from the container alone, no decode.

    Two independent signals because either can stand alone: the VP8X chunk's
    ANIM flag (bit 0x02 of the flags byte at offset 20), and the `ANIM` fourcc
    itself. The fourcc normally sits at offset 30 but a large ICCP chunk can
    push it back, hence the bounded search rather than a fixed offset.
    """
    if len(data) < 21 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return False
    if data[12:16] != b"VP8X":
        return False  # VP8 / VP8L are single-frame by definition
    return bool(data[20] & 0x02) or data.find(b"ANIM", 12, 4096) != -1


# --- saver -----------------------------------------------------------------

#: Modes PNG can hold. Anything else (CMYK, YCbCr, F, LAB) makes PIL's PNG
#: writer raise, so it is converted to RGB first — the same normalization
#: aio-dl.py already does before its WebP saves (grep `fmt_local.startswith`).
_PNG_SAFE_MODES = frozenset({"1", "L", "LA", "I", "I;16", "P", "PA", "RGB", "RGBA"})


def _save_webp(im, fp, filename) -> None:
    """`Image.SAVE["WEBP"]` handler: render to PNG in memory, hand it to the
    backend, write what comes back.

    Honoured `encoderinfo` keys: `quality` (default 80, matching libwebp's and
    PIL's own default) and `lossless`. DROPPED, with no error: `method`,
    `exact`, `alpha_quality`, `icc_profile`, `exif`, `xmp` — `Bitmap.compress`
    exposes a format and a quality integer and nothing else. `method` in
    particular is a pure CPU<->size effort knob, so ignoring it costs bytes,
    never pixels; aio-dl.py passes 2 or 4 (grep `method=`).

    NO `register_save_all`, deliberately: a static encoder registered as an
    animation writer would silently flatten an animated source. A caller asking
    for `save_all=True` gets PIL's own KeyError, which is the truth.
    """
    encoderinfo = getattr(im, "encoderinfo", {}) or {}
    quality = encoderinfo.get("quality", 80)
    try:
        quality = max(0, min(100, int(quality)))
    except (TypeError, ValueError):
        quality = 80
    lossless = bool(encoderinfo.get("lossless", False))

    buffer = io.BytesIO()
    source = im if im.mode in _PNG_SAFE_MODES else im.convert("RGB")
    try:
        source.save(buffer, format="PNG")
    except OSError:
        # Defensive second chance for a mode the table above did not predict.
        im.convert("RGB").save(buffer, format="PNG")

    data = encode_webp(buffer.getvalue(), quality, lossless)
    if not data:
        # Same exception type and the same shape of message PIL's own encoder
        # raises when libwebp returns nothing, so callers that already handle
        # an encode failure (aio-dl.py's recompress path catches broadly and
        # keeps the original) behave identically.
        raise OSError("cannot write file as WebP (image-codec bridge returned nothing)")
    fp.write(data)


# --- diagnostics -----------------------------------------------------------


def capabilities() -> Dict[str, Any]:
    """What this process can actually do with images, as plain JSON-able data.

    Consumed by aio_android.image_capabilities (which is in turn a UI contract
    — see its docstring). Kept here rather than there so the knowledge of what
    "effective" means lives next to the code that makes it true.
    """
    shimmed = shims_installed()
    return {
        # Pillow's OWN codec, decode and encode reported separately even though
        # they are equivalent by construction (WebPImagePlugin registers save
        # `if SUPPORTED`, the same flag features.check reads). Reporting both
        # proves that equivalence on device instead of asking the reader to
        # know it — which is exactly what PARITY.md D8 was missing.
        "pillow_webp_decode": pillow_supports("WEBP"),
        "pillow_webp_encode": native_save("WEBP"),
        "bridge_formats": sorted(formats()),
        "bridge_installed": sorted(shimmed),
        # What the UI should actually gate on: can this process read and write
        # this format by ANY route, native or bridged.
        #
        # DECODE AND ENCODE ARE SPLIT FOR AVIF BECAUSE THEY GENUINELY DIFFER.
        # _install_one_locked registers an opener for every declared format but
        # a SAVER only for WEBP, because `Bitmap.CompressFormat` has JPEG, PNG
        # and WebP and nothing else. So on a real device (measured: API 35,
        # bridge declaring ["AVIF","WEBP"]) a single conflated `effective_avif`
        # reported True while an AVIF write would raise KeyError('AVIF') — a
        # capability probe whose whole job is to keep a UI control from being
        # offered, telling that UI the control is safe.
        #
        # effective_webp stays one key: for WEBP the opener and the saver are
        # installed together, so read and write really are equivalent there.
        "effective_webp": pillow_supports("WEBP") or "WEBP" in shimmed,
        "effective_avif_decode": pillow_supports("AVIF") or "AVIF" in shimmed,
        "effective_avif_encode": (
            pillow_supports("AVIF") or registered_save("AVIF")
        ),
    }
