"""Android (Chaquopy) entry point.

What this module owns: every difference between "aio-dl.py run as a CLI on a
desktop" and "aio-dl.py run in-process inside an Android app". Nothing in the
rest of the tree imports this file — it imports them. That direction is the
whole point: desktop must not grow an Android dependency.

Who reads from it: the Kotlin side, via Chaquopy
(`Python.getInstance().getModule("aio_android")`).

Depends on: aio-dl.py (lazily — see _aio_dl), sites.browser_backend,
library_state, aio_config.

---------------------------------------------------------------------------
WHY A SHIM AT ALL

aio-dl.py's main() is ~5,500 lines driven entirely by argparse, with the
download engine living in closures inside it. There is no library API to call.
Rather than refactor that (huge diff, huge regression risk, zero desktop
upside), this module drives it the way the CLI does — build an argv, call
main() — and fixes up the four things that assumption breaks on Android:

  1. CWD. aio-dl.py resolves several paths relative to it (`tmp_<hid>`,
     build_epub's staging dir). Chaquopy's CWD is not writable and not
     predictable, so configure() sets one.
  2. Cancellation. Desktop cancels by killing the process. In-process there
     is nothing to kill, so we flip aio-dl.py's cooperative _RUN_CANCEL.
  3. Progress. Desktop scrapes stdout with regexes. In-process we install a
     structured event sink instead.
  4. The browser. Patchright cannot run here; a WebView can. We register a
     BrowserBackend so the vrf signer and the Cloudflare solver keep working.

---------------------------------------------------------------------------
THE JNI BOUNDARY

Chaquopy marshals str/int/bool/float across JNI painlessly. Python keyword
arguments, dicts, and lists of dicts do NOT survive that trip cleanly from a
Java/Kotlin object. So the Kotlin bridge implements a deliberately dumb
POSITIONAL, PRIMITIVES-ONLY interface and _WebViewBackend below adapts it to
the richer BrowserBackend protocol:

    forProfile(profile: String) -> ProfileBridge          // one browser session
      .goto(url: String, timeoutMs: Int) -> Unit
      .evaluate(script: String, argJson: String) -> String   // "" == no arg; returns JSON
      .content() -> String
      .waitForSelector(selector: String, timeoutMs: Int) -> Boolean
      .userAgent() -> String
      .cookies(url: String) -> String                        // raw "a=b; c=d" header
      .solveChallenge(url: String, timeoutMs: Int) -> String  // JSON {cookies, userAgent}

Everything structured crosses as a JSON string. Keep it that way — the moment
this boundary grows a Python-shaped argument it stops working from Kotlin.

`forProfile` is the ONE structural concession, and it earns its place: Android
has a single Chromium, but the profile names sites/browser_backend.py hands out
("mangafire", "fetch", "cf") are genuinely separate SESSIONS. MangaFire's signer
bootstraps `window.__aioMfSign` into a live page and reuses it for the whole
run, so an unrelated fetch_html_playwright navigation sharing that WebView would
wipe it — intermittently, depending on which sites a run happened to touch.
Cookies stay shared across all of them regardless (CookieManager is
process-global), which is what makes one solved challenge count everywhere.
---------------------------------------------------------------------------
"""

from __future__ import annotations

import importlib
import json
import os
import queue
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

_CONFIG_LOCK = threading.Lock()
_CONFIGURED: Dict[str, str] = {}

# aio-dl.py is not an importable identifier (the dash), and importing it is
# expensive — it pulls the whole handler registry. Memoized, and deliberately
# NOT imported at module scope so configure() can set env/CWD first: several
# module-level constants in that file bake in values read at import time.
_AIO_DL = None


def _aio_dl():
    global _AIO_DL
    if _AIO_DL is None:
        _AIO_DL = importlib.import_module("aio-dl")
    return _AIO_DL


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


#: What --metadata-source accepts (grep its argparse `choices` in aio-dl.py).
#: Used to VALIDATE what configure() writes into AIO_METADATA_SOURCE — argparse
#: uses that env var as the flag's DEFAULT, so an unrecognized value there is
#: not an inert typo: it makes argparse reject its own default with "invalid
#: choice" and every single run fails, including the ones that never asked for
#: enrichment.
_METADATA_SOURCES = ("none", "anilist")


def configure(
    output_dir: str,
    cache_dir: str,
    temp_dir: Optional[str] = None,
    metadata_source: Optional[str] = None,
) -> str:
    """Point the downloader at Android-writable locations. Call ONCE, before
    anything else in this module.

    output_dir -> the manga library root (AIO_OUTPUT_DIR).
    cache_dir  -> app cache; becomes XDG_CACHE_HOME, which is what
                  sites/mangafire_vrf.py:_profile_dir and sites/comix.py's
                  equivalent already fall back to on non-win/darwin. Setting it
                  matters because HOME may be "/" on Android, and
                  os.path.expanduser("~") would then hand them an unwritable
                  path.
    temp_dir   -> working dir for tmp_<hid>/ and build_epub's staging tree.
                  Defaults to <cache_dir>/work.
    metadata_source -> AniList enrichment, the ENV half ("none" | "anilist").

    Returns the resolved temp/working directory.

    THE metadata_source ARGUMENT IS BELT-AND-BRACES, not the primary route.
    [build_argv] emits `--metadata-source` from the settings dict, which covers
    every download and is what a user's toggle should actually drive. This
    covers the entry points that do NOT build their argv through build_argv and
    therefore fall back to argparse's default: [list_chapters] and
    [refresh_library_metadata] when called without an explicit source. Before
    this existed, `--metadata-source` defaulted to "none" and nothing on the
    device ever set it, so AniList enrichment was off and could not be turned
    on from anywhere — android/PARITY.md's "the library can never be repaired"
    row.

    WHY SET CWD rather than patch aio-dl.py: several paths there are resolved
    against the process CWD (`os.path.abspath(f"tmp_{hid}")`, build_epub's
    `temp_epub_<hid>`). Giving the process a sane CWD fixes all of them at once
    and keeps the desktop file untouched.
    """
    with _CONFIG_LOCK:
        output_dir = os.path.abspath(output_dir)
        cache_dir = os.path.abspath(cache_dir)
        work_dir = os.path.abspath(temp_dir or os.path.join(cache_dir, "work"))
        for path in (output_dir, cache_dir, work_dir):
            os.makedirs(path, exist_ok=True)

        os.environ["AIO_OUTPUT_DIR"] = output_dir
        os.environ["XDG_CACHE_HOME"] = cache_dir
        # Explicit rather than relying on the XDG fallback: these two own
        # persistent browser profiles, and pinning them keeps a solved
        # Cloudflare clearance across runs.
        os.environ.setdefault(
            "AIO_MANGAFIRE_PROFILE_DIR", os.path.join(cache_dir, "mangafire-profile")
        )
        os.environ.setdefault(
            "AIO_COMIX_PROFILE_DIR", os.path.join(cache_dir, "comix-profile")
        )
        # No terminal here; ANSI would just litter the log panel.
        os.environ.setdefault("NO_COLOR", "1")

        # Set UNCONDITIONALLY rather than setdefault-ed: Chaquopy's interpreter
        # outlives every screen, so a setdefault would pin whatever the first
        # call passed for the life of the process and turning enrichment back
        # OFF in the UI would silently not turn it off. Anything unrecognized
        # normalizes to "none" — see _METADATA_SOURCES for why an invalid value
        # here is not survivable.
        source = str(metadata_source or "").strip().lower()
        os.environ["AIO_METADATA_SOURCE"] = (
            source if source in _METADATA_SOURCES else "none"
        )

        os.chdir(work_dir)
        _CONFIGURED.update(
            {
                "output_dir": output_dir,
                "cache_dir": cache_dir,
                "work_dir": work_dir,
                # Recorded so diagnostics() answers "is enrichment on" without
                # the reader having to know it lives in an env var.
                "metadata_source": os.environ["AIO_METADATA_SOURCE"],
            }
        )
        return work_dir


def diagnostics() -> str:
    """JSON health snapshot. This is the M0 spike's assertion target: it proves
    the handler registry imported and says which optional handlers dropped out
    (a missing `cryptography` wheel shows up here as three named entries rather
    than as a mystery "no handler for this URL" later).
    """
    out: Dict[str, Any] = dict(_CONFIGURED)
    out["python_version"] = sys.version
    out["cwd"] = os.getcwd()
    try:
        import sites

        out["registered_handlers"] = len(sites._REGISTERED_HANDLERS)
        out["base_handlers"] = len(sites._BASE_HANDLERS)
        out["optional_handler_errors"] = dict(
            getattr(sites, "_OPTIONAL_HANDLER_ERRORS", {})
        )
    except Exception as exc:
        out["sites_import_error"] = f"{type(exc).__name__}: {exc}"

    # Capability probe. Each of these degrades gracefully, but knowing WHICH
    # degraded turns "why is this slow / why did that site fail" into a
    # one-line answer.
    caps: Dict[str, bool] = {}
    for label, module in (
        ("pillow", "PIL"),
        ("cryptography", "cryptography"),
        ("lxml", "lxml"),
        ("numpy", "numpy"),
        ("cloudscraper", "cloudscraper"),
        ("pypdf", "pypdf"),
        ("rapidfuzz", "rapidfuzz"),
        ("curl_cffi", "curl_cffi"),
        ("impit", "impit"),
    ):
        try:
            importlib.import_module(module)
            caps[label] = True
        except Exception:
            caps[label] = False
    out["capabilities"] = caps

    # Pillow gets extra fields because IMPORTABILITY IS NOT THE WHOLE STORY,
    # and that blind spot was its own defect (android/PARITY.md D8): Chaquopy's
    # Pillow==11.0.0 wheel ships no `_webp.so`, so `capabilities.pillow` reads
    # true on device while `Image.save(format="WebP")` raises KeyError('WEBP')
    # and `Image.open` on a .webp raises UnidentifiedImageError. That is
    # `--webtoon-recompress` silently doing nothing (D3) and EPUB/PDF/--width/
    # --scaling/--quality<100 silently dropping pages (D4), neither of which
    # this probe could see.
    #
    #   pillow_webp       - PIL.features.check("webp"): the compiled codec.
    #   pillow_webp_save  - "WEBP" in PIL.Image.SAVE, forced past PIL's LAZY
    #                       plugin init (a naive read is False for every format
    #                       until Image.init() has run).
    #   image_codec       - the full capability snapshot, incl. whether the
    #                       Android bridge shimmed a codec in. See
    #                       aio_android.image_capabilities for the key contract.
    try:
        from sites import image_codec

        out["pillow_webp"] = image_codec.pillow_supports("WEBP")
        out["pillow_webp_save"] = image_codec.registered_save("WEBP")
        out["image_codec"] = image_codec.capabilities()
    except Exception as exc:
        out["pillow_webp"] = None
        out["image_codec"] = {"error": f"{type(exc).__name__}: {exc}"}

    # rapidfuzz gets two extra fields because it is the one dependency whose
    # PRESENCE is not the whole story. Android runs its pure-Python backend
    # (android/wheels/README.md); desktop runs the compiled one, and the two are
    # not bit-identical unless sites/fuzzy_match normalizes the inputs.
    #
    #   rapidfuzz_backend     - "python" on device, "cpp" on desktop, None if the
    #                           wheel went missing entirely.
    #   rapidfuzz_fingerprint - scores over a fixed corpus, so device-vs-desktop
    #                           parity is a string comparison rather than a
    #                           reasoning exercise. MUST match desktop exactly;
    #                           if it does not, the matcher will pick different
    #                           series here than there, silently.
    try:
        from sites import fuzzy_match

        out["rapidfuzz_backend"] = fuzzy_match.rapidfuzz_backend()
        out["rapidfuzz_fingerprint"] = _match_fingerprint()
    except Exception as exc:
        out["rapidfuzz_backend"] = None
        out["rapidfuzz_fingerprint"] = f"{type(exc).__name__}: {exc}"
    return json.dumps(out)


# Fixed pairs spanning the three scorer shapes, INCLUDING the two characters
# sites/fuzzy_match normalizes (U+005F, U+00A0) so a device whose normalizer
# regressed shows up as a changed fingerprint.
#
# Deliberately NO Unicode-skew character here: those legitimately score
# differently on the two backends (fuzzy_match header, cause 2), and a
# fingerprint that is expected to differ is a fingerprint nobody can use.
# Frozen otherwise -- changing these invalidates every fingerprint recorded in
# an old bug report.
_FINGERPRINT_PAIRS = (
    ("Frieren", "Sousou no Frieren"),
    ("Solo Leveling", "Solo Leveling"),
    ("FULL METAL ALCHEMIST", "Full Metal Alchemist"),
    ("Kaguya_sama", "Kaguya-sama: Love is War"),  # U+005F must become a space
    ("Solo\u00a0Leveling", "Solo Leveling"),  # U+00A0 must become a space
    ("Hata Kenjirou", "Kenjirou Hata"),
)


def _match_fingerprint() -> str:
    """Compact digest of this build's fuzzy-match scores.

    Compare against desktop with:
        python -c "import aio_android; print(aio_android._match_fingerprint())"
    """
    from sites import fuzzy_match

    parts = []
    for a, b in _FINGERPRINT_PAIRS:
        parts.append(
            "%.4f/%.4f/%.4f"
            % (
                fuzzy_match.wratio(a, b),
                fuzzy_match.processed_wratio(a, b),
                fuzzy_match.processed_token_set_ratio(a, b),
            )
        )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Browser backend
# ---------------------------------------------------------------------------


# Local "no argument supplied" sentinel for _WebViewBackend.evaluate. Distinct
# from sites.browser_backend.NOARG only because that module is imported lazily;
# both are honoured. See evaluate's docstring.
_NOARG = object()


def _parse_cookie_header(raw: str, url: str) -> List[Dict[str, str]]:
    """Turn CookieManager's flat "a=b; c=d" string into the backend's
    [{name, value, domain, path}] shape.

    Android's CookieManager.getCookie gives us NO domain, path, or HttpOnly
    metadata — only the pairs that would be sent to that URL. That is
    sufficient here because the sole consumer, sites/crawlee_utils.get_cf_session,
    only ever does `session.cookies.set(name, value, domain=...)`. Domain is
    synthesized from the URL, which is exactly the scope the browser applied.
    """
    host = urlparse(url).netloc or ""
    cookies: List[Dict[str, str]] = []
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if not name:
            continue
        cookies.append(
            {"name": name, "value": value.strip(), "domain": host, "path": "/"}
        )
    return cookies


class _WebViewBackend:
    """BrowserBackend over an Android WebView, reached through the Kotlin
    bridge described in the module header.

    Not thread-safe by design — sites/mangafire_vrf.py already serializes every
    call onto its own worker thread, and the Kotlin side must marshal each call
    onto the main looper anyway (WebView is main-thread-only). Adding a lock
    here would just hide that requirement.
    """

    def __init__(self, bridge: Any, profile: str = "default") -> None:
        self._bridge = bridge
        self._profile = profile

    # -- navigation / evaluation ------------------------------------------

    def goto(
        self, url: str, *, wait_until: str = "domcontentloaded", timeout_ms: int = 45_000
    ) -> None:
        # wait_until is dropped on purpose: WebView reports onPageFinished, with
        # no networkidle equivalent. The Kotlin side resolves goto() there, which
        # is closest to "domcontentloaded" — the only value any caller passes.
        self._bridge.goto(url, int(timeout_ms))

    def evaluate(self, script: str, arg: Any = _NOARG) -> Any:
        """See sites/browser_backend.py's module header for the contract. The
        promise-awaiting and function-invocation wrapper live on the KOTLIN
        side, because only it can hold the evaluateJavascript callback.

        Encoding note: `argJson` is "" for "no argument" rather than "null",
        since `null` is itself a legal argument value.

        TWO sentinels are accepted. The default has to be a local one because
        browser_backend is imported lazily (a run that never touches a browser
        must not pay for the import), so it cannot be spelled in the signature —
        but callers holding the real `bb.NOARG` must work too. Getting this
        wrong is quiet: the default was plain None until 2026-08-08, which
        serialized to "null" and invoked every no-arg script as `fn(null)`.
        Harmless for the two scripts in the tree today, wrong the moment one
        distinguishes an absent argument from a null one.
        """
        from sites import browser_backend as bb

        no_arg = arg is _NOARG or arg is bb.NOARG
        arg_json = "" if no_arg else json.dumps(arg)
        raw = self._bridge.evaluate(script, arg_json)
        if raw is None or raw == "":
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            # A non-JSON return means the page handed back something
            # unserializable. Surface the string rather than raising — the vrf
            # bootstrap checks isinstance(dict) and reports its own error.
            return raw

    def content(self) -> str:
        return self._bridge.content() or ""

    def wait_for_selector(self, selector: str, *, timeout_ms: int = 10_000) -> bool:
        try:
            return bool(self._bridge.waitForSelector(selector, int(timeout_ms)))
        except Exception:
            return False

    # -- identity -----------------------------------------------------------

    def user_agent(self) -> str:
        try:
            return self._bridge.userAgent() or ""
        except Exception:
            return ""

    def cookies(self, url: str) -> List[Dict[str, str]]:
        try:
            return _parse_cookie_header(self._bridge.cookies(url), url)
        except Exception:
            return []

    # -- anti-bot -----------------------------------------------------------

    @property
    def supports_challenge_solving(self) -> bool:
        # True, and this is the one place Android beats desktop: the challenge
        # is shown to a human who taps it, in a real Chromium with a real UA,
        # instead of zendriver driving a headless browser at it.
        return True

    def solve_challenge(
        self, url: str, *, timeout_s: float = 45.0, interactive: bool = True
    ) -> Dict[str, Any]:
        raw = self._bridge.solveChallenge(url, int(timeout_s * 1000))
        try:
            data = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            data = {}
        cookies = data.get("cookies")
        if isinstance(cookies, str):
            cookies = _parse_cookie_header(cookies, url)
        elif not isinstance(cookies, list):
            cookies = []
        return {
            "cookies": cookies,
            "user_agent": str(data.get("userAgent") or data.get("user_agent") or ""),
        }

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        # The WebView's lifetime is owned by the Android Activity/Service, not
        # by this object. Nothing to do.
        pass

    @property
    def unavailable_reason(self) -> Optional[str]:
        return None if self._bridge is not None else "no WebView bridge installed"


def set_browser_bridge(bridge: Any) -> None:
    """Install the Kotlin WebView bridge (or None to remove it).

    `bridge` is a FACTORY, not a session: each profile gets its own WebView via
    `bridge.forProfile(name)`. See the module header for why that split is load
    bearing rather than cosmetic.

    Installing this is what turns MangaFire and Cloudflare-challenged sites from
    hard failures into working downloads — every consumer
    (sites/mangafire_vrf.py, sites/playwright_utils.py, sites/crawlee_utils.py)
    already checks `browser_backend.custom_backend()` and diverts when one is
    present.
    """
    from sites import browser_backend as bb

    if bridge is None:
        bb.set_backend_factory(None)
        return
    bb.set_backend_factory(
        lambda profile: _WebViewBackend(bridge.forProfile(profile), profile)
    )


# ---------------------------------------------------------------------------
# Image codec bridge
#
# Chaquopy's Pillow==11.0.0 wheel ships no `_webp.so` (verified by unpacking it:
# android/app/build/python/pip/debug/{arm64-v8a,x86_64}/PIL/ holds five .so
# files and none of them is _webp). So on device `im.save(format="WebP")` raises
# KeyError('WEBP') and `Image.open` on a WebP raises UnidentifiedImageError —
# android/PARITY.md defects D3, D4 and D8, all three from that one fact.
#
# Android's PLATFORM has had a WebP codec since API 14; it is simply not wired
# to Pillow. sites/image_codec.py is that wire, and it works by registering
# opener/saver shims into PIL's OWN dispatch tables — so aio-dl.py's ~15
# Image.open / im.save sites and sites/_image_io.py's magic sniffing are
# untouched, and desktop (where PIL has a real codec) registers nothing at all.
# Read that module's header before changing anything here.
#
# SAME JNI DISCIPLINE as the browser bridge: positional, primitives only. The
# ONE thing that crosses non-primitively is the image payload itself, as a
# `ByteArray` — Java ARRAYS convert to Python bytes where a java.util.List does
# not (the trap this module's header calls out). Base64 would be the safe-looking
# alternative and is deliberately NOT used: it is a 33% size tax and an extra
# copy in each direction on a payload that can be tens of megabytes.
# ---------------------------------------------------------------------------


class _BridgeImageCodec:
    """sites.image_codec.ImageCodecBackend over the Kotlin ImageCodecBridge.

    Adapts two shapes at the boundary: Kotlin returns a `ByteArray`, which
    arrives as a `bytearray` where PIL's writers want `bytes`, and a
    comma-joined String where Python wants a set.

    Re-entrant, which is required (see sites/image_codec.py's THREADING note):
    aio-dl.py encodes pages from a ThreadPoolExecutor, and unlike the WebView
    bridge there is no main-thread hop here — BitmapFactory and Bitmap.compress
    are ordinary thread-safe calls.
    """

    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge

    def decode_to_png(self, data: bytes) -> bytes:
        return bytes(self._bridge.decodeToPng(data) or b"")

    def encode_webp(self, png: bytes, quality: int, lossless: bool) -> bytes:
        return bytes(self._bridge.encodeWebp(png, int(quality), bool(lossless)) or b"")

    def formats(self) -> set:
        # Comma-joined rather than a collection: a Kotlin List does not arrive
        # as a Python iterable, and a one-line split is cheaper than JSON for a
        # value with no structure.
        raw = self._bridge.formats() or ""
        return {part.strip().upper() for part in str(raw).split(",") if part.strip()}


def set_image_codec_bridge(bridge: Any) -> str:
    """Install the Kotlin image-codec bridge (or None to remove it), then
    register the PIL shims it makes possible.

    Returns the JSON capability snapshot, so the caller can log in one line
    what the install actually bought — on a device that is the difference
    between "WebP works now" and "the bridge is installed but Pillow already
    had a codec, so nothing changed".

    NEVER RAISES. Aio.kt calls this during one-time configuration; a codec
    upgrade path that can take the whole app down at startup would be a far
    worse bug than the missing codec it is fixing.
    """
    try:
        from sites import image_codec

        image_codec.set_backend(None if bridge is None else _BridgeImageCodec(bridge))
        image_codec.install_pillow_shims()
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
    return image_capabilities()


def image_capabilities() -> str:
    """What this process can do with images, as JSON.

    **NO CONSUMER YET.** This docstring used to assert, in the present tense,
    that "the Android Settings/Download UI gates five controls on them" — it
    does not. Grep the Kotlin: nothing calls `image_capabilities` (`Aio.kt` only
    installs the bridge). The five lossy controls — `--format epub`,
    `--format pdf`, `--width`, `--scaling < 100`, `--quality < 100` — plus the
    `--webtoon-recompress` switch are still ungated, which is the user-facing
    half of PARITY.md D3/D4 and belongs to the Kotlin settings arm. Written as
    fact, that sentence told the next reader the UI half was closed while the
    recompress switch still promises "around 90% smaller" with nothing checking.

    The key names ARE intended as the contract for that future consumer:

        pillow_webp_decode      Pillow's OWN codec is present (features.check).
        pillow_webp_encode      Pillow's OWN saver is registered — i.e. NOT ours.
        bridge_formats          what the host codec declares it can decode.
        bridge_installed        formats actually shimmed into PIL.
        effective_webp          WebP works by SOME route, read AND write. One
                                key is honest here because the shim installs the
                                WebP opener and saver together.
        effective_avif_decode   AVIF can be READ by some route.
        effective_avif_encode   AVIF can be WRITTEN. Split from decode because
                                they really differ: `Bitmap.CompressFormat` has
                                no AVIF, so the bridge registers an AVIF opener
                                and no saver. The old single `effective_avif`
                                reported True on a device that would raise
                                KeyError('AVIF') on write — measured on API 35.

    Never raises — a UI that cannot ask this question would have to assume the
    worst and hide working controls.
    """
    try:
        from sites import image_codec

        return json.dumps(image_codec.capabilities())
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


# ---------------------------------------------------------------------------
# Running a download
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Progress events
#
# aio-dl.py:_emit pushes structured dicts to whatever set_event_sink installed.
# Android drains them by POLLING rather than by handing Python a Kotlin
# callback.
#
# WHY POLL: a callback would mean Python calling INTO the JVM from whichever
# worker thread happened to emit, which needs JNI thread attachment and turns
# every emit site into a potential cross-language deadlock. A queue keeps the
# boundary one-directional and primitives-only — the same rule the module
# header sets out — and the UI only repaints a few times a second anyway.
# ---------------------------------------------------------------------------

# Bounded so a service that stops polling (screen off, process frozen) can't
# grow this without limit. Oldest events are dropped first: progress is only
# interesting when it is current, and the terminal "done" event is re-derivable
# from run_download's return code.
_EVENT_QUEUE_MAX = 2000
_EVENT_QUEUE: "queue.Queue[str]" = queue.Queue(maxsize=_EVENT_QUEUE_MAX)
_EVENTS_DROPPED = 0


def _queue_event(payload: str) -> None:
    """Event sink installed for the duration of a run.

    Takes the ALREADY-SERIALIZED string that run_download hands its sink, so
    there is no second json.dumps. Must never raise — it runs at aio-dl.py's
    emit sites, including inside download worker threads.
    """
    global _EVENTS_DROPPED
    if not isinstance(payload, str):
        return
    while True:
        try:
            _EVENT_QUEUE.put_nowait(payload)
            return
        except queue.Full:
            try:
                _EVENT_QUEUE.get_nowait()
                _EVENTS_DROPPED += 1
            except queue.Empty:
                return  # drained concurrently; next put will fit


def poll_events(max_items: int = 200) -> str:
    """Drain up to `max_items` progress events, oldest first, as a JSON array
    string. Returns "[]" when idle — safe and cheap to call on a timer.

    Each element is one aio-dl.py `_emit` payload; `kind` says which
    (`series`, `chapters_selected`, `chapter_start`, `chapter_saved`, `phase`,
    `file_saved`, `missed`, `recovered`, `still_missed`, `done`).
    """
    out: List[Any] = []
    for _ in range(max(0, int(max_items))):
        try:
            out.append(json.loads(_EVENT_QUEUE.get_nowait()))
        except queue.Empty:
            break
        except ValueError:
            continue
    return json.dumps(out)


def drain_events() -> None:
    """Discard anything queued. Called before a run so a new download never
    shows the tail of the previous one."""
    global _EVENTS_DROPPED
    while True:
        try:
            _EVENT_QUEUE.get_nowait()
        except queue.Empty:
            break
    _EVENTS_DROPPED = 0


def events_dropped() -> int:
    """How many events were discarded to keep the queue bounded. Non-zero means
    the consumer is polling too slowly — useful when progress looks jumpy."""
    return _EVENTS_DROPPED


# ---------------------------------------------------------------------------
# ETA
#
# Behavioural port of UI-source/electron/downloader.js:applyChapterEta — grep
# applyChapterEta there when changing either. Same EMA (alpha 0.3), same
# two-sample floor, same "skip intervals opened by an already-processed
# chapter" rule.
#
# MEASURED HERE, IN PYTHON, AT EMIT TIME — not in Kotlin off the poll loop.
# The desktop makes the same call for the same reason: its renderer coalesces
# bursts, so measuring downstream of the buffer merges two chapters into one
# sample. Android's buffer is worse, not better — DownloadService polls every
# 700ms and gets events in batches, so two chapters finishing inside one
# interval would read as one ~0ms sample plus one full-interval sample. The
# emit site is the only place with the real timing.
#
# monotonic(), not wall clock (the desktop's Date.now()): a phone re-syncs NTP
# and changes timezone under a running download, and a backwards clock jump
# would otherwise produce a negative sample and poison the average.
# ---------------------------------------------------------------------------

_ETA_EMA_ALPHA = 0.3
# Below this, the average is one lucky (or unlucky) chapter. The desktop shows
# an indeterminate bar until then and so should any consumer here.
_ETA_MIN_SAMPLES = 2


class _EtaEstimator:
    """Folds chapter ticks into a rolling per-chapter average and stamps
    `eta_ms` / `eta_samples` / `chapter_ms_ema` / `processed` / `total` onto the
    outgoing `chapter_start` event.

    One instance per run (see run_download). Only the emitting side touches it,
    and aio-dl.py's chapter loop is single-threaded, so no lock.

    Stamping `processed`/`total` onto the event is deliberate: it makes the
    notification a pure function of the last event received, instead of asking
    every consumer to keep its own counters in sync with this one.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        # `clock` is injectable so the tests can drive time directly instead of
        # sleeping or monkeypatching the time module out from under everything.
        self._clock = clock
        self._last_tick_at: Optional[float] = None
        self._last_tick_cached = False
        self._ema_ms = 0.0
        self._samples = 0
        self._total = 0
        self._processed = 0

    def observe(self, event: Dict[str, Any]) -> None:
        """Update from `event`, mutating it in place. Must never raise — it runs
        inside the event sink, on aio-dl.py's own threads."""
        kind = event.get("kind")
        if kind == "chapters_selected":
            total = event.get("total")
            if isinstance(total, int) and total > 0:
                self._total = total
            return
        if kind != "chapter_start":
            return

        # Counter first: the ETA is computed off (total - processed), so the
        # in-flight chapter must already be counted. Mirrors the ordering in
        # downloader.js (`entry.processedChapters++` before applyChapterEta).
        self._processed += 1

        now = self._clock()
        # A sample is the interval between two ticks, and it is only real work
        # if the chapter that OPENED it was actually downloaded. On a resume the
        # leading already-processed ticks cost ~0ms; averaging them in yields a
        # wildly optimistic ETA that stalls the moment real downloading starts.
        # Excluding them over-estimates while the cached prefix drains, which is
        # the honest direction to be wrong in.
        if self._last_tick_at is not None and not self._last_tick_cached:
            sample_ms = (now - self._last_tick_at) * 1000.0
            self._ema_ms = (
                self._ema_ms + _ETA_EMA_ALPHA * (sample_ms - self._ema_ms)
                if self._samples
                else sample_ms
            )
            self._samples += 1
        self._last_tick_at = now
        self._last_tick_cached = event.get("resumed") is True

        event["processed"] = self._processed
        if self._total:
            event["total"] = self._total
        if self._samples < _ETA_MIN_SAMPLES:
            return

        event["chapter_ms_ema"] = round(self._ema_ms)
        event["eta_samples"] = self._samples
        # Explicit None rather than an absent key when the total is unknown:
        # consumers merge event fields onto held state, so omitting it would
        # leave a stale ETA on screen forever.
        remaining = max(0, self._total - self._processed)
        event["eta_ms"] = round(remaining * self._ema_ms) if self._total > 0 else None


def cancel() -> None:
    """Ask the in-flight run to stop at its next checkpoint.

    Cooperative: pages already in flight finish or time out, then the chapter
    loop breaks and everything downloaded so far stays on disk and resumable.
    Safe to call from any thread, and safe to call when nothing is running.
    """
    _aio_dl().request_run_cancel()


# Distinct exit code for "the user cancelled this", so a consumer never has to
# guess. 130 is the shell convention for SIGINT (128 + 2), which is the closest
# existing meaning: the run was interrupted on purpose and its partial output is
# intact and resumable. It deliberately OVERRIDES the code main() would have
# returned — a run that aborts because cancellation cut a chapter short is a
# cancellation, not a download failure, and reporting it as failure would send
# the user hunting for a nonexistent site problem.
CANCELLED_EXIT_CODE = 130


# ---------------------------------------------------------------------------
# Engine serialization
#
# EVERY entry point that calls aio-dl.py's main() must hold this. On the desktop
# each run is its own OS process, so nothing there needs a lock; here one
# interpreter is shared by the download service AND the library's update checks,
# and main() reaches for state that is global to the module:
#
#   * run_download clears _RUN_CANCEL on entry — so a `--list-chapters` started
#     while a download is being cancelled would UN-CANCEL it, and the download
#     would carry on as though Cancel had never been pressed.
#   * _reset_host_concurrency_caps wipes the per-host backoff the running
#     download has been learning.
#   * aio-dl.py's own module globals (_image_prefetch_queue,
#     _RATE_LIMIT_SCHEDULE, the event sink) are single-run by construction.
#
# The download path WAITS for the lock; a library check does not — it reports
# ENGINE_BUSY instead, because a user tapping "check for updates" wants an
# answer or a reason, not a UI that silently hangs until a 40-chapter download
# finishes. Kotlin also checks DownloadRepository.active before offering the
# button, so this is the backstop rather than the usual path.
# ---------------------------------------------------------------------------

_ENGINE_LOCK = threading.RLock()

#: `error` value returned by the non-blocking entry points when a run holds the
#: engine. Kotlin matches on this string — grep ENGINE_BUSY in LibraryRepository.
ENGINE_BUSY = "engine_busy"


def run_download(argv: List[str], sink: Optional[Callable[[str], None]] = None) -> int:
    """Run one download to completion. Returns the process-style exit code
    (0 ok, 1 aborted, 2 user-actionable site failure, 130 cancelled).

    `argv` is the CLI argument list WITHOUT the program name — exactly what the
    desktop app passes to the Python process, so Kotlin's arg builder and
    UI-source/electron/downloader.js:buildCliArgs stay behaviourally identical.

    `sink` receives one JSON string per structured progress event (see
    aio-dl.py:_emit), enriched with the ETA fields (see _EtaEstimator). It is
    called on the emitting thread, so it must be cheap — queue and return.

    BLOCKS for the whole download, and blocks additionally on [_ENGINE_LOCK] if
    a library update check happens to be mid-flight (seconds). Call it on a
    background thread; the Android side runs it inside a foreground Service.
    """
    with _ENGINE_LOCK:
        return _run_engine(argv, sink)


def _cf_interactive_solving():
    """The interactive-CF-solve permission scope for one engine run, as False.

    Degrades to a no-op context rather than raising: this module is imported on
    a device where a missing optional dependency must not take the app down, and
    the fallback direction is safe — crawlee_utils' own default is already False,
    so the only thing lost is the containment barrier, not the gate.
    """
    try:
        from sites.crawlee_utils import interactive_solving

        return interactive_solving(False)
    except Exception:
        from contextlib import nullcontext

        return nullcontext()


def _run_engine(argv: List[str], sink: Optional[Callable[[str], None]] = None) -> int:
    """run_download's body, minus the locking. Callers MUST hold [_ENGINE_LOCK]."""
    mod = _aio_dl()

    # Fresh cancellation state per run: this process is reused across
    # downloads, unlike the desktop's one-process-per-download model.
    mod.clear_run_cancel()
    try:
        mod._reset_host_concurrency_caps()
    except Exception:
        # Per-host backoff state leaking between runs costs a little
        # concurrency, never correctness. Not worth failing the run over.
        pass

    if sink is not None:
        eta = _EtaEstimator()

        def _sink(event: Dict[str, Any]) -> None:
            try:
                eta.observe(event)
            except Exception:
                # An ETA is a nicety; a run is not. Never let the estimator
                # take down an emit site.
                pass
            # default=str so one exotic field can't cost the whole event.
            # aio-dl.py's _emit swallows sink exceptions, so a TypeError here
            # would drop the event silently and progress would just stop with
            # nothing to diagnose. Chapter numbers are str/int/float today
            # (`n = ch["chap"]`), but the event set is explicitly open-ended.
            sink(json.dumps(event, default=str))

        mod.set_event_sink(_sink)

    saved_argv = sys.argv
    sys.argv = ["aio-dl.py", *[str(a) for a in argv]]
    try:
        code = 0
        try:
            _interactive = _cf_interactive_solving()
            with _interactive:
                # CONTAINMENT BARRIER for the interactive-CF-solve permission,
                # and the reason it is a context manager rather than a flag.
                #
                # main() escalates this to True for a foreground download (grep
                # the opt-in in aio-dl.py). Desktop gets away with never
                # restoring it because a run IS a process. Here ONE interpreter
                # serves every entry point, and _run_engine is the single door
                # they all come through — run_download, list_chapters, search,
                # check_series_updates. Without this, one download would leave
                # the permission set on its thread and the NEXT update sweep to
                # reuse that thread could pop a ChallengeActivity at a person
                # who only opened the library.
                #
                # reset() restores the value from before this `with`, discarding
                # whatever main() set inside it — which is precisely the
                # guarantee needed, since main() has no scope of its own.
                mod.main()
        except SystemExit as exc:
            # argparse errors and every explicit sys.exit() land here. main() is
            # written for a process that is about to die; we just want the code.
            raw = exc.code
            code = 0 if raw is None else (raw if isinstance(raw, int) else 1)
        except KeyboardInterrupt:
            code = 1
        # Checked AFTER main() returns, not inside the handlers: a cancelled run
        # exits through whichever path it happened to be on (clean finish,
        # sys.exit(1) from the abort branch), and all of them mean the same
        # thing here. _RUN_CANCEL is never cleared mid-run, so this still reads
        # True at the end. See aio-dl.py:run_cancelled.
        return CANCELLED_EXIT_CODE if mod.run_cancelled() else code
    finally:
        sys.argv = saved_argv
        if sink is not None:
            mod.set_event_sink(None)


def _require_settings_object(settings_json: str) -> Dict[str, Any]:
    """Decode a settings blob from the Kotlin side, failing loudly.

    Every `*_json` entry point goes through this. The alternative — treating a
    malformed blob as an empty dict — would silently start a download with
    default settings, which looks like the app ignoring the whole settings
    screen and is far harder to diagnose than an exception with the parser's own
    message in it.
    """
    try:
        settings = json.loads(settings_json)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"settings_json is not valid JSON: {exc!s}") from exc
    if not isinstance(settings, dict):
        raise TypeError(
            f"settings_json must encode a JSON object, got {type(settings).__name__}"
        )
    return settings


def _contained_in(root: str, path: str) -> bool:
    """Is `path` inside `root`? Both arguments must already be realpath'd.

    The shared half of every containment guard in this module — see
    [delete_series] for why they exist at all. commonpath raises rather than
    returning False for paths on different Windows drives, which never happens
    on device but does in the desktop test suite that also runs this file.
    """
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Resource limits
#
# Behavioural port of UI-source/electron/resource-limits.js — which is the
# REFERENCE, and which already has one renderer mirror at
# UI-source/src/lib/resourceLimits.js. grep NETWORK_PRESETS in all three when a
# table changes; the numbers must stay equal.
#
# SEMANTICS = HARD OVERRIDE, not a ceiling. A level other than "unlimited"
# REPLACES the manual concurrency knobs outright; "unlimited" is a true no-op
# that leaves them exactly as the user set them.
#
# APPLIED INSIDE build_argv, whereas the desktop applies it in main.js around
# buildCliArgs. Same intent, different geography: main.js's spawn handler is the
# one place every desktop download funnels through, and on Android build_argv IS
# that place — Kotlin only collects settings into JSON. Doing it here means a
# download started from the queue, the library update check, or a future search
# result all honour the current level without each remembering to ask.
# ---------------------------------------------------------------------------

# aio-dl.py's argparse defaults for the network knobs. Only used by
# resume_throttle_flags, which must emit a CONCRETE number for every knob (see
# its docstring) even when the user has never touched one.
_NET_DEFAULTS: Dict[str, int] = {
    "imageConcurrency": 8,
    "imageWorkers": 3,
    "imagePrefetchParallel": 2,
    "imagePrefetchDepth": 2,
    "searchParallelism": 6,
}

_NETWORK_PRESETS: Dict[str, Dict[str, int]] = {
    "high": {"imageConcurrency": 6, "imageWorkers": 3, "imagePrefetchParallel": 2, "imagePrefetchDepth": 2, "searchParallelism": 5},
    "balanced": {"imageConcurrency": 4, "imageWorkers": 2, "imagePrefetchParallel": 1, "imagePrefetchDepth": 1, "searchParallelism": 3},
    "low": {"imageConcurrency": 2, "imageWorkers": 1, "imagePrefetchParallel": 1, "imagePrefetchDepth": 1, "searchParallelism": 2},
}

# --max-cpu-percent value per level; 100 == aio-dl.py's prior behaviour (the
# pool budget equals os.cpu_count()). grep _cpu_pool_budget in aio-dl.py.
_CPU_PRESETS: Dict[str, int] = {"high": 75, "balanced": 50, "low": 25}

_NETWORK_KNOBS = ("imageConcurrency", "imageWorkers", "imagePrefetchParallel", "imagePrefetchDepth")


def _norm_level(level: Any) -> str:
    """Any stored value -> a known key. Anything unrecognized, absent, or
    non-string reads as "unlimited", so a corrupt setting can only ever fail
    OPEN (full speed), never silently throttle a user to 2 connections."""
    value = level.lower() if isinstance(level, str) else "unlimited"
    return value if value in _NETWORK_PRESETS else "unlimited"


def is_network_limited(level: Any) -> bool:
    return _norm_level(level) != "unlimited"


def apply_network_limit(settings: Dict[str, Any], level: Any) -> Dict[str, Any]:
    """Hard-override the four download concurrency knobs for `level`.

    Returns a COPY when a preset applies, and the original object when the level
    is unlimited — so callers can pass the result straight through without
    worrying about which they got. searchParallelism is deliberately not set
    here: it belongs to the search path, not a download.
    """
    lvl = _norm_level(level)
    if lvl == "unlimited":
        return settings
    preset = _NETWORK_PRESETS[lvl]
    merged = dict(settings)
    for knob in _NETWORK_KNOBS:
        merged[knob] = preset[knob]
    return merged


def cpu_percent_for_level(level: Any) -> int:
    lvl = _norm_level(level)
    return 100 if lvl == "unlimited" else _CPU_PRESETS[lvl]


def search_parallelism_for_level(current: Any, level: Any) -> Any:
    """Preset fan-out when a level is active, else the caller's own value (which
    may be None — aio-dl.py then applies its default of 6)."""
    lvl = _norm_level(level)
    return current if lvl == "unlimited" else _NETWORK_PRESETS[lvl]["searchParallelism"]


def resume_throttle_flags(settings: Dict[str, Any]) -> List[str]:
    """CLI flags that make the CURRENT throttle beat the one persisted in
    run_params.json.

    Emits concrete values for all five knobs ALWAYS, never a subset. That is
    what makes "current wins" hold in BOTH directions — including
    was-limited-now-unlimited, where the persisted preset has to be overridden
    back UP and an omitted flag would let the old low value stand. aio-dl.py
    keeps explicit CLI dests over `--restore-parameters` ones (grep
    _user_set_dests), and none of these are in _RESUME_GATING_DESTS, so
    overriding them never re-downloads a completed chapter.

    Reads the knobs FLAT, unlike the reference, which pulls imageWorkers out of
    a nested `defaults` object — that split is an artifact of the Electron
    settings file's shape, and Android's settings dict is the flat one
    build_argv already consumes.
    """
    settings = settings or {}
    with_defaults = {
        knob: (settings[knob] if settings.get(knob) is not None else _NET_DEFAULTS[knob])
        for knob in _NETWORK_KNOBS
    }
    effective = apply_network_limit(with_defaults, settings.get("networkLimit"))
    return [
        "--image-concurrency", str(effective["imageConcurrency"]),
        "--image-workers", str(effective["imageWorkers"]),
        "--image-prefetch-parallel", str(effective["imagePrefetchParallel"]),
        "--image-prefetch-depth", str(effective["imagePrefetchDepth"]),
        "--max-cpu-percent", str(cpu_percent_for_level(settings.get("cpuLimit"))),
    ]


def resume_throttle_flags_json(settings_json: str) -> str:
    """JSON-in / JSON-out wrapper for the Kotlin side. See run_download_json on
    why lists can't cross that boundary directly."""
    return json.dumps(resume_throttle_flags(_require_settings_object(settings_json)))


#: Human labels for a level, so the UI's banner text and this module's idea of
#: the level set cannot drift. Mirror of NETWORK_LEVELS in
#: UI-source/src/lib/resourceLimits.js.
_LEVEL_LABELS: Dict[str, str] = {
    "unlimited": "Unlimited",
    "high": "High",
    "balanced": "Balanced",
    "low": "Low",
}


def effective_limits(settings: Dict[str, Any]) -> Dict[str, Any]:
    """What the concurrency knobs will ACTUALLY be, given the resource limits.

    Exists because [apply_network_limit] is a HARD OVERRIDE: at any level other
    than unlimited it replaces the four download knobs outright, and there was
    no way for a UI to find that out. The Android Download screen went on
    rendering the user's typed `imageWorkers` while every run used the preset's
    — android/PARITY.md D9, a display that is simply false. The desktop has had
    the matching affordance since Resource Limits shipped (the lock icon and
    the disabled-but-showing-the-real-number inputs in
    UI-source/src/components/SettingsTab.jsx; the logic it calls is
    `networkEffective` / `isNetworkManaged` / `networkPreviewText` in
    UI-source/src/lib/resourceLimits.js, which this mirrors).

    Returns, for each of the five knobs, BOTH numbers:

        effective  what the run will use.
        stored     what the user typed, untouched — a hard override never
                   erases a manual setting, and returning it is what lets the
                   UI restore the field the instant the level goes back to
                   unlimited.

    plus `networkManaged` (drives the lock/disabled state), the normalized
    levels and their labels, `maxCpuPercent`, and the two one-line preview
    strings the desktop shows under each dropdown. `networkPreview` /
    `cpuPreview` are None at unlimited, matching the reference's `null`.

    searchParallelism is in here even though [apply_network_limit] deliberately
    leaves it alone — a search is where the user SEES that number, and its
    Android baseline is [_MOBILE_SEARCH_PARALLELISM] (4), not the desktop's
    argparse default of 6. Reporting the desktop's number would be a second
    display lie in the shape of a fix for the first one.
    """
    settings = settings or {}
    net_level = _norm_level(settings.get("networkLimit"))
    cpu_level = _norm_level(settings.get("cpuLimit"))

    stored: Dict[str, Any] = {
        knob: (
            settings[knob] if settings.get(knob) is not None else _NET_DEFAULTS[knob]
        )
        for knob in _NETWORK_KNOBS
    }
    effective = apply_network_limit(dict(stored), net_level)

    # The search knob follows build_search_argv's own rule, not _NET_DEFAULTS'.
    stored_search = settings.get("searchParallelism")
    if stored_search is None:
        stored_search = _MOBILE_SEARCH_PARALLELISM
    effective_search = search_parallelism_for_level(stored_search, net_level)

    knobs = {
        knob: {"effective": effective[knob], "stored": stored[knob]}
        for knob in _NETWORK_KNOBS
    }
    knobs["searchParallelism"] = {
        "effective": effective_search,
        "stored": stored_search,
    }

    preset = _NETWORK_PRESETS.get(net_level)
    return {
        "networkLimit": net_level,
        "networkLimitLabel": _LEVEL_LABELS[net_level],
        "networkManaged": net_level != "unlimited",
        "cpuLimit": cpu_level,
        "cpuLimitLabel": _LEVEL_LABELS[cpu_level],
        "maxCpuPercent": cpu_percent_for_level(cpu_level),
        "knobs": knobs,
        "networkPreview": (
            None
            if preset is None
            else (
                f"curl_cffi {preset['imageConcurrency']} · "
                f"workers {preset['imageWorkers']} · "
                f"prefetch {preset['imagePrefetchParallel']}×"
                f"{preset['imagePrefetchDepth']} · "
                f"search {preset['searchParallelism']}"
            )
        ),
        "cpuPreview": (
            None
            if cpu_level == "unlimited"
            else f"~{_CPU_PRESETS[cpu_level]}% of CPU cores"
        ),
    }


def effective_limits_json(settings_json: str) -> str:
    """JSON-in / JSON-out wrapper around [effective_limits] for the Kotlin side.

    Never raises: a settings screen that cannot ask this question would have to
    guess, and guessing is the defect it exists to close. A malformed blob
    reports the unlimited state, which is what an unconfigured app has anyway.
    """
    try:
        settings = _require_settings_object(settings_json) if settings_json else {}
    except (TypeError, ValueError):
        settings = {}
    return json.dumps(effective_limits(settings))


# ---------------------------------------------------------------------------
# CLI argument building
#
# Behavioural port of UI-source/electron/downloader.js:buildCliArgs. That file
# is the REFERENCE — when it changes, change this, and vice versa. grep
# buildCliArgs in both.
#
# WHY THIS LIVES IN PYTHON rather than Kotlin (the Android plan originally said
# Kotlin): the guards below are load-bearing — aio-dl.py HARD-ERRORS on several
# flag combinations, so getting them wrong fails the download outright. In
# Python they get offline pytest coverage (tests/test_android_argv.py) next to
# the argparse they feed; in Kotlin, testing them would need a device or a JVM
# test harness and a rebuild per iteration. Kotlin just collects UI state into
# JSON and calls build_argv_json.
# ---------------------------------------------------------------------------

# UI key -> valued CLI flag. Order is preserved (dict insertion order) so the
# emitted argv is stable and diffable against the Electron spawn line.
_VALUED_FLAGS: Dict[str, str] = {
    "format": "--format",
    "epubLayout": "--epub-layout",
    # Moves only the EPUB artifact, never the metadata — .aio_series.json and
    # details.json stay in the series folder under --output-dir, which is why
    # aio-dl.py's final-file coverage map is keyed per format (grep
    # _final_file_recorded_coverage). A phone use for it: EPUBs onto shared
    # storage for a reader app while the CBZ library stays app-scoped.
    "epubDir": "--epub-dir",
    "quality": "--quality",
    "scaling": "--scaling",
    "width": "--width",
    "aspectRatio": "--aspect-ratio",
    "chapters": "--chapters",
    "language": "--language",
    "split": "--split",
    "site": "--site",
    "cookies": "--cookies",
    "group": "--group",
    "mtl": "--mtl",
    "excludeGroup": "--exclude-group",
    "jobs": "--jobs",
    "imageWorkers": "--image-workers",
    "httpTimeout": "--http-timeout",
    "httpMaxRetries": "--http-max-retries",
    "httpBackoffBase": "--http-backoff-base",
    "httpBackoffCap": "--http-backoff-cap",
    "netMinGap": "--net-min-gap",
    "multiSourceQualityMin": "--multi-source-quality-min",
    "multiSourcePrefetched": "--multi-source-prefetched",
    "prefetchImageWorkers": "--prefetch-image-workers",
    "imageConcurrency": "--image-concurrency",
    "imagePrefetchDepth": "--image-prefetch-depth",
    "imagePrefetchParallel": "--image-prefetch-parallel",
    "maxCpuPercent": "--max-cpu-percent",
    "missedRetries": "--missed-retries",
    "missedLog": "--missed-log",
    # ── Per-chapter watchdog + inline-retry knobs ──────────────────────────
    # These matter MORE on a phone than on the desktop, which is why they are
    # worth a settings surface here at all. The 90s default deadline was tuned
    # against a wired connection; a chapter that would finish in 100s on a
    # train is failed and retried from scratch, spending the radio twice. The
    # backoff pair is the other direction — a mobile CDN under a captive
    # portal or a carrier-grade NAT benefits from waiting longer, not from
    # hammering. Emitted only when they differ from the Python default (see
    # _VALUED_FLAG_DEFAULTS); each of those defaults is itself read from an env
    # var, so an omitted flag lets an env override stand.
    "chapterDeadlineSeconds": "--chapter-deadline-seconds",
    "chapterHostPoisonThreshold": "--chapter-host-poison-threshold",
    "inlineChapterRetries": "--inline-chapter-retries",
    "inlineChapterBackoff": "--inline-chapter-backoff",
    "jobStallTimeout": "--job-stall-timeout",
    "jobHardTimeout": "--job-hard-timeout",
    "jobRetries": "--job-retries",
    "jobSpawnGap": "--job-spawn-gap",
    "coordDir": "--coord-dir",
    "metadataSource": "--metadata-source",
    "metadataTagMinRank": "--metadata-tag-min-rank",
}

# DELIBERATELY ABSENT vs the Electron flagMap: mangafireImageConcurrency. It is
# a pre-2026-05-13 back-compat alias that aio-dl.py routes onto
# --image-concurrency with a DeprecationWarning. Android has no saved settings
# predating that rename, so carrying it would only create a way to emit both.

_BOOL_FLAGS: Dict[str, str] = {
    "keepChapters": "--keep-chapters",
    "noFinalFile": "--no-final-file",
    "keepImages": "--keep-images",
    "multiSource": "--multi-source",
    "noProcessing": "--no-processing",
    "noCleanup": "--no-cleanup",
    "noPartials": "--no-partials",
    "mixByUpvote": "--mix-by-upvote",
    # Companion to `group`: skip a chapter that has none of the preferred
    # groups rather than falling back to another one. RESUME-GATING on the
    # Python side (grep _RESUME_GATING_DESTS) — toggling it invalidates the
    # on-disk images, so a resume with it flipped re-downloads.
    "noGroupFallback": "--no-group-fallback",
    # Also resume-gating, and it changes WHICH listing the handler fetches, so
    # it is a property of the download and not of the device.
    "downloadVolumes": "--download-volumes",
    "verbose": "--verbose",
    "debug": "--debug",
    "noRetryMissedChapters": "--no-retry-missed-chapters",
    "restoreParameters": "--restore-parameters",
    "seededOnly": "--seeded-only",
    "webtoonRecompress": "--webtoon-recompress",
    "komikku": "--komikku",
    "modernize": "--modernize",
    "noFastDownload": "--no-fast-download",
    "metadataRefresh": "--metadata-refresh",
    # MORE THAN COSMETIC on a phone, which is why it earns a control here even
    # though the desktop treats it as an archival nicety. Auxiliary assets are
    # fetched by aio-dl.py's _fetch_binary_asset_bytes, which is deliberately
    # EXEMPT from the per-chapter watchdog (grep the chapter-watchdog invariant
    # in CLAUDE.md — honoring the deadline there silently dropped BGM that the
    # ComicInfo had already claimed). The consequence on mobile: a slow audio
    # CDN extends per-chapter wall time with nothing to cut it short, on a
    # metered radio, and until this flag was emittable there was no way to
    # decline. Not resume-gating, so turning it on mid-series is free.
    "noSidecarAssets": "--no-sidecar-assets",
}

# `promptUrls` is in the Electron boolMap but omitted here on purpose: it makes
# aio-dl.py read URLs from stdin, and there is no stdin under Chaquopy.


# Keys whose CLI flag is declared `nargs="+"` in aio-dl.py, and which therefore
# CANNOT be emitted as a plain `[flag, value]` pair.
#
# THE BUG THIS EXISTS TO FIX, which is a run-killer and not a nicety: with
# `nargs="+"` argparse keeps consuming tokens until it meets the next option, so
# whenever one of these is the LAST flag before the positional URL, argparse
# swallows the URL as a group name and aio-dl.py dies with "You must provide at
# least one URL". Reachable today from an ordinary form — a user who sets only a
# preferred group and leaves every other knob at its default produces exactly
# `--group A <url>`. Measured against the real parser, all three of
# `--group "A, B" <url>`, `--group A --group B <url>` and
# `--group A --exclude-group B <url>` lose the URL, so the repeated-flag form
# alone does NOT fix it.
#
# The `=` form does: `--group=A` attaches the value to the option token, which
# nargs cannot reach past. One flag per name, so a name arrives as its own
# argparse entry rather than depending on main()'s later `group_string.split(",")`
# (grep it in aio-dl.py) — the same shape _append_saved_update_options emits on
# the --update-all replay path. It also makes a name that starts with "-" safe,
# which the detached form does not.
#
# NOT a fix for a group name that CONTAINS a comma: main() splits every value on
# commas after parsing, so that ambiguity is the CLI's own and cannot be closed
# from the emitting side.
_REPEATED_VALUE_FLAGS = frozenset({"group", "excludeGroup"})

# Python-side defaults for valued keys, so an at-default value produces no flag.
#
# Emitting a default is not WRONG — argparse would apply the same number — but
# the built command line is logged and read by a human when a run misbehaves,
# and a wall of default flags is where the one genuinely odd flag hides. All
# four watchdog defaults are additionally read from env vars in aio-dl.py's
# argparse (AIO_CHAPTER_DEADLINE, AIO_CHAPTER_HOST_POISON,
# AIO_INLINE_CHAPTER_RETRIES, AIO_INLINE_CHAPTER_BACKOFF), so an OMITTED flag
# also lets an env override stand where an emitted one would silently beat it.
#
# String defaults compare as strings; numeric ones go through _differs, which
# coerces — so a settings blob carrying "90" is recognized as the default rather
# than emitted as a redundant flag. A non-numeric value for a numeric knob also
# reads as "default" and is dropped, which is the safe direction: argparse would
# hard-error on it and kill the run.
_VALUED_FLAG_DEFAULTS: Dict[str, Any] = {
    "chapters": "all",
    "mtl": "avoid",
    "chapterDeadlineSeconds": 90.0,
    "chapterHostPoisonThreshold": 5,
    "inlineChapterRetries": 2,
    "inlineChapterBackoff": 30.0,
}


def _is_true(value: Any) -> bool:
    """Exact analogue of JS `=== true`, so a 1 or a "true" string does NOT
    enable a flag (mirrors the reference, and stops a sloppy settings blob from
    silently turning on an incompatible mode)."""
    return value is True


def _num(value: Any) -> Optional[float]:
    """Numeric coercion for the differ-from-default tests. Returns None when the
    value isn't numeric, which the callers treat as "absent"."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _differs(value: Any, default: float) -> bool:
    """True when a knob should be emitted: present, numeric, and != the Python
    argparse default.

    Deliberately coerces where the JS reference compares with a bare `!==`
    against a number literal — there, a settings dict carrying the string "85"
    emits `--webtoon-recompress-quality 85` even though it IS the default. That
    is a latent quirk, not intended behaviour; coercing here can only ever omit
    a flag whose value equals the default, which aio-dl.py then applies itself.
    Same resulting download, cleaner command line.
    """
    n = _num(value)
    return n is not None and n != default


def _is_default_valued(key: str, value: Any) -> bool:
    """True when `value` is the Python-side default for `key`, so the flag can
    be left off entirely. See [_VALUED_FLAG_DEFAULTS]."""
    if key not in _VALUED_FLAG_DEFAULTS:
        return False
    default = _VALUED_FLAG_DEFAULTS[key]
    if isinstance(default, str):
        return str(value) == default
    return not _differs(value, default)


def _split_names(value: Any) -> List[str]:
    """A `--group` / `--exclude-group` setting -> one name per entry.

    Accepts BOTH shapes a caller may hold: the single comma-separated string the
    Android form's free-text field produces today, and a list, which is what a
    chip-style picker would produce. Splitting the string here rather than
    forwarding it whole is what lets each name become its own argparse entry —
    see [_REPEATED_VALUE_FLAGS].
    """
    if isinstance(value, (list, tuple)):
        raw = [str(v) for v in value]
    else:
        raw = str(value).split(",")
    return [name.strip() for name in raw if name.strip()]


class UnsupportedUrlError(ValueError):
    """A URL no download on this platform can serve.

    Raised by [build_argv] and converted to a renderable JSON object by
    [build_argv_json] — the JNI boundary carries structured data, never
    exceptions. See [_UNSUPPORTED_DOWNLOAD_SITES] for what qualifies and
    build_argv_json's docstring for the exact shape Kotlin receives.
    """

    def __init__(self, url: str, site: str, message: str) -> None:
        super().__init__(message)
        self.url = url
        self.site = site
        self.message = message


# Handler name -> its base domain, for URLs this process must refuse to
# download rather than fail halfway through.
#
# ONLY comix, and it is not the same reason it is excluded from search. A search
# merely wastes fan-out budget on it (its search() swallows failures to []);
# a DOWNLOAD cannot work at all, because comix drives its own Patchright
# session (grep _comix_worker_loop in sites/comix.py) and never goes through
# sites/browser_backend.py — so the WebView bridge that rescues every other
# browser-dependent handler here does nothing for it.
#
# WHY build_argv HAS TO BE THE ONE TO REFUSE: `--disable-sites` explicitly
# exempts a directly-downloaded URL ("an explicit pick overrides the block" —
# grep the flag's help in aio-dl.py), and it is the only site filter that
# reaches the download path. So forwarding `disabledSites` cannot stop a pasted
# comix link; the engine would accept it, boot a browser that is not there, and
# fail after the user had already queued the job.
#
# LITERAL DOMAINS, DELIBERATELY, rather than reading ComixSiteHandler.domains:
# build_argv is a pure function that Kotlin calls at enqueue time and that the
# offline tests exercise thousands of times, and importing the handler pulls in
# the whole 303-entry registry. The cost of the literal is that a domain
# rotation needs an edit here — grep `domains = (` in sites/comix.py, which is
# the one line to compare against.
_UNSUPPORTED_DOWNLOAD_SITES: Dict[str, str] = {
    "comix": "comix.to",
}


def _unsupported_download_site(url: str) -> Optional[str]:
    """The handler name that claims `url` and cannot download here, else None.

    Subdomain-tolerant (`*.comix.to` matches) and port-tolerant, so the check
    does not turn into a game of listing every host variant. A URL with no
    parseable host never matches — this refuses known-broken sites, it is not a
    URL validator, and aio-dl.py's own handler resolution owns that job.
    """
    try:
        host = urlparse(str(url).strip()).netloc.lower()
    except ValueError:
        return None
    host = host.rsplit("@", 1)[-1].split(":", 1)[0].rstrip(".")
    if not host:
        return None
    for site, domain in _UNSUPPORTED_DOWNLOAD_SITES.items():
        if host == domain or host.endswith("." + domain):
            return site
    return None


def build_argv(settings: Dict[str, Any]) -> List[str]:
    """Turn a UI settings dict into an aio-dl.py argv (WITHOUT the program name).

    A "url" key, if present, is appended LAST as the positional argument —
    aio-dl.py takes the series URL positionally.

    Returns flags in a stable order: valued, then boolean, then the special
    cases. Matching the Electron order matters only for readability of the
    logged command line; aio-dl.py's argparse is order-insensitive.

    RAISES [UnsupportedUrlError] for a URL no download here can serve. Callers
    reaching this from Kotlin should use [build_argv_json], which converts that
    into a renderable JSON object instead of an exception crossing JNI.
    """
    # Checked FIRST, before any flag work: refusing the job is the whole
    # outcome, and doing it here means the refusal happens at enqueue time with
    # the user still looking at the screen, rather than inside a foreground
    # Service several taps later.
    unsupported = _unsupported_download_site(settings.get("url") or "")
    if unsupported:
        raise UnsupportedUrlError(
            str(settings.get("url") or "").strip(),
            unsupported,
            f"{unsupported} downloads need a desktop browser this app does not "
            "have, so this link cannot be downloaded on Android. Search for the "
            "series instead and pick another source.",
        )
    # Resource limits first, so everything below sees the EFFECTIVE knobs. A
    # network preset replaces the four image-concurrency values outright; the
    # CPU preset is emitted only below 100 so an unlimited run produces the same
    # clean command line it always did. See the resource-limits section.
    settings = apply_network_limit(settings, settings.get("networkLimit"))
    cpu_percent = cpu_percent_for_level(settings.get("cpuLimit"))
    if cpu_percent < 100:
        settings = {**settings, "maxCpuPercent": cpu_percent}

    argv: List[str] = []

    for key, flag in _VALUED_FLAGS.items():
        value = settings.get(key)
        if value is None or value == "":
            continue
        if _is_default_valued(key, value):
            continue
        if key in _REPEATED_VALUE_FLAGS:
            # `--flag=name`, one per name. The attached form is load-bearing,
            # not a style choice — see _REPEATED_VALUE_FLAGS.
            argv.extend(f"{flag}={name}" for name in _split_names(value))
            continue
        argv.extend([flag, str(value)])

    fmt = settings.get("format")
    komikku = _is_true(settings.get("komikku"))

    # --webtoon-recompress needs an archive output; aio-dl.py hard-errors on
    # pdf/none. --komikku coerces format->cbz BEFORE that check, so it stays
    # valid then.
    recompress_incompatible = fmt in ("none", "pdf") and not komikku

    # --modernize rides the CBZ byte-passthrough fast path, and aio-dl.py
    # rejects it with a HARD error on ANY fast-path-disabling flag. Stricter
    # than the webtoon check above: cbz only, NOT epub.
    modernize_blocked = (
        (fmt != "cbz" and not komikku)
        or (_num(settings.get("quality")) is not None and _num(settings.get("quality")) < 100)
        or (_num(settings.get("scaling")) is not None and _num(settings.get("scaling")) < 100)
        or settings.get("cbzPreserveOriginals") is False
        or _is_true(settings.get("noProcessing"))
        or (settings.get("width") is not None and settings.get("width") != "")
        or (settings.get("aspectRatio") is not None and settings.get("aspectRatio") != "")
    )

    for key, flag in _BOOL_FLAGS.items():
        if not _is_true(settings.get(key)):
            continue
        if key == "webtoonRecompress" and recompress_incompatible:
            continue
        if key == "modernize" and modernize_blocked:
            continue
        argv.append(flag)

    # Negative-default flag: default-ON in Python, so only an EXPLICIT false
    # emits the negative form. `is False` means absent/None/True all leave it on
    # — older settings blobs without the field must not silently disable it.
    if settings.get("cbzPreserveOriginals") is False:
        argv.append("--no-cbz-preserve-originals")

    # Absent-means-ON, nested inside the multi-source opt-in: only an explicit
    # false suppresses it. Gated on multiSource so non-multi-source runs keep a
    # clean command line (Python would ignore it anyway).
    if _is_true(settings.get("multiSource")) and settings.get("multiSourceLazy") is not False:
        argv.append("--multi-source-lazy")

    if _is_true(settings.get("collapseSplits")):
        argv.append("--collapse-splits")

    disabled = settings.get("disabledSites")
    if isinstance(disabled, (list, tuple)) and len(disabled) > 0:
        argv.extend(["--disable-sites", ",".join(str(s) for s in disabled)])

    # Valued knobs for the two master toggles. Emitted only when the toggle is
    # on, the mode is satisfiable, and the value differs from the Python default
    # — Python applies the default when a flag is absent, so this is purely
    # about not polluting the command line.
    if _is_true(settings.get("webtoonRecompress")) and not recompress_incompatible:
        if _differs(settings.get("webtoonRecompressQuality"), 85):
            argv.extend(["--webtoon-recompress-quality", str(settings["webtoonRecompressQuality"])])
        if _differs(settings.get("webtoonRecompressMethod"), 4):
            argv.extend(["--webtoon-recompress-method", str(settings["webtoonRecompressMethod"])])

    if _is_true(settings.get("modernize")) and not modernize_blocked:
        if _is_true(settings.get("modernizeReversible")):
            # "Fully reversible (archival)" is a UI-level preset with NO Python
            # flag of its own. It forces the PAIR jxl + distance 0 and ignores
            # the stored routing knobs — a PAIR because `auto` + distance 0 is
            # NOT reversible (auto still routes color pages to the always-lossy
            # AVIF branch).
            argv.extend(["--modernize-format", "jxl", "--modernize-distance", "0"])
        else:
            mf = settings.get("modernizeFormat")
            if mf is not None and mf != "" and mf != "auto":
                argv.extend(["--modernize-format", str(mf)])
            if _differs(settings.get("modernizeDistance"), 1.0):
                argv.extend(["--modernize-distance", str(settings["modernizeDistance"])])
            if _differs(settings.get("modernizeQuality"), 90):
                argv.extend(["--modernize-quality", str(settings["modernizeQuality"])])
            # speed 0 is a valid non-default (slowest/smallest), so this must be
            # a differs-from-6 test, not a truthiness test.
            if _differs(settings.get("modernizeAvifSpeed"), 6):
                argv.extend(["--modernize-avif-speed", str(settings["modernizeAvifSpeed"])])
        # Both apply on either path: effort is a pure CPU<->size knob that also
        # affects lossless encodes, and min-saving still guards the PNG
        # pixel-lossless tier (JPEG reconstructions are exempt Python-side).
        if _differs(settings.get("modernizeMinSaving"), 0.92):
            argv.extend(["--modernize-min-saving", str(settings["modernizeMinSaving"])])
        if _differs(settings.get("modernizeEffort"), 7):
            argv.extend(["--modernize-effort", str(settings["modernizeEffort"])])

    url = settings.get("url")
    if url is not None and str(url).strip() != "":
        argv.append(str(url).strip())

    return argv


def build_argv_json(settings_json: str) -> str:
    """JSON-in / JSON-out wrapper around build_argv for the Kotlin side.

    TWO RETURN SHAPES, and the caller MUST branch on which it got:

        [ "--format", "cbz", "https://…" ]        success — a JSON ARRAY
        { "error": "unsupported_site", … }        refusal — a JSON OBJECT

    The array is exactly what run_download_json takes, so Kotlin can log the
    command line and then run it without touching the list. The object carries
    `error` ("unsupported_site"), `site` (the handler name), `url`, and
    `message` — a finished, user-facing sentence, so the UI renders it rather
    than composing its own wording from the code.

    DISCRIMINATE ON THE FIRST CHARACTER. `{` and `[` are the only two openers
    either shape can start with, which makes this a one-line branch with no
    speculative parse:

        val raw = aio.callAttr("build_argv_json", settingsJson).toString()
        if (raw.startsWith("[")) JSONArray(raw)          // run it
        else JSONObject(raw).getString("message")        // show it

    WHY AN OBJECT rather than letting the exception cross JNI: Chaquopy turns a
    Python raise into a PyException whose message is a stringified traceback.
    That is a crash to Kotlin, not a thing a Compose screen can render, and the
    one case this fires for — a pasted comix link — is an ordinary user mistake
    that deserves a sentence, not a stack trace.

    Cross-file: DownloadService.kt currently does a bare `JSONArray(...)` on
    this result and would throw on the object; SearchScreen/DownloadScreen are
    the natural places to surface `message`.
    """
    try:
        return json.dumps(build_argv(_require_settings_object(settings_json)))
    except UnsupportedUrlError as exc:
        return json.dumps(
            {
                "error": "unsupported_site",
                "site": exc.site,
                "url": exc.url,
                "message": exc.message,
            }
        )


def run_download_json(argv_json: str, sink: Optional[Callable[[str], None]] = None) -> int:
    """run_download with argv as a JSON array string. **This is the entry point
    Kotlin must call** — run_download itself is for Python-side callers.

    WHY IT EXISTS: a Java/Kotlin `List` does NOT arrive as a Python iterable.
    Chaquopy wraps it as an opaque proxy, so run_download's
    `[str(a) for a in argv]` dies with `TypeError: 'ArrayList' object is not
    iterable`. Java ARRAYS convert, java.util.List does not — a distinction
    that costs a device round-trip to rediscover. This is the same
    primitives-only boundary the module header describes: argv is structured,
    so it crosses as JSON.
    """
    try:
        argv = json.loads(argv_json)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"argv_json is not valid JSON: {exc!s}") from exc
    if not isinstance(argv, list):
        raise TypeError(f"argv_json must encode a JSON array, got {type(argv).__name__}")
    # Default the sink to the internal queue so Kotlin gets progress by calling
    # poll_events() — no callback has to cross back into the JVM. Drain first so
    # a new run never replays the tail of the previous one.
    if sink is None:
        drain_events()
        sink = _queue_event
    return run_download([str(a) for a in argv], sink)


def list_chapters(url: str, extra_args: Optional[List[str]] = None) -> str:
    """Chapter list for `url` as a JSON string — the update-check path.

    Mirrors what the desktop UI does (spawn `--list-chapters` and read the last
    stdout line starting with `{`), but in-process. Cross-file:
    UI-source/electron/main.js:_checkSeriesUpdates.

    Returns `{"error": "engine_busy"}` rather than waiting when a download holds
    the engine — see the _ENGINE_LOCK header. The lock is REENTRANT, so a caller
    already holding it still gets through.
    """
    import io
    from contextlib import redirect_stdout

    if not _ENGINE_LOCK.acquire(blocking=False):
        return json.dumps({"error": ENGINE_BUSY})
    try:
        argv = ["--list-chapters", *(extra_args or []), url]
        buffer = io.StringIO()
        # Captured rather than let through: under Chaquopy stdout IS logcat, and
        # the payload for an 800-chapter series would bury the run's own log.
        with redirect_stdout(buffer):
            _run_engine(argv)
        # Same "last line that starts with {" rule as the Electron side: the
        # payload is preceded by ordinary progress logging.
        for line in reversed(buffer.getvalue().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return line
        return json.dumps({"error": "no chapter payload emitted"})
    finally:
        _ENGINE_LOCK.release()


# ---------------------------------------------------------------------------
# Cross-site search
#
# Behavioural port of UI-source/electron/searcher.js:buildSearchArgs, run the
# same way list_chapters runs --list-chapters: in-process, stdout captured. The
# `--search-json` contract is a {"candidates": [...]} dict on every successful
# exit (aio_search_cli.py, grep json_output), and an EMPTY candidate list is a
# legitimate "no results" — distinct from the error shapes returned here.
#
# Two mobile-specific departures from the desktop args, both deliberate:
#
#   1. comix is force-excluded. It is the one handler that never went through
#      the browser seam — it drives its own Patchright session
#      (_comix_worker_loop), which does not exist on Android. Its search() is
#      required to swallow failures to [] rather than raise (CLAUDE.md), so
#      leaving it in would not break the run; it would just spend the fan-out's
#      soft-barrier budget failing. Excluding it also keeps it out of the image
#      -quality probe, which is the expensive half.
#   2. search parallelism defaults BELOW the desktop's 6. The fan-out opens one
#      TLS connection per site; a phone radio handles that worse than an
#      ethernet NIC, and the Resource Limits presets tighten it further.
# ---------------------------------------------------------------------------

# Search-capable handlers that cannot work in this process, regardless of user
# settings.
#
# EXACTLY ONE ENTRY, and the two names this comment used to promise as "coming"
# were both wrong — the tuple never contained them and, measured, neither
# belongs:
#
#   weebcentral — its fallback ladder is `cloudscraper -> rescue_cf_html ->
#       raise` now, and rescue_cf_html's SECOND tier is the embedder browser,
#       i.e. this app's WebView bridge. So weebcentral rescues itself HERE. The
#       old "hard-raises without impit" rationale described the pre-2026-08
#       hand-ordered ladder; impit is genuinely absent on Android (see the
#       "NOT INSTALLED" list in android/app/build.gradle.kts), which only means
#       rescue_cf_html's first tier no-ops and the WebView tier takes it.
#   kagane — not search-capable on EITHER platform. It never overrides
#       BaseSiteHandler.search, so sites.iter_search_capable_handlers() filters
#       it out before the fan-out and `--disable-sites kagane` would be inert.
#       Its pywidevine dependency gates DOWNLOADS, which is a different list.
#
# Verify both claims:
#   python -c "import sites; n={h.name for h in sites.iter_search_capable_handlers()}; print({s: s in n for s in ('comix','weebcentral','kagane')})"
#   -> comix True, weebcentral True, kagane False
#
# A tuple rather than a bare string only because the shape should not have to
# change when a second real entry appears. See [_UNSUPPORTED_DOWNLOAD_SITES] for
# the download-path analogue, which is a DIFFERENT question with a different
# consequence: excluding comix from search costs it fan-out budget, while a
# comix DOWNLOAD cannot run at all.
_UNAVAILABLE_SEARCH_SITES = ("comix",)

# Mobile fan-out default, against aio-dl.py's argparse default of 6. Not a
# Resource Limits preset: those are an opt-in HARD OVERRIDE and this is the
# baseline they override. grep _NETWORK_PRESETS for the preset table.
_MOBILE_SEARCH_PARALLELISM = 4


def build_search_argv(query: str, settings: Optional[Dict[str, Any]] = None) -> List[str]:
    """argv for one cross-site search. Pure function — unit-tested offline in
    tests/test_android_search.py, which is why the I/O lives in search()."""
    settings = settings or {}
    argv: List[str] = ["--search", str(query), "--search-json"]

    language = settings.get("language")
    if language and str(language).strip():
        argv += ["--search-language", str(language).strip()]

    # Resource Limits wins when active; otherwise the mobile baseline, unless
    # the user set an explicit value.
    parallelism = settings.get("searchParallelism")
    if parallelism is None:
        parallelism = _MOBILE_SEARCH_PARALLELISM
    parallelism = search_parallelism_for_level(parallelism, settings.get("networkLimit"))
    argv += ["--search-parallelism", str(parallelism)]

    for key, flag in (
        ("searchTimeout", "--search-timeout"),
        ("searchMinMatch", "--search-min-match"),
        ("multiSourceQualityMin", "--multi-source-quality-min"),
    ):
        value = settings.get(key)
        if value is not None and str(value).strip() != "":
            argv += [flag, str(value)]

    if _is_true(settings.get("seededOnly")):
        argv.append("--seeded-only")
    if _is_true(settings.get("multiSource")):
        argv.append("--multi-source")
    if settings.get("collapseSplits") is True:
        argv.append("--collapse-splits")
    # An old saved enableMlRating setting is ignored: the ML rating was removed
    # with the image-ranker rework (2026-10-21) and aio-dl.py no longer accepts
    # --enable-ml-rating, so emitting it would fail the whole search.

    disabled = settings.get("disabledSites")
    names: List[str] = []
    if isinstance(disabled, (list, tuple)):
        names = [str(s).strip().lower() for s in disabled if str(s).strip()]
    elif isinstance(disabled, str):
        names = [s.strip().lower() for s in disabled.split(",") if s.strip()]
    for site in _UNAVAILABLE_SEARCH_SITES:
        if site not in names:
            names.append(site)
    argv += ["--disable-sites", ",".join(names)]

    return argv


def build_search_argv_json(query: str, settings_json: str) -> str:
    """JSON-in / JSON-out wrapper. See run_download_json on why lists cannot
    cross the JNI boundary directly."""
    return json.dumps(build_search_argv(query, _require_settings_object(settings_json)))


def search(query: str, settings_json: Optional[str] = None) -> str:
    """Run one cross-site search; return the --search-json payload as a string.

    Same shape as list_chapters, and for the same reasons: the engine lock is
    try-acquired so a tap during a download fails fast with `engine_busy`
    instead of blocking the UI behind a 40-minute run, and stdout is captured
    because under Chaquopy stdout IS logcat — the JSON payload for a 40-source
    search would bury the run's own log.

    Error shapes (all distinguishable from a legitimate empty result, which is
    `{"candidates": []}`):
      {"error": "engine_busy"}      a download holds the engine
      {"error": "no_query"}         empty query, before touching the engine
      {"error": "no_search_payload"}  engine exited without writing the contract
      {"error": "search_failed", "detail": "..."}  the engine raised

    rapidfuzz is a hard requirement of this path and is vendored for exactly
    that reason — android/wheels/README.md. Without it the orchestrator raises
    RuntimeError, which surfaces here as search_failed.
    """
    import io
    from contextlib import redirect_stdout

    if not str(query or "").strip():
        return json.dumps({"error": "no_query"})
    if not _ENGINE_LOCK.acquire(blocking=False):
        return json.dumps({"error": ENGINE_BUSY})
    try:
        settings = {}
        if settings_json:
            try:
                settings = _require_settings_object(settings_json)
            except (TypeError, ValueError):
                settings = {}
        argv = build_search_argv(query, settings)
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                _run_engine(argv)
        except SystemExit:
            # aio_search_cli sys.exit()s on some no-result paths; the payload
            # check below decides whether anything usable was written.
            pass
        except Exception as exc:  # noqa: BLE001 - reported to the UI, not raised
            return json.dumps({"error": "search_failed", "detail": f"{type(exc).__name__}: {exc}"})
        # Same "last line that starts with {" rule as list_chapters and the
        # Electron side: the payload trails ordinary progress logging. Search
        # pretty-prints with indent=2, so the opening brace is its own line and
        # the scan has to keep going until the buffer's tail parses.
        text = buffer.getvalue()
        start = text.rfind("\n{")
        if start == -1 and text.lstrip().startswith("{"):
            start = text.index("{")
        if start != -1:
            payload = text[start:].strip()
            try:
                json.loads(payload)
                return payload
            except ValueError:
                pass
        return json.dumps({"error": "no_search_payload"})
    finally:
        _ENGINE_LOCK.release()


# ---------------------------------------------------------------------------
# Library
#
# The scan itself is library_state.scan_library — pure stdlib, already shared
# with the desktop, and it does the whole job (series meta, chapter numbers,
# cover discovery, the next_update range). Everything below is the four things
# it deliberately does NOT do, each with a reason it lives here rather than
# there:
#
#   1. Cover extraction out of a CBZ/EPUB. library_state does that only under
#      write_cache=True, which writes a `.cover.*` INTO the series folder — and
#      that folder is the one Komikku/Mihon read, so on a phone it stays exactly
#      as the download left it. library_cover redirects the write to the app
#      cache instead.
#   2. The per-series file listing, which would multiply the grid's payload by
#      the chapter count for data only the detail screen wants.
#   3. The update diff, which runs the engine and therefore needs the lock.
#   4. Deletion, which needs a containment guard that only means anything once
#      there is a configured library root to contain things.
# ---------------------------------------------------------------------------


def scan_library(root: Optional[str] = None) -> str:
    """The library as a JSON string.

    Delegates to library_state.scan_library, which is pure stdlib and already
    does the whole job (series meta, chapter numbers, cover discovery incl.
    extraction from CBZ/EPUB, next_update arg). Kotlin renders it; it does not
    reimplement it.

    NOTE `cover` is empty for a series whose only cover lives INSIDE a CBZ/EPUB
    — the scan is deliberately read-only (library_state.find_cover_path's MISC-3
    comment). [library_cover] is the lazy per-series resolver for those.
    """
    from aio_config import resolve_output_dir
    from library_state import scan_library as _scan, to_jsonable

    return json.dumps(to_jsonable(_scan(resolve_output_dir(root))))


def _cover_cache_dir() -> str:
    base = _CONFIGURED.get("cache_dir") or os.path.join(os.getcwd(), "cache")
    path = os.path.join(base, "covers")
    os.makedirs(path, exist_ok=True)
    return path


def library_cover(folder: str) -> str:
    """An on-disk image path to show as `folder`'s cover, or "" if there is none.

    Resolution order, cheapest first:
      1. Whatever library_state.find_cover_path finds WITHOUT writing — a
         `cover.jpg` at the series root (which --komikku always produces) or the
         first page of a raw `Chapter_N/` directory. Returned as is, no copy.
      2. The best-ranked image inside the first CBZ/EPUB, extracted into the app
         cache.

    A PDF-only series resolves to "" on purpose: there is nothing to unzip, and
    rendering page 1 is Android's job — see core/CoverStore.kt (PdfRenderer).

    Cache keying is (book path, mtime, size), so re-downloading a series yields
    a different filename and a stale bitmap can never be shown. Nothing prunes
    the cache: it lives under the app's cacheDir, which Android reclaims under
    storage pressure, and each entry is one page-sized image.
    """
    import hashlib
    import zipfile

    # Private, and the RIGHT thing to import: _cover_sort_key is the heuristic
    # deciding WHICH member of an archive is the cover, and reimplementing it
    # would let the phone and the desktop quietly disagree about which image
    # represents a series. grep _cover_sort_key.
    from library_state import (
        SUPPORTED_COVER_EXTS,
        _cover_sort_key,
        find_cover_path,
        list_saved_books,
    )

    if not folder or not os.path.isdir(folder):
        return ""

    existing = find_cover_path(folder)
    if existing:
        return existing

    for book in list_saved_books(folder):
        if os.path.splitext(book)[1].lower() not in {".cbz", ".epub"}:
            continue
        try:
            stat = os.stat(book)
        except OSError:
            continue
        digest = hashlib.sha1(
            f"{book}|{stat.st_mtime_ns}|{stat.st_size}".encode("utf-8", "replace")
        ).hexdigest()[:24]

        try:
            with zipfile.ZipFile(book) as archive:
                members = [
                    name
                    for name in archive.namelist()
                    if not name.endswith("/")
                    and os.path.splitext(name.lower())[1] in SUPPORTED_COVER_EXTS
                ]
                if not members:
                    continue
                member = sorted(members, key=_cover_sort_key)[0]
                ext = os.path.splitext(member)[1].lower() or ".jpg"
                out = os.path.join(_cover_cache_dir(), f"{digest}{ext}")
                if os.path.isfile(out) and os.path.getsize(out) > 0:
                    return out
                data = archive.read(member)
        except (OSError, zipfile.BadZipFile, KeyError):
            continue

        # Written to a sibling and renamed. A half-written file that a
        # concurrently-scrolling card decodes as a corrupt image is worse than
        # no cover at all, and os.replace is atomic on every filesystem Android
        # ships.
        tmp = f"{out}.part"
        try:
            with open(tmp, "wb") as handle:
                handle.write(data)
            os.replace(tmp, out)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            continue
        return out

    return ""


def series_files(folder: str) -> str:
    """The detail screen's file list for one series, as JSON.

    `{"files": [{name, path, size, ext}], "chapter_dirs": [{name, path, images,
    size}]}` — archives in library_state's own reading order (the combined file
    first, then per-chapter newest-first), plus the raw `Chapter_N/` directories
    a `--format none` series has instead of archives.

    Separate from [scan_library] because a 400-chapter komikku series has 400
    entries here and the grid wants none of them.
    """
    from library_state import RAW_IMAGE_DIR_RE, SUPPORTED_COVER_EXTS, list_saved_books

    files: List[Dict[str, Any]] = []
    for path in list_saved_books(folder):
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        files.append(
            {
                "name": os.path.basename(path),
                "path": path,
                "size": size,
                "ext": os.path.splitext(path)[1].lower().lstrip("."),
            }
        )

    chapter_dirs: List[Dict[str, Any]] = []

    def _describe(path: str) -> None:
        images = 0
        size = 0
        try:
            for name in os.listdir(path):
                child = os.path.join(path, name)
                if os.path.splitext(name.lower())[1] not in SUPPORTED_COVER_EXTS:
                    continue
                if not os.path.isfile(child):
                    continue
                images += 1
                try:
                    size += os.path.getsize(child)
                except OSError:
                    pass
        except OSError:
            return
        chapter_dirs.append(
            {"name": os.path.basename(path), "path": path, "images": images, "size": size}
        )

    try:
        names = sorted(os.listdir(folder))
    except OSError:
        names = []
    for name in names:
        path = os.path.join(folder, name)
        if not os.path.isdir(path):
            continue
        if RAW_IMAGE_DIR_RE.match(name):
            _describe(path)
        elif name == "images":
            # aio-dl.py nests raw pages one level deeper when a final file was
            # produced too. Same two shapes library_state.scan_downloaded_chapters
            # walks — keep them in step.
            try:
                children = sorted(os.listdir(path))
            except OSError:
                children = []
            for child in children:
                child_path = os.path.join(path, child)
                if os.path.isdir(child_path) and RAW_IMAGE_DIR_RE.match(child):
                    _describe(child_path)

    return json.dumps({"files": files, "chapter_dirs": chapter_dirs})


class _ListError(Exception):
    """A failed --list-chapters call inside check_series_updates: `code` is the
    payload's error (or "check_failed"), reported to Kotlin unchanged."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def check_series_updates(
    folder: str,
    collapse_splits: bool = False,
    peer_folders: Optional[List[str]] = None,
) -> str:
    """Which chapters exist on the site but not on this device, as JSON.

    Behavioural port of UI-source/electron/main.js:_checkSeriesUpdates — the
    same `--list-chapters` call, the same `--language` / `--site` forwarding out
    of the saved metadata, the same subtraction of `chapters_skipped_fragments`
    (fragment labels the download path merged away, which a consensus-free
    listing re-lists and which would otherwise read as new forever), and the
    same error vocabulary, so the two UIs can say the same things.

    `peer_folders` are OTHER folders holding the same series — the forks a site
    rename used to create (grep seriesIdentityKey; library_state.
    group_entries_by_series is what finds them). Everything the check reads off
    disk is unioned across `folder` + peers, because a series split over two
    folders is still one series: without this the 59-chapter half reports
    "5 new" while its 5-chapter fork reports "59 new" and neither is true.

    ONE DELIBERATE DIVERGENCE: the desktop offers two mutually exclusive modes —
    trust `chapters_downloaded` (its default) or scan filenames
    (`useFileBasedChapterCheck`). This UNIONS them. The recorded list is a
    superset of what is on disk in normal operation, so the union is identical
    to the desktop default in the normal case; where it differs is a series
    downloaded before that key existed, or one copied onto the device by hand,
    and there the desktop's default reports every chapter as new. A union can
    only ever suppress a false "new", never hide a real one.

    VOLUMES (native volume releases, MangaFire; series_ledger / item_kinds).
    The ledger keeps them in their own namespace, and a series tracks chapters,
    volumes or both (series_ledger.tracked_kinds, from the ledger plus volume
    files on disk). Each kind is diffed only against its own downloads, with
    the same spawn plan as the desktop (main.js, grep "Spawn plan"):
      chapters only -> the plain listing, exactly as before
      volumes only  -> one --download-volumes listing; newChapters stays EMPTY,
                       since a chapter listing diffed against a volume-only
                       ledger would offer every chapter of the series
      both          -> one --with-volumes listing when both kinds use the same
                       language, else one listing per kind
    The volume half is reported as newVolumes / ignoredVolumes / volumesTotal /
    volumesDownloaded (plus volumesError when only that listing failed), the
    desktop's keys. The Kotlin app has no volume UI yet (deferred) and reads
    only the chapter keys, so on the device a volume-only series simply shows
    no chapter updates. No volume download is ever queued from here.
    """
    import series_ledger
    from item_kinds import CHAPTER, VOLUME
    from library_state import (
        format_chapter_number,
        scan_downloaded_chapters,
        scan_downloaded_volumes,
        volume_file_base,
    )

    meta_path = os.path.join(folder, ".aio_series.json")
    if not os.path.isfile(meta_path):
        return json.dumps({"error": "no_metadata"})
    try:
        with open(meta_path, "r", encoding="utf-8") as handle:
            meta = json.load(handle)
        if not isinstance(meta, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError):
        return json.dumps({"error": "invalid_metadata"})

    url = str(meta.get("url") or "").strip()
    if not url:
        return json.dumps({"error": "no_url"})

    # The series' folders, `folder` first so a lone series (the overwhelmingly
    # common case) costs exactly what it used to. Loaded up front because the
    # kinds a series tracks decide what to list.
    members: List[Tuple[str, Dict[str, Any]]] = [(folder, meta)]
    for member in peer_folders or []:
        member_path = os.path.join(member, ".aio_series.json")
        try:
            with open(member_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            members.append((member, loaded if isinstance(loaded, dict) else {}))
        except (OSError, ValueError):
            # An unreadable peer contributes nothing rather than failing the
            # check — the primary's own answer is still worth reporting.
            members.append((member, {}))

    # Volume files on disk count as "tracks volumes" too: an older build that
    # rewrote the ledger drops the volumes block, and the files are then the
    # only record (series_ledger.tracked_kinds, volume_files_present).
    volume_disk: Set[str] = set()
    kinds: Set[str] = set()
    for member, member_meta in members:
        base = volume_file_base(member_meta.get("title") or meta.get("title"))
        on_disk = scan_downloaded_volumes(member, base=base)
        volume_disk.update(format_chapter_number(n) for n in on_disk)
        # An unreadable peer ({}) would read as a legacy chapter series and
        # turn chapter tracking on for a volume-only series; its volume files
        # still count. The primary always counts, as before.
        if member_meta or member == folder:
            kinds |= series_ledger.tracked_kinds(member_meta, volume_files_present=bool(on_disk))
        elif on_disk:
            kinds.add(VOLUME)
    want_chapters = CHAPTER in kinds
    want_volumes = VOLUME in kinds

    site = str(meta.get("site") or "").strip()
    chapter_lang = str(meta.get("language") or "").strip() or "en"
    volume_lang = series_ledger.kind_view(meta, VOLUME)["language"] or chapter_lang

    def run_list(flags: List[str], lang: str) -> Dict[str, Any]:
        """One --list-chapters call; raises _ListError with the payload's error."""
        extra = list(flags)
        if lang and lang != "en":
            extra.extend(["--language", lang])
        if site:
            extra.extend(["--site", site])
        # Volume numbers have no fragments, and the engine forces collapse off
        # in volume mode, so the flag rides only on a chapter listing.
        if collapse_splits and "--download-volumes" not in flags:
            extra.append("--collapse-splits")
        try:
            result = json.loads(list_chapters(url, extra))
        except (TypeError, ValueError) as exc:
            raise _ListError("check_failed", str(exc))
        if not isinstance(result, dict):
            raise _ListError("check_failed", "unexpected payload")
        if result.get("error"):
            raise _ListError(str(result["error"]), str(result.get("message") or ""))
        return result

    # `payload` carries the chapter listing and the series fields (status,
    # cover…); `volume_list` is the volume labels, or None when listing them
    # failed while the chapter half succeeded.
    volume_list: Optional[List[Any]] = None
    volumes_error: Optional[str] = None
    try:
        if not want_volumes:
            payload = run_list([], chapter_lang)
        elif not want_chapters:
            payload = run_list(["--download-volumes"], volume_lang)
            volume_list = payload.get("volumes") or []
        elif volume_lang == chapter_lang:
            payload = run_list(["--with-volumes"], chapter_lang)
            # --with-volumes reports volumes: null when listing them raised and
            # still returns the chapter half (aio-dl.py _series_listing_head).
            if isinstance(payload.get("volumes"), list):
                volume_list = payload["volumes"]
            else:
                volumes_error = "Couldn't list volumes"
        else:
            payload = run_list([], chapter_lang)
            try:
                volume_list = run_list(["--download-volumes"], volume_lang).get("volumes") or []
            except _ListError as exc:
                volumes_error = exc.message or exc.code
    except _ListError as exc:
        return json.dumps({"error": exc.code, "message": exc.message})
    if not want_chapters:
        payload = {**payload, "chapters": []}

    downloaded: Set[str] = set()
    skipped: Set[str] = set()
    crossed_out: Set[str] = set()
    for member, member_meta in members:
        downloaded.update(str(c) for c in (member_meta.get("chapters_downloaded") or []))
        for number in scan_downloaded_chapters(member):
            downloaded.add(format_chapter_number(number))
        skipped.update(str(c) for c in (member_meta.get("chapters_skipped_fragments") or []))
        crossed_out.update(str(c) for c in (member_meta.get("chapters_ignored") or []))
    # `crossed_out` (unioned above) is chapters the user crossed out in the
    # desktop Updates Center. Split off rather than subtracted like `skipped`:
    # the desktop renders them struck through so they can be undone, and this
    # side reports them under the same key so an Android build that grows the
    # affordance needs no engine change. Until it does, the effect here is
    # simply that a chapter crossed out on the desktop is not offered for
    # download on the device either — which is the whole point of the two UIs
    # sharing one metadata file.
    # Cross-file: UI-source/electron/main.js:_checkSeriesUpdates does the same
    # split; grep chapters_ignored.

    relevant = [str(c) for c in (payload.get("chapters") or []) if str(c) not in skipped]
    absent = [c for c in relevant if c not in downloaded]
    missing = [c for c in absent if c not in crossed_out]
    ignored = [c for c in absent if c in crossed_out]
    for group in (missing, ignored):
        try:
            group.sort(key=float)
        except (TypeError, ValueError):
            group.sort()

    # Volume diff, in the volume namespace only (ledger ∪ files on disk).
    volume_fields: Dict[str, Any] = {}
    if want_volumes:
        have_volumes = set(volume_disk)
        crossed_volumes: Set[str] = set()
        for _member, member_meta in members:
            view = series_ledger.kind_view(member_meta, VOLUME)
            have_volumes.update(view["downloaded"])
            crossed_volumes.update(view["ignored"])
        site_volumes = [series_ledger.label_str(v) for v in (volume_list or [])]
        absent_volumes = [v for v in site_volumes if v not in have_volumes]
        new_volumes = sorted(
            (v for v in absent_volumes if v not in crossed_volumes), key=series_ledger.label_sort_key
        )
        ignored_volumes = sorted(
            (v for v in absent_volumes if v in crossed_volumes), key=series_ledger.label_sort_key
        )
        volume_fields = {
            "newVolumes": new_volumes,
            "ignoredVolumes": ignored_volumes,
            "volumesTotal": len(site_volumes),
            "volumesDownloaded": len(have_volumes),
        }
        if volumes_error:
            volume_fields["volumesError"] = volumes_error

    return json.dumps(
        {
            "ok": True,
            "folder": folder,
            "trackedKinds": [k for k in (CHAPTER, VOLUME) if k in kinds],
            **volume_fields,
            "newChapters": missing,
            "ignoredChapters": ignored,
            "range": chapters_to_range(missing),
            "total": len(relevant),
            "downloaded": len(downloaded),
            "status": payload.get("status") or meta.get("status"),
            "title": payload.get("title") or meta.get("title"),
            # Live values worth splicing back into the card without a rescan: a
            # series that finished publishing since the last download should not
            # go on claiming "Ongoing".
            "updatedMeta": {
                "status": payload.get("status"),
                "authors": payload.get("authors"),
                "cover": payload.get("cover"),
                "genres": payload.get("genres"),
            },
        }
    )


def chapters_to_range(chapters: List[Any]) -> str:
    """Collapse chapter labels into an aio-dl.py `--chapters` spec.

    Port of UI-source/src/lib/utils.js:chaptersToRangeString, with ONE narrowed
    rule. The reference joins any two labels within 1.001 of each other, so
    `[5, 6]` becomes `5-6` — and `is_chapter_wanted` reads that as a RANGE, so
    if the site also has 5.5 and the device already has it, the run re-fetches
    it. Here only consecutive WHOLE numbers join; anything else is listed
    literally. The update path is unaffected either way (a "what's new" delta is
    the newest contiguous tail, so both emit `51-53`); the difference shows up
    when filling a hole in the middle of a decimal-dense series, and on a
    metered phone connection re-downloading chapters the user already has is the
    wrong way to be wrong.

    Cost of the narrowing is a longer spec for decimal-dense sets — `5,5.5,6`
    where the desktop writes `5-6`. `is_chapter_wanted` splits on commas and
    strips, so length is the only difference.
    """
    # `c is not None` is not redundant with the strip test: str(None) is the
    # four-character string "None", which is truthy and would ride into the spec
    # as a literal chapter label.
    labels = [
        str(c).strip() for c in (chapters or []) if c is not None and str(c).strip()
    ]
    if not labels:
        return "all"

    def _sort_key(label: str) -> float:
        try:
            return float(label)
        except (TypeError, ValueError):
            return float("inf")

    labels.sort(key=_sort_key)
    parts: List[str] = []
    run: List[int] = []  # [start, end] of the current whole-number run

    def _flush() -> None:
        if not run:
            return
        parts.append(str(run[0]) if run[0] == run[1] else f"{run[0]}-{run[1]}")
        run.clear()

    for label in labels:
        whole: Optional[int] = None
        try:
            value = float(label)
            if value.is_integer():
                whole = int(value)
        except (TypeError, ValueError):
            whole = None

        if whole is not None and run and whole == run[1] + 1:
            run[1] = whole
            continue
        _flush()
        if whole is None:
            parts.append(label)
        else:
            run.extend((whole, whole))
    _flush()
    return ",".join(parts)


def delete_series(folder: str) -> str:
    """Recursively delete one series folder. `{"ok": true}` or `{"error": ...}`.

    Guarded on containment in the CONFIGURED library root rather than trusting
    the caller: this is the only destructive entry point in the module, it is
    reached from a single tap, and the path makes a full round trip through JSON
    and the JVM before it arrives. Refusing the root itself is deliberate —
    "delete this series" must never be able to mean "delete the library".
    """
    import shutil

    root = _CONFIGURED.get("output_dir")
    if not root:
        return json.dumps({"error": "not_configured"})
    try:
        real_root = os.path.realpath(root)
        real_folder = os.path.realpath(folder)
    except OSError as exc:
        return json.dumps({"error": "bad_path", "message": str(exc)})

    if real_folder == real_root:
        return json.dumps({"error": "refused_root"})
    try:
        contained = os.path.commonpath([real_root, real_folder]) == real_root
    except ValueError:
        # Different drives on Windows. Never happens on device; the desktop
        # test suite runs this file too.
        contained = False
    if not contained:
        return json.dumps({"error": "outside_library"})
    if not os.path.isdir(real_folder):
        return json.dumps({"error": "not_found"})

    try:
        shutil.rmtree(real_folder)
    except OSError as exc:
        return json.dumps({"error": "delete_failed", "message": str(exc)})
    return json.dumps({"ok": True})


# ---------------------------------------------------------------------------
# Library repair
#
# Two standalone aio-dl.py modes that REPAIR an existing library instead of
# downloading into it. Before these there was no in-place repair path of ANY
# kind on the device: a series enriched against the wrong AniList entry, or an
# archive the overwrite guard declined to rebuild, stayed that way forever.
#
# Both follow list_chapters' shape rather than run_download's — engine lock
# try-acquired (a repair that silently queued behind a 40-minute download would
# look like a dead button), stdout captured (under Chaquopy stdout IS logcat),
# a JSON verdict returned. Neither streams progress events: aio-dl.py's _emit
# sites are all in the download path, so there is nothing to stream, and the
# captured log is what a UI shows instead.
#
# NEITHER OPTS INTO INTERACTIVE CLOUDFLARE SOLVING, contrary to what a reading
# of "it reaches main(), so it opts in" would suggest. main()'s grant sits at
# the handler-resolution stage (grep allow_interactive_solving_for_this_run),
# and both of these modes sys.exit/return well before it — the refresh from its
# dispatch near the top of main(), build-final-file from the multi-URL runner.
# _run_engine's interactive_solving(False) barrier holds regardless.
# ---------------------------------------------------------------------------

#: Cap on the captured log a repair returns. A 130-series refresh writes one
#: line per series; this is roughly 500 of them, which is more than any UI will
#: scroll and small enough to cross JNI without thought. The TAIL is kept
#: because the summary line is at the end.
_REPAIR_LOG_MAX_CHARS = 64_000


def _tail(text: str, limit: int = _REPAIR_LOG_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return "…\n" + text[-limit:]


def refresh_library_metadata(
    folder_filter: str = "",
    rewrite_cbz: bool = False,
    force_refresh: bool = False,
    tag_min_rank: int = 50,
) -> str:
    """Re-pull AniList metadata for the library IN PLACE, without downloading.

    Runs `--refresh-library-metadata`, which for each series folder carrying a
    `.aio_series.json` re-runs the AniList match and rewrites `details.json` +
    `.aio_series.json` (and each chapter CBZ's ComicInfo.xml under
    `rewrite_cbz`). A series that does not match is left untouched.

    `folder_filter` restricts the sweep to series whose folder name or source
    URL contains it, case-insensitively — the repair-one-series path, and the
    documented cure for a poisoned AniList match (root CLAUDE.md, the
    cached-ID self-heal). Empty sweeps everything.
    `force_refresh` bypasses the cached anilist_id, for when AniList itself
    re-tagged the series.
    `tag_min_rank` is the AniList relevance floor for the tags that land in
    ComicInfo/details.json; 50 is aio-dl.py's own default.

    NOT GATED ON `--metadata-source`, which is worth knowing because it looks
    like it should be: `_refresh_library_metadata` imports and calls
    `enrich_from_anilist` directly and never reads that flag, so a repair works
    even on a device where enrichment is switched off for live downloads.
    Emitting `--metadata-source` here would therefore be inert, and it is not
    emitted — `configure(metadata_source=…)` and build_argv's flag are what
    govern LIVE downloads.

    Returns `{"ok": true, "matched": N, "skipped": N, "failed": N,
    "exitCode": N, "output": "<log tail>"}`, or one of:
      {"error": "engine_busy"}    a download holds the engine
      {"error": "not_configured"} configure() has not run
      {"error": "refresh_failed", "detail": "..."}  the engine raised

    `matched`/`skipped`/`failed` are parsed out of the mode's own summary line
    and are absent-as-zero when it printed none (an empty library, or a filter
    that matched nothing) — `output` is the authoritative record either way,
    which is why it is always returned.

    REWRITE_CBZ IS I/O-HEAVY: it repackages every chapter archive in every
    matched series. On a phone that is the difference between a few seconds and
    several minutes, and it rewrites files a reader app may have open.
    """
    import io
    import re
    from contextlib import redirect_stdout

    root = _CONFIGURED.get("output_dir")
    if not root:
        return json.dumps({"error": "not_configured"})
    if not _ENGINE_LOCK.acquire(blocking=False):
        return json.dumps({"error": ENGINE_BUSY})
    try:
        # -o EXPLICITLY, rather than letting resolve_output_dir fall back to
        # AIO_OUTPUT_DIR. configure() sets both to the same value, so in
        # production they agree — but then the guard above and the directory
        # actually swept would be reading two different sources, and any caller
        # that set one without the other would sweep the wrong library while
        # passing the check. One value, read once.
        argv: List[str] = ["--refresh-library-metadata", "-o", str(root)]
        if rewrite_cbz:
            argv.append("--refresh-rewrite-cbz")
        if force_refresh:
            argv.append("--metadata-refresh")
        rank = _num(tag_min_rank)
        if rank is not None and int(rank) != 50:
            argv.extend(["--metadata-tag-min-rank", str(int(rank))])

        term = str(folder_filter or "").strip()
        if term:
            # `--` first: the filter is free text the user typed, and one
            # starting with "-" would otherwise be parsed as an unknown option
            # and kill the run with an argparse error instead of matching
            # nothing. Verified against the real parser.
            argv.extend(["--", term])

        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                code = _run_engine(argv)
        except Exception as exc:  # noqa: BLE001 - reported to the UI, not raised
            return json.dumps(
                {
                    "error": "refresh_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "output": _tail(buffer.getvalue()),
                }
            )

        text = buffer.getvalue()
        counts = {"matched": 0, "skipped": 0, "failed": 0}
        summary = re.search(
            r"Refresh complete: (\d+) updated, (\d+) skipped, (\d+) failed", text
        )
        if summary:
            counts = {
                "matched": int(summary.group(1)),
                "skipped": int(summary.group(2)),
                "failed": int(summary.group(3)),
            }
        return json.dumps(
            {"ok": True, "exitCode": code, "output": _tail(text), **counts}
        )
    finally:
        _ENGINE_LOCK.release()


def build_final_file(folder: str) -> str:
    """Recombine the per-chapter PDFs already in `folder` into one series PDF.

    THIS IS A PDF-ONLY MODE, and saying so is the point of this docstring.
    `--build-final-file` globs `*.pdf` in the folder, groups the matches by the
    `<Series Title> Ch <n>.pdf` name shape, and merges each group — grep
    build_final_pdf_from_chapter_folder in aio-dl.py. There is no CBZ or EPUB
    branch, so a CBZ library gets `{"ok": true, "built": 0}` and a log line
    saying no per-chapter PDFs were found. DO NOT present this as the recovery
    for a skipped CBZ/EPUB rebuild; aio-dl.py's own guard prints the two
    remedies separately for exactly this reason (grep _final_file_would_shrink
    and read the two print() calls under the "partial_coverage" branch — the
    `--build-final-file` hint is inside `if args.format == "pdf"`).

    WHAT IT IS THE RECOVERY FOR: the archive-overwrite guard now KEEPS an
    existing combined file when a run would shrink it, and emits
    `final_file_skipped`. For a PDF series whose per-chapter files are still on
    disk (`--keep-chapters`), this is the "recombine it now" button. For cbz and
    epub the honest instruction is to re-run the URL with the full chapter
    range, and a UI must say that instead of offering this.

    Returns `{"ok": true, "built": N, "files": [...], "exitCode": N,
    "output": "..."}` or an error object (`engine_busy`, `not_configured`,
    `outside_library`, `not_found`, `build_failed`).

    Containment-guarded like [delete_series]: the mode WRITES `<prefix>.pdf`
    into the folder it is given, and that path has crossed JSON and JNI to get
    here. The engine lock is held for the same reason [write_book_metadata]
    holds it — a download may be writing archives into this very folder.
    """
    import io
    import re
    from contextlib import redirect_stdout

    root = _CONFIGURED.get("output_dir")
    if not root:
        return json.dumps({"error": "not_configured"})
    try:
        real_root = os.path.realpath(root)
        real_folder = os.path.realpath(str(folder or ""))
    except OSError as exc:
        return json.dumps({"error": "bad_path", "message": str(exc)})
    if not _contained_in(real_root, real_folder):
        return json.dumps({"error": "outside_library"})
    if not os.path.isdir(real_folder):
        return json.dumps({"error": "not_found"})

    if not _ENGINE_LOCK.acquire(blocking=False):
        return json.dumps({"error": ENGINE_BUSY})
    try:
        # NOTHING may be added to this argv. _validate_build_final_cli scans
        # sys.argv and p.error()s on ANY option other than -v/-d, and
        # _run_engine sets sys.argv to exactly what is passed here. The `--`
        # is safe because that validator breaks at it, and it keeps a folder
        # path that begins with "-" from being read as an option.
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                code = _run_engine(["--build-final-file", "--", real_folder])
        except Exception as exc:  # noqa: BLE001 - reported to the UI, not raised
            return json.dumps(
                {
                    "error": "build_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "output": _tail(buffer.getvalue()),
                }
            )

        text = buffer.getvalue()
        # The mode prints one "PDF saved → <name>" per file it built and
        # returns no count, so the log IS the result. A per-folder failure is
        # caught and printed by main() rather than raised, which is why an
        # exitCode of 0 does not by itself mean anything was built.
        files = re.findall(r"^PDF saved → (.+)$", text, flags=re.MULTILINE)
        return json.dumps(
            {
                "ok": True,
                "built": len(files),
                "files": [name.strip() for name in files],
                "exitCode": code,
                "output": _tail(text),
            }
        )
    finally:
        _ENGINE_LOCK.release()


# ---------------------------------------------------------------------------
# Resume
#
# Behavioural port of UI-source/electron/downloader.js:scanResumable + resume().
# Both sides look for the same thing: a `tmp_<hid>/` directory holding a
# `run_params.json`, which is aio-dl.py's own record that a run got far enough
# to be continuable.
#
# WHERE THEY LIVE: aio-dl.py resolves `tmp_<hid>` against the PROCESS CWD (grep
# main_tmp_dir), and configure() chdir's to work_dir — so work_dir is the whole
# search space, and nothing else has to be threaded through.
#
# WHAT RESUME ACTUALLY IS: `--restore-parameters` re-reads every persisted dest
# out of run_params.json, so the resume CLI carries almost nothing. The two
# exceptions are the point of the feature:
#
#   1. --format is DELIBERATELY not persisted (aio-dl.py:get_behavior_params),
#      so the output format can be changed on resume — PDF to CBZ without
#      re-downloading a page. It therefore must ALWAYS be emitted: argparse
#      defaults it to "epub", so omitting it would silently convert a CBZ run.
#   2. The throttle flags come from the CURRENT settings, never the saved ones —
#      a resume may well be running on a worse connection or a hotter device
#      than the run it continues. See resume_throttle_flags.
# ---------------------------------------------------------------------------

# aio-dl.py drops one of these markers in each `ch_*` directory it finishes.
# Two names because the marker depends on the mode: normal runs process images
# (`.processed_complete`), `--no-processing` runs only download them.
_CHAPTER_DONE_MARKERS = (".processed_complete", ".download_complete")


def _read_json_file(path: str) -> Dict[str, Any]:
    """A JSON object from `path`, or {} for anything unreadable. Every caller
    here treats a missing or corrupt sidecar as "no information", never as an
    error — a tmp folder is still resumable when its metadata is unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _describe_tmp_dir(tmp_dir: str) -> Dict[str, Any]:
    """Cached-chapter count and on-disk size for one `tmp_<hid>/`.

    ONE walk for both numbers. The desktop only counts chapters, but it also
    never offers to delete these folders from a device where storage is the
    scarce resource — a Delete button that cannot say what it reclaims is not
    worth tapping.
    """
    cached = 0
    size = 0
    try:
        # `with`, because scandir holds a directory handle — and on Windows an
        # unclosed one blocks the rmtree that Discard is about to attempt on
        # this very folder.
        with os.scandir(tmp_dir) as entries:
            children = list(entries)
    except OSError:
        return {"cachedChapters": cached, "sizeBytes": size}

    for entry in children:
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        if not is_dir:
            try:
                size += entry.stat().st_size
            except OSError:
                pass
            continue
        if entry.name.startswith("ch_") and any(
            os.path.exists(os.path.join(entry.path, marker))
            for marker in _CHAPTER_DONE_MARKERS
        ):
            cached += 1
        for root, _dirs, files in os.walk(entry.path):
            for name in files:
                try:
                    size += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    return {"cachedChapters": cached, "sizeBytes": size}


def scan_resumable() -> str:
    """Unfinished downloads found under the working directory, as JSON.

    `{"root": <work dir>, "items": [{hid, tmpDir, folderName, url, title,
    format, cachedChapters, sizeBytes, modifiedAt, quality, scaling,
    language}]}`, newest first.

    NOTE the currently-RUNNING download's tmp folder is in here too — it only
    stops being "unfinished" when the run completes and cleans up. Filtering it
    out needs to know what is running, which is Kotlin's business
    (DownloadRepository), so ResumeRepository matches on `url` rather than this
    function pretending to know.
    """
    root = _CONFIGURED.get("work_dir") or os.getcwd()
    items: List[Dict[str, Any]] = []
    try:
        with os.scandir(root) as handle:
            entries = sorted(handle, key=lambda e: e.name)
    except OSError:
        return json.dumps({"root": root, "items": []})

    for entry in entries:
        if not entry.name.startswith("tmp_") or not entry.is_dir():
            continue
        params_path = os.path.join(entry.path, "run_params.json")
        if not os.path.isfile(params_path):
            # No params, nothing for --restore-parameters to read: the run died
            # before it recorded anything, and "resuming" it would just be a
            # fresh download with extra steps.
            continue

        saved = _read_json_file(params_path)
        # Schema has two shapes: the current {"gating_hash":…, "params": {…}}
        # and the pre-2026-05 flat dict. See aio-dl.py:get_resumable_params.
        params = saved.get("params") if isinstance(saved.get("params"), dict) else saved
        meta = _read_json_file(os.path.join(entry.path, "run_meta.json"))

        try:
            modified = int(os.path.getmtime(entry.path) * 1000)
        except OSError:
            modified = 0

        item: Dict[str, Any] = {
            "hid": entry.name[4:],
            "tmpDir": entry.path,
            "folderName": entry.name,
            # run_meta.json is the canonical record (aio-dl.py writes it on
            # EVERY run); params is the legacy fallback for older tmp folders.
            "url": str(meta.get("url") or params.get("url") or ""),
            "title": str(meta.get("title") or params.get("title") or ""),
            "format": str(meta.get("format") or params.get("format") or ""),
            "language": str(params.get("language") or ""),
            "quality": params.get("quality"),
            "scaling": params.get("scaling"),
            "modifiedAt": modified,
        }
        item.update(_describe_tmp_dir(entry.path))
        items.append(item)

    items.sort(key=lambda entry: entry.get("modifiedAt") or 0, reverse=True)
    return json.dumps({"root": root, "items": items})


def build_resume_argv(
    url: str,
    output_format: str,
    epub_layout: str = "",
    settings: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """argv that continues an existing `tmp_<hid>/`. Pure function — the I/O
    lives in scan_resumable, so this is unit-testable offline.

    Deliberately does NOT emit `--verbose`, where the desktop hardcodes it: that
    is there for its stdout progress parser, and Android reads structured
    `_emit` events instead (which are not gated on the flag — grep `_VERBOSE`).
    Resuming a 200-chapter series would otherwise bury the Logs screen.
    """
    argv: List[str] = ["--restore-parameters"]

    fmt = str(output_format or "").strip().lower()
    # Never omitted, even when unknown: argparse defaults --format to "epub",
    # so a missing flag silently converts a CBZ library to EPUB on resume.
    argv.extend(["--format", fmt if fmt in ("pdf", "epub", "cbz", "none") else "cbz"])
    if fmt == "epub":
        layout = str(epub_layout or "").strip().lower()
        if layout in ("page", "vertical"):
            argv.extend(["--epub-layout", layout])

    # Current throttle beats the persisted one, in both directions. These are
    # explicit CLI dests, so --restore-parameters keeps them over the saved
    # values (aio-dl.py, grep _user_set_dests), and none are resume-gating —
    # overriding them can never re-download a completed chapter.
    argv.extend(resume_throttle_flags(settings or {}))

    argv.append(str(url).strip())
    return argv


def build_resume_argv_json(
    url: str,
    output_format: str,
    epub_layout: str,
    settings_json: str,
) -> str:
    """JSON-out wrapper for Kotlin. See run_download_json on why a list cannot
    cross the JNI boundary directly."""
    return json.dumps(
        build_resume_argv(
            url,
            output_format,
            epub_layout,
            _require_settings_object(settings_json) if settings_json else {},
        )
    )


def delete_resumable(tmp_dir: str) -> str:
    """Discard one unfinished download's working folder.

    Same containment reasoning as delete_series, with one extra gate: the
    directory must be named `tmp_*`. The library guard protects the user's
    books; this one protects the working directory itself, which also holds the
    browser profiles and the vrf cache — a path that arrives here malformed must
    not be able to take those with it.
    """
    import shutil

    root = _CONFIGURED.get("work_dir")
    if not root:
        return json.dumps({"error": "not_configured"})
    try:
        real_root = os.path.realpath(root)
        real_dir = os.path.realpath(tmp_dir)
    except OSError as exc:
        return json.dumps({"error": "bad_path", "message": str(exc)})

    if real_dir == real_root:
        return json.dumps({"error": "refused_root"})
    # Containment is tested BEFORE the naming rule so a path that is both
    # outside and oddly named reports the more serious of the two.
    if os.path.dirname(real_dir) != real_root:
        return json.dumps({"error": "outside_work_dir"})
    if not os.path.basename(real_dir).startswith("tmp_"):
        return json.dumps({"error": "not_a_tmp_dir"})
    if not os.path.isdir(real_dir):
        return json.dumps({"error": "not_found"})

    try:
        shutil.rmtree(real_dir)
    except OSError as exc:
        return json.dumps({"error": "delete_failed", "message": str(exc)})
    return json.dumps({"ok": True})


# ---------------------------------------------------------------------------
# Embedded metadata editing
#
# Thin JSON wrapper over metadata_editor.py, which the desktop reaches through
# metadata_cli.py (a subprocess) and which we call in-process. That module owns
# every format detail — ComicInfo.xml for CBZ, the OPF for EPUB, the document
# info dict for PDF — and is shared, so the two apps write byte-compatible
# metadata by construction rather than by agreement.
#
# TWO GUARDS THAT THE DESKTOP DOES NOT NEED:
#
#   1. Containment in the library root. update_metadata REWRITES the archive
#      (temp file + move), and the path has crossed JSON and JNI to get here.
#   2. The engine lock. A download is actively writing archives into the
#      library; rewriting one underneath it is a corrupted file. Non-blocking,
#      reported as ENGINE_BUSY — the same vocabulary the update checks use.
# ---------------------------------------------------------------------------

#: What metadata_editor.update_metadata can actually route. Anything else would
#: be a silent no-op there (its router just falls through), which reads in the
#: UI as "Save did nothing".
_EDITABLE_BOOK_EXTS = (".cbz", ".epub", ".pdf")

#: The fields metadata_editor round-trips, in the order the editor shows them.
#: Kotlin sends exactly these keys; anything else is dropped here rather than
#: forwarded, so a typo cannot reach the XML writer as a bogus element.
_METADATA_FIELDS = ("title", "writers", "pencillers", "genres", "publisher", "synopsis")


#: Image extensions a picked cover may have. metadata_editor embeds the file
#: as-is for CBZ (`0000_cover<ext>`), renames it to cover.jpg for EPUB and
#: decodes it through PIL for PDF — so the gate here is "is this plausibly an
#: image", not "is this the one format the writer wants".
_COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def _cover_path_error(path: str) -> Optional[str]:
    """Why `path` may not be embedded as a cover, or None when it may be.

    CONTAINMENT IS WIDER THAN [_book_path_error]'s, and deliberately so. The
    book being edited must live in the library, but a picked cover CANNOT:
    Android hands the picker's result back as a `content://` URI that Python
    cannot open(), so the caller copies the bytes into an app cache file first
    and passes THAT path (android/README.md on why SAF is not usable directly).
    A library-only guard would therefore reject every cover the feature can
    actually produce. The accepted set is the two directories this app owns —
    the configured library root and the configured cache dir — which still
    keeps an arbitrary JNI-delivered string from being copied into the user's
    archives.

    Empty is not an error: it means "leave the existing cover alone", which is
    what every caller that only edits text fields passes.
    """
    raw = str(path or "").strip()
    if not raw:
        return None
    roots = [
        _CONFIGURED.get("output_dir") or "",
        _CONFIGURED.get("cache_dir") or "",
    ]
    roots = [r for r in roots if r]
    if not roots:
        return "not_configured"
    try:
        real_path = os.path.realpath(raw)
        real_roots = [os.path.realpath(r) for r in roots]
    except OSError:
        return "bad_cover_path"
    if os.path.splitext(real_path)[1].lower() not in _COVER_EXTS:
        return "unsupported_cover_format"
    if not any(_contained_in(root, real_path) for root in real_roots):
        return "cover_outside_app_dirs"
    if not os.path.isfile(real_path):
        return "cover_not_found"
    return None


def _book_path_error(path: str) -> Optional[str]:
    """Why `path` may not be edited, or None when it may be."""
    root = _CONFIGURED.get("output_dir")
    if not root:
        return "not_configured"
    try:
        real_root = os.path.realpath(root)
        real_path = os.path.realpath(path)
    except OSError:
        return "bad_path"
    if os.path.splitext(real_path)[1].lower() not in _EDITABLE_BOOK_EXTS:
        return "unsupported_format"
    # Containment before existence: a path outside the library reports THAT,
    # rather than the far less useful "not_found" it would also be true of.
    try:
        if os.path.commonpath([real_root, real_path]) != real_root:
            return "outside_library"
    except ValueError:
        # Different drives on Windows. Never on device; the desktop suite runs
        # this file too.
        return "outside_library"
    if not os.path.isfile(real_path):
        return "not_found"
    return None


def read_book_metadata(path: str) -> str:
    """Embedded metadata for one archive as JSON.

    `{"ok": true, "metadata": {title, writers, pencillers, genres, publisher,
    synopsis}}`. Every field is a STRING (metadata_editor joins multi-value
    fields on write), and missing ones come back empty rather than absent, so
    the editor can bind to a fixed form.

    Reading does not touch the engine lock: it opens the archive read-only, and
    making the editor refuse to even DISPLAY metadata during a download would be
    caution with no hazard behind it.
    """
    error = _book_path_error(path)
    if error:
        return json.dumps({"error": error})
    try:
        from metadata_editor import read_metadata
    except ImportError as exc:
        return json.dumps({"error": "unavailable", "message": str(exc)})

    raw = read_metadata(path) or {}
    return json.dumps(
        {
            "ok": True,
            "path": path,
            "metadata": {key: str(raw.get(key) or "") for key in _METADATA_FIELDS},
        }
    )


def write_book_metadata(paths_json: str, data_json: str, cover_path: str = "") -> str:
    """Write the same metadata — and optionally the same cover — into one or
    more archives.

    Takes a JSON ARRAY of paths rather than a single one so "apply to every
    chapter" is ONE call holding the engine lock ONCE. Per-file calls would let
    a download start between files and leave the series half-edited, which is
    the worst of the available outcomes.

    `cover_path` is an on-disk image to embed; "" (the default) leaves each
    archive's existing cover alone. It must be a real filesystem path — a
    picker's `content://` URI cannot be opened by Python, so the caller copies
    the bytes to a cache file first and passes that. Guarded by
    [_cover_path_error], whose containment set is wider than the book guard's
    for exactly that reason; read its docstring before narrowing it.

    A BAD COVER FAILS THE WHOLE CALL rather than being dropped, and that is the
    opposite of how a bad book path is treated below. The asymmetry is
    deliberate: skipping one unwritable archive out of 300 still leaves the
    user better off, but silently ignoring the cover they just picked and
    reporting `{"ok": true}` is a save that lies about what it did.

    Returns `{"ok": true, "written": N, "failed": [{path, error}]}`. A failure
    on one file never aborts the rest — with 300 chapter archives, stopping at
    the first bad one would leave the user worse off than skipping it.

    NOTE embedding a cover REPACKAGES every archive it is applied to, so
    "apply to all chapters" with a cover is far more expensive than the same
    call without one. That is a UI decision, not one this function can make.
    """
    try:
        paths = json.loads(paths_json)
    except (TypeError, ValueError) as exc:
        return json.dumps({"error": "bad_paths", "message": str(exc)})
    if not isinstance(paths, list) or not paths:
        return json.dumps({"error": "no_paths"})

    data = _require_settings_object(data_json)
    # Only known fields travel onward, and a key the caller omitted stays
    # omitted: metadata_editor treats a PRESENT key as "set this" and a present
    # empty one as "remove this element", so forwarding a default-empty field
    # would silently wipe metadata the editor never showed.
    payload = {key: data[key] for key in _METADATA_FIELDS if key in data}
    cover = str(cover_path or "").strip()
    # A cover-only edit is legitimate — the user changed the picture and
    # nothing else — so "no fields" is only an error when there is no cover
    # either. Before the cover argument existed this was an unconditional
    # refusal, and leaving it that way would have made the picker a no-op
    # whenever the text fields happened to be untouched.
    if not payload and not cover:
        return json.dumps({"error": "no_fields"})

    cover_error = _cover_path_error(cover)
    if cover_error:
        return json.dumps({"error": cover_error, "coverPath": cover})

    try:
        from metadata_editor import update_metadata
    except ImportError as exc:
        return json.dumps({"error": "unavailable", "message": str(exc)})

    if not _ENGINE_LOCK.acquire(blocking=False):
        return json.dumps({"error": ENGINE_BUSY})
    try:
        written = 0
        failed: List[Dict[str, str]] = []
        for raw_path in paths:
            path = str(raw_path)
            error = _book_path_error(path)
            if error:
                failed.append({"path": path, "error": error})
                continue
            try:
                # None, not "": metadata_editor tests the argument for
                # truthiness at every use site, so either works — but None is
                # what its own signature defaults to and what the absence of a
                # cover means.
                update_metadata(path, payload, cover or None)
                written += 1
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                failed.append({"path": path, "error": f"{type(exc).__name__}: {exc}"})
        return json.dumps(
            {"ok": True, "written": written, "failed": failed, "cover": bool(cover)}
        )
    finally:
        _ENGINE_LOCK.release()


# ---------------------------------------------------------------------------
# Library root selection
#
# The default root is app-scoped external storage, which needs no permission and
# which Android deletes with the app. A user who wants Komikku or Mihon to READ
# this library has to point it somewhere shared, and that needs
# MANAGE_EXTERNAL_STORAGE — see android/README.md for why SAF is not an option
# (it hands back content:// URIs, and Python cannot open() one).
#
# The permission and the folder picking are Kotlin's; what belongs here is the
# question Kotlin cannot answer without Python: "can the downloader actually
# USE this path?" A granted permission is not the same as a writable directory,
# and finding out at the first chapter write is finding out too late.
# ---------------------------------------------------------------------------


def probe_library_root(path: str) -> str:
    """Can the downloader read and write `path`? JSON verdict.

    `{"ok": true, "path": …, "freeBytes": N, "seriesCount": N}` on success.
    Proves writability by actually CREATING and removing a file rather than by
    consulting os.access, which reports the permission bits and not what the
    Android sandbox will do with them — under scoped storage those two answers
    routinely disagree.

    `seriesCount` is the reassurance half: pointed at an existing library it
    says how many series are already there, so the user can tell "the right
    folder" from "a plausible-looking empty one" before committing.
    """
    import shutil

    raw = str(path or "").strip()
    if not raw:
        return json.dumps({"error": "empty_path"})
    target = os.path.abspath(raw)

    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:
        return json.dumps({"error": "cannot_create", "message": str(exc)})
    if not os.path.isdir(target):
        return json.dumps({"error": "not_a_directory"})

    probe = os.path.join(target, ".aio_write_probe")
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.unlink(probe)
    except OSError as exc:
        return json.dumps({"error": "not_writable", "message": str(exc)})

    try:
        free = shutil.disk_usage(target).free
    except OSError:
        free = 0

    series = 0
    try:
        with os.scandir(target) as entries:
            for entry in entries:
                if entry.is_dir() and os.path.isfile(
                    os.path.join(entry.path, ".aio_series.json")
                ):
                    series += 1
    except OSError:
        pass

    return json.dumps(
        {"ok": True, "path": target, "freeBytes": free, "seriesCount": series}
    )
