from __future__ import annotations

from .assortedscans import AssortedScansSiteHandler


class ArcRelightSiteHandler(AssortedScansSiteHandler):
    name = "arcrelight"
    domains = ("arc-relight.com", "www.arc-relight.com")
    _BASE_URL = "https://arc-relight.com"


__all__ = ["ArcRelightSiteHandler"]
