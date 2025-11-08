from __future__ import annotations

import json
import re
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, FeatureNotFound

from .base import BaseSiteHandler, SiteComicContext


class ArtlapsaSiteHandler(BaseSiteHandler):
    name = "artlapsa"
    domains = ("artlapsa.com", "www.artlapsa.com")

    _BASE_URL = "https://artlapsa.com"

    def __init__(self) -> None:
        super().__init__()
        try:
            from lxml import etree  # noqa: F401

            self._parser = "lxml"
        except Exception:
            self._parser = "html.parser"

    # ---------------------------------------------------------------- helpers
    def _make_soup(self, html: str) -> BeautifulSoup:
        try:
            return BeautifulSoup(html, self._parser)
        except FeatureNotFound:
            return BeautifulSoup(html, "html.parser")

    def _normalize_url(self, url: str) -> str:
        if not url.lower().startswith("http"):
            return urljoin(self._BASE_URL, url.lstrip("/"))
        return url

    def _path_segments(self, url: str) -> List[str]:
        parsed = urlparse(url)
        return [segment for segment in parsed.path.split("/") if segment]

    def _series_id_from_url(self, url: str) -> Optional[str]:
        parts = self._path_segments(url)
        if len(parts) >= 2 and parts[0] == "series":
            return parts[1]
        return None

    def _chapter_id_from_url(self, url: str) -> Optional[str]:
        parts = self._path_segments(url)
        if len(parts) >= 2 and parts[0] == "read":
            return parts[1]
        return None

    def _find_series_id(self, soup: BeautifulSoup, fallback_url: str) -> Optional[str]:
        series_id = self._series_id_from_url(fallback_url)
        if series_id:
            return series_id
        link = soup.select_one("a[href*='/series/']")
        if link:
            href = self._normalize_url(link.get("href", ""))
            return self._series_id_from_url(href)
        return None

    def _meta_content(
        self,
        soup: BeautifulSoup,
        name: Optional[str] = None,
        property_name: Optional[str] = None,
    ) -> Optional[str]:
        if name:
            tag = soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return tag["content"].strip()
        if property_name:
            tag = soup.find("meta", attrs={"property": property_name})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    def _extract_series_title(self, soup: BeautifulSoup) -> Optional[str]:
        hidden = soup.select_one("#serieTitle")
        if hidden and hidden.get("value"):
            value = hidden.get("value", "").strip()
            if value:
                return value
        link = soup.select_one("a[href*='/series/']")
        if link:
            text = link.get_text(strip=True)
            if text:
                return text
        title_tag = soup.find("title")
        if title_tag and title_tag.get_text(strip=True):
            return title_tag.get_text(strip=True)
        return None

    def _extract_chapter_number(self, text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        match = re.search(r"(\d+(?:\.\d+)?)", text.replace(",", "."))
        if match:
            return match.group(1)
        return None

    def _chapter_sort_key(self, chapter: Dict) -> float:
        chap = chapter.get("chap")
        if chap is None:
            return float("inf")
        try:
            return float(chap)
        except (TypeError, ValueError):
            return float("inf")

    def _extract_chapter_links(self, soup: BeautifulSoup) -> List[Dict]:
        container = soup.select_one("#chapters")
        anchors = container.select("a[href*='/read/']") if container else []
        if not anchors:
            anchors = soup.select("a[href*='/read/']")

        chapters: List[Dict] = []
        seen: set[str] = set()
        for link in anchors:
            href = (link.get("href") or "").strip()
            if not href:
                continue
            abs_url = self._normalize_url(href)
            chapter_id = self._chapter_id_from_url(abs_url)
            if not chapter_id or chapter_id in seen:
                continue
            seen.add(chapter_id)

            title = (link.get("title") or link.get_text(" ", strip=True) or chapter_id).strip()
            chap_number = self._extract_chapter_number(title)

            chapters.append(
                {
                    "hid": chapter_id,
                    "chap": chap_number or title,
                    "title": title,
                    "url": urljoin(self._BASE_URL, f"/read/{chapter_id}/"),
                }
            )

        chapters.sort(key=self._chapter_sort_key)
        return chapters

    def _find_reader_data(self, soup: BeautifulSoup) -> Optional[str]:
        for element in soup.find_all(attrs={"x-data": True}):
            data_attr = element.get("x-data") or ""
            if "immersiveReader" in data_attr:
                return data_attr
        return None

    def _extract_json_array(self, data: str, key: str) -> Optional[str]:
        marker = f"{key}:"
        idx = data.find(marker)
        if idx == -1:
            return None
        start = data.find("[", idx)
        if start == -1:
            return None
        depth = 0
        for pos in range(start, len(data)):
            char = data[pos]
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return data[start : pos + 1]
        return None

    def _extract_string_value(self, data: str, key: str) -> Optional[str]:
        match = re.search(rf"{re.escape(key)}\s*:\s*'([^']*)'", data)
        if match:
            return match.group(1)
        return None

    # ----------------------------------------------------------- Base overrides
    def configure_session(self, scraper, args) -> None:
        scraper.headers.setdefault("Referer", self._BASE_URL + "/")

    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        normalized_url = self._normalize_url(url)
        response = make_request(normalized_url, scraper)
        soup = self._make_soup(response.text)

        series_id = self._find_series_id(soup, normalized_url)
        if not series_id:
            raise RuntimeError("Unable to determine series identifier.")

        title = self._extract_series_title(soup) or series_id
        desc = self._meta_content(soup, name="description")
        cover = self._meta_content(soup, property_name="og:image")

        comic: Dict[str, object] = {
            "hid": series_id,
            "title": title,
            "url": urljoin(self._BASE_URL, f"/series/{series_id}/"),
        }
        if desc:
            comic["desc"] = desc
        if cover:
            comic["cover"] = cover

        return SiteComicContext(
            comic=comic,
            title=title,
            identifier=series_id,
            soup=soup,
        )

    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        soup = context.soup
        if soup is None:
            raise RuntimeError("Series page HTML is unavailable.")

        chapters = self._extract_chapter_links(soup)
        if not chapters:
            raise RuntimeError("No chapters were detected on this page.")
        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        chapter_id = chapter.get("hid")
        chapter_url = chapter.get("url") or urljoin(self._BASE_URL, f"/read/{chapter_id}/")
        if not chapter_url:
            raise RuntimeError("Chapter URL missing.")

        response = make_request(chapter_url, scraper)
        soup = self._make_soup(response.text)

        data_attr = self._find_reader_data(soup)
        if not data_attr:
            raise RuntimeError("Reader data block not found; chapter may be locked.")

        pages_json = self._extract_json_array(data_attr, "pages")
        if not pages_json:
            raise RuntimeError("Unable to locate page list for this chapter.")
        pages = json.loads(pages_json)

        base_link = self._extract_string_value(data_attr, "baseLink") or self._BASE_URL + "/"

        images: List[str] = []
        for page in pages:
            path = page.get("path")
            if not path:
                continue
            if path.startswith("http"):
                image_url = path
            else:
                image_url = urljoin(base_link, path.lstrip("/"))
            if image_url not in images:
                images.append(image_url)

        if not images:
            raise RuntimeError("No images could be extracted for this chapter.")
        return images


__all__ = ["ArtlapsaSiteHandler"]
