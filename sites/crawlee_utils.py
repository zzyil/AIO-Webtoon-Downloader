"""Anti-bot fetch utilities for bot-protected sites.

Three strategies are provided:
1. impit (Chrome/Firefox TLS impersonation, handles zstd/brotli) — fast, no browser launch.
   Use for sites that are blocked by header/fingerprint checks but serve readable HTML.
2. zendriver (CDP-based Chrome with built-in CF challenge solver) — for Cloudflare-protected
   sites. Uses zendriver's cloudflare module to interact with and solve CF challenges.
3. an embedder-supplied browser (sites/browser_backend.py) — Android installs its in-app
   WebView here, which shows the challenge to the person holding the phone. get_cf_session
   prefers it over zendriver when one is installed.

Because of (3), **zendriver is no longer the only CF solver**. Handlers must ask
`cf_solver_available()` before deciding they cannot rescue a challenge; gating on
`ZENDRIVER_AVAILABLE` makes the whole rescue vanish on any platform without it (grep
cf_solver_available for the four handlers this bit).

Strategies 2 and 3 put a challenge in front of a PERSON, so they are additionally
gated on `interactive_solve_allowed()` — an opt-in, dynamically-scoped permission
that only a foreground download grants. Strategy 1 is headless and always available.
Read the "TWO SEPARATE QUESTIONS" block further down before touching either gate.

All functions are synchronous and safe to call from multiprocessing subprocesses.
zendriver is async; fetch_html_zendriver wraps it synchronously via asyncio.run().
"""

from __future__ import annotations

from contextlib import contextmanager as _contextmanager
from contextvars import ContextVar as _ContextVar
from typing import TYPE_CHECKING, List, Optional
from urllib.parse import urljoin

if TYPE_CHECKING:  # `ContextVar[bool]` in an annotation, without the runtime import
    from contextvars import ContextVar

# ---------------------------------------------------------------------------
# impit — fast TLS/browser impersonation (part of crawlee's dependency set)
# ---------------------------------------------------------------------------

try:
    import impit as _impit
    IMPIT_AVAILABLE = True
except ImportError:
    IMPIT_AVAILABLE = False


def fetch_html_impit(
    url: str,
    browser: str = "chrome",
    headers: Optional[dict] = None,
    timeout: float = 20.0,
) -> str:
    """Fetch a URL using impit (Chrome/Firefox TLS fingerprint impersonation).

    Handles zstd, brotli, and gzip compression transparently.
    Much faster than Camoufox (no browser launch), but cannot execute JS.

    Args:
        url: Page URL to fetch.
        browser: Browser to impersonate ('chrome' or 'firefox').
        headers: Extra headers to send.
        timeout: Request timeout in seconds.

    Returns:
        Full page HTML string.

    Raises:
        RuntimeError: If impit is not installed or the request fails.
    """
    if not IMPIT_AVAILABLE:
        raise RuntimeError("impit is not installed (should be part of crawlee)")
    client = _impit.Client(browser=browser, follow_redirects=True, timeout=timeout)
    resp = client.get(url, headers=headers or {})
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# zendriver — CF cookie capture strategy
#
# Strategy: launch a real (non-headless) Chrome once per domain to solve the
# Cloudflare Managed Challenge and capture the resulting cookies
# (primarily `cf_clearance` + `__cf_bm`).  Those cookies are then injected
# into a plain requests.Session so that all subsequent page/image fetches
# run headlessly without re-launching a browser.
#
# Cookie cache: {domain -> {"cookies": [...], "user_agent": str, "ts": float}}
# Cookies are reused until they expire (cf_clearance lasts ~30 min).
# ---------------------------------------------------------------------------

try:
    import zendriver as _zd
    ZENDRIVER_AVAILABLE = True

    # -----------------------------------------------------------------------
    # Monkey-patch zendriver's Cookie.from_json to handle missing 'sameParty'.
    #
    # Chrome removed the deprecated 'sameParty' field from its CDP Cookie
    # response, but zendriver's auto-generated CDP bindings still require it
    # (using json["sameParty"] instead of json.get("sameParty")).  This causes
    # a KeyError crash on every cookie retrieval, hanging browser.cookies
    # and browser.stop().  We patch from_json to use .get() at import time.
    # -----------------------------------------------------------------------
    try:
        from zendriver.cdp.network import Cookie as _ZDCookie

        _orig_cookie_from_json = _ZDCookie.from_json.__func__  # unwrap classmethod

        @classmethod  # type: ignore[misc]
        def _patched_cookie_from_json(cls, json):
            json.setdefault("sameParty", False)
            return _orig_cookie_from_json(cls, json)

        _ZDCookie.from_json = _patched_cookie_from_json
    except Exception:
        pass  # If the patch fails zendriver may still work for non-cookie flows.

except ImportError:
    ZENDRIVER_AVAILABLE = False

import threading as _threading
import time as _time
from urllib.parse import urlparse as _urlparse

_cf_cookie_cache: dict = {}          # domain -> {cookies, user_agent, ts}
_cf_cookie_lock = _threading.Lock()
_CF_COOKIE_TTL = 25 * 60             # 25 minutes (cf_clearance lasts ~30 min)

# Memoized Patchright/Playwright Chromium path used as a zendriver fallback
# when no system Chrome is installed. Tri-state: None = not probed yet,
# "" = probed and nothing found, anything else = absolute path to the
# executable. See _find_patchright_chromium for full rationale.
_PATCHRIGHT_CHROMIUM_PATH: Optional[str] = None


def _find_patchright_chromium() -> Optional[str]:
    """Resolve the path to Patchright (or Playwright) bundled Chromium.

    Why: zendriver's default browser lookup walks the system PATH plus
    common Chrome install locations and raises "could not find a valid
    browser binary" when nothing is installed. That breaks every
    CF-protected site in environments without system Chrome — Windows
    Sandbox / WDAG, the Electron AppImage's slim bundled Python env,
    minimal CI runners. But Patchright already installs a full Chromium
    binary for its own automation, so we hand zendriver THAT path.

    Probes patchright first (the stealthier build), falls back to
    vanilla playwright. Memoized at module scope because sync_playwright
    startup is ~200 ms and CF retries can fire repeatedly across a
    single downloader run, so the cost needs to amortize. The empty-
    string sentinel marks "probed and failed" so subsequent calls don't
    re-pay the probe cost.

    Returns the absolute path string, or None when neither package is
    installed / their Chromium isn't on disk. None makes the caller fall
    through to zendriver's default lookup, which will raise the original
    "could not find a valid browser binary" error — that's still the
    right behavior because there's nothing left to try.

    Cross-file: called from get_cf_session and fetch_html_zendriver,
    threaded into _solve_cf_async / _fetch_html_zendriver_async via
    the browser_executable_path kwarg. Cooperates with the existing
    `_PATCHRIGHT_CHROMIUM_PATH` cache.
    """
    global _PATCHRIGHT_CHROMIUM_PATH
    if _PATCHRIGHT_CHROMIUM_PATH is not None:
        return _PATCHRIGHT_CHROMIUM_PATH or None  # "" → None for caller
    import os as _os
    from importlib import import_module
    for module_name in ("patchright", "playwright"):
        try:
            mod = import_module(f"{module_name}.sync_api")
        except ImportError:
            continue
        path: Optional[str] = None
        try:
            # sync_playwright().start() spawns ONLY the driver subprocess —
            # the chromium binary is launched lazily on chromium.launch(),
            # which we never call. So this is a string-lookup, not a
            # browser launch; cost is ~200ms of subprocess overhead.
            pw = mod.sync_playwright().start()
            try:
                path = pw.chromium.executable_path
            finally:
                pw.stop()
        except Exception:
            continue
        if path and _os.path.exists(path):
            _PATCHRIGHT_CHROMIUM_PATH = path
            try:
                import sys as _sys
                print(
                    f"[*] zendriver: using {module_name}-bundled Chromium "
                    f"({path}) for CF challenges",
                    file=_sys.stderr,
                )
            except Exception:
                pass
            return path
    _PATCHRIGHT_CHROMIUM_PATH = ""
    return None


async def _solve_cf_async(
    url: str,
    overall_timeout: float = 45.0,
    *,
    browser_executable_path: Optional[str] = None,
) -> dict:
    """Open a visible Chrome, solve CF challenge, return {cookies, user_agent}.

    The zendriver ``Cookie.from_json`` bug (``KeyError: 'sameParty'``) is
    fixed by the module-level monkey-patch above, so we can safely use
    ``browser.cookies.get_all()`` directly.

    ``browser_executable_path``: when set, zendriver launches THAT
    Chromium instead of its default system-Chrome lookup. Threaded in
    from get_cf_session via _find_patchright_chromium so the call works
    in environments where only Patchright's bundled Chromium exists
    (Windows Sandbox, the Electron AppImage's slim Python env). None →
    use zendriver's default lookup (which raises "could not find a valid
    browser binary" if no system Chrome is installed).
    """
    from zendriver.core.cloudflare import cf_is_interactive_challenge_present, verify_cf
    import signal as _signal

    browser = await _zd.start(
        headless=False,
        browser_executable_path=browser_executable_path,
    )
    chrome_pid = None
    try:
        if hasattr(browser, '_process') and browser._process:
            chrome_pid = browser._process.pid
        elif hasattr(browser, 'process') and browser.process:
            chrome_pid = browser.process.pid
    except Exception:
        pass

    async def _inner():
        page = await browser.get(url)

        try:
            has_challenge = await _asyncio.wait_for(
                cf_is_interactive_challenge_present(page, timeout=10), timeout=15
            )
            if has_challenge:
                await _asyncio.wait_for(verify_cf(page, timeout=30), timeout=40)
        except (_asyncio.TimeoutError, Exception) as e:
            # Catch DOM resolution errors or timeout and proceed anyway
            pass

        # Wait briefly for any post-challenge redirects to settle
        await _asyncio.sleep(2)

        # Get User-Agent
        ua = "Mozilla/5.0"
        try:
            ua = await _asyncio.wait_for(page.evaluate("navigator.userAgent"), timeout=5)
        except Exception:
            pass

        # Get cookies — the monkey-patched Cookie.from_json handles missing sameParty
        cookies = []
        try:
            raw_cookies = await _asyncio.wait_for(browser.cookies.get_all(), timeout=10)
            cookies = [
                {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                for c in raw_cookies
            ]
        except Exception as e:
            print(f"[!] cookie retrieval warning: {e}")

        # Diagnostic: surface what zendriver actually captured so silent-
        # failure cases (Chrome opened but CF wasn't actually solved, so
        # cookies list is empty / missing cf_clearance) are visible. Without
        # this the cookie-injection-into-Patchright path in sites/comix.py
        # looks like it failed when really there was nothing to inject.
        cf_clearance_present = any(c.get("name") == "cf_clearance" for c in cookies)
        try:
            import sys as _sys
            print(
                f"[*] zendriver: captured {len(cookies)} cookie(s) from "
                f"{url} (cf_clearance: "
                f"{'present' if cf_clearance_present else 'MISSING'})",
                file=_sys.stderr,
            )
        except Exception:
            pass

        return {"cookies": cookies, "user_agent": ua}

    try:
        return await _asyncio.wait_for(_inner(), timeout=overall_timeout)
    finally:
        # Force-kill Chrome first (fast, reliable), then try graceful stop
        if chrome_pid:
            try:
                import os as _os
                _os.kill(chrome_pid, _signal.SIGKILL)
            except Exception:
                pass
        try:
            await _asyncio.wait_for(browser.stop(), timeout=5)
        except Exception:
            pass


def get_cf_session(base_url: str) -> "requests.Session":
    """Return a requests.Session pre-loaded with valid CF cookies for *base_url*.

    If cached cookies are still fresh they are reused; otherwise a visible
    Chrome window opens, solves the CF challenge, captures cookies, then closes.
    All subsequent requests through the returned session pass CF checks.

    Args:
        base_url: Any URL on the target domain (used to identify / solve CF).

    Returns:
        requests.Session with CF cookies and matching User-Agent set.

    Raises:
        RuntimeError: If zendriver is not available or the solve fails.
    """
    # An embedder-supplied browser that can solve challenges takes precedence
    # over zendriver. On Android that's the WebView, where the "interactive"
    # solve is genuinely better than on desktop: there's a real Chromium with a
    # real UA, and the user just taps the checkbox.
    # Cross-file: sites/browser_backend.py:custom_backend.
    from . import browser_backend as _bb

    _backend = _bb.custom_backend("cf")
    _use_backend = _backend is not None and _backend.supports_challenge_solving

    if not _use_backend and not ZENDRIVER_AVAILABLE:
        raise RuntimeError("zendriver is not installed. Run: pip install zendriver")

    import requests as _requests
    global _asyncio
    import asyncio as _asyncio

    domain = _urlparse(base_url).netloc

    with _cf_cookie_lock:
        cached = _cf_cookie_cache.get(domain)
        now = _time.time()
        if cached and now - cached["ts"] < _CF_COOKIE_TTL:
            cookies = cached["cookies"]
            user_agent = cached["user_agent"]
        else:
            # THE interactive gate, and it lives here rather than at the four
            # handler call sites for one reason: this is the only line in the
            # process that actually costs a human anything. Everything above it
            # — the cookie cache, and therefore every request a background
            # search makes against a host some earlier download already solved —
            # stays free and ungated, so the gate cannot cost a rescue that was
            # never going to interrupt anyone.
            #
            # Raised, not returned-empty: callers already treat a raising solve
            # as "keep the challenged body and warn" (madara / mangathemesia /
            # manhwaread) or "try the next rung" (weebcentral), so the blocked
            # path reuses proven handling instead of adding a second one.
            if not interactive_solve_allowed():
                warn_cf_rescue(
                    base_url,
                    "needs a human to solve it, and this is a background "
                    "operation (search / probe / update-check). Not opening a "
                    "browser; re-run this series as a download to solve it once.",
                    kind="background",
                )
                raise InteractiveSolveBlocked(
                    f"Cloudflare solve for {domain} needs an interactive browser, "
                    f"which is not permitted in this context"
                )
            # Probe for a Patchright/Playwright Chromium up-front so
            # zendriver doesn't blow up with "could not find a valid
            # browser binary" on sandboxed boxes (Windows Sandbox /
            # WDAG, the Electron bundle's slim Python env). When the
            # probe returns None, the call still goes through with
            # browser_executable_path=None and zendriver's default
            # lookup runs unchanged — so installs with system Chrome
            # behave exactly as before.
            if _use_backend:
                result = _backend.solve_challenge(base_url, timeout_s=45.0)
            else:
                browser_path = _find_patchright_chromium()
                result = _asyncio.run(
                    _solve_cf_async(base_url, browser_executable_path=browser_path)
                )
            cookies = result["cookies"]
            user_agent = result["user_agent"]
            _cf_cookie_cache[domain] = {"cookies": cookies, "user_agent": user_agent, "ts": now}

    session = _requests.Session()
    session.headers["User-Agent"] = user_agent
    for c in cookies:
        session.cookies.set(c["name"], c["value"], domain=c.get("domain", domain))
    return session


_CF_CHALLENGE_PHRASES = (
    "just a moment",
    "checking your browser",
    "enable javascript and cookies",
    "cf-browser-verification",
    "cloudflare ray id",
    "cf_chl_opt",
    "challenge-platform",
)

_CF_PLAIN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _count_cf_phrases(text: str) -> int:
    """How many of _CF_CHALLENGE_PHRASES appear in *text*.

    The one primitive both detectors below share, so the phrase list can never
    drift between the HTTP-body check and the rendered-DOM check. Bounds the
    scan: an interstitial is ~5 KB of mostly inline script and the markers are
    all near the top, so lowercasing a whole 2 MB reader page would be pure
    waste on the hot path.
    """
    if not text:
        return 0
    lower = text[:200_000].lower()
    return sum(1 for phrase in _CF_CHALLENGE_PHRASES if phrase in lower)


def looks_like_cf_interstitial(text: str, title: Optional[str] = None) -> bool:
    """True when *text* is Cloudflare's interstitial, judged WITHOUT a status code.

    For callers holding a RENDERED DOM rather than an HTTP body — i.e. a
    Playwright `page.content()`. Two things make is_cf_challenge wrong there:
    there is no status code to key on (a browser that followed the challenge
    reports nothing useful), and its 200-branch requires `len(text) < 15_000`,
    which a hydrated interstitial (Turnstile iframe, inline challenge script)
    can exceed. So this drops the length gate and raises the phrase bar to 2 to
    keep the false-positive rate where the length gate used to hold it.

    `title` is checked as well because "just a moment..." is the interstitial's
    <title> and survives even when the body markup is unrecognisable.

    Deliberately NOT merged with sites/comix.py:_looks_like_waf_challenge —
    comix runs Cloudflare AND its own first-party CAPTCHA, only the latter needs
    a human, and conflating them would send a solvable CF challenge down the
    "ask the user" path. Cross-file: sites/mangafire_vrf.py:_challenged.
    """
    if title and "just a moment" in title.lower():
        return True
    return _count_cf_phrases(text) >= 2


def is_cf_challenge(status_code: int, text: str) -> bool:
    """Return True if the HTTP response looks like a Cloudflare challenge page.

    Checks both the status code and page content so it works regardless of
    whether CF returns 403, 503, or even 200 for the interstitial.

    Args:
        status_code: HTTP status code of the response.
        text: Response body text.

    Returns:
        True if a CF challenge / block is detected.
    """
    if status_code in (403, 429, 503):
        if _count_cf_phrases(text) >= 1:
            return True
    # CF sometimes serves the interstitial with 200 (JS-redirect variant)
    if status_code == 200 and len(text) < 15_000:
        if _count_cf_phrases(text) >= 2:
            return True
    return False


def fetch_html_with_cf_cookies(
    url: str,
    base_url: Optional[str] = None,
    extra_headers: Optional[dict] = None,
    timeout: float = 20.0,
) -> str:
    """Fetch *url*, automatically solving Cloudflare challenges only when needed.

    Strategy:
    1. Attempt a plain requests.get() with a realistic User-Agent.
    2. If the response looks like a CF challenge (or connection error), invoke
       get_cf_session() to solve it via a visible Chrome and retry with the
       resulting cookies.
    3. Subsequent calls for the same domain reuse cached CF cookies (TTL 25 min).

    Args:
        url: Page URL to fetch.
        base_url: Override the URL used to trigger the CF solve (defaults to url).
        extra_headers: Additional headers to send.
        timeout: requests timeout in seconds.

    Returns:
        Full page HTML as a string.

    Raises:
        RuntimeError: If the fetch fails even after CF solve.
    """
    import requests as _req

    headers = {"User-Agent": _CF_PLAIN_UA}
    if extra_headers:
        headers.update(extra_headers)

    # Step 1 — plain request (fast path, no browser)
    try:
        resp = _req.get(url, headers=headers, timeout=timeout)
        if not is_cf_challenge(resp.status_code, resp.text):
            resp.raise_for_status()
            return resp.text
    except _req.RequestException:
        pass  # fall through to CF solve

    # Step 2 — CF challenge detected, solve and retry
    session = get_cf_session(base_url or url)
    if extra_headers:
        session.headers.update(extra_headers)
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def sync_cf_cookies(scraper, url: str) -> None:
    """If we have cached CF cookies for url's domain, sync them and User-Agent to scraper."""
    # Gate on the CACHE, not on the zendriver import. The cache is written by
    # whoever solved the challenge, and since get_cf_session can now be served
    # by an embedder backend, zendriver is no longer the only writer. The old
    # `if not ZENDRIVER_AVAILABLE: return` made this a SILENT no-op on any
    # platform without zendriver — every solved challenge got thrown away,
    # cookies never reached the scraper, and the next request was challenged
    # again with no indication why. Android is exactly that platform.
    domain = _urlparse(url).netloc
    domain_no_www = domain[4:] if domain.startswith("www.") else domain

    with _cf_cookie_lock:
        cached = _cf_cookie_cache.get(domain) or _cf_cookie_cache.get(domain_no_www)
        if cached:
            # Sync user agent
            scraper.headers["User-Agent"] = cached["user_agent"]
            # Sync cookies
            for c in cached["cookies"]:
                scraper.cookies.set(
                    c["name"],
                    c["value"],
                    domain=c.get("domain", domain),
                    path=c.get("path", "/"),
                )


# ---------------------------------------------------------------------------
# CF rescue seam — the gate + the diversion + the log, shared by every handler
# that detects a Cloudflare interstitial in a response it already has.
#
# Consumers: sites/madara.py (_fetch_html, the chokepoint for 244 handlers),
# sites/manhwaread.py (image path), sites/mangathemesia.py (_fetch_html_guarded,
# 28 handlers), sites/weebcentral.py (its own fallback ladder).
# Offline coverage: tests/test_cf_rescue_seam.py.
#
# TWO SEPARATE QUESTIONS, and conflating them is what shipped a hole here once:
#   cf_solver_available()      — CAPABILITY. "Does this process own a solver?"
#   interactive_solve_allowed() — PERMISSION. "May I interrupt a human RIGHT NOW?"
# A rescue needs both. The first is a property of the install; the second is a
# property of the CALL CONTEXT, which is why it is dynamically scoped rather
# than passed as an argument (see the block below).
# ---------------------------------------------------------------------------

# Is a human sitting in front of this call, expecting it to do work?
#
# DEFAULT IS FALSE — interactive solving is OPT-IN, and main()'s foreground
# download is the only opt-in. That direction is deliberate and was chosen after
# the first attempt at this shipped protecting 1 of ~6 call sites: with opt-OUT,
# a path nobody remembered to mark pops a Chrome window mid-search or launches a
# ChallengeActivity on someone's phone, and the only way to find out is to see it
# happen. With opt-IN, a path nobody remembered to mark merely loses the browser
# tier — it still gets impit, still gets cached clearance, and says so on stderr.
# Degraded and visible beats surprising and interactive, so the default is the
# one that stays safe the next time a call site is missed.
#
# CONSEQUENCE WORTH KNOWING: `--list-chapters` and `--search` never opt in, so
# desktop library update-checks and the Android update sweep are non-interactive
# BY CONSTRUCTION — those are precisely the runs that must never demand a human.
#
# THREADING TRAP, designed around rather than worked around: a fresh thread does
# NOT inherit the spawning thread's context — it starts empty and reads the
# DEFAULT. So the permission cannot be granted around a pool submission and
# expected to reach the workers, and (the direction that matters here) a worker
# can never accidentally inherit a download's permission. The orchestrator still
# sets False explicitly at its probe entry point (search_orchestrator.py:
# _probe_one) because that body ALSO runs on the main thread in some paths, e.g.
# a --multi-source download that probes inside an already-opted-in main().
_INTERACTIVE_SOLVE: "ContextVar[bool]" = _ContextVar(
    "aio_cf_interactive_solve", default=False
)


class InteractiveSolveBlocked(RuntimeError):
    """A solve was needed, a solver existed, and the context forbade using it.

    Distinct from "no solver installed" on purpose: the caller's fallback is the
    same (keep the challenged body / try the next rung) but the OPERATOR's fix is
    opposite — install zendriver, versus re-run this as a foreground download.
    """


@_contextmanager
def interactive_solving(allowed: bool = True):
    """Scope permission to open a browser/WebView for a CF solve.

    Restores the previous value on exit, including on exception, so a download
    that raises cannot leave the permission set for whatever runs next in the
    same thread. Enter it INSIDE the thread whose calls it should govern.
    """
    token = _INTERACTIVE_SOLVE.set(bool(allowed))
    try:
        yield
    finally:
        _INTERACTIVE_SOLVE.reset(token)


def allow_interactive_solving_for_this_run() -> None:
    """Grant the permission for the REST OF THIS CONTEXT, with no way to undo it.

    For an entry point that owns its whole process and has no enclosing scope to
    hang a `finally` on — i.e. exactly one caller, aio-dl.py's main(), which runs
    to EOF and returns from many points. Everything else must use
    `interactive_solving()` so the grant is scoped.

    Safe despite being unscoped because the two hosts bound it themselves:
    desktop runs one download per PROCESS, and Android wraps every main() call
    in `interactive_solving(False)` (grep _cf_interactive_solving in
    aio_android.py), whose reset() discards whatever was set inside it.
    """
    _INTERACTIVE_SOLVE.set(True)


def interactive_solve_allowed() -> bool:
    """May this call block on a human solving a challenge? See the block above.

    Named for the question it answers, not for the mechanism. The old name at
    this check site (`cf_solver_available`) asked about the INSTALL and read as
    though it had already settled the permission question, which is a large part
    of why a background search reached an interactive solve.
    """
    return bool(_INTERACTIVE_SOLVE.get())


def cf_solver_available(*, embedder_only: bool = False) -> bool:
    """True when SOMETHING in this process can get a caller past a CF interstitial.

    CAPABILITY ONLY — it does not ask whether using that solver is appropriate
    here. Pair it with interactive_solve_allowed() at any site that would open a
    window; get_cf_session enforces that pairing itself, so the common path
    cannot forget.

    Gate every rescue on this, never on ZENDRIVER_AVAILABLE. get_cf_session has
    preferred an embedder-supplied backend over zendriver since the Android
    port, so the import flag stopped being the right question — and four
    handlers kept asking it, inside `except Exception: pass`, which meant the
    interstitial HTML was parsed as the page with nothing logged. The visible
    symptom was never Cloudflare: a challenge page yields no title and no
    chapters, so the run died on "No chapters selected."

    embedder_only=True asks the narrower "did an embedder install a backend via
    sites/browser_backend.set_backend_factory". sites/weebcentral.py needs the
    distinction to order its ladder: on Android that backend is the ONLY rescue
    (impit is not installed there), while on desktop impit is a fast headless
    fetch that must keep winning over a solver which opens a visible Chrome
    window in the user's face.
    """
    try:
        from . import browser_backend as _bb

        backend = _bb.custom_backend("cf")
        if backend is not None and backend.supports_challenge_solving:
            return True
    except Exception:
        pass
    if embedder_only:
        return False
    return ZENDRIVER_AVAILABLE


def rescue_cf_html(
    url: str,
    *,
    base_url: Optional[str] = None,
    scraper=None,
    timeout: float = 20.0,
) -> str:
    """Re-fetch *url* through whichever solver this process has, and hand the
    clearance to *scraper*.

    The sync_cf_cookies half is not optional bookkeeping: the handler's own
    later requests (chapter pages, image URLs) go through `scraper`, so without
    it every one of them is challenged again and only the single rescued page
    ever succeeds.

    Raises whatever the solve raises. Callers decide what a failed rescue
    means — for madara/manhwaread/mangathemesia it means "keep the body we
    already had and warn", for weebcentral it means "try the next rung".

    THREE RESCUE TIERS, and only the last two can cost a human anything:

      1. impit — headless TLS impersonation, no window, no wait. Always tried
         first and NEVER gated, which is what keeps the interactive gate from
         being a capability regression: a background search on desktop still
         rescues itself here, silently, exactly as it did before the gate.
      2. an embedder browser (Android) — shows a ChallengeActivity.
      3. zendriver (desktop) — opens a visible Chrome window.

    Tiers 2 and 3 both run through get_cf_session, which is where the gate sits,
    so this function does not re-check permission — one check, one place.
    """
    # Tier 1. A challenged body coming back from impit is not a rescue, so it
    # falls through rather than being returned as the page (the exact mistake
    # this seam exists to stop). Status 200 is the right argument here: impit
    # raises on transport failure, so anything returned WAS a 200 body.
    if IMPIT_AVAILABLE:
        try:
            html = fetch_html_impit(url, browser="chrome", timeout=timeout)
            if html and not is_cf_challenge(200, html):
                return html
        except Exception:
            pass

    # Tiers 2-3.
    html = fetch_html_with_cf_cookies(url, base_url=base_url, timeout=timeout)
    if scraper is not None:
        sync_cf_cookies(scraper, url)
    return html


# One line per (host, kind) per process. These are standing conditions, not
# per-request events: a fully-blocked site would otherwise print the same line
# once per chapter for a 200-chapter run.
_cf_warn_seen: set = set()


def warn_cf_rescue(url: str, reason: str, *, kind: str = "failed") -> None:
    """Report a Cloudflare rescue that did not happen, on **stderr**.

    stderr, not stdout, because aio_search_cli.py writes its JSON to stdout and
    a network-noise line there corrupts the whole `--search-json` payload (same
    reason sites/hardening.py moved its cooldown lines; grep sys.stderr there).

    `kind` is the dedup key, so a host that has no solver at all says so once
    rather than once per chapter.
    """
    host = ""
    try:
        host = _urlparse(url).netloc
    except Exception:
        pass
    key = (host, kind)
    if key in _cf_warn_seen:
        return
    _cf_warn_seen.add(key)
    try:
        import sys as _sys

        print(
            f"[!] Cloudflare challenge on {host or url}: {reason}",
            file=_sys.stderr,
        )
    except Exception:
        pass


def warn_cf_no_solver(url: str) -> None:
    """The challenge was detected and nothing in this process can solve it.

    Split out from warn_cf_rescue only so the wording lives in one place — it
    is the message a user hits on a bare `pip install`-less environment, and it
    has to name the fix rather than the symptom.
    """
    warn_cf_rescue(
        url,
        "no solver available — install zendriver (pip install zendriver) or run "
        "inside the Android app, which solves challenges in its own WebView. "
        "Serving the challenge page as-is; expect a missing title and no chapters.",
        kind="no-solver",
    )


async def _fetch_html_zendriver_async(
    url: str,
    wait_selector: Optional[str] = None,
    overall_timeout: float = 60.0,
    *,
    browser_executable_path: Optional[str] = None,
) -> dict:
    from zendriver.core.cloudflare import cf_is_interactive_challenge_present, verify_cf
    import asyncio as _asyncio
    import signal as _signal

    browser = await _zd.start(
        headless=False,
        browser_executable_path=browser_executable_path,
    )
    chrome_pid = None
    try:
        if hasattr(browser, "_process") and browser._process:
            chrome_pid = browser._process.pid
        elif hasattr(browser, "process") and browser.process:
            chrome_pid = browser.process.pid
    except Exception:
        pass

    async def _inner():
        page = await browser.get(url)

        try:
            has_challenge = await _asyncio.wait_for(
                cf_is_interactive_challenge_present(page, timeout=10), timeout=15
            )
            if has_challenge:
                await _asyncio.wait_for(verify_cf(page, timeout=30), timeout=40)
        except Exception:
            pass

        await _asyncio.sleep(3)  # Wait for SPA load/redirects

        if wait_selector:
            try:
                for _ in range(20):
                    el = await page.query_selector(wait_selector)
                    if el:
                        break
                    await _asyncio.sleep(0.5)
            except Exception:
                pass

        # Extract fully rendered HTML
        html = await page.evaluate("document.documentElement.outerHTML")

        # Get User-Agent
        ua = "Mozilla/5.0"
        try:
            ua = await page.evaluate("navigator.userAgent")
        except Exception:
            pass

        # Get cookies
        cookies = []
        try:
            raw_cookies = await browser.cookies.get_all()
            cookies = [
                {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                for c in raw_cookies
            ]
        except Exception as e:
            print(f"[!] cookie retrieval warning: {e}")

        return {"html": html, "cookies": cookies, "user_agent": ua}

    try:
        return await _asyncio.wait_for(_inner(), timeout=overall_timeout)
    finally:
        if chrome_pid:
            try:
                import os as _os
                _os.kill(chrome_pid, _signal.SIGKILL)
            except Exception:
                pass
        try:
            await _asyncio.wait_for(browser.stop(), timeout=5)
        except Exception:
            pass


def fetch_html_zendriver(url: str, wait_selector: Optional[str] = None) -> str:
    """Fetch URL and return fully-rendered HTML using zendriver (handling Cloudflare & SPA)."""
    if not ZENDRIVER_AVAILABLE:
        raise RuntimeError("zendriver is not installed.")

    import asyncio as _asyncio

    domain = _urlparse(url).netloc
    # Same Patchright Chromium fallback as get_cf_session — see
    # _find_patchright_chromium for the why. None passthrough preserves
    # zendriver's default lookup on installs with system Chrome.
    browser_path = _find_patchright_chromium()
    result = _asyncio.run(
        _fetch_html_zendriver_async(
            url, wait_selector, browser_executable_path=browser_path
        )
    )

    # Cache cookies
    now = _time.time()
    with _cf_cookie_lock:
        _cf_cookie_cache[domain] = {
            "cookies": result["cookies"],
            "user_agent": result["user_agent"],
            "ts": now,
        }

    return result["html"]




