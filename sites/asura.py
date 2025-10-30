from __future__ import annotations

import json
import re
from typing import Dict, List, Optional
from urllib.parse import urlparse

from .base import BaseSiteHandler, SiteComicContext


class AsuraSiteHandler(BaseSiteHandler):
    name = "asura"
    domains = (
        "asuracomic.net",
        "www.asuracomic.net",
        "asurascans.net",
        "www.asurascans.net",
    )

    BASE_URL = "https://asuracomic.net"

    def configure_session(self, scraper, args) -> None:
        scraper.headers.update(
            {
                "Referer": self.BASE_URL + "/",
                "Origin": self.BASE_URL,
            }
        )

    # -- Helpers -----------------------------------------------------
    def _fetch_html(self, url: str, scraper, make_request) -> str:
        response = make_request(url, scraper)
        response.encoding = response.encoding or "utf-8"
        return response.text

    def _extract_flight_content(self, html: str) -> str:
        """
        Next.js App Router streams data via self.__next_f pushes. We decode the
        escaped payload into plain text for easier parsing.
        """
        chunks: List[str] = []
        search = 'self.__next_f.push([1,"'
        idx = 0
        while True:
            start = html.find(search, idx)
            if start == -1:
                break
            start += len(search)
            end = html.find('"])', start)
            if end == -1:
                break
            raw = html[start:end]
            chunks.append(bytes(raw, "utf-8").decode("unicode_escape"))
            idx = end
        return "\n".join(chunks)

    def _extract_json_block(self, text: str, pattern: str) -> Optional[Dict]:
        idx = text.find(pattern)
        if idx == -1:
            return None
        start = text.find("{", idx)
        if start == -1:
            return None
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    block = text[start : i + 1]
                    return json.loads(block)
        return None

    def _extract_array_block(self, text: str, pattern: str) -> Optional[List]:
        idx = text.find(pattern)
        if idx == -1:
            return None
        start = text.find("[", idx)
        if start == -1:
            return None
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    block = text[start : i + 1]
                    return json.loads(block)
        return None

    def _build_pointer_map(self, content: str) -> Dict[str, object]:
        pattern = re.compile(r"^([0-9a-z]+):(.*)$", re.MULTILINE)
        values: Dict[str, object] = {}
        for match in pattern.finditer(content):
            key = match.group(1)
            raw = match.group(2).strip()
            if not raw:
                continue
            if raw.startswith("$"):
                values[key] = raw
                continue
            if raw.startswith('"'):
                try:
                    values[key] = json.loads(raw)
                except json.JSONDecodeError:
                    values[key] = raw
                continue
            try:
                if raw.startswith("[") or raw.startswith("{"):
                    values[key] = json.loads(raw)
                else:
                    values[key] = raw
            except json.JSONDecodeError:
                values[key] = raw
        return values

    def _resolve(
        self,
        value,
        mapping: Dict[str, object],
        visited: Optional[set] = None,
    ):
        if visited is None:
            visited = set()
        if isinstance(value, str) and value.startswith("$"):
            token = value[1:]
            if token in visited:
                return None
            visited.add(token)
            return self._resolve(mapping.get(token), mapping, visited)
        if isinstance(value, list):
            return [self._resolve(v, mapping, set()) for v in value]
        if isinstance(value, dict):
            return {k: self._resolve(v, mapping, set()) for k, v in value.items()}
        return value

    def _find_chapter_map(self, mapping: Dict[str, object]) -> Optional[List[Dict]]:
        for value in mapping.values():
            resolved = self._resolve(value, mapping)
            if (
                isinstance(resolved, list)
                and resolved
                and isinstance(resolved[0], dict)
                and {"label", "value"}.issubset(resolved[0].keys())
            ):
                return resolved
        return None

    def _find_pages(self, mapping: Dict[str, object]) -> Optional[List[Dict]]:
        for value in mapping.values():
            resolved = self._resolve(value, mapping)
            if (
                isinstance(resolved, list)
                and resolved
                and isinstance(resolved[0], dict)
                and {"order", "url"}.issubset(resolved[0].keys())
            ):
                return resolved
        return None

    def _parse_chapter_page(self, html: str) -> Dict:
        content = self._extract_flight_content(html)
        pointer_map = self._build_pointer_map(content)
        comic = self._extract_json_block(content, '"comic":{')
        chapter = self._extract_json_block(content, '"chapter":{"id"')
        chapter_map = self._extract_array_block(content, '"chapterMapData":[')
        if comic:
            comic = self._resolve(comic, pointer_map)
        if chapter:
            chapter = self._resolve(chapter, pointer_map)
        if chapter_map:
            chapter_map = self._resolve(chapter_map, pointer_map)
        else:
            chapter_map = self._find_chapter_map(pointer_map) or []
        if chapter and not chapter.get("pages"):
            chapter["pages"] = self._find_pages(pointer_map) or []
        return {
            "comic": comic or {},
            "chapter": chapter or {},
            "chapter_map": chapter_map or [],
            "pointers": pointer_map,
        }

    def _chapter_url(self, slug: str, chapter_value: str) -> str:
        return f"{self.BASE_URL}/series/{slug}/chapter/{chapter_value}"

    # -- Base overrides ----------------------------------------------
    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        html = self._fetch_html(url, scraper, make_request)
        data = self._parse_chapter_page(html)
        comic = data["comic"] or {}
        if not comic:
            raise RuntimeError("Unable to parse comic metadata from Asura page.")

        slug = comic.get("slug") or self._slug_from_url(url)
        title = comic.get("name") or slug
        comic.setdefault("slug", slug)
        comic.setdefault("name", title)
        comic.setdefault("hid", str(comic.get("id") or slug))

        # inject helpers
        comic["_chapter_map"] = data.get("chapter_map", [])
        comic["_base_url"] = self._series_base(slug)

        if not comic["_chapter_map"]:
            series_url = self._series_base(slug)
            series_html = self._fetch_html(series_url, scraper, make_request)
            series_data = self._parse_chapter_page(series_html)
            if series_data.get("chapter_map"):
                comic["_chapter_map"] = series_data["chapter_map"]
            if series_data.get("comic"):
                for key, value in series_data["comic"].items():
                    if key not in comic or comic[key] in (None, "", []):
                        comic[key] = value
        if not comic["_chapter_map"]:
            try:
                first_html = self._fetch_html(
                    self._chapter_url(slug, "1"), scraper, make_request
                )
                first_data = self._parse_chapter_page(first_html)
                if first_data.get("chapter_map"):
                    comic["_chapter_map"] = first_data["chapter_map"]
            except Exception:
                pass

        return SiteComicContext(
            comic=comic,
            title=title,
            identifier=slug,
            soup=None,
        )

    def extract_additional_metadata(
        self, context: SiteComicContext
    ) -> Dict[str, List[str]]:
        comic = context.comic or {}
        metadata: Dict[str, List[str]] = {}

        description = comic.get("description") or comic.get("summary")
        if description:
            # store in comic for downstream builder
            comic["desc"] = description

        # genres may be available as a list under "genres"
        genres = comic.get("genres")
        if isinstance(genres, list):
            metadata["genres"] = [g["name"] for g in genres if isinstance(g, dict) and g.get("name")]

        # authors / artists might be included under different keys; handle gracefully
        for key, target in (("authors", "authors"), ("artists", "artists")):
            if key in comic and isinstance(comic[key], list):
                metadata[target] = [
                    item["name"]
                    for item in comic[key]
                    if isinstance(item, dict) and item.get("name")
                ]

        return metadata

    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        slug = context.identifier
        chapter_map: List[Dict] = context.comic.get("_chapter_map", [])
        chapters: List[Dict] = []

        normalized_entries = []
        for entry in chapter_map:
            if not isinstance(entry, dict):
                continue
            label = entry.get("label") or ""
            value = entry.get("value")
            if value is None:
                continue
            chapter_no = self._normalize_chapter_number(value)
            normalized_entries.append((chapter_no, label))

        def chapter_sort_key(item):
            chap_no, _ = item
            try:
                return float(chap_no)
            except ValueError:
                return float("inf")

        for chapter_no, label in sorted(normalized_entries, key=chapter_sort_key):
            chapters.append(
                {
                    "hid": f"{slug}-{chapter_no}",
                    "chap": chapter_no,
                    "title": label or f"Chapter {chapter_no}",
                    "url": self._chapter_url(slug, chapter_no),
                    "group_name": None,
                }
            )

        return chapters

    def get_group_name(self, chapter_version: Dict) -> Optional[str]:
        return chapter_version.get("group_name")

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        chapter_url = chapter.get("url")
        if not chapter_url:
            raise RuntimeError("Chapter URL missing for Asura chapter.")

        html = self._fetch_html(chapter_url, scraper, make_request)
        data = self._parse_chapter_page(html)
        pages = data.get("chapter", {}).get("pages", [])

        image_urls: List[str] = []
        for page in sorted(
            pages,
            key=lambda p: p.get("order", 0) if isinstance(p, dict) else 0,
        ):
            if isinstance(page, dict) and page.get("url"):
                image_urls.append(page["url"])
        return image_urls

    # -- Internal helpers --------------------------------------------
    def _series_base(self, slug: str) -> str:
        return f"{self.BASE_URL}/series/{slug}"

    def _slug_from_url(self, url: str) -> str:
        path = urlparse(url).path
        # path like /series/<slug>/chapter/<name> or /series/<slug>
        parts = [part for part in path.split("/") if part]
        if not parts:
            return ""
        if parts[0] == "series":
            return parts[1] if len(parts) > 1 else ""
        return parts[0]

    def _normalize_chapter_number(self, value) -> str:
        if isinstance(value, (int, float)):
            if isinstance(value, float) and not value.is_integer():
                return str(value).rstrip("0").rstrip(".")
            return str(int(value))
        return str(value)


__all__ = ["AsuraSiteHandler"]
