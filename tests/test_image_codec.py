"""Coverage for sites/image_codec — the embedder image-codec seam and the PIL
registry shim it installs.

WHY THIS EXISTS: Chaquopy's `Pillow==11.0.0` wheel ships no `_webp.so`, so on
Android `im.save(format="WebP")` raises `KeyError('WEBP')` and `Image.open` on a
WebP raises `UnidentifiedImageError` (android/PARITY.md D3/D4/D8). The fix is a
shim registered into PIL's own dispatch tables, which means every `Image.open` /
`im.save` site in aio-dl.py keeps working unchanged — and it also means a bug
here would silently change how the DESKTOP opens images. Hence the two halves
below: the shim must work when it is meant to, and must not exist at all when it
is not.

THE DEV MACHINE HAS A WORKING `_webp`, so the codec-less branch is unreachable
offline unless it is faked. Every test that needs it monkeypatches
`image_codec.pillow_supports`, which exists as a function for exactly this
reason. Nothing here uninstalls or shadows Pillow.

Cross-file: aio_android.py:set_image_codec_bridge (the Android installer),
android/…/core/ImageCodecBridge.kt (the backend it installs).
"""

from __future__ import annotations

import io
import os
import sys

import pytest

from PIL import Image, ImageFile

from sites import image_codec


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_leaked_registration():
    """Every test starts and ends with a clean PIL registry.

    Autouse and not optional: a leaked WEBP opener would change how every LATER
    test in the process (and in the rest of the suite) reads an image, and the
    failure would surface somewhere unrelated.
    """
    image_codec.reset()
    Image.init()
    before = (
        dict(Image.OPEN),
        dict(Image.SAVE),
        dict(Image.EXTENSION),
        dict(Image.MIME),
        list(Image.ID),
    )
    yield
    image_codec.reset()
    assert (
        dict(Image.OPEN),
        dict(Image.SAVE),
        dict(Image.EXTENSION),
        dict(Image.MIME),
        list(Image.ID),
    ) == before, "sites/image_codec leaked a PIL registration"


def _png_bytes(color=(10, 200, 30), size=(7, 5), mode="RGB") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format="PNG")
    return buffer.getvalue()


#: A minimal still-WebP header. Only the 16 bytes PIL passes to `_accept` have
#: to be real — the fake backend never looks at the payload.
STILL_WEBP = b"RIFF\x28\x00\x00\x00WEBPVP8 " + b"\x00" * 32

#: VP8X with the ANIM flag (0x02) set at offset 20, plus the `ANIM` fourcc.
#: Both signals are present in a real animated file; _webp_is_animated accepts
#: either, and test_animated_webp_is_flagged pins that it fires.
ANIMATED_WEBP = (
    b"RIFF\x64\x00\x00\x00WEBPVP8X\x0a\x00\x00\x00\x02\x00\x00\x00"
    b"\x03\x00\x00\x03\x00\x00ANIM\x06\x00\x00\x00" + b"\x00" * 24
)


class FakeBackend:
    """Stands in for ImageCodecBridge.kt. Records its calls so the tests can
    assert the shim routed through it rather than through libwebp."""

    def __init__(self, png=None, webp=b"FAKEWEBP-BYTES", declared=("WEBP",)):
        self.png = _png_bytes() if png is None else png
        self.webp = webp
        self.declared = set(declared)
        self.decoded: list[bytes] = []
        self.encoded: list[tuple[bytes, int, bool]] = []

    def decode_to_png(self, data: bytes) -> bytes:
        self.decoded.append(data)
        return self.png

    def encode_webp(self, png: bytes, quality: int, lossless: bool) -> bytes:
        self.encoded.append((png, quality, lossless))
        return self.webp

    def formats(self):
        return set(self.declared)


@pytest.fixture
def codec_less_pillow(monkeypatch):
    """Report every shimmable format as missing from Pillow.

    `pillow_supports` is the module's single capability read precisely so this
    is possible — the alternative would be uninstalling Pillow's `_webp`, which
    no test may do to a shared interpreter.
    """
    monkeypatch.setattr(image_codec, "pillow_supports", lambda fmt: False)


@pytest.fixture
def shimmed(codec_less_pillow):
    """A fake backend installed AND shimmed into PIL. Yields the backend."""
    backend = FakeBackend()
    image_codec.set_backend(backend)
    assert image_codec.install_pillow_shims() == {"WEBP"}
    return backend


@pytest.fixture
def real_webp_shimmed(codec_less_pillow):
    """Shim installed over a REAL Pillow-backed codec; yields real WebP bytes.

    FakeBackend returns a canned PNG, which is enough for registry and dispatch
    assertions but not for anything about file handles or pixels. This uses the
    same bypass-the-registry discipline as PillowAsHostCodec (a Python backend
    that went through `Image.open` would re-enter the shim it is servicing).
    """
    from PIL import WebPImagePlugin

    def _native_webp(image, **kw):
        image.encoderinfo = kw
        image.encoderconfig = ()
        out = io.BytesIO()
        WebPImagePlugin._save(image, out, "")
        return out.getvalue()

    class _Host:
        def formats(self):
            return {"WEBP"}

        def decode_to_png(self, data):
            image = WebPImagePlugin.WebPImageFile(io.BytesIO(data))
            out = io.BytesIO()
            image.convert("RGB").save(out, format="PNG")
            return out.getvalue()

        def encode_webp(self, png, quality, lossless):
            image = Image.open(io.BytesIO(png))  # PNG is not shimmed; safe
            image.load()
            return _native_webp(image, quality=quality, lossless=lossless)

    source = Image.new("RGB", (24, 16), (200, 30, 70))
    payload = _native_webp(source, lossless=True)

    image_codec.set_backend(_Host())
    assert image_codec.install_pillow_shims() == {"WEBP"}
    return payload


# --------------------------------------------------------------------------
# The desktop-safety proof
#
# This is the half that protects the 40k-line desktop codebase, so it comes
# first and is deliberately over-specified.
# --------------------------------------------------------------------------


def test_no_backend_registers_nothing():
    """No backend -> no registration, whatever Pillow's own state is."""
    assert image_codec.install_pillow_shims() == set()
    assert image_codec.shims_installed() == set()
    assert image_codec.custom_backend() is None
    assert image_codec.available() is False


def test_no_backend_leaves_pil_tables_untouched(codec_less_pillow):
    """Even pretending Pillow has no codec, an absent backend must register
    nothing — "PIL can't" is necessary but not sufficient."""
    open_before, save_before = dict(Image.OPEN), dict(Image.SAVE)
    assert image_codec.install_pillow_shims() == set()
    assert dict(Image.OPEN) == open_before
    assert dict(Image.SAVE) == save_before


def test_working_pillow_is_not_shimmed_even_with_a_backend():
    """The self-disabling path: if the wheel spike ever lands and
    `features.check("webp")` goes True, the shim must step aside with no code
    change. This is the desktop's real configuration, so it runs unpatched."""
    if not image_codec.pillow_supports("WEBP"):
        pytest.skip("this interpreter's Pillow has no WebP codec; nothing to prove")
    image_codec.set_backend(FakeBackend())
    assert image_codec.install_pillow_shims() == set()
    assert image_codec.shims_installed() == set()


def test_desktop_webp_still_round_trips_after_a_no_op_install():
    """The end-to-end desktop assertion: a real WebP encode/decode through
    PIL's own codec is unaffected by this module existing."""
    if not image_codec.pillow_supports("WEBP"):
        pytest.skip("this interpreter's Pillow has no WebP codec")
    image_codec.set_backend(FakeBackend())
    image_codec.install_pillow_shims()

    buffer = io.BytesIO()
    Image.new("RGB", (6, 4), (1, 2, 3)).save(buffer, format="WebP", lossless=True)
    data = buffer.getvalue()
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    reopened = Image.open(io.BytesIO(data))
    assert reopened.size == (6, 4)
    assert reopened.getpixel((0, 0)) == (1, 2, 3)


# --------------------------------------------------------------------------
# The shim, when it is meant to exist
# --------------------------------------------------------------------------


def test_open_routes_through_the_backend(shimmed):
    im = Image.open(io.BytesIO(STILL_WEBP))
    assert im.format == "WEBP"
    assert im.size == (7, 5)
    assert im.mode == "RGB"
    im.load()
    assert im.getpixel((0, 0)) == (10, 200, 30)
    assert shimmed.decoded == [STILL_WEBP]


def test_opened_image_supports_the_operations_the_slow_paths_use(shimmed):
    """D4's blast radius is EPUB/PDF/--width/--scaling/--quality, i.e. convert,
    resize and re-save. A shim that opens but cannot be transformed would fix
    nothing."""
    im = Image.open(io.BytesIO(STILL_WEBP))
    assert im.convert("L").size == (7, 5)
    assert im.resize((3, 3)).size == (3, 3)
    assert im.copy().getpixel((1, 1)) == (10, 200, 30)
    out = io.BytesIO()
    im.save(out, format="JPEG")
    assert out.getvalue()[:2] == b"\xff\xd8"


def test_open_from_a_file_path_and_context_manager(shimmed, tmp_path):
    """`recompress_chapter_images_to_webp` does `with Image.open(src) as im:`
    on a PATH, which exercises PIL's exclusive-fp handling."""
    path = tmp_path / "page.webp"
    path.write_bytes(STILL_WEBP)
    with Image.open(path) as im:
        im.load()
        assert im.size == (7, 5)


def test_undecodable_bytes_raise_unidentified_not_something_new(codec_less_pillow):
    """A backend that cannot decode must produce the SAME exception a
    codec-less Pillow already produces, so every existing caller's error
    handling stays correct."""
    image_codec.set_backend(FakeBackend(png=b""))
    image_codec.install_pillow_shims()
    with pytest.raises(Image.UnidentifiedImageError):
        Image.open(io.BytesIO(STILL_WEBP))


def test_a_backend_that_echoes_its_input_does_not_recurse(codec_less_pillow):
    """The interchange contract is PNG, and the opener ENFORCES it rather than
    trusting it. A backend that echoed its input (or fell back to "return the
    original bytes" on an unsupported feature) would otherwise re-enter this
    same opener through Image.open and die with a RecursionError from inside a
    page save."""
    image_codec.set_backend(FakeBackend(png=STILL_WEBP))
    image_codec.install_pillow_shims()
    with pytest.raises(Image.UnidentifiedImageError):
        Image.open(io.BytesIO(STILL_WEBP))


def test_replacing_a_backend_drops_the_old_shims(codec_less_pillow):
    """A new backend declaring FEWER formats must not inherit a registration it
    cannot service."""
    image_codec.set_backend(FakeBackend(declared=("WEBP", "AVIF")))
    assert image_codec.install_pillow_shims() == {"WEBP", "AVIF"}
    image_codec.set_backend(FakeBackend(declared=("WEBP",)))
    assert image_codec.shims_installed() == set()
    assert image_codec.install_pillow_shims() == {"WEBP"}


def test_save_routes_through_the_backend(shimmed):
    out = io.BytesIO()
    Image.new("RGB", (4, 4), (9, 9, 9)).save(out, format="WebP", quality=85, method=2)
    assert out.getvalue() == b"FAKEWEBP-BYTES"
    png, quality, lossless = shimmed.encoded[-1]
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert (quality, lossless) == (85, False)


def test_save_honours_lossless_and_ignores_effort(shimmed):
    """aio-dl.py's two real call shapes: `lossless=True, method=4, quality=100`
    and `quality=85, method=2` (grep save_kwargs there). `method` has no
    Bitmap.compress equivalent and must be dropped silently, not rejected."""
    out = io.BytesIO()
    Image.new("RGB", (4, 4)).save(
        out, format="WebP", lossless=True, method=4, quality=100
    )
    assert shimmed.encoded[-1][1:] == (100, True)


def test_save_by_extension_without_an_explicit_format(shimmed, tmp_path):
    """`register_extension` is what makes `im.save("x.webp")` work."""
    path = tmp_path / "page.webp"
    Image.new("RGB", (4, 4)).save(path)
    assert path.read_bytes() == b"FAKEWEBP-BYTES"


def test_save_converts_a_mode_png_cannot_hold(shimmed):
    """CMYK makes PIL's PNG writer raise; the interchange format must not be
    able to reject an image the caller could otherwise have saved."""
    out = io.BytesIO()
    Image.new("CMYK", (4, 4)).save(out, format="WebP")
    assert out.getvalue() == b"FAKEWEBP-BYTES"


def test_failed_encode_raises_oserror(codec_less_pillow):
    """PIL's own encoder raises OSError when libwebp returns nothing, and
    aio-dl.py's recompress path catches broadly and keeps the original. Any
    other exception type would change that behaviour."""
    image_codec.set_backend(FakeBackend(webp=b""))
    image_codec.install_pillow_shims()
    with pytest.raises(OSError):
        Image.new("RGB", (4, 4)).save(io.BytesIO(), format="WebP")


def test_animated_webp_is_flagged(shimmed):
    """The host decoder returns frame 0 of an animated WebP without complaint.
    Saying `is_animated` is what keeps aio-dl.py's flatten guard honest — grep
    `getattr(im, "is_animated", False)` there; it preserves the original bytes.
    """
    assert getattr(Image.open(io.BytesIO(ANIMATED_WEBP)), "is_animated", False) is True
    assert getattr(Image.open(io.BytesIO(STILL_WEBP)), "is_animated", False) is False


def test_shim_is_an_imagefile_subclass(shimmed):
    """Callers (and PIL itself) rely on the ImageFile contract, not just on
    something duck-typed."""
    assert isinstance(Image.open(io.BytesIO(STILL_WEBP)), ImageFile.ImageFile)


# --------------------------------------------------------------------------
# End to end with real pixels
# --------------------------------------------------------------------------


def test_real_webp_round_trips_through_the_seam(codec_less_pillow):
    """The strongest test here: a GENUINE WebP file, decoded and re-encoded
    through the bridge, with the pixels checked.

    Everything above uses a stub backend, which proves the wiring but not that
    the wiring carries an image. This stands a real codec up behind the seam —
    PIL's own, standing in for BitmapFactory/Bitmap.compress, doing exactly what
    ImageCodecBridge.kt does: bytes -> decode -> PNG, and PNG -> decode ->
    WebP. If this passes, the only thing left unproven on device is Android's
    codec itself.
    """
    from PIL import features

    if not features.check("webp"):
        pytest.skip("needs a real WebP codec to stand in for the host's")

    from PIL import WebPImagePlugin

    class PillowAsHostCodec:
        """What ImageCodecBridge.kt does, in Pillow.

        It reaches for `WebPImagePlugin`'s decoder and saver DIRECTLY rather
        than through `Image.open` / `im.save`, and that is not fussiness — a
        Python-side backend that went through the registry would re-enter the
        very shim it is servicing, recurse, and (because image_codec swallows
        backend exceptions) surface as a bare UnidentifiedImageError with no
        hint of the cause. Cost one debugging round here. Android never meets
        this: its backend is Kotlin and has never heard of PIL. See the
        "A PYTHON-SIDE BACKEND MUST BYPASS THE REGISTRY" note in
        sites/image_codec.py's header.
        """

        def decode_to_png(self, data):
            image = WebPImagePlugin.WebPImageFile(io.BytesIO(data))
            out = io.BytesIO()
            image.convert("RGB").save(out, format="PNG")
            return out.getvalue()

        def encode_webp(self, png, quality, lossless):
            image = Image.open(io.BytesIO(png))  # PNG is not shimmed; safe
            image.load()
            image.encoderinfo = {"quality": quality, "lossless": lossless, "method": 4}
            image.encoderconfig = ()
            out = io.BytesIO()
            WebPImagePlugin._save(image, out, "")
            return out.getvalue()

        def formats(self):
            return {"WEBP"}

    source = Image.new("RGB", (24, 16), (200, 30, 70))
    source.putpixel((0, 0), (0, 0, 255))
    original = io.BytesIO()
    source.save(original, format="WEBP", lossless=True)

    image_codec.set_backend(PillowAsHostCodec())
    assert image_codec.install_pillow_shims() == {"WEBP"}

    # Decode: through the shim, since `pillow_supports` is patched to False.
    decoded = Image.open(io.BytesIO(original.getvalue()))
    assert decoded.format == "WEBP"
    assert decoded.size == (24, 16)
    decoded.load()
    assert decoded.getpixel((0, 0)) == (0, 0, 255)
    assert decoded.getpixel((10, 10)) == (200, 30, 70)

    # Encode: lossless, so the pixels must survive exactly.
    encoded = io.BytesIO()
    decoded.save(encoded, format="WebP", lossless=True, method=4, quality=100)
    data = encoded.getvalue()
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"

    # Re-read the shim's output, again through the shim.
    final = Image.open(io.BytesIO(data))
    final.load()
    assert final.size == (24, 16)
    assert final.getpixel((0, 0)) == (0, 0, 255)
    assert final.getpixel((10, 10)) == (200, 30, 70)


# --------------------------------------------------------------------------
# The Image.init() ordering guard
#
# install_pillow_shims() calls Image.init() BEFORE registering. Skipping it has
# two consequences and neither is visible at the time: PIL's own
# WebPImagePlugin, imported by a later unrelated init(), calls register_open and
# silently REPLACES our opener; and because Image.SAVE is empty until init, the
# snapshot reset() restores from records "absent" for entries that merely had
# not loaded yet — so reset() then deletes Pillow's own WebP registration.
#
# This cannot be tested in-process: the autouse fixture (and any earlier test)
# has already initialized PIL, which makes the guarded branch unreachable and
# any assertion about it vacuous. A test named for this behaviour previously
# passed with the Image.init() call deleted.
# --------------------------------------------------------------------------


_INIT_GUARD_PROBE = '''
import sys
sys.path.insert(0, sys.argv[1])
from PIL import Image
assert Image._initialized == 0, "PIL was already initialized"
import sites.image_codec as ic
ic.pillow_supports = lambda fmt: False

class H:
    def formats(self): return {"WEBP"}
    def decode_to_png(self, d): return b"\\x89PNG\\r\\n\\x1a\\n"
    def encode_webp(self, p, quality, lossless): return b"RIFF"

ic.set_backend(H())
_real_init = Image.init
if "--neutralize" in sys.argv:
    Image.init = lambda: None
ic.install_pillow_shims()
Image.init = _real_init
Image.init()
print(Image.OPEN.get("WEBP", (None,))[0].__name__)
print(Image.SAVE.get("WEBP") is ic._save_webp)
'''


def _run_init_guard_probe(*extra):
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [sys.executable, "-c", _INIT_GUARD_PROBE, root, *extra],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    opener, save_is_ours = result.stdout.split()
    return opener, save_is_ours == "True"


def test_the_shim_survives_a_later_pil_init():
    """A fresh interpreter, the shim installed, then an unrelated Image.init()."""
    opener, save_is_ours = _run_init_guard_probe()
    assert opener == "_BridgeImageFile"
    assert save_is_ours


def test_removing_the_init_guard_really_does_break_it():
    """Proves the test above is not vacuous. Without the guard the opener is
    silently swapped back to PIL's WebPImageFile — the one that cannot decode on
    Android — leaving decode broken while encode still routes to the bridge."""
    opener, save_is_ours = _run_init_guard_probe("--neutralize")
    assert opener == "WebPImageFile", (
        "the clobber did not happen, so the guarded test proves nothing"
    )
    assert not save_is_ours


# --------------------------------------------------------------------------
# Capability reporting must not promise a write it cannot do
# --------------------------------------------------------------------------


def test_avif_reports_decode_without_promising_encode(codec_less_pillow, monkeypatch):
    """MEASURED ON DEVICE (API 35): the bridge declares ["AVIF","WEBP"], the
    shim installs an AVIF opener, and `Bitmap.CompressFormat` has no AVIF — so
    no AVIF saver is registered. The old single `effective_avif` reported True
    anyway, and a UI control gated on it would have been enabled onto a
    KeyError('AVIF').

    Image.SAVE's AVIF entry is removed for the duration because THIS desktop's
    Pillow 12.1 ships native AVIF write, so the un-simulated assertion passes
    for a reason that does not exist on the device. `codec_less_pillow` only
    fakes `pillow_supports`; `registered_save` reads the real table on purpose.
    """
    Image.init()  # SAVE is empty until this runs; delitem would then be a no-op
    monkeypatch.delitem(Image.SAVE, "AVIF", raising=False)

    class _AvifCapableBackend:
        def formats(self):
            return {"WEBP", "AVIF"}

        def decode_to_png(self, data):
            return b"\x89PNG\r\n\x1a\n"

        def encode_webp(self, png, quality, lossless):
            return b"RIFF0000WEBP"

    image_codec.set_backend(_AvifCapableBackend())
    image_codec.install_pillow_shims()
    caps = image_codec.capabilities()

    assert "AVIF" in caps["bridge_installed"], "the opener really is installed"
    assert caps["effective_avif_decode"] is True
    assert caps["effective_avif_encode"] is False, (
        "no AVIF saver exists — reporting write capability re-opens the bug"
    )
    # WebP keeps ONE key because its opener and saver install together.
    assert caps["effective_webp"] is True
    assert image_codec.registered_save("WEBP") is True
    assert image_codec.registered_save("AVIF") is False
    assert "effective_avif" not in caps, "the conflated key must be gone"

    # Uninstall while the simulated state still holds. The shim snapshots
    # Image.SAVE at INSTALL time — with AVIF deleted — so letting reset() run in
    # the autouse teardown, after monkeypatch has already put AVIF back, would
    # restore "absent" over it and trip the leak detector.
    image_codec.reset()


# --------------------------------------------------------------------------
# Decompression-bomb guard
# --------------------------------------------------------------------------


def test_a_bomb_is_rejected_before_the_backend_is_asked_to_decode(codec_less_pillow):
    """PIL's own check runs only AFTER the opener returns, which is fine for a
    lazy plugin and useless here: this opener decodes eagerly, so by the time
    PIL looks, a JVM Bitmap + an interchange PNG + a core image are already
    committed. Checking the header dimensions first is what makes the guard
    protect anything on a phone."""
    calls = []

    class _CountingBackend:
        def formats(self):
            return {"WEBP"}

        def decode_to_png(self, data):
            calls.append(len(data))
            return b"\x89PNG\r\n\x1a\n"

        def encode_webp(self, png, quality, lossless):
            return b""

    image_codec.set_backend(_CountingBackend())
    image_codec.install_pillow_shims()

    # A VP8X header declaring a ~16 gigapixel canvas (0xFFFFFF+1 on both axes).
    header = (
        b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"VP8X"
        + b"\x0a\x00\x00\x00" + b"\x00\x00\x00\x00"
        + b"\xff\xff\xff" + b"\xff\xff\xff"
    )
    with pytest.raises(Image.DecompressionBombError):
        Image.open(io.BytesIO(header + b"\x00" * 32))

    assert calls == [], "the backend was asked to decode a bomb"


def test_an_ordinary_image_still_reaches_the_backend(real_webp_shimmed):
    """The guard must not cost the normal path — a real page decodes as before."""
    image = Image.open(io.BytesIO(real_webp_shimmed))
    image.load()
    assert image.size == (24, 16)


# --------------------------------------------------------------------------
# File-handle discipline
#
# The shim borrows an already-decoded core image and leaves `tile` empty, and
# ImageFile.load() RETURNS EARLY on an empty tile list — before the
# exclusive-fp close that lives past the tile loop. So Image.open(path) held an
# open handle for the image's lifetime while PIL's native opener left fp=None.
# PIL's own WebPImageFile closes in load() for exactly this reason; _open()
# cannot, because Image.open sets `_exclusive_fp` only AFTER _open returns.
# --------------------------------------------------------------------------


def test_open_from_a_path_does_not_leak_the_file_handle(tmp_path, real_webp_shimmed):
    """On Windows the leak makes os.remove fail outright (WinError 32). On
    Linux — the platform this shim actually runs on — the unlink succeeds and it
    presents as fd pressure instead, which is why this is asserted on the handle
    itself and not only on the delete."""
    path = tmp_path / "page.webp"
    path.write_bytes(real_webp_shimmed)

    image = Image.open(path)
    image.load()

    assert image.fp is None, "the shim kept the file open after load()"
    os.remove(path)  # raises PermissionError on Windows if the handle leaked


def test_a_caller_supplied_buffer_is_never_closed(real_webp_shimmed):
    """Only OUR fp may be closed. PIL's contract is that a caller who passes a
    file object keeps ownership of it (`_exclusive_fp` is False there), and
    closing it would break every in-memory decode in sites/_image_io.py."""
    buffer = io.BytesIO(real_webp_shimmed)
    image = Image.open(buffer)
    image.load()

    assert buffer.closed is False


def test_repeated_opens_do_not_accumulate_handles(tmp_path, real_webp_shimmed):
    """The device-side symptom: the PDF path appends every page image of a
    chapter to a list, so an N-page chapter pinned N descriptors at once."""
    path = tmp_path / "page.webp"
    path.write_bytes(real_webp_shimmed)

    images = []
    for _ in range(200):
        image = Image.open(path)
        image.load()
        images.append(image)

    assert all(i.fp is None for i in images)
    assert images[0].size == images[-1].size


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def test_install_is_idempotent(shimmed):
    """A second install must report nothing new AND must not overwrite the
    saved-state snapshot reset restores from."""
    assert image_codec.install_pillow_shims() == set()
    assert image_codec.shims_installed() == {"WEBP"}
    opener = Image.OPEN["WEBP"]
    assert image_codec.install_pillow_shims() == set()
    assert Image.OPEN["WEBP"] is opener


def test_reset_unregisters_everything(shimmed):
    image_codec.reset()
    assert image_codec.shims_installed() == set()
    assert image_codec.custom_backend() is None
    # The autouse fixture asserts full table equality; this pins the observable
    # consequence, which is that a WebP is no longer openable through the shim.
    assert Image.SAVE.get("WEBP") is not image_codec._save_webp


def test_clearing_the_backend_uninstalls_the_shim(shimmed):
    """A shim pointing at a removed backend would turn every WebP page into an
    UnidentifiedImageError, which is worse than not having shimmed at all."""
    image_codec.set_backend(None)
    assert image_codec.shims_installed() == set()


def test_a_format_the_backend_does_not_declare_is_not_shimmed(codec_less_pillow):
    """AVIF is in the shimmable table but Android only declares it on API 31+.
    Registering an opener with no decoder behind it would break AVIF pages that
    currently fail loudly."""
    image_codec.set_backend(FakeBackend(declared=("WEBP",)))
    assert image_codec.install_pillow_shims() == {"WEBP"}
    assert "AVIF" not in image_codec.shims_installed()


def test_a_declared_extra_format_is_decode_only(codec_less_pillow):
    """Bitmap.CompressFormat has no AVIF, so a declared AVIF gets an opener and
    deliberately NO saver.

    Asserted as "the AVIF save entry is exactly what it was", not as "AVIF is
    absent from Image.SAVE": Pillow 11.3+ has its own AVIF codec, so on a
    modern desktop that entry legitimately exists and belongs to PIL.
    """
    Image.init()
    save_before = Image.SAVE.get("AVIF")
    image_codec.set_backend(FakeBackend(declared=("WEBP", "AVIF")))
    assert image_codec.install_pillow_shims() == {"WEBP", "AVIF"}
    assert Image.OPEN["AVIF"][0].__name__ == "_BridgeImageFile"
    assert Image.SAVE.get("AVIF") is save_before


def test_a_misbehaving_backend_cannot_take_the_process_down(codec_less_pillow):
    """This installs from configure-time code on a device with no console."""

    class Exploding:
        def decode_to_png(self, data):
            raise RuntimeError("boom")

        def encode_webp(self, png, quality, lossless):
            raise RuntimeError("boom")

        def formats(self):
            raise RuntimeError("boom")

    image_codec.set_backend(Exploding())
    assert image_codec.formats() == set()
    assert image_codec.install_pillow_shims() == set()
    assert image_codec.decode_to_png(b"x") == b""
    assert image_codec.encode_webp(b"x", 80, False) == b""


# --------------------------------------------------------------------------
# Capability reporting (PARITY.md D8)
# --------------------------------------------------------------------------


def test_capabilities_on_a_plain_desktop():
    caps = image_codec.capabilities()
    native = image_codec.pillow_supports("WEBP")
    assert caps["pillow_webp_decode"] is native
    assert caps["pillow_webp_encode"] is native
    assert caps["effective_webp"] is native
    assert caps["bridge_formats"] == []
    assert caps["bridge_installed"] == []


def test_capabilities_distinguish_native_from_bridged(shimmed):
    caps = image_codec.capabilities()
    assert caps["pillow_webp_decode"] is False
    # The sharp edge: "WEBP" IS in Image.SAVE now, but it is OUR handler.
    assert image_codec.registered_save("WEBP") is True
    assert caps["pillow_webp_encode"] is False
    assert caps["effective_webp"] is True
    assert caps["bridge_formats"] == ["WEBP"]
    assert caps["bridge_installed"] == ["WEBP"]


def test_registered_save_forces_pil_lazy_init():
    """`Image.SAVE` is empty until `Image.init()` runs, so a naive membership
    read reports False for every format in a fresh interpreter. This is the bug
    the helper exists to avoid."""
    assert image_codec.registered_save("PNG") is True


# --------------------------------------------------------------------------
# The Android side of the seam (aio_android)
#
# The JNI boundary itself cannot be tested offline, but the ADAPTER can — and
# the adapter is where the boundary's rules get broken (a Kotlin ByteArray is
# not `bytes`, a Kotlin List is not iterable at all).
# --------------------------------------------------------------------------


def _aio_android():
    """The Android adapter module, or a skip.

    aio_android.py is the Chaquopy entry point and is NOT part of the desktop
    tree — these tests exercise the adapter across the JNI boundary, so they
    skip wherever it is absent instead of erroring the whole suite. They
    re-activate by themselves once the Android port lands.
    """
    return pytest.importorskip("aio_android")


class FakeKotlinBridge:
    """What Chaquopy hands Python: camelCase methods, a `bytearray` where
    Kotlin returned a `ByteArray`, and a comma-joined String for `formats()`."""

    def __init__(self, declared="WEBP"):
        self.declared = declared
        self.calls: list[tuple] = []

    def decodeToPng(self, data):
        self.calls.append(("decode", bytes(data)))
        return bytearray(_png_bytes())

    def encodeWebp(self, png, quality, lossless):
        self.calls.append(("encode", bytes(png), quality, lossless))
        return bytearray(b"KOTLIN-WEBP")

    def formats(self):
        return self.declared


def test_android_bridge_installs_and_reports(monkeypatch, codec_less_pillow):
    aio_android = _aio_android()

    bridge = FakeKotlinBridge()
    caps = __import__("json").loads(aio_android.set_image_codec_bridge(bridge))
    assert caps["bridge_formats"] == ["WEBP"]
    assert caps["bridge_installed"] == ["WEBP"]
    assert caps["effective_webp"] is True
    assert caps["pillow_webp_decode"] is False

    im = Image.open(io.BytesIO(STILL_WEBP))
    im.load()
    assert im.size == (7, 5)
    out = io.BytesIO()
    im.save(out, format="WebP", quality=70)
    assert out.getvalue() == b"KOTLIN-WEBP"
    assert bridge.calls[0][0] == "decode"
    assert bridge.calls[-1][0] == "encode" and bridge.calls[-1][2:] == (70, False)


def test_android_bridge_adapter_coerces_bytearray_to_bytes():
    """Kotlin's `ByteArray` arrives as a Python `bytearray`, and PIL's writers
    want `bytes`. Getting this wrong fails deep inside a save, not here."""
    aio_android = _aio_android()

    adapter = aio_android._BridgeImageCodec(FakeKotlinBridge())
    assert isinstance(adapter.decode_to_png(b"x"), bytes)
    assert isinstance(adapter.encode_webp(b"x", 80, False), bytes)
    assert adapter.formats() == {"WEBP"}


def test_android_bridge_adapter_parses_a_multi_format_declaration():
    aio_android = _aio_android()

    adapter = aio_android._BridgeImageCodec(FakeKotlinBridge("webp, avif"))
    assert adapter.formats() == {"WEBP", "AVIF"}


def test_android_bridge_removal_is_clean(codec_less_pillow):
    aio_android = _aio_android()

    aio_android.set_image_codec_bridge(FakeKotlinBridge())
    assert image_codec.shims_installed() == {"WEBP"}
    aio_android.set_image_codec_bridge(None)
    assert image_codec.shims_installed() == set()
    assert image_codec.custom_backend() is None


def test_android_bridge_install_never_raises():
    """Aio.kt calls this during one-time configuration; an exception here takes
    the whole app down at startup."""
    aio_android = _aio_android()

    class Hostile:
        def formats(self):
            raise RuntimeError("boom")

    payload = __import__("json").loads(aio_android.set_image_codec_bridge(Hostile()))
    assert isinstance(payload, dict)


def test_diagnostics_reports_the_codec_state():
    """PARITY.md D8: the probe used to say `pillow: true` on a device whose
    Pillow cannot touch a WebP. These fields are the fix, and the old key names
    must survive — DiagnosticsSheet.kt reads the same payload."""
    aio_android = _aio_android()

    payload = __import__("json").loads(aio_android.diagnostics())
    assert payload["pillow_webp"] is image_codec.pillow_supports("WEBP")
    assert payload["pillow_webp_save"] is image_codec.registered_save("WEBP")
    assert payload["image_codec"]["effective_webp"] is payload["pillow_webp"]
    assert "pillow" in payload["capabilities"]
    assert payload["registered_handlers"] > 0
