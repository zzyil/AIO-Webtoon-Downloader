"""Pluggable browser automation backend.

What this module owns: the ONE narrow contract every browser-dependent code
path in this repo talks to, plus a per-profile registry that hands out backend
instances lazily.

Who reads from it:
  * sites/mangafire_vrf.py  — vrf signing (goto + evaluate only)
  * sites/playwright_utils.py — fetch_html_playwright (goto + content)
  * sites/crawlee_utils.py  — get_cf_session (solve_challenge + cookies)

Depends on: nothing at import time. Patchright/Playwright are imported lazily
inside PatchrightBackend._start, so a process that never touches a
browser-backed site never pays for the import (and a machine without a browser
never sees an ImportError from merely importing this module).

---------------------------------------------------------------------------
WHY THIS EXISTS

Three call sites each grew their own Patchright lifecycle, and all three want
the same five operations. That was tolerable while Patchright was the only
possible driver. It stopped being tolerable when Android entered the picture:
Playwright cannot run there, but Android's WebView *is* a full Chromium and can
serve every one of those five operations. So the driver became a policy
decision, which means it needs a seam.

The seam is deliberately TINY. It is not a browser-automation framework — it is
the smallest set of operations that the existing three consumers actually use.
Resist widening it; a wider contract is a contract the WebView backend has to
reimplement.

---------------------------------------------------------------------------
THE evaluate() CONTRACT — read this before writing a backend

`script` is a JavaScript **function expression**, not a statement list:

    "() => document.title"
    "async (specs) => { ... return out; }"

`arg`, when supplied, is passed as that function's single argument. A returned
promise **must be awaited by the backend** before the value comes back, and the
resolved value must be JSON-serializable.

This mirrors Playwright's `page.evaluate(expression, arg)` semantics exactly,
because that is what sites/mangafire_vrf.py's `_BOOTSTRAP_JS` (returns a
promise, takes no arg) and `_SIGN_BATCH_JS` (async, takes one arg) already
depend on. Playwright auto-awaits; **Android's WebView.evaluateJavascript does
NOT** — a WebView backend has to wrap the call itself, roughly:

    Promise.resolve((<script>)(<arg JSON>)).then(v => Bridge.resolve(id, JSON.stringify(v)))

Getting this wrong fails in the most confusing possible way: the bootstrap
returns `{}` (a stringified un-awaited Promise) and every signature comes back
null. Cross-file: grep _BOOTSTRAP_JS in sites/mangafire_vrf.py.
---------------------------------------------------------------------------
"""

from __future__ import annotations

import atexit
import os
import sys
import threading
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

# Sentinel distinguishing "no argument" from "the argument None" — evaluate(js,
# None) must be able to pass a literal null through to the page.
NOARG = object()


class BrowserUnavailable(RuntimeError):
    """No usable browser backend. Carries the concrete reason (missing binary,
    disabled by env, launch crash) so callers can surface something actionable
    instead of a generic failure — sites/mangafire_vrf.py already builds its
    user-facing message out of exactly this string."""


@runtime_checkable
class BrowserBackend(Protocol):
    """One browser session. Implementations are NOT required to be thread-safe;
    callers serialize their own access (sites/mangafire_vrf.py does this with a
    dedicated worker thread + queue — see its module header)."""

    # -- navigation / evaluation: all sites/mangafire_vrf.py and
    #    sites/playwright_utils.py need --------------------------------------
    def goto(
        self, url: str, *, wait_until: str = "domcontentloaded", timeout_ms: int = 45_000
    ) -> None: ...

    def evaluate(self, script: str, arg: Any = NOARG) -> Any:
        """Run a JS function expression in the page. See the module header for
        the exact contract — promises MUST be awaited."""
        ...

    def content(self) -> str:
        """Fully-rendered document HTML (post-hydration)."""
        ...

    def wait_for_selector(self, selector: str, *, timeout_ms: int = 10_000) -> bool:
        """True if the selector appeared. Never raises on timeout — callers
        treat a missing selector as "maybe it never renders" and continue."""
        ...

    # -- identity: sites/crawlee_utils.py needs these to build a requests
    #    session that the origin will accept ----------------------------------
    def user_agent(self) -> str: ...

    def cookies(self, url: str) -> List[Dict[str, str]]:
        """[{name, value, domain, path}, ...] for `url`'s origin."""
        ...

    # -- anti-bot ----------------------------------------------------------
    @property
    def supports_challenge_solving(self) -> bool:
        """False means "I can navigate, but I can't get past an interactive
        challenge" — the caller then falls back to its own solver rather than
        burning a user interaction on a backend that cannot finish the job.
        Patchright returns False (zendriver owns CF solving on desktop); a
        WebView backend returns True (it can just show the page to the user)."""
        ...

    def solve_challenge(
        self, url: str, *, timeout_s: float = 45.0, interactive: bool = True
    ) -> Dict[str, Any]:
        """Land on `url` past whatever interstitial guards it, then return
        {"cookies": [...], "user_agent": str}. Raises BrowserUnavailable when
        unsupported."""
        ...

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None: ...

    @property
    def unavailable_reason(self) -> Optional[str]:
        """Sticky verdict, or None while the backend still looks usable."""
        ...


# ---------------------------------------------------------------------------
# Registry
#
# Profiles exist because the consumers genuinely want separate browser state:
# mangafire keeps a persistent profile so its Cloudflare cookies survive across
# runs, while the CF solver wants a visible window and per-domain cookies. On
# Android every profile collapses onto the single WebView, which is fine — the
# WebView backend just ignores the name.
# ---------------------------------------------------------------------------

_FACTORY: Optional[Callable[[str], BrowserBackend]] = None
_INSTANCES: Dict[str, BrowserBackend] = {}
_LOCK = threading.RLock()


def set_backend_factory(factory: Optional[Callable[[str], BrowserBackend]]) -> None:
    """Install the process-wide backend factory, replacing any existing one and
    closing already-built instances.

    Android calls this exactly once from aio_android.py before any handler
    runs. Desktop never calls it — the default Patchright factory below is used
    when this is None.
    """
    global _FACTORY
    with _LOCK:
        _close_all_locked()
        _FACTORY = factory


def get_backend(profile: str = "default") -> Optional[BrowserBackend]:
    """Memoized per-profile backend, or None when no browser is usable here.

    Returning None rather than raising is deliberate: every caller already has
    a no-browser path (mangafire raises its own typed error, crawlee falls back
    to a plain request, playwright_utils raises ImportError), and those paths
    produce far better messages than a generic exception from here would.
    """
    with _LOCK:
        existing = _INSTANCES.get(profile)
        if existing is not None:
            return existing
        factory = _FACTORY or _default_factory
        try:
            backend = factory(profile)
        except Exception:
            return None
        if backend is None:
            return None
        _INSTANCES[profile] = backend
        return backend


def custom_backend(profile: str = "default") -> Optional[BrowserBackend]:
    """The EMBEDDER-INSTALLED backend, or None when running on the built-in
    desktop default.

    This is the function the three consumers actually call, and the reason
    desktop behavior cannot regress: each of them keeps its existing,
    hard-won Patchright/zendriver code path verbatim and only diverts when
    somebody has explicitly supplied a browser via set_backend_factory.

    Concretely, that preserves things a generic backend would quietly drop —
    sites/playwright_utils.py's `ignore_https_errors` and pinned UA, and
    sites/mangafire_vrf.py's signer session, which is verified byte-for-byte
    against tokens the live site emitted. Rewriting those to go through here
    would be a refactor with no desktop upside and real desktop risk.

    Returns None (never raises) when no factory is installed.
    """
    if _FACTORY is None:
        return None
    return get_backend(profile)


def available(profile: str = "default") -> bool:
    backend = get_backend(profile)
    return backend is not None and backend.unavailable_reason is None


def reset() -> None:
    """Close and forget every instance. For tests, and for the Android side
    when the hosting Activity goes away and its WebView dies with it."""
    with _LOCK:
        _close_all_locked()


def _close_all_locked() -> None:
    for backend in _INSTANCES.values():
        try:
            backend.close()
        except Exception:
            pass
    _INSTANCES.clear()


def _default_factory(profile: str) -> Optional[BrowserBackend]:
    return PatchrightBackend(profile)


# Best-effort teardown. The backends are memoized for the process lifetime, so
# without this a run that touched one leaves a Chromium child alive until the
# interpreter's own cleanup gets to it. No join, no error propagation — an
# at-exit hook that raises would turn a successful run into a noisy one.
atexit.register(reset)


# ---------------------------------------------------------------------------
# Desktop implementation
# ---------------------------------------------------------------------------


def _profile_dir(profile: str) -> str:
    """App-owned Chromium user-data dir, one per profile name.

    Persisted (not throwaway) so Cloudflare cookies survive across runs — a
    virgin profile per process is the most bot-like fingerprint available.
    Deliberately NOT the user's real Chrome profile: Chromium locks a user-data
    dir, so that would fail whenever Chrome is open.

    Layout matches sites/comix.py:_comix_profile_dir and
    sites/mangafire_vrf.py:_profile_dir, whose per-consumer env overrides still
    win — see PatchrightBackend.__init__.
    """
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        root = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        root = os.environ.get("XDG_CACHE_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache"
        )
    return os.path.join(root, "AIO-Webtoon-Downloader", f"{profile}-profile")


class PatchrightBackend:
    """BrowserBackend over Patchright (or vanilla Playwright as a fallback).

    This is the desktop default and is the code that used to live inline in
    sites/playwright_utils.py:28-33 and
    sites/mangafire_vrf.py:_SignerSession._start.

    NOT thread-safe, and cannot be made so: Playwright's sync API demands every
    call run on the thread that started it. Callers own the serialization.
    """

    # Per-profile env overrides, preserved so existing docs/tests/muscle memory
    # keep working. Cross-file: sites/mangafire_vrf.py documents
    # AIO_MANGAFIRE_PROFILE_DIR; tests/conftest.py sets AIO_MANGAFIRE_NO_SIGNER.
    _PROFILE_DIR_ENV = {
        "mangafire": "AIO_MANGAFIRE_PROFILE_DIR",
        "comix": "AIO_COMIX_PROFILE_DIR",
    }
    _DISABLE_ENV = {"mangafire": "AIO_MANGAFIRE_NO_SIGNER"}
    _HEADFUL_ENV = {"mangafire": "AIO_MANGAFIRE_HEADFUL"}

    def __init__(self, profile: str = "default") -> None:
        self._profile = profile
        self._pw = None
        self._context = None
        self._page = None
        self._unavailable: Optional[str] = None

    # ----------------------------- lifecycle -----------------------------

    def _mark_unavailable(self, reason: str) -> None:
        """Collapse to one short line: Playwright's launch error embeds a
        multi-line ASCII install banner, and this string ends up inside an
        exception message and in test output."""
        flat = " ".join(str(reason).split())
        self._unavailable = (flat[:197] + "…") if len(flat) > 200 else flat

    def _resolved_profile_dir(self) -> str:
        override = (
            os.environ.get(self._PROFILE_DIR_ENV.get(self._profile, ""), "") or ""
        ).strip()
        return os.path.abspath(override) if override else _profile_dir(self._profile)

    def _start(self) -> bool:
        # Sticky no-browser verdict. WHY: without it every caller retries the
        # whole Chromium launch — a 13-page chapter list on a machine with
        # patchright installed but no browser downloaded pays 13 failed
        # launches and prints the install banner 13 times.
        if self._unavailable:
            return False
        disable_env = self._DISABLE_ENV.get(self._profile)
        if disable_env and (os.environ.get(disable_env) or "").strip() == "1":
            self._mark_unavailable(f"disabled via {disable_env}")
            return False
        if self._page is not None:
            try:
                if not self._page.is_closed():
                    return True
            except Exception:
                pass
            self._cleanup()  # a dead page is not reusable; every later call throws

        try:
            from patchright.sync_api import sync_playwright  # type: ignore
        except ImportError:
            try:
                from playwright.sync_api import sync_playwright  # type: ignore
            except ImportError:
                self._mark_unavailable("patchright/playwright not installed")
                return False
        try:
            self._pw = sync_playwright().start()
        except Exception as exc:
            self._mark_unavailable(f"Playwright start failed: {exc}")
            return False
        try:
            profile_dir = self._resolved_profile_dir()
            os.makedirs(profile_dir, exist_ok=True)
            headful_env = self._HEADFUL_ENV.get(self._profile)
            headless = not (
                headful_env and (os.environ.get(headful_env) or "").strip() == "1"
            )
            self._context = self._pw.chromium.launch_persistent_context(
                profile_dir,
                headless=headless,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            pages = list(getattr(self._context, "pages", None) or [])
            self._page = pages[0] if pages else self._context.new_page()
        except Exception as exc:
            # Most common real cause: patchright installed but the browser
            # binary never downloaded (`patchright install chromium`).
            self._mark_unavailable(f"browser launch failed: {exc}")
            self._cleanup()
            return False
        return True

    def _require_page(self):
        if not self._start():
            raise BrowserUnavailable(self._unavailable or "browser unavailable")
        return self._page

    def _cleanup(self) -> None:
        for attr, closer in (("_context", "close"), ("_pw", "stop")):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            try:
                getattr(obj, closer)()
            except Exception:
                pass
            setattr(self, attr, None)
        self._page = None

    def close(self) -> None:
        self._cleanup()

    @property
    def unavailable_reason(self) -> Optional[str]:
        return self._unavailable

    # ----------------------------- operations -----------------------------

    def goto(
        self, url: str, *, wait_until: str = "domcontentloaded", timeout_ms: int = 45_000
    ) -> None:
        self._require_page().goto(url, wait_until=wait_until, timeout=timeout_ms)

    def evaluate(self, script: str, arg: Any = NOARG) -> Any:
        page = self._require_page()
        # Playwright awaits returned promises for us, so the module-header
        # contract holds here with no extra work. The WebView backend is where
        # that stops being free.
        if arg is NOARG:
            return page.evaluate(script)
        return page.evaluate(script, arg)

    def content(self) -> str:
        return self._require_page().content()

    def wait_for_selector(self, selector: str, *, timeout_ms: int = 10_000) -> bool:
        try:
            self._require_page().wait_for_selector(selector, timeout=timeout_ms)
            return True
        except BrowserUnavailable:
            raise
        except Exception:
            return False

    def user_agent(self) -> str:
        # navigator.userAgent is correct HERE because this backend never applies
        # a UA override. Do NOT copy this into a context that pins one: once an
        # override is set the page reports the pin straight back, so a wrong
        # value looks self-consistent forever. sites/comix.py hit exactly that
        # and now probes via CDP Browser.getVersion instead — grep
        # _probe_true_user_agent.
        try:
            return str(self._require_page().evaluate("() => navigator.userAgent") or "")
        except BrowserUnavailable:
            raise
        except Exception:
            return ""

    def cookies(self, url: str) -> List[Dict[str, str]]:
        self._require_page()
        try:
            raw = self._context.cookies(url)
        except Exception:
            return []
        return [
            {
                "name": c.get("name", ""),
                "value": c.get("value", ""),
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
            }
            for c in (raw or [])
        ]

    # ----------------------------- anti-bot -----------------------------

    @property
    def supports_challenge_solving(self) -> bool:
        # False ON PURPOSE. Patchright can navigate, but it has no interactive
        # Cloudflare solver — zendriver owns that on desktop and already works
        # (sites/crawlee_utils.py:_solve_cf_async). Claiming True here would
        # route CF solving away from the code that can actually finish it.
        return False

    def solve_challenge(
        self, url: str, *, timeout_s: float = 45.0, interactive: bool = True
    ) -> Dict[str, Any]:
        raise BrowserUnavailable(
            "PatchrightBackend cannot solve interactive challenges; "
            "sites/crawlee_utils.py falls back to zendriver on desktop"
        )
