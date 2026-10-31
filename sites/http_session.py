"""The one constructor for the shared plain-HTTP session every handler gets.

What this module owns: how a run's base `requests`-compatible session is built
(cloudscraper when it imports, else `requests.Session`) and the identity it
presents before any handler's `configure_session` customises it.

Who reads from it: aio-dl.py main() (the download / --list-chapters /
update-check session) and the AniList cover refresh, aio_search_cli.py
_build_scraper (cross-site search), api.py get_scraper (--serve). Grep
create_http_session. Handlers still own per-site headers in configure_session,
which runs AFTER this, so a handler that pins its own UA (mangadex, comix,
anything syncing a cached Cloudflare solve via crawlee_utils) still wins.

Depends on: sites/browser_identity.py for FALLBACK_UA, the project's maintained
modern Chrome UA. cloudscraper is optional.

WHY THE UA IS PINNED instead of left to cloudscraper (measured 2026-10-31):
cloudscraper 1.2.71 picks a RANDOM User-Agent per session from a bundled list
whose every Chrome entry is 2016-2019 (Chrome 53-72), so each process presented
a different decade-old browser. atsu.moe's Cloudflare started answering the
macOS picks with its "Attention Required!" block page: 12/12 darwin picks got
403 while Windows/Linux picks and every modern UA got 200, and plain `requests`
with only the darwin UA reproduced it, so the UA string alone decided it (not
TLS, not cookies). The run-level session asked for platform "darwin", so EVERY
atsu.moe request failed (update checks, downloads, search). comix had hit the
same wall in 2026-08 and fixed it for itself (grep _comix_http_headers). This
fixes the class: one fixed modern identity, the same on every run.

The UA and the document Accept are replaced together, because a modern UA
beside cloudscraper's Chrome-6x Accept is a pairing no real browser sends
(comix's lesson; grep "modern UA needs a modern Accept"). Accept-Language and
Accept-Encoding stay. Browser-navigation headers (Sec-Fetch-*, client hints)
are NOT added: they would be wrong on the many JSON API calls this session
also makes; comix layers those on itself. Image downloads are unaffected by the
Accept: they send a per-request image Accept (sites/_image_io.py:IMAGE_ACCEPT).
"""

from __future__ import annotations

import sys
from typing import Any

import requests

from sites.browser_identity import FALLBACK_UA

try:
    import cloudscraper  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    cloudscraper = None  # type: ignore

# The UA every shared session starts with. An alias rather than a second
# constant so a FALLBACK_UA refresh (kept in step with Patchright's Chromium)
# moves the HTTP sessions with it.
SESSION_USER_AGENT = FALLBACK_UA

# Current Chrome's top-level document Accept, the partner of the UA above.
SESSION_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,image/apng,*/*;q=0.8,"
    "application/signed-exchange;v=b3;q=0.7"
)


def create_http_session() -> Any:
    """A fresh session with the shared modern identity.

    Never raises: a cloudscraper init failure falls back to requests.Session,
    as the callers did before this existed. cloudscraper is asked for Windows
    Chrome so its non-UA defaults match the pinned UA's platform.
    """
    session = None
    if cloudscraper is not None and sys.version_info >= (3, 7):
        try:
            session = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False}
            )
        except Exception:
            session = None
    if session is None:
        session = requests.Session()
    session.headers["User-Agent"] = SESSION_USER_AGENT
    session.headers["Accept"] = SESSION_ACCEPT
    return session
