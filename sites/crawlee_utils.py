"""Anti-bot fetch utilities for bot-protected sites.

Two strategies are provided:
1. impit (Chrome/Firefox TLS impersonation, handles zstd/brotli) — fast, no browser launch.
   Use for sites that are blocked by header/fingerprint checks but serve readable HTML.
2. zendriver (CDP-based Chrome with built-in CF challenge solver) — for Cloudflare-protected
   sites. Uses zendriver's cloudflare module to interact with and solve CF challenges.

All functions are synchronous and safe to call from multiprocessing subprocesses.
zendriver is async; fetch_html_zendriver wraps it synchronously via asyncio.run().
"""

from __future__ import annotations

from typing import List, Optional
from urllib.parse import urljoin

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
    if not ZENDRIVER_AVAILABLE:
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
            # Probe for a Patchright/Playwright Chromium up-front so
            # zendriver doesn't blow up with "could not find a valid
            # browser binary" on sandboxed boxes (Windows Sandbox /
            # WDAG, the Electron bundle's slim Python env). When the
            # probe returns None, the call still goes through with
            # browser_executable_path=None and zendriver's default
            # lookup runs unchanged — so installs with system Chrome
            # behave exactly as before.
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
        lower = text.lower()
        for phrase in _CF_CHALLENGE_PHRASES:
            if phrase in lower:
                return True
    # CF sometimes serves the interstitial with 200 (JS-redirect variant)
    if status_code == 200 and len(text) < 15_000:
        lower = text.lower()
        hits = sum(1 for phrase in _CF_CHALLENGE_PHRASES if phrase in lower)
        if hits >= 2:
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
    if not ZENDRIVER_AVAILABLE:
        return
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




