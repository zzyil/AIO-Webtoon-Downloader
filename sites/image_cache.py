"""Module-level cache for image bytes captured by browser-based site
handlers.

Background:
Some sites (notably comix.to) sign their CDN URLs with short-lived
HMAC tokens that expire within ~1 minute. The site's reader fetches
every image once via the browser; those responses are valid AT the
moment the browser fetches them, but by the time aio-dl.py's
downloader re-fetches them through cloudscraper (often 30-60s later),
the tokens of less-popular shards have expired and the CDN returns
404. Empirically ~5% of 500+ comix URLs were 404ing on re-fetch even
with "Preload all images" enabled, causing chapter aborts.

Solution:
While the browser is scraping the chapter, capture the response BODY
(via Playwright's response.body()) and stash the bytes here keyed by
URL. The downloader then checks this cache before any HTTP fetch and
uses the cached bytes when present. Since the bytes ARE what the
browser already saw, we bypass the token-expiry issue entirely.

Cross-file:
  - sites/comix.py:_ComixBrowserSession.fetch_chapter_images_via_dom
    populates the cache via cache_image() in the response listener.
    Called clear_cache() at start of each scrape to bound memory.
  - aio-dl.py:dl_image checks get_cached_image(url) before any HTTP
    fetch. On hit, writes bytes to the pending file and finalizes
    using the cached content-type for format detection. On miss,
    falls through to the existing scraper-based fetch.

Memory bound:
The cache lives for the duration of a single chapter scrape +
download. A typical comix chapter captures 100-700 unique URLs; with
an average payload of ~400KB that's 40-280MB in the worst case.
clear_cache() is called at the start of each new chapter scrape so
memory doesn't grow unboundedly across chapters. Individual responses
larger than 20MB are skipped (an image that big is either a bug or a
malicious payload we shouldn't honor).

Thread safety:
The module-level dict is guarded by _lock. Patchright's response
listener (writer) runs on the comix-pw daemon thread; aio-dl.py's
parallel image downloaders (readers, up to 3 by default) run on
worker threads. Operations are short (dict get/set) so the lock is
not a perf bottleneck even at 500+ cache writes per chapter.
"""
from __future__ import annotations

import threading
from typing import Dict, Optional, Tuple

# url -> (body_bytes, content_type). content_type is the raw
# "image/webp" etc. string; downstream code uses it for format
# sniffing in _finalize_pending_image when the magic bytes alone are
# ambiguous (webp's RIFF header can match a few wrong formats).
_cache: Dict[str, Tuple[bytes, str]] = {}
_lock = threading.Lock()

# Per-image size cap. An image larger than 20MB is either a bug
# (server serving the wrong asset), or a malicious payload, or a
# legitimate but ridiculous full-resolution scan we shouldn't waste
# memory caching. The downloader will fall through to HTTP for these,
# which has its own size handling.
_MAX_BODY_BYTES = 20 * 1024 * 1024


def cache_image(url: str, body: bytes, content_type: str = "") -> bool:
    """Stash an image's bytes + content-type by URL.

    Idempotent — re-cache with the same URL overwrites the prior
    entry. Thread-safe. Returns True if cached, False if rejected
    (empty url, empty body, or body over _MAX_BODY_BYTES).
    """
    if not url or not body:
        return False
    if len(body) > _MAX_BODY_BYTES:
        return False
    with _lock:
        _cache[url] = (body, content_type or "")
    return True


def get_cached_image(url: str) -> Optional[Tuple[bytes, str]]:
    """Return (body_bytes, content_type) tuple if URL is cached,
    None otherwise. Thread-safe.
    """
    if not url:
        return None
    with _lock:
        return _cache.get(url)


def clear_cache() -> None:
    """Drop all cached entries. Called at the start of each chapter
    scrape so memory doesn't grow across chapters in a multi-chapter
    run. Thread-safe.
    """
    with _lock:
        _cache.clear()


def cache_stats() -> Tuple[int, int]:
    """Return (entry_count, total_byte_count) for diagnostic logging.
    Thread-safe.
    """
    with _lock:
        return (len(_cache), sum(len(b) for b, _ in _cache.values()))
