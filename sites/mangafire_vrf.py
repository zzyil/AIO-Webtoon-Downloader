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
from typing import Any, Dict, List, Optional, Sequence, Tuple

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


def _signer_alive(page) -> bool:
    """True when the live page still carries a bootstrapped signer. A SPA
    navigation (or a crash-and-reload) wipes window globals, so the fast path
    has to verify rather than assume."""
    try:
        return bool(page.evaluate("typeof window.__aioMfSign === 'function'"))
    except Exception:
        return False


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

    def _start(self) -> bool:
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
        try:
            profile = _profile_dir()
            os.makedirs(profile, exist_ok=True)
            headless = (os.environ.get("AIO_MANGAFIRE_HEADFUL") or "").strip() != "1"
            self._context = self._pw.chromium.launch_persistent_context(
                profile,
                headless=headless,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
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
        return True

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
            if _signer_alive(self._page) and self._namespace:
                return True
        except Exception:
            pass
        try:
            self._page.goto(_BASE_URL + "/", wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"[!] MangaFire: could not load {_BASE_URL} for signing: {e}")
            return False
        try:
            info = self._page.evaluate(_BOOTSTRAP_JS)
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
            signed = self._page.evaluate(_SIGN_BATCH_JS, payload)
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
