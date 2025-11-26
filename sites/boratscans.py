from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .madara import MadaraSiteHandler


class BoratScansSiteHandler(MadaraSiteHandler):
    """
    Dedicated handler for boratscans.com.
    
    Boratscans uses WordPress and serves multiple image sizes with dimensions
    appended to filenames (e.g., cover-300x420.jpg for thumbnails).
    This handler strips those suffixes to get full-size original images.
    """
    
    def __init__(self):
        super().__init__(
            site_name="boratscans",
            base_url="https://boratscans.com",
            extra_domains=None
        )
    
    def _extract_cover(self, soup: BeautifulSoup, page_url: str) -> Optional[str]:
        """
        Override to remove WordPress size suffixes from cover URLs.
        
        WordPress appends dimensions like -300x420.jpg to resized images.
        We strip these to get the full-size original.
        """
        cover = soup.select_one(".summary_image img, .img-responsive, div.thumb img")
        if not cover:
            return None
        src = cover.get("data-src") or cover.get("data-lazy-src") or cover.get("src")
        if not src:
            return None
        src = src.strip()
        
        # Remove WordPress size suffixes (e.g., -300x420.jpg -> .jpg)
        # This ensures we get the full-size original image instead of a thumbnail
        src = re.sub(r'-\d+x\d+(\.(jpg|jpeg|png|webp|gif))$', r'\1', src, flags=re.IGNORECASE)
        
        if src.startswith("//"):
            return "https:" + src
        if src.startswith("/"):
            return urljoin(page_url, src)
        if src.startswith("http"):
            return src
        return urljoin(page_url, src)
    
    def fetch_comic_context(self, url, scraper, make_request):
        """
        Override to extract cover from JSON-LD structured data.
        
        Boratscans includes structured data in a <script type="application/ld+json"> tag
        which contains the correct portrait/book-style cover image.
        """
        # Call parent implementation
        context = super().fetch_comic_context(url, scraper, make_request)
        
        # Look for JSON-LD structured data
        json_ld_script = context.soup.find("script", type="application/ld+json")
        if json_ld_script and json_ld_script.string:
            try:
                import json
                json_data = json.loads(json_ld_script.string)
                
                # Extract image URL from JSON-LD
                # Format: "image": {"@type": "ImageObject", "url": "...", "height": 391, "width": 696}
                if isinstance(json_data.get("image"), dict):
                    image_url = json_data["image"].get("url")
                    if image_url:
                        # Strip WordPress size suffixes as a safety measure
                        image_url = re.sub(r'-\d+x\d+(\.(jpg|jpeg|png|webp|gif))$', r'\1', image_url, flags=re.IGNORECASE)
                        context.comic["cover"] = image_url
                        return context
            except (json.JSONDecodeError, KeyError):
                pass
        
        # Fallback: try og:image with suffix stripping
        cover = context.comic.get("cover")
        if not cover or re.search(r'-\d+x\d+\.(jpg|jpeg|png|webp|gif)$', cover, re.IGNORECASE):
            og_image = context.soup.find("meta", property="og:image")
            if og_image and og_image.get("content"):
                og_url = og_image["content"].strip()
                og_url = re.sub(r'-\d+x\d+(\.(jpg|jpeg|png|webp|gif))$', r'\1', og_url, flags=re.IGNORECASE)
                context.comic["cover"] = og_url
        
        return context


__all__ = ["BoratScansSiteHandler"]
