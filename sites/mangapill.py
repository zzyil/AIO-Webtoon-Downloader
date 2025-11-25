from __future__ import annotations

from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BaseSiteHandler, SiteComicContext


class MangaPillSiteHandler(BaseSiteHandler):
    name = "mangapill"
    domains = ("mangapill.com", "www.mangapill.com")
    
    _BASE_URL = "https://mangapill.com"

    def configure_session(self, scraper, args) -> None:
        scraper.headers.update(
            {
                "Referer": "https://mangapill.com/",
            }
        )

    # -- Helpers -----------------------------------------------------
    def _make_soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def _slug_from_url(self, url: str) -> str:
        # URL: https://mangapill.com/manga/123/title-slug
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        
        if len(path_parts) >= 2 and path_parts[0] == "manga":
            return path_parts[1] # Return ID as slug/identifier
        return path_parts[-1]

    # -- Base overrides ----------------------------------------------
    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        response = make_request(url, scraper)
        soup = self._make_soup(response.text)
        
        # Title: div.container > div:first-child > div:last-child > div:nth-child(2) > h1
        # Or just h1
        title = soup.select_one("h1")
        title = title.get_text(strip=True) if title else "Unknown"
        
        # Description: div.container > div:first-child > div:last-child > div:nth-child(2) > p
        desc_node = soup.select_one("div.container > div:first-child > div:last-child > div:nth-child(2) > p")
        description = desc_node.get_text(strip=True) if desc_node else ""
        
        # Cover: div.container > div:first-child > div:first-child > img
        cover_node = soup.select_one("div.container > div:first-child > div:first-child > img")
        cover = cover_node.get("data-src") or cover_node.get("src") if cover_node else None
        
        # Genres: a[href*=genre]
        genres = [a.get_text(strip=True) for a in soup.select("a[href*=genre]")]
        
        # Status
        status_node = soup.select_one("div.container > div:first-child > div:last-child > div:nth-child(3) > div:nth-child(2) > div")
        status = status_node.get_text(strip=True) if status_node else "Unknown"
        
        slug = self._slug_from_url(url)
        
        comic = {
            "hid": slug,
            "title": title,
            "desc": description,
            "status": status,
            "cover": cover,
            "genres": genres,
            "url": url,
        }

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
            response = make_request(context.comic.get("url"), scraper)
            soup = self._make_soup(response.text)
            
        chapters = []
        # Selector: #chapters > div > a
        for link in soup.select("#chapters > div > a"):
            href = link.get("href")
            if not href:
                continue
                
            title = link.get_text(strip=True)
            url = urljoin(self._BASE_URL, href)
            
            # Extract chapter number from title or URL
            # Title example: Chapter 123
            chap_num = title
            
            chapters.append({
                "hid": href,
                "chap": chap_num,
                "title": title,
                "url": url,
                "uploaded": None, # No date in list
            })
            
        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        url = chapter.get("url")
        response = make_request(url, scraper)
        soup = self._make_soup(response.text)
        
        images = []
        # Selector: picture img
        for img in soup.select("picture img"):
            src = img.get("data-src") or img.get("src")
            if src:
                images.append(src)
                
        return images
