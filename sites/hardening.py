from __future__ import annotations

import os
import random
import threading
import time
from typing import Dict, Optional, Tuple, Union
from urllib.parse import urlparse
import requests

# Thread-local storage to avoid double-throttling in nested calls
_TLS = threading.local()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def looks_like_cloudflare_rate_limit(resp) -> bool:
    """Detect Cloudflare/app rate limiting and challenge pages."""
    status = getattr(resp, "status_code", None)
    
    # 404 is almost never a rate limit, and strictly used for "not found"
    # essential for brute-forcing handlers like flamecomics.
    if status == 404:
        return False
        
    if status in (403, 429, 503):
        return True

    # Content-Type is the safest clue
    ct = ""
    try:
        ct = (getattr(resp, "headers", {}) or {}).get("content-type", "") or ""
    except Exception:
        ct = ""
    ct_l = ct.lower()

    if ct_l.startswith("image/") or "application/octet-stream" in ct_l:
        return False

    # JSON API responses
    if "application/json" in ct_l or ct_l.endswith("+json"):
        try:
            data = resp.json()
        except Exception:
            return False

        if isinstance(data, dict):
            st = data.get("status") or data.get("code")
            try:
                st_int = int(st)
            except Exception:
                st_int = None

            if st_int in (403, 429, 503):
                return True

            msg = str(data.get("message") or data.get("error") or data.get("result") or "").lower()
            if "error 1015" in msg or "you are being rate limited" in msg:
                return True
            if "too many requests" in msg or "rate limit" in msg or "rate-limited" in msg:
                return True
        return False

    # HTML / text responses
    try:
        text = getattr(resp, "text", "") or ""
    except Exception:
        return False

    text_l = text.lower()
    
    # If it is a 200 OK, we should be very conservative.
    # Many sites have "cloudflare" in their scripts or footers.
    is_200 = (status == 200)

    # If 200 OK, require TITLE match or specific short body typical of challenge pages
    if is_200:
        # Challenge pages are usually small
        if len(text) > 15000: 
            return False
            
        # Check specific titles for 200 OK challenges (Turnstile/JS challenge)
        if "<title>just a moment...</title>" in text_l:
            return True
        if "<title>attention required! | cloudflare</title>" in text_l:
            return True
        if "<title>one more step</title>" in text_l:
            return True
        if "challenge-platform" in text_l and "window._cf_chl_opt" in text_l:
             # This specific combination often appears in Turnstile pages
             return True
             
        # If it's 200 but contains strong error markers in visible text, maybe?
        if "error 1015" in text_l: 
            return True

    else:
        # Non-200 (e.g. 403, 503, 429) can be looser (but not 404, we checked that already)
        strong_markers = (
            "error 1015",
            "you are being rate limited",
            "cf-error-code",
            "checking your browser",
            "attention required",
            "one more step",
            "verify you are human",
            "challenge-platform",
        )
        if any(m in text_l for m in strong_markers):
            if ("cloudflare" in text_l) or ("cf-ray" in text_l) or ("/cdn-cgi/" in text_l) or ("cf-error-code" in text_l) or ("error 1015" in text_l):
                return True

    return False


class MonotonicRateLimiter:
    """Thread-safe minimum-spacing limiter using a monotonic clock."""

    def __init__(self, gaps: Dict[str, float], jitter_s: float):
        self._gaps = {k: max(0.0, float(v)) for k, v in (gaps or {}).items()}
        self._jitter_s = max(0.0, float(jitter_s))
        self._lock = threading.Lock()
        self._next_time = 0.0

    def wait(self, kind: str = "default") -> None:
        gap = self._gaps.get(kind, self._gaps.get("default", 0.0))
        jitter = random.uniform(0.0, self._jitter_s) if self._jitter_s > 0 else 0.0

        with self._lock:
            now = time.monotonic()
            wait_s = max(0.0, self._next_time - now)
            self._next_time = max(now, self._next_time) + gap + jitter

        if wait_s > 0:
            time.sleep(wait_s)


def get_request_kind(url: str) -> str:
    """Classify requests."""
    if not url:
        return "other"
    if url.startswith("/"):
        path = url
    else:
        try:
            path = urlparse(url).path
        except Exception:
            path = ""

    p = (path or "").lower()
    if any(p.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")):
        return "image"
    if "/ajax/" in p or "/api/" in p or ".json" in p:
        return "ajax"
    return "page"


def configure_throttling(
    scraper,
    domains: Tuple[str, ...],
    gaps: Optional[Dict[str, float]] = None,
    jitter: float = 0.6,
    max_retries: int = 4,
    backoff_base: float = 12.0
) -> None:
    """
    Patches a scraper session to apply rate limiting and Cloudflare-aware retries
    only for requests to specific domains.
    """
    if getattr(scraper, "_hardening_patched", False):
        return

    if not gaps:
        gaps = {
            "default": 1.25,
            "ajax": 1.75,
            "page": 2.25,
            "image": 1.0,
        }

    limiter = MonotonicRateLimiter(gaps=gaps, jitter_s=jitter)
    
    # Store origin request method
    orig_request = scraper.request

    def patched_request(method, url, *args, **kwargs):
        # 1. Check for infinite recursion (e.g. from cloudscraper internal solving)
        if getattr(_TLS, "inside_hardening", False):
            # If we are already inside, we MUST bypass our logic and calling `orig_request`
            # (since orig_request might be the one calling us via super().get()).
            # We explicitly delegate to the base requests.Session.request to avoid the loop.
            return requests.Session.request(scraper, method, url, *args, **kwargs)

        # 2. Check if URL matches target domains
        is_target = False
        if url:
            try:
                host = urlparse(url).netloc.lower()
                if any(host.endswith(d) for d in domains) or url.startswith("/"):
                    is_target = True
            except Exception:
                pass

        if getattr(_TLS, "skip_patch_throttle", False) or not is_target:
            # Not a target domain, just pass through to original (Cloudscraper) logic
            return orig_request(method, url, *args, **kwargs)

        # 3. Apply throttle and retries
        kind = get_request_kind(url)
        limiter.wait(kind)

        last_err: Optional[Exception] = None
        last_resp = None

        _TLS.inside_hardening = True
        try:
            for attempt in range(max_retries):
                try:
                    resp = orig_request(method, url, *args, **kwargs)
                    last_resp = resp
                    
                    if looks_like_cloudflare_rate_limit(resp):
                        cooldown = backoff_base * (2 ** attempt) + random.uniform(0.0, 4.0)
                        print(f"[!] {domains[0]} rate-limit/challenge (HTTP {getattr(resp, 'status_code', '???')}). Cooling down {cooldown:.1f}s...")
                        time.sleep(cooldown)
                        continue
                    
                    return resp
                except Exception as e:
                    # Network errors often mean "connection reset" by firewall
                    last_err = e
                    cooldown = backoff_base * (2 ** attempt) + random.uniform(0.0, 4.0)
                    print(f"[!] {domains[0]} request error: {e}. Cooling down {cooldown:.1f}s...")
                    time.sleep(cooldown)

            if last_resp is not None:
                 return last_resp
            raise last_err or RuntimeError(f"Hardened request failed for {url}")
        finally:
            _TLS.inside_hardening = False

    scraper.request = patched_request
    setattr(scraper, "_hardening_patched", True)
    setattr(scraper, "_hardening_limiter", limiter)


def throttled_request(make_request, scraper, url: str, domains: Tuple[str, ...]):
    """
    Wrapper for manual requests inside handlers that might not go through scraper.request
    or need explicit throttling logic (though the patch above usually covers it).
    """
    return make_request(url, scraper)
