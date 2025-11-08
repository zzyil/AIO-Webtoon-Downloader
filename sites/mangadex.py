from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlparse

from datetime import datetime, timezone

from .base import BaseSiteHandler, SiteComicContext


class MangaDexSiteHandler(BaseSiteHandler):
    name = "mangadex"
    domains = ("mangadex.org", "www.mangadex.org")

    _API_BASE = "https://api.mangadex.org"
    _UPLOADS_BASE = "https://uploads.mangadex.org"

    def configure_session(self, scraper, args) -> None:
        scraper.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36",
        )

    # ------------------------------------------------------------------ helpers
    def _extract_manga_id(self, url: str) -> str:
        parsed = urlparse(url)
        segments = [seg for seg in parsed.path.split("/") if seg]
        uuid_pattern = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
        for segment in segments:
            if uuid_pattern.fullmatch(segment):
                return segment.lower()
        if uuid_pattern.fullmatch(parsed.path.strip("/")):
            return parsed.path.strip("/").lower()
        raise RuntimeError(
            "Unable to determine MangaDex ID. Use a URL of the form "
            "'https://mangadex.org/title/<uuid>/...'."
        )

    def _request(
        self,
        scraper,
        make_request,
        endpoint: str,
        params: Optional[Dict[str, str]] = None,
    ):
        url = f"{self._API_BASE}{endpoint}"
        resp = make_request(url if not params else (url, params), scraper)
        return resp.json()

    def _title_from_attributes(self, attributes: Dict) -> str:
        title = attributes.get("title") or {}
        if isinstance(title, dict):
            for key in ("en", "ja", "jp", "ko"):
                if key in title and title[key]:
                    return title[key]
            if title:
                return next(iter(title.values()))
        return attributes.get("altTitles", [{}])[0].get("en") or "Unknown Manga"

    def _description_from_attributes(self, attributes: Dict) -> Optional[str]:
        description = attributes.get("description") or {}
        if isinstance(description, dict):
            for key in ("en", "ja", "jp", "ko"):
                if description.get(key):
                    return description[key]
            if description:
                return next(iter(description.values()))
        return None

    def _cover_url(self, manga_id: str, relationships: Iterable[Dict]) -> Optional[str]:
        for rel in relationships:
            if rel.get("type") == "cover_art" and rel.get("attributes"):
                file_name = rel["attributes"].get("fileName")
                if file_name:
                    return f"{self._UPLOADS_BASE}/covers/{manga_id}/{file_name}"
        return None

    # ----------------------------------------------------------- Base overrides
    def fetch_comic_context(
        self,
        url: str,
        scraper,
        make_request,
    ) -> SiteComicContext:
        manga_id = self._extract_manga_id(url)
        params = [
            ("includes[]", "author"),
            ("includes[]", "artist"),
            ("includes[]", "cover_art"),
        ]
        resp = make_request(
            f"{self._API_BASE}/manga/{manga_id}?{'&'.join(f'{k}={v}' for k, v in params)}",
            scraper,
        )
        data = resp.json().get("data")
        if not data:
            raise RuntimeError("MangaDex API did not return data for this ID.")
        attributes = data.get("attributes") or {}
        relationships = data.get("relationships") or []

        title = self._title_from_attributes(attributes)
        description = self._description_from_attributes(attributes)

        authors = []
        artists = []
        for rel in relationships:
            if rel.get("type") == "author" and rel.get("attributes"):
                name = rel["attributes"].get("name")
                if name:
                    authors.append(name)
            if rel.get("type") == "artist" and rel.get("attributes"):
                name = rel["attributes"].get("name")
                if name:
                    artists.append(name)

        comic: Dict[str, object] = {
            "hid": manga_id,
            "title": title,
            "desc": description,
            "cover": self._cover_url(manga_id, relationships),
            "authors": authors or artists,
            "artists": artists or authors,
            "genres": [tag.get("name") for tag in attributes.get("tags", []) if tag.get("name")],
            "_manga_id": manga_id,
        }

        return SiteComicContext(comic=comic, title=title, identifier=manga_id, soup=None)

    def get_chapters(
        self,
        context: SiteComicContext,
        scraper,
        language: str,
        make_request,
    ) -> List[Dict]:
        manga_id = context.comic.get("_manga_id") or context.identifier
        params = {
            "manga": manga_id,
            "limit": "100",
            "offset": "0",
            "order[chapter]": "asc",
            "includes[]": ["scanlation_group", "user"],
            "contentRating[]": ["safe", "suggestive", "erotica"],
        }
        languages = []
        if language and language.lower() != "all":
            languages = [lang.strip() for lang in language.split(",") if lang.strip()]
        if languages:
            params["translatedLanguage[]"] = languages

        chapters: List[Dict] = []
        offset = 0
        while True:
            query_params = []
            for key, value in params.items():
                if isinstance(value, list):
                    for entry in value:
                        query_params.append((key, entry))
                else:
                    query_params.append((key, value))
            query_params = [(k, str(v)) for k, v in query_params]
            query_params.append(("offset", str(offset)))
            url = f"{self._API_BASE}/chapter?" + "&".join(f"{k}={v}" for k, v in query_params)
            resp = make_request(url, scraper).json()
            data = resp.get("data", [])
            total = resp.get("total", 0)
            for chapter in data:
                attr = chapter.get("attributes") or {}
                relationships = chapter.get("relationships") or []
                chapter_id = chapter.get("id")
                group_name = None
                for rel in relationships:
                    if rel.get("type") == "scanlation_group":
                        group_name = rel.get("attributes", {}).get("name")
                        break
                chapters.append(
                    {
                        "hid": chapter_id,
                        "chap": attr.get("chapter") or attr.get("title") or attr.get("volume"),
                        "title": attr.get("title"),
                        "url": chapter_id,
                        "group_name": group_name,
                        "language": attr.get("translatedLanguage"),
                        "uploaded": self._parse_timestamp(attr.get("publishAt")),
                    }
                )
            offset += len(data)
            if offset >= total or not data:
                break
        return chapters

    def get_group_name(self, chapter_version: Dict) -> Optional[str]:
        group = chapter_version.get("group_name")
        return group if isinstance(group, str) and group else None

    def get_chapter_images(
        self,
        chapter: Dict,
        scraper,
        make_request,
    ) -> List[str]:
        chapter_id = chapter.get("url") or chapter.get("hid")
        if not chapter_id:
            raise RuntimeError("MangaDex chapter missing identifier.")
        resp = make_request(f"{self._API_BASE}/at-home/server/{chapter_id}", scraper).json()
        base_url = resp.get("baseUrl")
        chapter_data = resp.get("chapter") or {}
        if not base_url or not chapter_data:
            raise RuntimeError("MangaDex At-Home API returned incomplete data.")
        file_hash = chapter_data.get("hash")
        data = chapter_data.get("data")
        if not file_hash or not data:
            raise RuntimeError("MangaDex chapter payload missing image list.")
        return [
            f"{base_url}/data/{file_hash}/{filename}"
            for filename in data
        ]


    def _parse_timestamp(self, value: Optional[str]) -> int:
        if not value:
            return 0
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except Exception:
            return 0


__all__ = ["MangaDexSiteHandler"]
