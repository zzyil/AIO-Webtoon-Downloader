from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from .base import BaseSiteHandler, SiteComicContext


class AtsumaruSiteHandler(BaseSiteHandler):
    name = "atsumaru"
    domains = ("atsu.moe", "www.atsu.moe")

    _BASE_URL = "https://atsu.moe"

    def __init__(self) -> None:
        super().__init__()
        self._api_headers = {
            "Accept": "*/*",
            "Referer": f"{self._BASE_URL}/",
            "Host": urlparse(self._BASE_URL).netloc,
        }

    # ----------------------------------------------------------------- helpers
    def _slug_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            return parsed.netloc
        if parts[0] == "manga":
            return parts[-1]
        return parts[-1]

    def _api_get(self, scraper, path: str, params: Optional[Dict[str, str]] = None):
        url = urljoin(self._BASE_URL, path)
        response = scraper.get(url, params=params, headers=self._api_headers)
        response.raise_for_status()
        return response

    def _parse_cover(self, payload: Dict) -> Optional[str]:
        poster = payload.get("poster") or payload.get("image")
        if isinstance(poster, dict):
            poster = poster.get("image")
        if isinstance(poster, str):
            poster = poster.lstrip("/")
            if poster.startswith("static/"):
                poster = poster[len("static/") :]
            return f"{self._BASE_URL}/static/{poster}"
        return None

    def _parse_people(self, entries: Optional[List[Dict]]) -> List[str]:
        if not entries:
            return []
        return [entry.get("name") for entry in entries if entry.get("name")]

    # ----------------------------------------------------------- Base overrides
    def configure_session(self, scraper, args) -> None:
        scraper.headers.setdefault("Referer", f"{self._BASE_URL}/")

    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        slug = self._slug_from_url(url)
        payload = self._api_get(scraper, "/api/manga/page", params={"id": slug}).json()
        manga = payload.get("mangaPage") or {}

        title = manga.get("title") or slug
        description = manga.get("synopsis")
        cover = self._parse_cover(manga)

        comic: Dict[str, object] = {
            "hid": slug,
            "title": title,
            "desc": description,
            "cover": cover,
            "genres": [tag.get("name") for tag in manga.get("tags") or [] if tag.get("name")],
            "url": url,
            # Store embedded chapters from the manga page as a fallback.
            # For adult-content manga, /api/manga/chapters returns empty without
            # an authenticated session, but /api/manga/page always includes the
            # first batch of chapters regardless of login state.
            "_embedded_chapters": manga.get("chapters") or [],
            "_has_more_chapters": bool(manga.get("hasMoreChapters")),
        }

        authors = self._parse_people(manga.get("authors"))
        if authors:
            comic["authors"] = authors

        comic["language"] = "en"

        return SiteComicContext(comic=comic, title=title, identifier=slug, soup=None)

    def _fetch_chapter_batch(self, slug: str, page: int, scraper) -> Dict:
        response = self._api_get(
            scraper,
            "/api/manga/chapters",
            params={
                "id": slug,
                "filter": "all",
                "sort": "desc",
                "page": str(page),
            },
        )
        return response.json()

    def _fetch_manga_page_chapter_batch(self, slug: str, index: int, scraper) -> Dict:
        """Fetch chapters via /api/manga/page with an index offset.

        atsu.moe uses ``index`` (0-based position in descending order) to
        paginate chapters through this endpoint, which works for adult-content
        manga even without an authenticated session.
        """
        response = self._api_get(
            scraper,
            "/api/manga/page",
            params={"id": slug, "index": str(index)},
        )
        payload = response.json()
        manga = payload.get("mangaPage") or {}
        return {
            "chapters": manga.get("chapters") or [],
            "hasMoreChapters": bool(manga.get("hasMoreChapters")),
        }

    def _parse_chapter_entry(self, slug: str, entry: Dict, fallback_index: int = 0) -> Dict:
        """Convert a raw chapter dict from either API source into a normalised chapter dict."""
        chapter_id = entry.get("id")
        chap_number = entry.get("number")
        title = entry.get("title")
        date = entry.get("createdAt")
        uploaded = 0
        if isinstance(date, str):
            try:
                uploaded = int(dt.datetime.strptime(date, "%Y-%m-%dT%H:%M:%S.%fZ").timestamp())
            except ValueError:
                try:
                    uploaded = int(dt.datetime.strptime(date, "%Y-%m-%dT%H:%M:%SZ").timestamp())
                except ValueError:
                    uploaded = 0

        # Determine the chapter number string used for filtering.
        # If chap_number is set, use it directly. Otherwise fall back to the
        # title string. If the title is also non-numeric (common for adult
        # content where chapters use descriptive titles), assign a sequential
        # index so the main filtering logic does not silently discard it.
        if chap_number is not None:
            chap_str = str(chap_number)
        elif title is not None:
            # Try to parse a number out of the title first
            import re as _re
            m = _re.search(r"(\d+(?:\.\d+)?)", str(title))
            if m:
                chap_str = m.group(1)
            else:
                # Non-numeric title — use a positional index so the chapter
                # isn't discarded by the float-parsing filter in aio-dl.py.
                chap_str = str(fallback_index)
        else:
            chap_str = str(fallback_index)

        return {
            "hid": f"{slug}-{chapter_id}",
            "chap": chap_str,
            "title": title,
            "url": f"/read/{slug}/{chapter_id}",
            "_slug": slug,
            "_chapter_id": chapter_id,
            "uploaded": uploaded,
        }

    def get_chapters(
        self,
        context: SiteComicContext,
        scraper,
        language: str,
        make_request,
    ) -> List[Dict]:
        slug = context.identifier
        page = 0
        chapters: List[Dict] = []
        while True:
            batch = self._fetch_chapter_batch(slug, page, scraper)
            entries = batch.get("chapters") or []
            for i, entry in enumerate(entries):
                chapters.append(self._parse_chapter_entry(slug, entry, fallback_index=len(chapters) + 1))
            pages_total = batch.get("pages")
            current_page = batch.get("page", page)
            has_next = (
                isinstance(pages_total, int)
                and isinstance(current_page, int)
                and current_page + 1 < pages_total
            )
            if not has_next or not entries:
                break
            page = current_page + 1

        if chapters:
            return chapters

        # --- Fallback for adult-content manga ---
        # /api/manga/chapters returns empty chapters without an authenticated
        # session. Fall back to the chapters that were embedded in the
        # /api/manga/page response (stored during fetch_comic_context), then
        # keep paginating via the manga page endpoint if hasMoreChapters is set.
        embedded: List[Dict] = list(context.comic.get("_embedded_chapters") or [])
        has_more: bool = bool(context.comic.get("_has_more_chapters"))

        for entry in embedded:
            chapters.append(self._parse_chapter_entry(slug, entry, fallback_index=len(chapters) + 1))

        if has_more and embedded:
            # Paginate using the last chapter's index field as the offset.
            # The API returns chapters in descending order; we pass the index
            # of the last chapter we received so the server continues from
            # the next batch.
            last_index = embedded[-1].get("index")
            seen_ids = {c.get("id") for c in embedded if c.get("id")}
            
            while has_more and last_index is not None:
                try:
                    batch = self._fetch_manga_page_chapter_batch(slug, last_index, scraper)
                except Exception:
                    break
                next_entries = batch.get("chapters") or []
                has_more = bool(batch.get("hasMoreChapters"))
                if not next_entries:
                    break
                    
                added_any = False
                for entry in next_entries:
                    eid = entry.get("id")
                    if eid and eid not in seen_ids:
                        seen_ids.add(eid)
                        chapters.append(self._parse_chapter_entry(slug, entry, fallback_index=len(chapters) + 1))
                        added_any = True
                
                if not added_any:
                    break
                last_index = next_entries[-1].get("index")

        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        slug = chapter.get("_slug")
        chapter_id = chapter.get("_chapter_id")
        if not slug or not chapter_id:
            # fallback to parsing from URL
            url = chapter.get("url", "")
            parts = [p for p in url.split("/") if p]
            if len(parts) >= 3:
                slug = parts[-2]
                chapter_id = parts[-1]
        if not slug or not chapter_id:
            raise RuntimeError("Atsumaru chapter identifiers missing.")

        response = self._api_get(
            scraper,
            "/api/read/chapter",
            params={"mangaId": slug, "chapterId": chapter_id},
        )
        payload = response.json()
        pages = ((payload or {}).get("readChapter") or {}).get("pages") or []
        images: List[str] = []
        for page in pages:
            image = page.get("image")
            if not image:
                continue
            images.append(urljoin(self._BASE_URL, image))
        if not images:
            raise RuntimeError("No images returned for Atsumaru chapter.")
        return images


__all__ = ["AtsumaruSiteHandler"]
