from __future__ import annotations

import json
from typing import Dict, List, Optional
from urllib.parse import urlparse

from .base import BaseSiteHandler, SiteComicContext


class ZeroScansSiteHandler(BaseSiteHandler):
    name = "zeroscans"
    domains = ("zscans.com", "www.zscans.com")
    
    API_BASE = "https://zscans.com/swordflake"

    def configure_session(self, scraper, args) -> None:
        scraper.headers.update(
            {
                "Referer": "https://zscans.com/",
                "Origin": "https://zscans.com",
            }
        )

    # -- Helpers -----------------------------------------------------
    def _fetch_json(self, url: str, scraper) -> Dict:
        response = scraper.get(url)
        response.raise_for_status()
        return response.json()

    # -- Base overrides ----------------------------------------------
    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        # URL: https://zscans.com/comics/{slug}
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        
        slug = None
        if len(path_parts) >= 2 and path_parts[0] == "comics":
            slug = path_parts[1]
        else:
            # Fallback: try to extract from end
            slug = path_parts[-1]
            
        if not slug:
            raise RuntimeError(f"Could not extract slug from URL: {url}")
            
        # We need to find the comic in the full list because the API doesn't seem to have a direct details endpoint by slug?
        # Kotlin: comicList.first { comic -> comic.slug == mangaSlug }
        # It fetches ALL comics to find one. That's heavy but that's what the extension does.
        # Let's try to see if there is a better way or just do that.
        # API: https://zscans.com/swordflake/comics
        
        comics_data = self._fetch_json(f"{self.API_BASE}/comics", scraper)
        all_comics = comics_data.get("data", {}).get("comics", [])
        
        comic_data = next((c for c in all_comics if c.get("slug") == slug), None)
        
        if not comic_data:
            raise RuntimeError(f"Comic not found: {slug}")
            
        title = comic_data.get("name")
        comic_id = comic_data.get("id")
        
        comic = {
            "hid": slug,
            "title": title,
            "desc": comic_data.get("summary"),
            "status": comic_data.get("status"),
            "cover": comic_data.get("cover", {}).get("vertical"),
            "genres": [g.get("name") for g in comic_data.get("genres", [])],
            "_comic_id": comic_id,
        }

        return SiteComicContext(
            comic=comic,
            title=title,
            identifier=slug,
            soup=None,
        )

    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        comic_id = context.comic.get("_comic_id")
        slug = context.identifier
        
        if not comic_id:
            # Re-fetch if missing (shouldn't happen)
            return []
            
        # API: https://zscans.com/swordflake/comic/{id}/chapters?sort=desc&page={page}
        chapters = []
        page = 1
        has_more = True
        
        while has_more:
            url = f"{self.API_BASE}/comic/{comic_id}/chapters?sort=desc&page={page}"
            data = self._fetch_json(url, scraper)
            
            chap_data = data.get("data", {})
            current_chaps = chap_data.get("data", [])
            
            for chap in current_chaps:
                chap_id = chap.get("id")
                name = chap.get("name") # "123"
                created_at = chap.get("created_at")
                
                # Virtual URL: /comics/{slug}/{id}
                chap_url = f"https://zscans.com/comics/{slug}/{chap_id}"
                
                chapters.append({
                    "hid": str(chap_id),
                    "chap": str(name),
                    "title": f"Chapter {name}",
                    "url": chap_url,
                    "uploaded": created_at,
                    "_chapter_id": chap_id,
                })
                
            current_page = chap_data.get("current_page")
            last_page = chap_data.get("last_page")
            has_more = current_page < last_page
            page += 1
            
        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        # API: https://zscans.com/swordflake/comic/{slug}/chapters/{id}
        # Wait, Kotlin says: GET("$baseUrl/$API_PATH/comic/$mangaSlug/chapters/$chapterId")
        # So it uses SLUG here, not ID?
        # Let's check `pageListRequest` in Kotlin.
        # val mangaSlug = chapterUrlPaths[1]
        # val chapterId = chapterUrlPaths[2]
        # return GET("$baseUrl/$API_PATH/comic/$mangaSlug/chapters/$chapterId")
        # Yes, it uses slug and chapter ID.
        
        # We need the slug. It's in the virtual URL we constructed or context.
        # But `get_chapter_images` only gets `chapter` dict.
        # We can extract it from the URL we built: https://zscans.com/comics/{slug}/{id}
        
        url = chapter.get("url")
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        # parts: comics, slug, id
        
        if len(path_parts) < 3:
             raise RuntimeError(f"Invalid chapter URL: {url}")
             
        slug = path_parts[1]
        chap_id = path_parts[2]
        
        api_url = f"{self.API_BASE}/comic/{slug}/chapters/{chap_id}"
        data = self._fetch_json(api_url, scraper)
        
        chap_detail = data.get("data", {}).get("chapter", {})
        
        # Kotlin: highQuality.takeIf { it.isNotEmpty() } ?: goodQuality
        high_quality = chap_detail.get("high_quality", [])
        good_quality = chap_detail.get("good_quality", [])
        
        images = high_quality if high_quality else good_quality
        
        return images
