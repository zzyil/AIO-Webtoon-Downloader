from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup

from .madara import MadaraSiteHandler


class ToonilySiteHandler(MadaraSiteHandler):
    name = "toonily"

    def __init__(self) -> None:
        super().__init__("Toonily", "https://toonily.com")

    def configure_session(self, scraper, args) -> None:
        super().configure_session(scraper, args)
        # Kotlin: .addNetworkInterceptor(CookieInterceptor(domain, "toonily-mature" to "1"))
        scraper.cookies.set("toonily-mature", "1", domain="toonily.com")

    def _slug_from_url(self, url: str) -> str:
        # Kotlin: override val mangaSubString = "serie"
        # URL: https://toonily.com/webtoon/series-slug/ OR https://toonily.com/serie/series-slug/
        # The base Madara handler might not handle "webtoon" or "serie" correctly if it expects "manga".
        
        from urllib.parse import urlparse
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            return parsed.netloc
        
        # Handle /webtoon/slug and /serie/slug
        if (parts[0] == "webtoon" or parts[0] == "serie") and len(parts) >= 2:
            return parts[1]
            
        return parts[-1]

    def _extract_cover(self, soup: BeautifulSoup, page_url: str) -> Optional[str]:
        cover = super()._extract_cover(soup, page_url)
        if cover:
            # Kotlin: sdCoverRegex = Regex("""-[0-9]+x[0-9]+(\.\w+)$""")
            # Replace with $1 to get HD cover
            cover = re.sub(r"-\d+x\d+(\.[a-zA-Z]+)$", r"\1", cover)
        return cover
