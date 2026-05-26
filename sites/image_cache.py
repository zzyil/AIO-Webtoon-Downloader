"""Module-level cache for image bytes captured by browser-based site
handlers.

Background:
Some sites (notably comix.to) sign their CDN URLs with short-lived
HMAC tokens that expire within ~1 minute AND aggressively rate-limit
re-fetches from the same IP that's already been driving the in-page
canvas-scrape traffic. The site's reader pulls every image once via
the browser; those responses are valid AT the moment the browser
fetches them, but by the time aio-dl.py's downloader re-fetches them
through cloudscraper (often 30-60 s later — and in parallel with the
image-prefetch chain pulling ahead to N+1 and N+2), the tokens of
less-popular shards have expired and the CDN returns 404, or the
host is hot enough that we get rate-limited. Empirically ~5% of
500+ comix URLs were 404ing on re-fetch even with "Preload all
images" enabled, causing chapter aborts and 30 s long-retry waits.

Solution:
While the browser is scraping the chapter, capture the response
BODY (via Playwright's response.body()) and stash the bytes here
keyed by URL. The downloader checks this cache before any HTTP
fetch and uses the cached bytes when present. Since the bytes ARE
what the browser already saw, we bypass the token-expiry and rate-
limit issues entirely.

Cross-file:
  - sites/comix.py:_ComixBrowserSession._start attaches a session-
    level page.on("response") listener that calls cache_image() for
    every image/* response. The same module also populates the
    cache from the canvas toDataURL path under synthetic
    `comix-page://<chap_id>/<NNNN>.webp` URL keys.
  - aio-dl.py:dl_image checks get_cached_image(url) before any
    HTTP fetch. On hit, writes bytes to the pending file and
    finalizes using the cached content-type for format detection.
    On miss, falls through to the existing scraper-based fetch.

Eviction (no per-chapter clear — see "Why no clear_cache()" below):
  TTL: 600 s. After 10 minutes the bytes are wasted memory — any
       chapter we'd still want is already finalized to disk.
  Total: 256 MB. When a cache_image() call would push the cache
       over this cap, oldest entries are evicted (by cache_image()
       timestamp, ascending) until total drops to ~75% of the cap.
  Per-entry: 20 MB. An individual response larger than this is a
       bug, a malicious payload, or a legitimate scan we shouldn't
       waste memory on. The downloader falls through to HTTP,
       which has its own size handling.

Why no clear_cache() per chapter:
The earlier design called clear_cache() at the start of every
scrape. That broke under aio-dl.py's image-prefetch chain — scrape
N+1 runs in parallel with chapter N's downloader, so N+1's clear
wiped N's bytes mid-download. The downloader then fell through to
HTTP, hit the signed-token-expiry / rate-limit failures described
above, marked the host poisoned, and triggered a 30 s long-retry.
TTL + size-based eviction has the same working-set memory bound
(2-3 chapters' worth) without the race.

Thread safety:
The module-level dict is guarded by _lock. Patchright's response
listener (writer) runs on the comix-pw daemon thread; aio-dl.py's
parallel image downloaders (readers, up to 3 by default) run on
worker threads. Operations are short (dict get/set + occasional
eviction sweep on add) so the lock is not a perf bottleneck even
at 500+ cache writes per chapter.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional, Tuple

# url -> (body_bytes, content_type, monotonic_ts). The timestamp is
# the cache_image() arrival time — used by both the TTL pass and
# the oldest-first size-cap eviction. content_type is the raw
# "image/webp" etc. string; downstream code uses it for format
# sniffing in _finalize_pending_image when the magic bytes alone
# are ambiguous (webp's RIFF header can match a few wrong formats).
_cache: Dict[str, Tuple[bytes, str, float]] = {}
_lock = threading.Lock()

# Per-entry cap. An image larger than 20 MB is a bug (server
# serving the wrong asset), a malicious payload, or a legitimate
# but ridiculous full-resolution scan we shouldn't waste memory
# caching. dl_image falls through to HTTP for these, which has its
# own size handling.
_MAX_BODY_BYTES = 20 * 1024 * 1024

# Total cache cap. 256 MB comfortably holds 4-6 comix chapters
# (typical ~50 MB/chapter when all 80-130 webps are cached), which
# is well over the prefetch chain's 2-chapter look-ahead.
_MAX_TOTAL_BYTES = 256 * 1024 * 1024

# After eviction the cache shrinks to this fraction of the cap so
# we don't ping-pong evict on every add when steady-state usage is
# right at the limit. 0.75 → drops to ~192 MB, leaving headroom
# for the next ~1 chapter before the next eviction sweep fires.
_EVICT_TARGET_RATIO = 0.75

# TTL. After 10 minutes any chapter we'd still want is already
# finalized to disk; the bytes are dead weight. TTL pass runs on
# every cache_image() call, so eviction is amortized into writes
# (the read path stays free of any sweep work).
_TTL_SECONDS = 600.0


def _evict_if_needed_locked() -> None:
    """Drop TTL-expired entries; if still over the total cap, drop
    oldest by timestamp until under the target ratio. Caller MUST
    hold _lock. O(n) per pass, n = entry count (small — a few
    hundred during steady state).
    """
    now = time.monotonic()
    expired = [
        u for u, (_, _, ts) in _cache.items() if now - ts > _TTL_SECONDS
    ]
    for u in expired:
        del _cache[u]

    total = sum(len(b) for b, _, _ in _cache.values())
    if total <= _MAX_TOTAL_BYTES:
        return
    target = int(_MAX_TOTAL_BYTES * _EVICT_TARGET_RATIO)
    # Sort by timestamp asc (oldest first); evict from the front
    # until we drop under the target. Same key the TTL pass uses,
    # so once-popular but stale entries get reaped before still-
    # warm ones.
    for u, (b, _, _) in sorted(_cache.items(), key=lambda kv: kv[1][2]):
        del _cache[u]
        total -= len(b)
        if total <= target:
            break


def cache_image(url: str, body: bytes, content_type: str = "") -> bool:
    """Stash an image's bytes + content-type by URL.

    Idempotent — re-cache with the same URL overwrites the prior
    entry (the new timestamp also bumps it to the back of the
    eviction queue). Thread-safe. Returns True if cached, False
    if rejected (empty url, empty body, or body over
    _MAX_BODY_BYTES). Triggers an eviction sweep on every call so
    the cache stays bounded without a separate maintenance thread.
    """
    if not url or not body:
        return False
    if len(body) > _MAX_BODY_BYTES:
        return False
    ts = time.monotonic()
    with _lock:
        _cache[url] = (body, content_type or "", ts)
        _evict_if_needed_locked()
    return True


def get_cached_image(url: str) -> Optional[Tuple[bytes, str]]:
    """Return (body_bytes, content_type) tuple if URL is cached,
    None otherwise. Thread-safe. Does NOT refresh the entry's
    timestamp — TTL is measured from cache_image() time because
    that's when the bytes most closely matched the CDN's current
    signed URL (touching the timestamp on read could keep a stale
    URL alive past the point its real source has rotated).
    """
    if not url:
        return None
    with _lock:
        entry = _cache.get(url)
        if entry is None:
            return None
        body, ct, _ = entry
        return (body, ct)


def clear_cache() -> None:
    """Drop every cached entry. NOT called from per-chapter scrapes
    anymore — see the module docstring's "Why no clear_cache() per
    chapter" note. Kept for tests + emergency manual flushes.
    Thread-safe.
    """
    with _lock:
        _cache.clear()


def cache_stats() -> Tuple[int, int]:
    """Return (entry_count, total_byte_count) for diagnostic
    logging. Thread-safe.
    """
    with _lock:
        return (len(_cache), sum(len(b) for b, _, _ in _cache.values()))
