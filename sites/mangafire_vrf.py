from __future__ import annotations

import atexit
import builtins as _builtins
import concurrent.futures as _futures
import hashlib
import json
import os
import queue
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import browser_identity as _bid

# ---------------------------------------------------------------------------
# MangaFire `vrf` request signer.
#
# What this module owns: minting the `vrf=` query parameter that mangafire.to
# requires on every `/api/*` call since 2026-08. Without it the API answers
# `403 {"message":"Missing token."}`; with a token that doesn't match the
# request's params, `403 {"message":"Invalid token."}`.
#
# Who reads from it: sites/mangafire.py only (grep `sign_api_query`). Nothing
# else should need a vrf.
#
# Depends on: patchright (preferred) or playwright, lazily imported. A run that
# never touches mangafire never launches a browser — the worker thread and the
# Chromium process are both created on first sign, not at import.
#
# ---------------------------------------------------------------------------
# WHY A BROWSER, when the rest of the handler is a plain JSON client
#
# The token is NOT a hash we could reimplement. It is a stream cipher over the
# request path + serialized query (ciphertext length == plaintext length,
# exactly), and the implementing code ships as a *virtualized bytecode* blob:
# a custom binary deserializer, seed-derived 512-entry opcode permutation
# tables, a VM dispatch loop, plus DisableDevtool anti-analysis. The opcode
# tables re-seed per build, so a transcription would break on every deploy.
#
# So we don't reimplement it — we call it. The signer is an ordinary ES module
# that the site loads, and its export installs an axios REQUEST INTERCEPTOR.
# Handing that export a fake axios instance hands us the interceptor, and
# calling the interceptor with a config yields `config.params.vrf`. Verified
# byte-for-byte against tokens the real site emitted.
#
# WHAT MAKES THIS CHEAP (and why this is not the comix DOM-scrape pattern):
#   * Signing is an in-page function call, NOT a page load. One browser boot
#     per process, then every subsequent sign is a single page.evaluate; a
#     whole chapter list's worth signs in one round-trip via sign_api_queries.
#   * Tokens are session-independent — a token minted here replays fine from a
#     cold, cookieless curl_cffi session. So ONLY the signing needs a browser;
#     all real fetching (and every image download) stays on the fast path.
#   * Tokens carry no timestamp or nonce (the length equality above leaves no
#     room for one), so they never expire and are safely cacheable on disk.
# That is why mangafire keeps EXPENSIVE_PROBE=False and stays a normal search
# participant, instead of paying comix's ~30-90s per-series browser cost.
#
# THE SECOND THING THIS BROWSER NOW DOES: get past Cloudflare, and then serve
# the API reads too. mangafire.to went behind a Managed Challenge sitewide
# (including /api/*) around 2026-08-16, and no headless client can pass it, so
# the browser is the only client that can read this site at all while the
# challenge is up. `fetch_api` does that read same-origin from the cleared page
# — see its docstring for why the alternative (transplanting cf_clearance into
# cloudscraper) is the thing comix already proved does not work. When Cloudflare
# is off, sites/mangafire.py never calls it and the fast path is unchanged.
#
# Cross-file: the daemon-thread + queue bridge below mirrors
# sites/comix.py:_ComixBrowserBridge (grep `_comix_worker_loop`) — Patchright's
# sync API demands that every call run on the one thread that started it, and
# that thread own an asyncio loop, which probe-pool and image-prefetch threads
# satisfy for neither. Keep the two structurally similar.
# ---------------------------------------------------------------------------


def _stderr_print(*args, **kwargs):
    """Same stdout-protection rule as sites/mangafire.py: diagnostics must not
    land on stdout, which carries --search-json's payload."""
    kwargs.setdefault("file", sys.stderr)
    return _builtins.print(*args, **kwargs)


print = _stderr_print  # noqa: A001 — intentional shadow of builtins.print


_BASE_URL = "https://mangafire.to"

# Wall-clock cap on one bridge call. Bootstrap (browser launch + page load +
# module probe) is the slow one; steady-state signing is milliseconds.
_SIGN_TIMEOUT_S = 90.0
_BOOTSTRAP_TIMEOUT_S = 120.0

# ---------------------------------------------------------------------------
# Cloudflare. mangafire.to went behind a Managed Challenge sitewide — including
# every /api/* path — around 2026-08-16 (measured 2026-08-19: plain requests,
# impit chrome, curl_cffi chrome and cloudscraper ALL get the interstitial, so
# no headless tier this repo owns can reach the site).
#
# That is what produced the original bug report: _bootstrap navigated to the
# homepage, landed on the interstitial, ran its module scan against it and
# truthfully reported "no vrf signer among 0 module candidates" — blaming the
# signer for a network-level block. Nothing here probes the DOM until
# _ensure_cleared says the page is real.
_CF_AUTO_CLEAR_TIMEOUT_S = 20.0
_CF_SOLVE_TIMEOUT_ENV = "AIO_MANGAFIRE_CF_SOLVE_TIMEOUT"
_CF_DEFAULT_SOLVE_TIMEOUT_S = 180.0
# Set by tests so a regression into prompting fails instead of hanging; mirrors
# sites/comix.py's AIO_COMIX_NO_INTERACTIVE_WAF.
_NO_INTERACTIVE_CF_ENV = "AIO_MANGAFIRE_NO_INTERACTIVE_CF"
# Debug seam: forces the detector to fire ONCE (popped, not read, so one run
# tests one challenge). Mirrors AIO_COMIX_FORCE_WAF.
_FORCE_CF_ENV = "AIO_MANGAFIRE_FORCE_CF"

# Handoff budget, same shape and reasoning as comix's: a SUCCESS must not count
# against the allowance (one run can legitimately need more than one solve),
# while a decline must stop us nagging. Grep _may_prompt.
_CF_MAX_SOLVES = 3
_CF_MAX_FAILURES = 1
_CF_MIN_PROMPT_INTERVAL_S = 20.0
_CF_HANDOFF_LOCK = threading.Lock()
_CF_SOLVES_DONE = 0
_CF_FAILURES = 0
_CF_LAST_PROMPT_AT = 0.0

# How many times to try the channel launch before downgrading the identity.
#
# WHY THIS IS NOT 1: Chromium locks its user-data-dir, and this profile is shared
# by every process the app runs (the Electron app spawns searcher.js and
# downloader.js separately, both carrying PLAYWRIGHT_BROWSERS_PATH). When another
# process holds it, Chrome starts and immediately self-exits — Playwright surfaces
# that as TargetClosedError with `exitCode=21`, which looks nothing like "this
# build doesn't know the channel" but hit the same except branch. The first cut
# downgraded the whole process to channel-less headless on that one transient
# collision, permanently reintroducing the HeadlessChrome client-hint leak for the
# rest of the run. Observed live 2026-08-20; the same launch succeeded on every
# retry afterwards.
_CHANNEL_LAUNCH_ATTEMPTS = 3
_CHANNEL_RETRY_DELAY_S = 0.75

# Substrings that mean the CHANNEL itself is the problem, so retrying is pointless
# and downgrading is correct. Everything else is treated as transient.
_CHANNEL_UNSUPPORTED_MARKERS = (
    "unsupported channel",
    "unknown channel",
    "is not supported",
    "executable doesn't exist",
    "executable does not exist",
    "unexpected keyword argument",
)


def _channel_unsupported(exc: BaseException) -> bool:
    """True when the failure says this Playwright/Chromium can't do `channel=`.

    Deliberately a whitelist: an unrecognised error is treated as transient and
    retried, because the cost of retrying a real incompatibility is one extra
    launch attempt, while the cost of misreading a transient collision as an
    incompatibility is running the whole process with a detectable identity.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _CHANNEL_UNSUPPORTED_MARKERS)


# The reader's default viewport is fine for a headless scrape but hands a HUMAN
# a window whose content sits below the bottom of any real display — see the
# "Playwright viewport vs OS window" note: viewport= is device-metrics
# EMULATION and holds regardless of window size, so a window a person must use
# needs the EMULATED viewport shrunk to fit or it reads as blank + unscrollable.
_INTERACTIVE_VIEWPORT = {"width": 1280, "height": 720}


# Why the signer is unusable, if it is. Written by the worker thread when it
# reaches a sticky verdict, read by sign_api_query on the caller thread so the
# raised error names the actual cause (missing browser binary, disabled by env,
# …) instead of a generic "unavailable". Plain str assignment — no lock needed.
_LAST_UNAVAILABLE_REASON: Optional[str] = None


class MangaFireSigningError(RuntimeError):
    """Raised when a vrf could not be produced. Callers in sites/mangafire.py
    let this propagate on download paths (a chapter that can't be signed must
    fail loudly, not return an empty page list) and swallow it in search()."""


# ---------------------------------------------------------------------------
# The in-page bootstrap.
#
# Deliberately discovers the signer BEHAVIOURALLY rather than by filename. The
# chunk currently ships as `polyfill-<hash>.js` — which is a decoy name, it is
# not a polyfill — and both the hash and the name are build artifacts we must
# not hardcode. So: collect every module URL the page loaded, import each, and
# for every exported function ask "does handing you a fake axios instance get
# me an interceptor that produces a vrf?". First one that does, wins.
#
# Returns the serialized query too, not just the token: the cipher covers the
# serialized query string, so echoing back exactly what the signer serialized
# removes any chance of a Python/JS urlencode mismatch (`[` -> %5B, space -> +)
# desyncing us from what the server will decrypt and compare against.
# ---------------------------------------------------------------------------
_BOOTSTRAP_JS = r"""
() => {
  window.__aioMfSignReady = (async () => {
    const seen = new Set();
    const urls = [];
    const add = (u) => {
      if (!u || seen.has(u)) return;
      if (!/\.(js|mjs)(\?|$)/.test(u)) return;
      seen.add(u);
      urls.push(u);
    };
    document
      .querySelectorAll('script[type="module"][src], link[rel="modulepreload"][href]')
      .forEach((el) => add(el.src || el.href));
    try {
      performance.getEntriesByType('resource').forEach((e) => add(e.name));
    } catch (e) {}

    const makeFake = (sink) => ({
      interceptors: {
        request: { use: (f) => { sink.fn = f; } },
        response: { use: () => {} },
      },
      defaults: {},
      get() {}, post() {}, put() {}, delete() {},
    });

    for (const u of urls) {
      let mod;
      try { mod = await import(u); } catch (e) { continue; }
      let names;
      try { names = Object.keys(mod); } catch (e) { continue; }
      for (const key of names) {
        let exported;
        try { exported = mod[key]; } catch (e) { continue; }
        if (typeof exported !== 'function') continue;
        const sink = {};
        try { exported(makeFake(sink)); } catch (e) { continue; }
        if (typeof sink.fn !== 'function') continue;
        try {
          const probe = await sink.fn({ url: '/titles', method: 'get', params: {}, headers: {} });
          const vrf = probe && probe.params && probe.params.vrf;
          if (typeof vrf !== 'string' || vrf.length < 4) continue;
          window.__aioMfSign = async (path, pairs) => {
            const params = {};
            for (const [k, v] of pairs) params[k] = v;
            const out = await sink.fn({ url: path, method: 'get', params, headers: {} });
            const p = (out && out.params) || {};
            if (typeof p.vrf !== 'string' || !p.vrf) return null;
            return { vrf: p.vrf, query: new URLSearchParams(p).toString() };
          };
          return { moduleUrl: u, exportName: key };
        } catch (e) { /* not the signer; keep probing */ }
      }
    }
    throw new Error('no vrf signer among ' + urls.length + ' module candidates');
  })();
  return window.__aioMfSignReady;
}
"""

_SIGN_BATCH_JS = r"""
async (specs) => {
  await window.__aioMfSignReady;
  if (typeof window.__aioMfSign !== 'function') throw new Error('signer missing after bootstrap');
  const out = [];
  for (const spec of specs) out.push(await window.__aioMfSign(spec[0], spec[1]));
  return out;
}
"""


_FETCH_API_JS = r"""
async (url) => {
  const r = await fetch(url, {
    credentials: 'include',
    headers: { 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
  });
  const body = await r.text();
  return { status: r.status, body: body };
}
"""


def _may_prompt() -> Optional[str]:
    """None when a verification window may be opened right now, else the reason
    it may not. CLAIMS the slot as a side effect, so a None return means "you
    are prompting now" — don't call it speculatively."""
    global _CF_LAST_PROMPT_AT
    if (os.environ.get(_NO_INTERACTIVE_CF_ENV) or "").strip() not in ("", "0"):
        return "disabled"
    with _CF_HANDOFF_LOCK:
        if _CF_FAILURES >= _CF_MAX_FAILURES:
            return "already_declined"
        if _CF_SOLVES_DONE >= _CF_MAX_SOLVES:
            return "solve_limit"
        waited = time.monotonic() - _CF_LAST_PROMPT_AT
        if _CF_LAST_PROMPT_AT and waited < _CF_MIN_PROMPT_INTERVAL_S:
            return "too_soon"
        _CF_LAST_PROMPT_AT = time.monotonic()
    return None


def _record_prompt_outcome(solved: bool) -> None:
    """A success spends one solve and RESETS the failure count; a failure spends
    the (deliberately tiny) failure allowance, so someone who declines once is
    not asked again this run."""
    global _CF_SOLVES_DONE, _CF_FAILURES
    with _CF_HANDOFF_LOCK:
        if solved:
            _CF_SOLVES_DONE += 1
            _CF_FAILURES = 0
        else:
            _CF_FAILURES += 1


def _profile_dir() -> str:
    """App-owned Chromium user-data dir. Persisted (not throwaway) so Cloudflare
    cookies for mangafire.to survive across runs and processes — a virgin
    profile per process is the most bot-like fingerprint available.

    Deliberately NOT the user's real Chrome profile: Chromium locks a user-data
    dir, so that would fail whenever Chrome is open. Same reasoning and layout
    as sites/comix.py:_comix_profile_dir. Override with AIO_MANGAFIRE_PROFILE_DIR.
    """
    override = (os.environ.get("AIO_MANGAFIRE_PROFILE_DIR") or "").strip()
    if override:
        return os.path.abspath(override)
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        root = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        root = os.environ.get("XDG_CACHE_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache"
        )
    return os.path.join(root, "AIO-Webtoon-Downloader", "mangafire-profile")


def _cache_path() -> str:
    return os.path.join(_profile_dir(), "vrf-cache.json")


# Distinguishes "no argument" from "the argument None" in _SignerSession._eval.
# Same role as sites/browser_backend.NOARG; kept local so this module doesn't
# import the backend at module scope (a run that never touches mangafire must
# not pay for it).
_EVAL_NOARG = object()


def _cache_key(path: str, pairs: Sequence[Tuple[str, Any]]) -> str:
    """Cache identity for one signable request.

    Order-sensitive ON PURPOSE: the cipher covers the serialized query, so two
    different orderings of the same params are two different tokens. Callers in
    sites/mangafire.py build their param lists in a fixed order, so this is
    stable in practice.
    """
    body = path + "|" + "&".join(f"{k}={v}" for k, v in pairs)
    return hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:32]


class _SignerSession:
    """Patchright lifecycle owner. Every method here runs on the `mangafire-pw`
    daemon worker (see _worker_loop), never on a caller thread."""

    def __init__(self) -> None:
        self._pw = None
        self._context = None
        self._page = None
        # Launch mode of the live context. A mode switch (the verification
        # handoff) forces a teardown: Chromium fixes headless at launch time and
        # the profile dir can only be held by ONE context at a time.
        self._headless: Optional[bool] = None
        # Sticky per-process Cloudflare verdict, so a blocked run doesn't re-pay
        # the auto-clear poll on every later call. Deliberately NOT _unavailable:
        # that flag also disables the browser for the in-page API fetch, which is
        # the very thing that works once clearance exists.
        self._cf_blocked: Optional[str] = None
        # Was that verdict reached only because we weren't ALLOWED to prompt?
        # If so it is provisional — see _ensure_cleared's re-open rule.
        self._cf_blocked_background = False
        self._cf_cookie_ts = 0.0
        # Embedder-supplied browser (Android WebView). When set, every browser
        # touchpoint below routes here instead of through Patchright; the
        # Patchright fields above stay None and _cleanup skips them.
        self._backend = None
        # Identity of the bundle the live page bootstrapped against. Namespaces
        # the disk cache: a deploy that rotates the key or the cipher must not
        # serve stale tokens.
        self._namespace: Optional[str] = None
        self._signer_desc: str = ""
        # Sticky "there is no usable browser here" verdict, set once per
        # process. WHY: without it every _api_get retries the whole Chromium
        # launch — a 13-page chapter list on a machine with patchright
        # installed but no browser downloaded pays 13 failed launches and
        # prints the Patchright install banner 13 times. The verdict covers
        # import/launch failure only; a browser that dies mid-run still gets
        # relaunched (see _start's health check).
        self._unavailable: Optional[str] = None
        self._cache: Dict[str, Dict[str, str]] = {}
        self._cache_loaded = False
        self._cache_dirty = False

    # ----------------------------- cache -----------------------------

    def _load_cache(self) -> None:
        if self._cache_loaded:
            return
        self._cache_loaded = True
        try:
            with open(_cache_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._cache = {
                    k: v for k, v in data.items() if isinstance(v, dict)
                }
        except Exception:
            self._cache = {}

    def _flush_cache(self) -> None:
        if not self._cache_dirty:
            return
        try:
            os.makedirs(_profile_dir(), exist_ok=True)
            tmp = _cache_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._cache, fh)
            os.replace(tmp, _cache_path())
            self._cache_dirty = False
        except Exception:
            # A cache we can't persist is a performance loss, never a
            # correctness one — the browser can always re-mint.
            pass

    def _cached(self, key: str) -> Optional[Dict[str, str]]:
        if not self._namespace:
            return None
        self._load_cache()
        entry = self._cache.get(self._namespace) or {}
        raw = entry.get(key)
        if not raw:
            return None
        try:
            vrf, query = raw.split("\x1f", 1)
        except ValueError:
            return None
        return {"vrf": vrf, "query": query}

    def _remember(self, key: str, signed: Dict[str, str]) -> None:
        if not self._namespace:
            return
        self._load_cache()
        self._cache.setdefault(self._namespace, {})[key] = (
            f"{signed.get('vrf', '')}\x1f{signed.get('query', '')}"
        )
        self._cache_dirty = True

    def purge_namespace(self) -> None:
        """Drop every cached token for the current bundle and force the next
        call to re-bootstrap (nulling _namespace fails _bootstrap's fast path).
        Called when the server rejects a token we believed was good — i.e. the
        site redeployed mid-run and our namespace is stale."""
        # Load first: purging before anything has read the cache would leave the
        # stale entries sitting on disk to be served by the next process.
        self._load_cache()
        if self._namespace and self._namespace in self._cache:
            self._cache.pop(self._namespace, None)
            self._cache_dirty = True
            self._flush_cache()
        self._namespace = None

    # ----------------------------- browser -----------------------------

    def _mark_unavailable(self, reason: str) -> None:
        """Record the sticky no-browser verdict in both places at once: on the
        session (so _start fails fast) and in the module global (so the caller
        thread's exception can name the cause).

        Collapsed to one short line: Playwright's launch error embeds a
        multi-line ASCII banner, and this string ends up inside an exception
        message and in test output.
        """
        global _LAST_UNAVAILABLE_REASON
        flat = " ".join(str(reason).split())
        if len(flat) > 200:
            flat = flat[:197] + "…"
        self._unavailable = flat
        _LAST_UNAVAILABLE_REASON = flat

    def _start(self, headless: Optional[bool] = None, *, _ua_relaunch: bool = False) -> bool:
        """Bring up (or reuse) the persistent Patchright context.

        ``headless`` defaults to the AIO_MANGAFIRE_HEADFUL env knob. It is fixed
        at launch time by Chromium and the profile dir can only be held by one
        context, so switching modes for the verification handoff means a full
        teardown + relaunch — hence the mode comparison on the fast path.

        ``_ua_relaunch`` marks the single permitted self-relaunch after the
        probed UA turns out to differ from the pin, so termination is provable
        here rather than resting on cache state.
        """
        # Already decided there's no browser here — don't re-pay the launch.
        if self._unavailable:
            return False
        # Hard opt-out. Set by tests/conftest.py so a unit test can never
        # silently launch Chromium (and reach the network) just by calling a
        # handler method; also useful on a headless box with no browser, where
        # the launch attempt is pure latency. Checked inside _start rather than
        # in sign() on purpose: cached tokens must still serve with it set.
        if (os.environ.get("AIO_MANGAFIRE_NO_SIGNER") or "").strip() == "1":
            self._mark_unavailable("disabled via AIO_MANGAFIRE_NO_SIGNER")
            return False
        # Embedder backend, checked AFTER the kill-switch above so
        # AIO_MANGAFIRE_NO_SIGNER still wins everywhere (tests rely on that
        # precedence) and BEFORE the Patchright launch so Android never tries
        # to start a Chromium it doesn't have.
        if self._backend is None:
            from . import browser_backend as _bb

            self._backend = _bb.custom_backend("mangafire")
        if self._backend is not None:
            return True

        if headless is None:
            headless = (os.environ.get("AIO_MANGAFIRE_HEADFUL") or "").strip() != "1"
        if self._page is not None and self._headless != headless:
            self._cleanup()
        if self._page is not None:
            try:
                if not self._page.is_closed():
                    return True
            except Exception:
                pass
            # A dead page object is not reusable; every later sign would throw.
            self._cleanup()
        try:
            from patchright.sync_api import sync_playwright  # type: ignore
        except ImportError:
            try:
                from playwright.sync_api import sync_playwright  # type: ignore
            except ImportError:
                self._mark_unavailable("patchright/playwright not installed")
                print(
                    "[!] MangaFire: patchright/playwright not installed — cannot "
                    "sign API requests. Install with: pip install patchright && "
                    "patchright install chromium"
                )
                return False
        try:
            self._pw = sync_playwright().start()
        except Exception as e:
            self._mark_unavailable(f"Playwright start failed: {e}")
            print(f"[!] MangaFire: Playwright start failed: {e}")
            return False

        profile = _profile_dir()
        ctx_kwargs: Dict[str, Any] = {}
        cf_ua: Optional[str] = None
        try:
            os.makedirs(profile, exist_ok=True)

            def _launch(**extra):
                return self._pw.chromium.launch_persistent_context(
                    profile,
                    headless=headless,
                    args=["--no-sandbox", "--disable-setuid-sandbox"],
                    **extra,
                )

            # Pin the UA. A cf_clearance cookie is bound to the UA that earned
            # it, so a solve captured by the shared zendriver solver outranks
            # matching the local browser — same priority order as
            # sites/comix.py:_resolve_stable_user_agent.
            cf_ua = self._cached_cf_user_agent()
            pinned_ua = cf_ua or _bid.cached_stable_user_agent(profile)
            if pinned_ua:
                ctx_kwargs["user_agent"] = pinned_ua

            # channel= is what aligns headless's client hints with the headed
            # handoff window; the pin alone only moves the UA header and leaves
            # Sec-CH-UA saying HeadlessChrome. BOTH levers are required — see
            # the measured table in sites/browser_identity.py. Degrade rather
            # than die if this Patchright build rejects the channel.
            self._context = None
            for _attempt in range(_CHANNEL_LAUNCH_ATTEMPTS):
                try:
                    self._context = _launch(
                        channel=_bid.BROWSER_CHANNEL, **ctx_kwargs
                    )
                    break
                except Exception as channel_exc:
                    fatal = _channel_unsupported(channel_exc)
                    last = _attempt == _CHANNEL_LAUNCH_ATTEMPTS - 1
                    if not fatal and not last:
                        # Almost always the shared profile being held by another
                        # of the app's processes; it clears in well under a second.
                        time.sleep(_CHANNEL_RETRY_DELAY_S * (_attempt + 1))
                        continue
                    why = (
                        "this build doesn't support the channel"
                        if fatal
                        else f"still failing after {_CHANNEL_LAUNCH_ATTEMPTS} attempts "
                        f"(usually the browser profile being held by another "
                        f"AIO process)"
                    )
                    print(
                        f"[!] MangaFire: channel={_bid.BROWSER_CHANNEL!r} launch "
                        f"failed — {why}: {type(channel_exc).__name__}: "
                        f"{str(channel_exc).splitlines()[0]}"
                    )
                    print(
                        "[!] MangaFire: falling back to default headless, which "
                        "advertises HeadlessChrome in Sec-CH-UA. Downloads still "
                        "work while the saved Cloudflare clearance lasts; expect "
                        "the check to fire more often once it expires."
                    )
                    self._context = _launch(**ctx_kwargs)
                    break
            self._headless = headless
            existing = list(getattr(self._context, "pages", None) or [])
            self._page = existing[0] if existing else self._context.new_page()
        except Exception as e:
            # Most common real-world cause: patchright is installed but the
            # browser binary was never downloaded (`patchright install
            # chromium`). Sticky, so we say it once and fail fast after.
            self._mark_unavailable(f"browser launch failed: {e}")
            print(f"[!] MangaFire: Playwright launch failed: {e}")
            self._cleanup()
            return False

        # Reconcile the pin against what this browser REALLY is, via CDP — once
        # an override is applied navigator.userAgent only reads our own pin
        # back, so a stale cached UA would otherwise re-pin itself forever.
        true_ua = _bid.probe_true_user_agent(self._context, self._page)
        stable_ua = _bid.stabilize_user_agent(true_ua)
        if stable_ua:
            _bid.remember_stable_user_agent(profile, stable_ua)
            # A CF pin is exempt: that value is bound to a cf_clearance cookie
            # and deliberately outranks matching the local browser.
            if (
                ctx_kwargs.get("user_agent") != stable_ua
                and not cf_ua
                and not _ua_relaunch
            ):
                self._cleanup()
                return self._start(headless, _ua_relaunch=True)
        return True

    # ----------------------------- Cloudflare -----------------------------

    def _cached_cf_user_agent(self) -> Optional[str]:
        """The User-Agent from any cached CF solve for mangafire.to, or None.

        Using THAT exact UA in the Patchright context is what keeps an injected
        cf_clearance valid — Cloudflare treats a cookie/UA mismatch as a bot
        signal. Cache written by sites/crawlee_utils.py:_solve_cf_async via
        get_cf_session; key is the bare netloc.
        """
        try:
            from . import crawlee_utils as _cu

            with _cu._cf_cookie_lock:
                cached = _cu._cf_cookie_cache.get("mangafire.to")
            if cached:
                return cached.get("user_agent") or None
        except Exception:
            pass
        return None

    def _page_html(self) -> str:
        try:
            if self._backend is not None:
                return self._backend.content() or ""
            return self._page.content() or ""
        except Exception:
            return ""

    def _page_title(self) -> str:
        try:
            if self._backend is not None:
                return ""
            return self._page.title() or ""
        except Exception:
            return ""

    def _challenged(self) -> bool:
        """True when Cloudflare's interstitial is in front of the live page.

        Cross-file: sites/crawlee_utils.py:looks_like_cf_interstitial — the
        DOM-shaped detector (no status code, no length gate), as opposed to
        is_cf_challenge which judges an HTTP response.
        """
        if os.environ.pop(_FORCE_CF_ENV, None):
            return True
        try:
            from .crawlee_utils import looks_like_cf_interstitial
        except Exception:
            return False
        return looks_like_cf_interstitial(self._page_html(), self._page_title())

    def _apply_viewport(self, size: Dict[str, int]) -> None:
        """Best-effort viewport re-emulation; never raises. See
        _INTERACTIVE_VIEWPORT for why a human-facing window needs it."""
        if self._page is None:
            return
        try:
            self._page.set_viewport_size(dict(size))
        except Exception:
            pass

    def _mark_cf_blocked(self, reason: str, *, background: bool = False) -> None:
        """Record a Cloudflare wall in both places: on the session (so later
        calls fail fast instead of re-polling) and in the module global that
        sign_api_query's error message reads, so the user is told about
        Cloudflare rather than about a missing signer.

        ``background`` marks a verdict that only holds because this call was not
        allowed to interrupt anyone — provisional, not final.
        """
        global _LAST_UNAVAILABLE_REASON
        flat = " ".join(str(reason).split())
        self._cf_blocked = flat
        self._cf_blocked_background = bool(background)
        _LAST_UNAVAILABLE_REASON = flat

    def _await_auto_clear(self, timeout_s: float = _CF_AUTO_CLEAR_TIMEOUT_S) -> bool:
        """Poll for Cloudflare's JS challenge to pass on its own.

        Worth the wait now in a way it never was before: with channel= + the UA
        pin the context no longer announces HeadlessChrome in either the UA or
        the client hints, which is the difference between a Managed Challenge
        that can clear itself and one that cannot.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if self._backend is None and self._page.is_closed():
                    return False
            except Exception:
                return False
            time.sleep(1.0)
            if not self._challenged():
                print("[*] MangaFire: Cloudflare check cleared on its own.")
                return True
        return False

    def _inject_cf_cookies(self) -> bool:
        """Copy the shared solver's captured cookies into this context."""
        if self._context is None:
            return False
        try:
            from . import crawlee_utils as _cu

            with _cu._cf_cookie_lock:
                cached = _cu._cf_cookie_cache.get("mangafire.to")
        except Exception:
            return False
        if not cached:
            return False
        raw = cached.get("cookies") or []
        pw_cookies = [
            {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain") or "mangafire.to",
                "path": c.get("path") or "/",
            }
            for c in raw
            if isinstance(c, dict) and c.get("name")
        ]
        if not pw_cookies:
            return False
        try:
            self._context.add_cookies(pw_cookies)
        except Exception as e:
            print(f"[!] MangaFire: could not inject Cloudflare cookies: {e}")
            return False
        self._cf_cookie_ts = float(cached.get("ts", 0) or 0)
        print(
            f"[*] MangaFire: injected {len(pw_cookies)} Cloudflare cookie(s) "
            f"into the browser profile."
        )
        return True

    def _solve_cf_via_zendriver(self) -> bool:
        """Tier 1 of the handoff: let the shared solver open a visible Chrome.

        zendriver's verify_cf clicks the Turnstile checkbox itself, so this is
        usually a glance rather than a task. The context is then RELAUNCHED
        before the cookies are injected, because _start pins the solver's UA
        (via _cached_cf_user_agent) and a clearance presented under a different
        UA is worthless.

        Cross-file: sites/crawlee_utils.py:get_cf_session — which owns the
        interactive gate, so this cannot prompt from a background operation.
        """
        try:
            from .crawlee_utils import get_cf_session
        except Exception:
            return False
        try:
            get_cf_session(_BASE_URL + "/")
        except Exception as e:
            print(f"[!] MangaFire: Cloudflare solve failed: {e}")
            return False
        if not self._cached_cf_user_agent():
            return False
        self._cleanup()
        if not self._start():
            return False
        if not self._inject_cf_cookies():
            return False
        try:
            self._goto(_BASE_URL + "/")
        except Exception:
            return False
        return not self._challenged()

    def _solve_cf_headed(self) -> bool:
        """Tier 2: show the user OUR OWN persistent profile, headed.

        Better than tier 1 in one important way — the clearance is earned by the
        exact profile the rest of the run uses, so nothing is transplanted. It
        is the fallback only because it always costs a human a click, whereas
        zendriver usually clicks for them.

        Restoring the original mode afterwards is safe precisely because of the
        channel= + UA pin: headed and headless present the identical UA and
        client hints (see sites/browser_identity.py), which is what makes a
        headed solve survive the relaunch. Without the channel it would not.
        """
        # An embedder owns its own UI — Android shows the challenge in its
        # WebView via the backend's solve_challenge, which tier 1 already
        # reached through get_cf_session. Tearing down and "relaunching headed"
        # means nothing there, and self._page is None, so the poll below would
        # fall out on an AttributeError and merely LOOK like a decline.
        if self._backend is not None:
            return False
        if sys.platform not in ("win32", "darwin") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        ):
            print(
                "[!] MangaFire: no display available, so the verification "
                "window cannot be shown."
            )
            return False
        try:
            timeout_s = float(
                os.environ.get(_CF_SOLVE_TIMEOUT_ENV) or _CF_DEFAULT_SOLVE_TIMEOUT_S
            )
        except (TypeError, ValueError):
            timeout_s = _CF_DEFAULT_SOLVE_TIMEOUT_S

        was_headless = self._headless
        self._cleanup()
        if not self._start(headless=False):
            print("[!] MangaFire: could not open a visible browser for verification.")
            self._cleanup()
            self._start(headless=was_headless)
            return False
        self._apply_viewport(_INTERACTIVE_VIEWPORT)

        solved = False
        try:
            try:
                self._goto(_BASE_URL + "/")
            except Exception as e:
                print(f"[!] MangaFire: could not load the verification page ({e}).")

            # Every line is [!]-prefixed on purpose: the Electron LogPanel
            # classifies by line shape (UI-source/electron/log-filter.js:
            # classifyLogLevel), and a line starting with 2+ spaces is demoted
            # to "verbose" — which would dim the one instruction that must be
            # read.
            print("[!] " + "=" * 68)
            print("[!] ACTION NEEDED - mangafire.to is behind a Cloudflare check.")
            print("[!] A browser window just opened. Complete the check in it")
            print("[!] (usually one click on the 'Verify you are human' box).")
            print(
                f"[!] The download resumes by itself once it passes (waiting up "
                f"to {int(timeout_s)}s)."
            )
            print("[!] The session is saved to disk, so this should be rare.")
            print("[!] " + "=" * 68)

            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                try:
                    if self._page.is_closed():
                        print(
                            "[!] MangaFire: verification window was closed before "
                            "the check completed."
                        )
                        break
                except Exception:
                    break
                if not self._challenged():
                    time.sleep(1.5)  # let the Set-Cookie land before teardown
                    solved = True
                    break
                time.sleep(1.0)
        except Exception as e:
            print(f"[!] MangaFire: verification window failed: {e}")

        # Put the browser back the way the run wants it, whatever happened —
        # context.close() is also what FLUSHES the freshly-solved cookies to the
        # profile dir, so this is not just tidiness.
        self._cleanup()
        if not self._start(headless=was_headless):
            return False
        if not solved:
            return False
        try:
            self._goto(_BASE_URL + "/")
        except Exception:
            return False
        return not self._challenged()

    def _ensure_cleared(self) -> bool:
        """Leave the live page on real mangafire content, or explain why not.

        Callers must not touch the DOM before this returns True. The original
        bug was exactly that: _bootstrap ran its module scan against the
        interstitial and faithfully reported "no vrf signer among 0 module
        candidates", blaming the signer for a network-level block.
        """
        if self._cf_blocked:
            # A verdict reached ONLY because we weren't allowed to prompt is
            # provisional: one process legitimately holds both contexts. A
            # --multi-source download probes with the permission explicitly OFF
            # (search_orchestrator.py:_probe_one) and then downloads with it ON,
            # and there is one _SignerSession per process — so a sticky
            # background verdict would poison the download that follows it.
            # Re-open the question only when the answer could actually differ.
            if not self._cf_blocked_background:
                return False
            try:
                from .crawlee_utils import interactive_solve_allowed
            except Exception:
                return False
            if not interactive_solve_allowed():
                return False
            self._cf_blocked = None
            self._cf_blocked_background = False
        if not self._challenged():
            return True

        print("[!] MangaFire: mangafire.to is behind a Cloudflare check.")
        if self._await_auto_clear():
            return True

        # A human is the only thing left, so BOTH gates apply: does this process
        # own a solver at all (capability), and may it interrupt someone right
        # now (permission)? The permission is context-scoped, so search,
        # --list-chapters and the library update-check never reach a window.
        # Cross-file: sites/crawlee_utils.py's "TWO SEPARATE QUESTIONS" block.
        try:
            from .crawlee_utils import (
                cf_solver_available,
                interactive_solve_allowed,
                warn_cf_rescue,
            )
        except Exception:
            self._mark_cf_blocked(
                "mangafire.to is behind a Cloudflare check and no solver is available"
            )
            return False

        if not interactive_solve_allowed():
            warn_cf_rescue(
                _BASE_URL,
                "needs a human to solve it, and this is a background operation "
                "(search / probe / update-check). Not opening a browser; re-run "
                "this series as a download to solve it once.",
                kind="background",
            )
            self._mark_cf_blocked(
                "mangafire.to is behind a Cloudflare check that needs a human, and "
                "this is a background operation",
                background=True,
            )
            return False

        why_not = _may_prompt()
        if why_not:
            self._mark_cf_blocked(
                f"mangafire.to is behind a Cloudflare check ({why_not})"
            )
            return False

        solved = False
        if cf_solver_available():
            solved = self._solve_cf_via_zendriver()
        if not solved:
            solved = self._solve_cf_headed()
        _record_prompt_outcome(solved)
        if not solved:
            self._mark_cf_blocked(
                "the Cloudflare check on mangafire.to was not completed"
            )
            return False
        print("[*] MangaFire: Cloudflare check passed - thanks. Resuming.")
        return True

    # -------------------- driver-agnostic touchpoints --------------------
    # The four operations this signer needs, routed to whichever driver _start
    # established. Everything below here is written against these, so adding a
    # driver means implementing BrowserBackend — not editing _bootstrap/sign.

    def _eval(self, script: str, arg=_EVAL_NOARG):
        if self._backend is not None:
            from . import browser_backend as _bb

            return (
                self._backend.evaluate(script)
                if arg is _EVAL_NOARG
                else self._backend.evaluate(script, arg)
            )
        return (
            self._page.evaluate(script)
            if arg is _EVAL_NOARG
            else self._page.evaluate(script, arg)
        )

    def _goto(self, url: str) -> None:
        if self._backend is not None:
            self._backend.goto(url, wait_until="domcontentloaded", timeout_ms=45000)
            return
        self._page.goto(url, wait_until="domcontentloaded", timeout=45000)

    def _alive(self) -> bool:
        """True when the live page still carries a bootstrapped signer. A SPA
        navigation (or a crash-and-reload) wipes window globals, so the fast
        path has to verify rather than assume."""
        try:
            return bool(self._eval("() => typeof window.__aioMfSign === 'function'"))
        except Exception:
            return False

    def _bootstrap(self) -> bool:
        """Load a mangafire.to page and install window.__aioMfSign.

        The page load is what makes the chunk list discoverable (we read the
        module URLs the real document pulled, rather than hardcoding a
        build-stamped filename), and it puts the import on the site's own
        origin with its cookies. Whether the signer additionally needs that
        origin at runtime is untested — loading the page is cheap and removes
        the question, so don't "optimize" it into an about:blank import
        without checking.
        """
        if not self._start():
            return False
        try:
            if self._alive() and self._namespace:
                return True
        except Exception:
            pass
        try:
            self._goto(_BASE_URL + "/")
        except Exception as e:
            print(f"[!] MangaFire: could not load {_BASE_URL} for signing: {e}")
            return False
        # Cloudflare first. Probing the DOM while the interstitial is up finds
        # zero module candidates and reports it as a signer fault — the exact
        # misdiagnosis this guard exists to prevent.
        if not self._ensure_cleared():
            return False
        try:
            info = self._eval(_BOOTSTRAP_JS)
        except Exception as e:
            print(f"[!] MangaFire: vrf signer bootstrap failed: {e}")
            return False
        if not isinstance(info, dict) or not info.get("moduleUrl"):
            print("[!] MangaFire: vrf signer bootstrap returned nothing usable.")
            return False

        # Disk-cache namespace = the identity of the code that mints the tokens.
        # The chunk URL carries a content hash, so ANY redeploy of the signer
        # lands in a fresh namespace and stale tokens are never served.
        #
        # NOT keyed on window.__build / window.__config: the site DELETES both
        # globals once its bundle has read them (verified 2026-08-02 — they are
        # present in the shell HTML and `undefined` by the time a page.evaluate
        # can run), so they read as empty here and would collapse every deploy
        # into one constant namespace. The residual case this misses — same
        # chunk, rotated key — is covered by the token-rejection backstop in
        # sites/mangafire.py:_api_get, which purges the namespace and re-signs.
        module_url = str(info.get("moduleUrl") or "")
        export_name = str(info.get("exportName") or "")
        self._namespace = hashlib.sha256(
            f"{module_url}#{export_name}".encode()
        ).hexdigest()[:16]
        self._signer_desc = f"{module_url.rsplit('/', 1)[-1]}#{export_name}"

        # Tokens minted under a superseded bundle can never be useful again, so
        # drop every other namespace rather than letting the file accumulate one
        # dead generation per site deploy.
        self._load_cache()
        if any(ns != self._namespace for ns in self._cache):
            self._cache = {self._namespace: self._cache.get(self._namespace, {})}
            self._cache_dirty = True

        print(f"[*] MangaFire: vrf signer ready ({self._signer_desc})")
        return True

    def sign(self, specs: Sequence[Tuple[str, Sequence[Tuple[str, Any]]]]) -> List[Optional[Dict[str, str]]]:
        """Sign a batch. Returns one {'vrf','query'} dict (or None) per spec,
        index-aligned. Cache hits never touch the browser, and a batch whose
        entries all hit the cache never launches one."""
        self._load_cache()
        results: List[Optional[Dict[str, str]]] = [None] * len(specs)
        pending: List[int] = []
        keys: List[str] = []
        for i, (path, pairs) in enumerate(specs):
            key = _cache_key(path, pairs)
            keys.append(key)
            hit = self._cached(key)
            if hit is not None:
                results[i] = hit
            else:
                pending.append(i)

        if not pending:
            return results

        if not self._bootstrap():
            return results

        # Re-check the cache: _bootstrap may have just established the
        # namespace (first call of the process), so entries that looked like
        # misses above can now resolve without a browser round-trip.
        still: List[int] = []
        for i in pending:
            hit = self._cached(keys[i])
            if hit is not None:
                results[i] = hit
            else:
                still.append(i)
        if not still:
            return results

        payload = [[specs[i][0], [list(p) for p in specs[i][1]]] for i in still]
        try:
            signed = self._eval(_SIGN_BATCH_JS, payload)
        except Exception as e:
            print(f"[!] MangaFire: vrf signing call failed: {e}")
            return results
        if not isinstance(signed, list):
            return results
        for slot, out in zip(still, signed):
            if isinstance(out, dict) and out.get("vrf"):
                entry = {"vrf": str(out.get("vrf")), "query": str(out.get("query") or "")}
                results[slot] = entry
                self._remember(keys[slot], entry)
        self._flush_cache()
        return results

    def fetch_api(self, url: str) -> Optional[Dict[str, Any]]:
        """GET *url* from inside the live mangafire page.

        Same-origin, so it carries the profile's cf_clearance, the browser's own
        TLS fingerprint and its client hints — which is the entire point.
        Handing that clearance to cloudscraper instead is the transplant comix
        already proved cannot work (grep _waf_recover_once in sites/comix.py):
        the cookie is bound to the client that earned it, and OpenSSL/urllib3 is
        not that client by any measure the edge uses.

        Returns {"status": int, "body": str}, or None when no browser could be
        brought up. Callers: sites/mangafire.py:_api_get, only once a plain HTTP
        attempt has come back challenged.
        """
        if not self._bootstrap():
            return None
        try:
            out = self._eval(_FETCH_API_JS, url)
        except Exception as e:
            print(f"[!] MangaFire: in-page API fetch failed: {e}")
            return None
        if not isinstance(out, dict):
            return None
        try:
            return {
                "status": int(out.get("status") or 0),
                "body": str(out.get("body") or ""),
            }
        except Exception:
            return None

    def _cleanup(self) -> None:
        # Drop the reference only — the backend instance is owned by
        # sites/browser_backend.py's registry (and closed by its atexit hook),
        # not by this session. Closing it here would tear down a browser other
        # consumers are still using.
        self._backend = None
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
        self._headless = None

    def close(self) -> None:
        self._flush_cache()
        self._cleanup()


# ---------------------------------------------------------------------------
# Daemon-thread bridge. See the module header for why Patchright cannot simply
# be called from whichever thread happens to want a token.
# ---------------------------------------------------------------------------
_REQUEST_QUEUE: "queue.Queue" = queue.Queue()
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()
_SESSION: Optional[_SignerSession] = None  # owned by the worker thread
_SHUTDOWN_SENTINEL = object()


def _worker_loop() -> None:
    global _SESSION
    while True:
        item = _REQUEST_QUEUE.get()
        if item is _SHUTDOWN_SENTINEL:
            try:
                if _SESSION is not None:
                    try:
                        _SESSION.close()
                    except Exception:
                        pass
                    _SESSION = None
            finally:
                return
        try:
            fut, fn_name, args, kwargs = item
        except (TypeError, ValueError):
            continue
        if fut.cancelled():
            continue
        try:
            if _SESSION is None:
                _SESSION = _SignerSession()
            result = getattr(_SESSION, fn_name)(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — propagate to the caller
            try:
                fut.set_exception(exc)
            except _futures.InvalidStateError:
                pass
        else:
            try:
                fut.set_result(result)
            except _futures.InvalidStateError:
                pass


def _ensure_worker() -> None:
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        threading.Thread(target=_worker_loop, name="mangafire-pw", daemon=True).start()
        _WORKER_STARTED = True


def _call(fn_name: str, *args, _timeout_s: float = _SIGN_TIMEOUT_S, **kwargs):
    _ensure_worker()
    fut: _futures.Future = _futures.Future()
    _REQUEST_QUEUE.put((fut, fn_name, args, kwargs))
    try:
        return fut.result(timeout=_timeout_s)
    except _futures.TimeoutError:
        fut.cancel()
        raise


# ----------------------------- public API -----------------------------


def sign_api_queries(
    specs: Sequence[Tuple[str, Sequence[Tuple[str, Any]]]],
) -> List[Optional[Dict[str, str]]]:
    """Sign a batch of API requests in one browser round-trip.

    Each spec is ``(path, pairs)`` where `path` is the API path WITHOUT the
    `/api` prefix (e.g. `/titles/dkw/chapters`) and `pairs` is an ordered
    sequence of (key, value) query params. Returns one dict per spec with
    `vrf` and the fully serialized `query` (vrf included), or None where
    signing failed.

    Order of `pairs` matters — see _cache_key.
    """
    if not specs:
        return []
    timeout = _BOOTSTRAP_TIMEOUT_S if not _WORKER_STARTED else _SIGN_TIMEOUT_S
    try:
        return _call("sign", list(specs), _timeout_s=timeout)
    except Exception as e:
        print(f"[!] MangaFire: vrf signing unavailable: {e}")
        return [None] * len(specs)


def sign_api_query(path: str, pairs: Sequence[Tuple[str, Any]] = ()) -> Dict[str, str]:
    """Single-request convenience wrapper. Raises MangaFireSigningError rather
    than returning None so download paths fail loudly (an unsigned request
    would 403, and a silently empty page list would be mistaken for a
    legitimately empty chapter — see sites/comix.py's empty_content trap)."""
    out = sign_api_queries([(path, list(pairs))])
    signed = out[0] if out else None
    if not signed or not signed.get("vrf"):
        why = _LAST_UNAVAILABLE_REASON or "browser-backed signer unavailable"
        raise MangaFireSigningError(
            f"could not sign MangaFire API request for {path} — the site requires "
            f"a per-request vrf token ({why})"
        )
    return signed


def fetch_api_json(url: str) -> Optional[Dict[str, Any]]:
    """Fetch *url* through the signer's browser page. See _SignerSession.fetch_api.

    Returns None rather than raising: sites/mangafire.py:_api_get has already
    tried plain HTTP by the time it calls this, and treats None as "that
    attempt produced nothing" so its existing non-200 handling applies.
    """
    timeout = _BOOTSTRAP_TIMEOUT_S if not _WORKER_STARTED else _SIGN_TIMEOUT_S
    try:
        return _call("fetch_api", url, _timeout_s=timeout)
    except Exception as e:
        print(f"[!] MangaFire: browser-backed API fetch unavailable: {e}")
        return None


def invalidate() -> None:
    """Forget every cached token for the current bundle and force the next call
    to re-bootstrap. Called by sites/mangafire.py when the server rejects a
    token we thought was valid (the site redeployed mid-run)."""
    try:
        _call("purge_namespace", _timeout_s=15.0)
    except Exception:
        pass


def _shutdown() -> None:
    """Best-effort at-exit close. The daemon dies with the interpreter either
    way; this just gives Chromium a chance to exit cleanly. No join."""
    if not _WORKER_STARTED:
        return
    try:
        _REQUEST_QUEUE.put_nowait(_SHUTDOWN_SENTINEL)
    except queue.Full:
        pass


atexit.register(_shutdown)
