from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup

from .madara import MadaraSiteHandler


class WebtoonXYZSiteHandler(MadaraSiteHandler):
    name = "webtoonxyz"

    def __init__(self) -> None:
        # 2026-05-13: webtoon.xyz unreachable (network timeout, in probe_failures.json);
        # site relocated to webtoonxyz.co. Same Madara/WordPress framework — /read/<slug>
        # URL pattern and chapter_selectors unchanged. www.webtoonxyz.co 301s to apex.
        # Keep old webtoon.xyz hostnames in extra_domains so cached/bookmarked URLs
        # still route to this handler via BaseSiteHandler.matches().
        super().__init__(
            "WebtoonXYZ",
            "https://webtoonxyz.co",
            extra_domains=("webtoon.xyz", "www.webtoon.xyz"),
        )

    def _slug_from_url(self, url: str) -> str:
        # Override to handle "read" instead of "manga" if needed,
        # but the base implementation handles generic paths well.
        # However, Kotlin says: override val mangaSubString = "read"
        # Base Madara uses "manga" by default for some logic, but _slug_from_url
        # just takes the last part or second to last if "manga".
        # Let's check if we need to handle "read" specifically.
        # URL: https://www.webtoon.xyz/read/series-slug/
        
        from urllib.parse import urlparse
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            return parsed.netloc
        if parts[0] == "read" and len(parts) >= 2:
            return parts[1]
        return parts[-1]

    def _extract_cover(self, soup: BeautifulSoup, page_url: str) -> Optional[str]:
        cover = super()._extract_cover(soup, page_url)
        if cover:
            # Kotlin: thumbnail_url = manga.thumbnail_url?.replace(thumbnailOriginalUrlRegex, "$1")
            # Regex: -\d+x\d+(\.[a-zA-Z]+)$ -> $1
            # Example: image-300x400.jpg -> image.jpg
            cover = re.sub(r"-\d+x\d+(\.[a-zA-Z]+)$", r"\1", cover)
        return cover
