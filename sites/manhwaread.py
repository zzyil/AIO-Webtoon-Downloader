from __future__ import annotations

import base64
import json
import re
from typing import Dict, List

from .madara import MadaraSiteHandler


class ManhwaReadHandler(MadaraSiteHandler):
    chapter_selectors = (
        "li.wp-manga-chapter",
        "div.chapter-list a",
        "ul.main.version-chap li",
        "div.flex.flex-col a[href*='chapter-']",
    )

    def __init__(self) -> None:
        super().__init__("manhwaread", "https://manhwaread.com")

    def fetch_comic_context(self, url: str, scraper, make_request):
        context = super().fetch_comic_context(url, scraper, make_request)
        
        # Refine title if fallback slug was used
        if context.title == context.identifier and context.soup:
            title_node = context.soup.select_one("h1.text-3xl.text-primary, h1.clipboard-copy")
            if title_node:
                context.title = title_node.get_text(strip=True)
                context.comic["title"] = context.title
                
        return context

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        chapter_url = chapter.get("url")
        if not chapter_url:
            raise RuntimeError("Chapter URL missing.")

        # Use the base handler's logic to fetch HTML (handles Cloudflare)
        if self.use_zendriver:
            from .crawlee_utils import fetch_html_with_cf_cookies
            html = fetch_html_with_cf_cookies(chapter_url, base_url=self.base_url)
        else:
            response = make_request(chapter_url, scraper)
            html = response.text
            # If Cloudflare blocked us, try CF cookie capture
            if (
                response.status_code in (403, 429, 503)
                or len(html) < 2000
                or "just a moment" in html.lower()
                or "checking your browser" in html.lower()
            ):
                try:
                    from .crawlee_utils import fetch_html_with_cf_cookies, ZENDRIVER_AVAILABLE
                    if ZENDRIVER_AVAILABLE:
                        html = fetch_html_with_cf_cookies(chapter_url, base_url=self.base_url)
                except Exception:
                    pass

        # Look for window.chapterData or chapterData
        # Example: chapterData = {"data":"...","base":"..."};
        match = re.search(r'chapterData\s*=\s*({.*?});', html, re.DOTALL)
        if not match:
            # Fallback to standard Madara if the script is not found
            # We recreate the soup to avoid re-fetching
            soup = self._make_soup(html)
            image_urls: List[str] = []
            for selector in self.reader_selectors:
                for img in soup.select(selector):
                    src = (
                        img.get("data-src")
                        or img.get("data-srcset")
                        or img.get("data-cfsrc")
                        or img.get("src")
                    )
                    if not src:
                        continue
                    src = src.strip()
                    if src.startswith("//"):
                        src = "https:" + src
                    if src not in image_urls:
                        image_urls.append(src)
                if image_urls:
                    break
            if image_urls:
                return image_urls
            raise RuntimeError("Unable to locate images for chapter (chapterData not found and standard selectors failed).")

        try:
            data_json = json.loads(match.group(1))
            encoded_data = data_json.get("data")
            base_url = data_json.get("base")

            if not encoded_data or not base_url:
                # Same fallback as above
                raise ValueError("Missing data or base in chapterData")

            # Decode base64 with padding fix
            padding = len(encoded_data) % 4
            if padding:
                encoded_data += "=" * (4 - padding)
            decoded_bytes = base64.b64decode(encoded_data)
            images_list = json.loads(decoded_bytes)

            image_urls = []
            for item in images_list:
                src = item.get("src")
                if src:
                    # Clean up escaped slashes if any
                    src = src.replace("\\/", "/")
                    # Construct absolute URL
                    full_url = f"{base_url.rstrip('/')}/{src.lstrip('/')}"
                    image_urls.append(full_url)

            if not image_urls:
                raise ValueError("No images found in decoded chapterData")

            return image_urls
        except Exception as e:
            # One last try with standard selectors if JSON parsing failed
            soup = self._make_soup(html)
            image_urls = []
            for selector in self.reader_selectors:
                for img in soup.select(selector):
                    src = img.get("data-src") or img.get("src")
                    if src:
                        image_urls.append(src.strip())
                if image_urls:
                    return image_urls
            raise RuntimeError(f"Failed to parse chapterData and fallback failed: {e}")
