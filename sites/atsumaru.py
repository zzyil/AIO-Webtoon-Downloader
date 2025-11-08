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
            for entry in entries:
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
                chapters.append(
                    {
                        "hid": f"{slug}-{chapter_id}",
                        "chap": str(chap_number) if chap_number is not None else title,
                        "title": title,
                        "url": f"/read/{slug}/{chapter_id}",
                        "_slug": slug,
                        "_chapter_id": chapter_id,
                        "uploaded": uploaded,
                    }
                )
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
