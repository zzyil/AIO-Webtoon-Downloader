from __future__ import annotations

import re
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BaseSiteHandler, SiteComicContext


class TCBScansSiteHandler(BaseSiteHandler):
    name = "tcbscans"
    domains = ("tcbonepiecechapters.com", "www.tcbonepiecechapters.com", "tcbscans.com", "www.tcbscans.com")

    def configure_session(self, scraper, args) -> None:
        # TCB Scans often requires standard headers
        pass

    # -- Helpers -----------------------------------------------------
    def _fetch_html(self, url: str, scraper, make_request) -> str:
        response = make_request(url, scraper)
        response.encoding = response.encoding or "utf-8"
        return response.text

    def _make_soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    # -- Base overrides ----------------------------------------------
    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        html = self._fetch_html(url, scraper, make_request)
        soup = self._make_soup(html)

        # Selector: div.order-1
        # Title: h1
        # Desc: p
        # Thumb: img src
        
        info_div = soup.select_one("div.order-1")
        if not info_div:
            # Fallback or error
            # Try to find any h1
            title = soup.select_one("h1")
            title = title.get_text(strip=True) if title else "Unknown"
        else:
            title_node = info_div.select_one("h1")
            title = title_node.get_text(strip=True) if title_node else "Unknown"
            
        slug = self._slug_from_url(url)
        
        comic = {
            "hid": slug,
            "title": title,
            "url": url,
        }
        
        if info_div:
            desc_node = info_div.select_one("p")
            if desc_node:
                comic["desc"] = desc_node.get_text(strip=True)
            
            img_node = info_div.select_one("img")
            if img_node:
                src = img_node.get("src")
                if src:
                    comic["cover"] = urljoin(url, src)

        return SiteComicContext(
            comic=comic,
            title=title,
            identifier=slug,
            soup=soup,
        )

    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        soup = context.soup
        if not soup:
             html = self._fetch_html(context.comic["url"], scraper, make_request)
             soup = self._make_soup(html)
             
        # Selector: div.grid a
        chapter_links = soup.select("div.grid a")
        
        chapters = []
        for link in chapter_links:
            href = link.get("href")
            if not href:
                continue
            
            url = urljoin(context.comic["url"], href)
            
            # Title extraction
            # Kotlin: element.select("div.font-bold:not(.flex)").text()
            title_node = link.select_one("div.font-bold:not(.flex)")
            raw_title = title_node.get_text(strip=True) if title_node else ""
            
            # Description/Chapter Name
            # Kotlin: element.selectFirst(".text-gray-500")
            desc_node = link.select_one(".text-gray-500")
            desc = desc_node.get_text(strip=True) if desc_node else ""
            
            # Parse chapter number
            # Regex: \d+.?\d+$
            match = re.search(r"(\d+(?:\.\d+)?)", raw_title)
            chap_num = match.group(1) if match else "0"
            
            # Construct full title
            full_title = raw_title
            if desc:
                full_title += f": {desc}"
                
            chapters.append({
                "hid": url, # Use URL as ID
                "chap": chap_num,
                "title": full_title,
                "url": url,
            })
            
        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        url = chapter["url"]
        html = self._fetch_html(url, scraper, make_request)
        soup = self._make_soup(html)
        
        # Selector: picture img, .image-container img
        images = soup.select("picture img, .image-container img")
        
        image_urls = []
        for img in images:
            src = img.get("src")
            if src:
                image_urls.append(urljoin(url, src))
                
        return image_urls

    def _slug_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        return parts[-1] if parts else "unknown"
