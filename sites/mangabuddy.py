from __future__ import annotations
from typing import Sequence
import re
from bs4 import BeautifulSoup

from .madara import MadaraSiteHandler
from .base import SiteComicContext


_MANGABUDDY_MIRRORS = (
    "mangacute.com",
    "mangaforest.com",
    "mangamirror.com",
    "mangapuma.com",
    "mangaxyz.com",
    "truemanga.com",
)


def _expand_domains(domains):
    expanded = set()
    for domain in domains:
        expanded.add(domain)
        if not domain.startswith("www."):
            expanded.add(f"www.{domain}")
    return tuple(sorted(expanded))


class MangaBuddySiteHandler(MadaraSiteHandler):
    def __init__(self) -> None:
        extra_domains = _expand_domains(_MANGABUDDY_MIRRORS)
        super().__init__("mangabuddy", "https://mangabuddy.com", extra_domains=extra_domains)
        
        # Override chapter selectors for Mangabuddy
        self.chapter_selectors = (
            "#chapter-list li",
            "ul.chapter-list li",
            "div#chapter-list li",
            # Keep defaults just in case
            "li.wp-manga-chapter",
            "div#chapterlist li",
        )
        
    def get_chapter_images(self, chapter: dict, scraper, make_request) -> list[str]:
        """
        Extract chapter images from MangaBuddy.
        Prioritizes extracting from 'var chapImages' JavaScript variable.
        """
        chapter_url = chapter["url"]
        response = make_request(chapter_url, scraper)
        
        # Try to find the 'var chapImages' script
        script_pattern = re.compile(r"var\s+chapImages\s*=\s*['\"](.*?)['\"]", re.DOTALL)
        script_match = script_pattern.search(response.text)
        
        if script_match:
            images_str = script_match.group(1)
            if images_str:
                return [url.strip() for url in images_str.split(",") if url.strip()]
        
        # Fallback to default selector-based extraction
        return super().get_chapter_images(chapter, scraper, make_request)

        # Override reader selectors
        self.reader_selectors = (
            "div#chapter-images div.chapter-image img",
            "#chapter-images img",
            "div#chapter-images img",
            "div.reading-content img",
        )

    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        response = make_request(url, scraper)
        soup = self._make_soup(response.text)
        
        # Title: <div class="name box"><h1>...</h1></div>
        title_node = soup.select_one("div.name.box h1")
        title = title_node.get_text(strip=True) if title_node else self._slug_from_url(url)
        
        # Cover: <div class="img-cover"><img data-src="..."></div>
        cover_url = None
        cover_node = soup.select_one("div.img-cover img")
        if cover_node:
            cover_url = cover_node.get("data-src") or cover_node.get("src")
        
        if not cover_url:
            # Fallback to og:image
            og_image = soup.find("meta", property="og:image")
            if og_image:
                cover_url = og_image.get("content")

        # Authors: <div class="author-content"><a>...</a></div>
        authors = self._extract_people(soup, ("author-content",))
        
        # Genres: <p><strong>Genres :</strong> <a ...>Action</a> ...</p>
        genres = []
        # Find the paragraph containing "Genres :"
        genre_label = soup.find(string=lambda t: t and "Genres" in t)
        if genre_label and genre_label.parent:
            # The label might be in a <strong> inside the <p>
            container = genre_label.parent.parent if genre_label.parent.name == "strong" else genre_label.parent
            if container:
                genres = [a.get_text(strip=True) for a in container.find_all("a")]

        # Status: <div class="status-label">...</div>
        status = None
        status_node = soup.select_one("div.status-label")
        if status_node:
            status = status_node.get_text(strip=True)

        # Description: <div class="manga-summary"><p>...</p></div>
        desc = None
        desc_node = soup.select_one("div.manga-summary p")
        if desc_node:
            desc = desc_node.get_text("\n", strip=True)

        comic = {
            "hid": self._slug_from_url(url),
            "title": title,
            "desc": desc,
            "cover": cover_url,
            "url": url,
            "authors": authors,
            "genres": genres,
            "status": status,
            "language": "en",
        }
        
        return SiteComicContext(comic=comic, title=title, identifier=comic["hid"], soup=soup)


__all__ = ["MangaBuddySiteHandler"]
