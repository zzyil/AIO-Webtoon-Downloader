from __future__ import annotations

import json
import re
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BaseSiteHandler, SearchHit, SiteComicContext
from .hardening import configure_throttling


class FlameComicsSiteHandler(BaseSiteHandler):
    name = "flamecomics"
    domains = ("flamecomics.xyz", "www.flamecomics.xyz")

    def configure_session(self, scraper, args) -> None:
        if "Referer" not in scraper.headers:
            scraper.headers.update(
                {
                    "Referer": "https://flamecomics.xyz/",
                    "Origin": "https://flamecomics.xyz",
                }
            )
        configure_throttling(
            scraper,
            domains=self.domains,
            gaps={"default": 1.25, "ajax": 1.50, "page": 2.00, "image": 1.0},
            jitter=0.5,
            max_retries=3,
        )

    # -- Helpers -----------------------------------------------------
    def _fetch_html(self, url: str, scraper, make_request) -> str:
        response = make_request(url, scraper)
        response.encoding = response.encoding or "utf-8"
        return response.text

    @staticmethod
    def _strip_html(value: Optional[str]) -> Optional[str]:
        """Strip HTML tags from a Mantine-framework-wrapped string.

        FlameComics's Next.js API returns the description with the Mantine
        UI wrapper baked in (e.g. `<p class="mantine-focus-auto m_b6d8b162
        mantine-Text-root">10 years ago...</p>`). Before this strip the raw
        HTML leaked into Komikku's details.json `description` field. Mirrors
        the same approach used in sites/dynasty.py:62-67 (`_clean_description`)
        and sites/mangathemesia.py:591-596 (`_clean_wp_text`). See
        dry_run_komikku_findings.md §A.
        """
        if not value:
            return None
        cleaned = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
        return cleaned or None

    def _fetch_build_id(self, scraper, make_request) -> str:
        """
        Fetches the current build ID from the homepage.
        """
        html = self._fetch_html("https://flamecomics.xyz", scraper, make_request)
        soup = BeautifulSoup(html, "html.parser")
        next_data = soup.select_one("script#__NEXT_DATA__")
        if not next_data:
            raise RuntimeError("Could not find __NEXT_DATA__ on FlameComics homepage.")
        
        try:
            data = json.loads(next_data.get_text())
            return data["buildId"]
        except (json.JSONDecodeError, KeyError):
            raise RuntimeError("Could not parse buildId from __NEXT_DATA__.")

    def _get_data_api_url(self, path: str, build_id: str) -> str:
        return f"https://flamecomics.xyz/_next/data/{build_id}/{path}.json"

    def _fetch_json_with_retry(self, url: str, scraper, make_request, build_id_ref: List[str]) -> Dict:
        """
        Fetches JSON from the Next.js data API. If it returns 404, it refreshes the build ID and retries.
        """
        try:
            response = scraper.get(url)
            if response.status_code == 404:
                # Build ID might be outdated
                new_build_id = self._fetch_build_id(scraper, make_request)
                build_id_ref[0] = new_build_id
                # Reconstruct URL with new build ID
                # URL format: .../_next/data/{OLD_ID}/{PATH}.json
                # We need to replace {OLD_ID} with {NEW_ID}
                parts = url.split("/_next/data/")
                if len(parts) == 2:
                    base = parts[0]
                    rest = parts[1].split("/", 1)[1]
                    new_url = f"{base}/_next/data/{new_build_id}/{rest}"
                    response = scraper.get(new_url)
            
            response.raise_for_status()
            return response.json()
        except Exception as e:
            raise RuntimeError(f"Failed to fetch data from {url}: {e}")

    # -- Base overrides ----------------------------------------------
    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        # URL format: https://flamecomics.xyz/series/{id}-{slug} or just {id}
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        
        series_id = None
        if len(path_parts) >= 2 and path_parts[0] == "series":
            # Try to extract numeric ID from the start of the second part
            # It could be "117" or "117-slug"
            match = re.match(r"^(\d+)", path_parts[1])
            if match:
                series_id = match.group(1)
        
        if not series_id:
             # Fallback: try to fetch HTML and extract ID from there if URL is weird
             # But standard URLs are /series/123-slug
             html = self._fetch_html(url, scraper, make_request)
             soup = BeautifulSoup(html, "html.parser")
             next_data = soup.select_one("script#__NEXT_DATA__")
             if next_data:
                 try:
                     data = json.loads(next_data.get_text())
                     series_data = data.get("pageProps", {}).get("series", {})
                     if series_data:
                         series_id = str(series_data.get("series_id"))
                 except:
                     pass
        
        if not series_id:
            raise RuntimeError(f"Could not extract series ID from URL: {url}")

        # We need the build ID to use the data API
        build_id = self._fetch_build_id(scraper, make_request)
        build_id_ref = [build_id]

        # API URL: https://flamecomics.xyz/_next/data/{build_id}/series/{id}.json?id={id}
        api_url = self._get_data_api_url(f"series/{series_id}", build_id)
        api_url += f"?id={series_id}"
        
        data = self._fetch_json_with_retry(api_url, scraper, make_request, build_id_ref)
        
        page_props = data.get("pageProps", {})
        series_data = page_props.get("series", {})
        
        if not series_data:
            raise RuntimeError("Could not find series data in API response.")

        title = series_data.get("title")
        slug = str(series_data.get("series_id"))
        
        comic = {
            "hid": slug,
            "title": title,
            # `description` arrives Mantine-wrapped; strip the HTML so
            # Komikku's details.json doesn't surface raw `<p class="mantine-x">`
            # markup. See _strip_html docstring above.
            "desc": self._strip_html(series_data.get("description")),
            "status": series_data.get("status"),
            "alt_names": series_data.get("altTitles", []),
            "authors": series_data.get("author", []),
            "artists": series_data.get("artist", []),
            # NOTE: `series_data.get("tags", [])` is server-side empty for at
            # least some FlameComics series (verified 2026-05-19 dry-run for
            # Solo Leveling). Accepted as a known limitation per user direction
            # — Komikku's `genre` array will be empty for affected titles. The
            # other 4 fields (desc, authors, artists, status) populate normally.
            "genres": [t.get("name") for t in series_data.get("tags", []) if isinstance(t, dict) and t.get("name")],
            "cover": f"https://cdn.flamecomics.xyz/uploads/images/series/{slug}/{series_data.get('cover')}",
            "_series_data": series_data, # Cache for get_chapters
            "_build_id": build_id_ref[0],
            "_page_props": page_props, # Cache full props including chapters
        }

        year_raw = series_data.get("year")
        if isinstance(year_raw, int) and year_raw > 0:
            comic["year"] = year_raw

        return SiteComicContext(
            comic=comic,
            title=title,
            identifier=slug,
            soup=None,
        )

    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        # We might already have chapter data from fetch_comic_context
        # But we need to check if it's the full list.
        # The Kotlin extension fetches `series/{id}.json` which returns `MangaDetailsResponseData`
        # And then `chapterListParse` uses `ChapterListResponseData`.
        # Wait, `mangaDetailsRequest` and `chapterListRequest` are the SAME in Kotlin.
        # So `series/{id}.json` contains BOTH details and chapters?
        # Let's check `MangaDetailsResponseData` vs `ChapterListResponseData` in Kotlin.
        # They seem to decode the SAME response body into different structures.
        # So yes, the series JSON contains the chapters.
        
        series_data = context.comic.get("_series_data")
        if not series_data:
             # Should not happen if fetch_comic_context was called
             return []

        # In Kotlin: `chaptersListResponseData.pageProps.chapters`
        # But we parsed `pageProps.series` in fetch_comic_context.
        # Let's check if `chapters` is in `pageProps` or `pageProps.series`.
        # Kotlin: `json.decodeFromString<ChapterListResponseData>(response.body.string()).pageProps.chapters`
        # So it's `pageProps.chapters`.
        
        # We need to re-fetch if we only saved `series` in context.
        # Actually, let's look at `fetch_comic_context` again.
        # I parsed `data.get("pageProps", {})`.
        # I should have saved `pageProps` or extracted chapters there.
        
        # Let's assume we need to re-fetch or use what we have.
        # If I used `_fetch_html` in `fetch_comic_context`, I have the initial props.
        # `next_data` has `pageProps`.
        # Let's verify if `chapters` are in `pageProps` of the HTML response.
        # Usually Next.js hydrates the page with full data.
        
        # However, to be robust and support pagination if it exists (Kotlin doesn't seem to handle pagination for chapters),
        # let's use the API endpoint which is cleaner.
        
        series_id = context.identifier
        build_id = context.comic.get("_build_id")
        if not build_id:
             build_id = self._fetch_build_id(scraper, make_request)
        
        # API URL: https://flamecomics.xyz/_next/data/{build_id}/series/{id}.json?id={id}
        url = self._get_data_api_url(f"series/{series_id}", build_id)
        # We need to append query param ?id={id} because Next.js dynamic routes often need it in the URL for the router,
        # but the data API URL itself is `.../series/{id}.json`.
        # The Kotlin code: `addQueryParameter("id", seriesID)`
        # So the full URL is `.../series/{id}.json?id={id}`
        
        url += f"?id={series_id}"
        
        build_id_ref = [build_id]
        data = self._fetch_json_with_retry(url, scraper, make_request, build_id_ref)
        
        page_props = data.get("pageProps", {})
        chapters_data = page_props.get("chapters", [])
        
        chapters = []
        for chap in chapters_data:
            # Kotlin: chapter_number = chapter.chapter.toFloat()
            # date_upload = chapter.release_date * 1000
            # name = "Chapter {chapter} - {title}"
            # token = chapter.token
            
            chap_num = str(chap.get("chapter"))
            title = chap.get("title") or ""
            token = chap.get("token")
            release_date = chap.get("release_date")
            
            full_title = f"Chapter {chap_num}"
            if title:
                full_title += f" - {title}"
                
            # We need the token for the page list
            
            chapters.append({
                "hid": token, # Use token as ID for chapter
                "chap": chap_num,
                "title": full_title,
                "url": f"series/{series_id}/{token}", # Virtual URL for internal use
                "uploaded": release_date, # Timestamp?
                "_token": token,
                "_series_id": series_id,
            })
            
        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        # URL: https://flamecomics.xyz/_next/data/{build_id}/series/{series_id}/{token}.json?id={series_id}&token={token}
        
        series_id = chapter.get("_series_id")
        token = chapter.get("_token")
        
        # We need a fresh build ID just in case
        build_id = self._fetch_build_id(scraper, make_request)
        build_id_ref = [build_id]
        
        url = self._get_data_api_url(f"series/{series_id}/{token}", build_id)
        url += f"?id={series_id}&token={token}"
        
        data = self._fetch_json_with_retry(url, scraper, make_request, build_id_ref)
        
        page_props = data.get("pageProps", {})
        chap_data = page_props.get("chapter", {})
        images = chap_data.get("images", [])
        
        # Image URL: https://cdn.flamecomics.xyz/uploads/images/series/{series_id}/{token}/{page_name}
        cdn_base = "https://cdn.flamecomics.xyz/uploads/images/series"
        
        image_urls = []
        for img in images:
            page_name = img.get("name") if isinstance(img, dict) else img
            if page_name:
                img_url = f"{cdn_base}/{series_id}/{token}/{page_name}"
                image_urls.append(img_url)

        return image_urls

    # -- Cross-site search ------------------------------------------
    # FlameComics has no server-side search via its Next.js data API. The
    # /browse.json endpoint accepts a `?q=…` param but ignores it server-side
    # — verified live: 153 entries returned regardless of query. So search()
    # fetches the full catalog and client-side filters on title. The catalog
    # is small (~150 series), and we cache the build-id from fetch_comic_context's
    # path so subsequent searches are cheap.
    #
    # Schema of each /browse.json entry: series_id (int), title, description,
    # cover (filename), year, type, status, language, country, author[], artist[],
    # publisher[], categories[], likes, last_edit, time. NO altTitles on the
    # browse listing (those live on the per-series /series/{id}.json detail).
    def search(
        self,
        query: str,
        scraper,
        make_request,
        *,
        language: str = "en",
        limit: int = 20,
    ) -> List[SearchHit]:
        clean = (query or "").strip()
        if not clean:
            return []
        # Walk the same build-id-refresh path as fetch_comic_context to keep
        # behavior consistent when FlameComics rebuilds (the build_id rotates).
        build_id = self._fetch_build_id(scraper, make_request)
        url = self._get_data_api_url("browse", build_id) + f"?q={clean}"
        response = make_request(url, scraper)
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError):
            return []
        series_list = (data.get("pageProps") or {}).get("series") or []
        if not isinstance(series_list, list):
            return []

        ql = clean.lower()
        query_tokens = set(t for t in ql.split() if t)

        scored: List[tuple] = []
        for entry in series_list:
            if not isinstance(entry, dict):
                continue
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            tl = title.lower()
            if ql in tl:
                relevance = 1.0
            elif query_tokens and all(tok in tl for tok in query_tokens):
                relevance = 0.7
            else:
                continue
            scored.append((relevance, entry))

        scored.sort(key=lambda x: -x[0])

        cdn_base = "https://cdn.flamecomics.xyz/uploads/images/series"
        hits: List[SearchHit] = []
        for idx, (relevance, entry) in enumerate(scored[:limit]):
            sid = entry.get("series_id")
            if sid is None:
                continue
            cover_filename = entry.get("cover")
            cover = f"{cdn_base}/{sid}/{cover_filename}" if cover_filename else None
            year = entry.get("year")
            if not isinstance(year, int):
                year = None
            url_full = f"https://flamecomics.xyz/series/{sid}"
            raw_score = max(0.05, relevance * (1.0 - (idx / max(1, len(scored)))))
            hits.append(
                SearchHit(
                    site=self.name,
                    title=entry.get("title") or "",
                    url=url_full,
                    cover=cover,
                    alt_titles=[],
                    year=year,
                    language=None,
                    chapter_count_hint=None,
                    raw_score=raw_score,
                )
            )
        return hits


__all__ = ["FlameComicsSiteHandler"]

