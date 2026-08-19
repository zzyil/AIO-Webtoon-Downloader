from __future__ import annotations

import base64
import json
import re
from typing import Dict, List, Optional
from urllib.parse import urljoin

from .madara import MadaraSiteHandler, _MadaraChapter


class ManhwaReadHandler(MadaraSiteHandler):
    # Keep legacy selectors as a fallback for the parent's _collect_chapter_elements
    chapter_selectors = (
        "li.wp-manga-chapter",
        "ul.main.version-chap li",
    )

    reader_selectors = (
        "div.reading-content img",
        "div#chapter-images img",
        "div.page-break img",
        "div#chapter-content img",
        "#chapter-content img",
    )

    def __init__(self) -> None:
        super().__init__(
            "manhwaread",
            "https://manhwaread.com",
            extra_domains=("mgread.io", "www.mgread.io")
        )


    # -------------------------------------------------------------- chapters
    def _collect_chapter_elements(self, soup) -> List[_MadaraChapter]:
        """Override: manhwaread.com uses `<a class="chapter-item">` inside
        `#chaptersList` instead of the standard Madara `<li>` layout.
        The chapter name lives in a child `.chapter-item__name` span and
        the date in `.chapter-item__date`.
        """
        chapters: List[_MadaraChapter] = []

        # Primary: site-specific selectors scoped to the chapter list container
        # to avoid picking up recommendation links that also use .chapter-item.
        for selector in (
            "div#chaptersList a.chapter-item",
            "div.chapters-list a.chapter-item",
        ):
            for link in soup.select(selector):
                href = link.get("href")
                if not href:
                    continue
                href = urljoin(self.base_url, href)

                # Prefer the dedicated name span; fall back to full link text
                name_node = link.select_one(".chapter-item__name")
                title = (
                    name_node.get_text(strip=True)
                    if name_node
                    else link.get_text(" ", strip=True)
                )

                date_node = link.select_one(".chapter-item__date")
                date_text = date_node.get_text(strip=True) if date_node else None

                chapters.append(
                    _MadaraChapter(url=href, title=title, date_text=date_text)
                )
            if chapters:
                return chapters

        # Fallback: delegate to the parent's generic Madara logic
        return super()._collect_chapter_elements(soup)

    # -------------------------------------------------------------- context
    def fetch_comic_context(self, url: str, scraper, make_request):
        context = super().fetch_comic_context(url, scraper, make_request)

        # Refine title if fallback slug was used
        if context.title == context.identifier and context.soup:
            title_node = context.soup.select_one("h1.text-3xl.text-primary, h1.clipboard-copy")
            if title_node:
                context.title = title_node.get_text(strip=True)
                context.comic["title"] = context.title

        return context

    # -------------------------------------------------------------- images
    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        chapter_url = chapter.get("url")
        if not chapter_url:
            raise RuntimeError("Chapter URL missing.")

        # Use the base handler's logic to fetch HTML (handles Cloudflare)
        if self.use_zendriver:
            from .crawlee_utils import rescue_cf_html
            html = rescue_cf_html(chapter_url, base_url=self.base_url, scraper=scraper)
        else:
            response = make_request(chapter_url, scraper)
            html = response.text
            # If Cloudflare blocked us, try CF cookie capture.
            #
            # The trigger keeps this handler's own heuristic (a short body is a
            # blocked body on this site) OR'd with the shared detector, which
            # adds the 200-with-interstitial case the length test misses. The
            # GATE is what was broken: it read `if ZENDRIVER_AVAILABLE`, so on
            # Android the rescue silently evaporated and the interstitial was
            # parsed for images — same defect as sites/madara.py:_fetch_html,
            # which this method deliberately mirrors.
            try:
                from .crawlee_utils import (
                    cf_solver_available,
                    is_cf_challenge,
                    rescue_cf_html,
                    warn_cf_no_solver,
                    warn_cf_rescue,
                )

                if (
                    is_cf_challenge(response.status_code, html)
                    or response.status_code in (403, 429, 503)
                    or len(html) < 2000
                    or "just a moment" in html.lower()
                    or "checking your browser" in html.lower()
                ):
                    if cf_solver_available():
                        try:
                            html = rescue_cf_html(
                                chapter_url, base_url=self.base_url, scraper=scraper
                            )
                        except Exception as exc:
                            warn_cf_rescue(chapter_url, f"rescue failed: {exc}")
                    else:
                        warn_cf_no_solver(chapter_url)
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
