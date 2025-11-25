from __future__ import annotations

import json
from typing import Dict, List, Optional
from urllib.parse import urlparse

from .base import BaseSiteHandler, SiteComicContext


class VoyceMeSiteHandler(BaseSiteHandler):
    name = "voyceme"
    domains = ("voyce.me", "www.voyce.me")

    GRAPHQL_URL = "https://graphql.voyce.me/v1/graphql"
    STATIC_URL = "https://dlkfxmdtxtzpb.cloudfront.net/"

    # Queries extracted from VoyceMeQueries.kt
    DETAILS_QUERY = """
        query($slug: String!) {
            voyce_series(
                where: {
                    publish: { _eq: 1 },
                    type: { id: { _in: [2, 4] } },
                    slug: { _eq: $slug }
                },
                limit: 1,
            ) {
                id
                slug
                thumbnail
                title
                description
                status
                author { username }
                genres(order_by: [{ genre: { title: asc } }]) {
                    genre { title }
                }
            }
        }
    """

    CHAPTERS_QUERY = """
        query($slug: String!) {
            voyce_series(
                where: {
                    publish: { _eq: 1 },
                    type: { id: { _in: [2, 4] } },
                    slug: { _eq: $slug }
                },
                limit: 1,
            ) {
                slug
                chapters(order_by: [{ created_at: desc }]) {
                    id
                    title
                    created_at
                }
            }
        }
    """

    PAGES_QUERY = """
        query($chapterId: Int!) {
            voyce_chapter_images(
                where: { chapter_id: { _eq: $chapterId } },
                order_by: { sort_order: asc }
            ) {
                image
            }
        }
    """

    def configure_session(self, scraper, args) -> None:
        scraper.headers.update(
            {
                "Origin": "https://www.voyce.me",
                "Referer": "https://www.voyce.me/",
                "Accept": "*/*",
                "Content-Type": "application/json",
            }
        )

    def _post_graphql(self, query: str, variables: Dict, scraper) -> Dict:
        payload = {"query": query, "variables": variables}
        response = scraper.post(self.GRAPHQL_URL, json=payload)
        response.raise_for_status()
        return response.json()

    # -- Base overrides ----------------------------------------------
    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        # URL: https://www.voyce.me/series/{slug}
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        
        if len(path_parts) < 2 or path_parts[0] != "series":
             # Try to extract slug from end if format is different
             slug = path_parts[-1] if path_parts else "unknown"
        else:
             slug = path_parts[1]
             
        data = self._post_graphql(self.DETAILS_QUERY, {"slug": slug}, scraper)
        
        series_list = data.get("data", {}).get("voyce_series", [])
        if not series_list:
            raise RuntimeError(f"Series not found for slug: {slug}")
            
        series = series_list[0]
        
        title = series.get("title")
        desc = series.get("description")
        
        authors = []
        author_data = series.get("author")
        if author_data:
            authors.append(author_data.get("username"))
            
        genres = []
        for g in series.get("genres", []):
            genre_title = g.get("genre", {}).get("title")
            if genre_title:
                genres.append(genre_title)
                
        thumb = series.get("thumbnail")
        cover = self.STATIC_URL + thumb if thumb else None
        
        comic = {
            "hid": slug,
            "title": title,
            "desc": desc,
            "authors": authors,
            "genres": genres,
            "cover": cover,
            "status": series.get("status"),
            "_slug": slug,
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
        slug = context.comic.get("_slug") or context.identifier
        
        data = self._post_graphql(self.CHAPTERS_QUERY, {"slug": slug}, scraper)
        
        series_list = data.get("data", {}).get("voyce_series", [])
        if not series_list:
             return []
             
        chapters_data = series_list[0].get("chapters", [])
        
        chapters = []
        for chap in chapters_data:
            chap_id = chap.get("id")
            title = chap.get("title")
            created_at = chap.get("created_at")
            
            # Title usually contains "Chapter X" or just the title
            # We can try to parse it or just use it as is.
            # Kotlin: distinctBy(SChapter::name)
            
            chapters.append({
                "hid": str(chap_id),
                "chap": str(chap_id), # Use ID as chapter number/ID
                "title": title,
                "url": f"https://www.voyce.me/series/{slug}/chapter/{chap_id}", # Virtual URL
                "uploaded": created_at,
                "_chapter_id": chap_id,
            })
            
        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        chap_id = chapter.get("_chapter_id")
        if not chap_id:
             # Try to extract from URL if _chapter_id is missing
             # URL: .../chapter/{id}
             url = chapter.get("url")
             if url:
                 chap_id = int(url.split("/")[-1])
        
        if not chap_id:
            raise RuntimeError("Chapter ID missing.")
            
        data = self._post_graphql(self.PAGES_QUERY, {"chapterId": chap_id}, scraper)
        
        images_data = data.get("data", {}).get("voyce_chapter_images", [])
        
        image_urls = []
        for img in images_data:
            path = img.get("image")
            if path:
                image_urls.append(self.STATIC_URL + path)
                
        return image_urls
