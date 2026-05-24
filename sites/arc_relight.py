from __future__ import annotations

from .assortedscans import AssortedScansSiteHandler


class ArcRelightSiteHandler(AssortedScansSiteHandler):
    # NOTE: arc-relight.com is a small SquareEnix-fan-scanlation site. Its
    # MangAdventure templates populate description + genres via meta tags
    # (which `AssortedScansSiteHandler.fetch_comic_context` reads first) but
    # the page itself lacks an Author/Artist/Status field. After the parent's
    # 2026-05-19 DOM fallback pass, authors/artists/status STILL stay empty
    # for arc-relight series — that's a site limitation, not a parser bug.
    # See dry_run_komikku_findings.md §C.
    name = "arcrelight"
    domains = ("arc-relight.com", "www.arc-relight.com")
    _BASE_URL = "https://arc-relight.com"


__all__ = ["ArcRelightSiteHandler"]
