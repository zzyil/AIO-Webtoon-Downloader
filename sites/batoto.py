from __future__ import annotations

import json
import re
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString

from .base import BaseSiteHandler, SiteComicContext
from .bato_mirrors import BATO_MIRRORS


class BatoToSiteHandler(BaseSiteHandler):
    name = "batoto"
    domains: Tuple[str, ...] = BATO_MIRRORS

    _BASE_URL = "https://bato.to"
    _GRAPHQL_ENDPOINT = f"{_BASE_URL}/apo/"
    _DEFAULT_HEADERS = {
        "Referer": _BASE_URL + "/",
        "Origin": _BASE_URL,
    }

    _COMIC_DETAILS_QUERY = """
    query ComicDetails($id: ID!, $last: Int!) {
      get_content_comicNode(id: $id) {
        id
        data {
          name
          slug
          altNames
          authors
          artists
          genres
          summary { text }
          urlPath
          urlCoverOri
          urlCover600
          urlCover300
          origLang
          tranLang
          uploadStatus
          originalStatus
          readDirection
        }
        last_chapterNodes(amount: $last) {
          id
          data {
            urlPath
            chaNum
            title
            dname
          }
        }
      }
    }
    """.strip()

    def configure_session(self, scraper, args) -> None:
        scraper.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36",
        )
        scraper.headers.update(self._DEFAULT_HEADERS)
        scraper.headers.setdefault("Accept", "application/json, text/plain, */*")

    # ------------------------------------------------------------------ helpers
    def _fetch_html(self, url: str, scraper, make_request) -> str:
        response = make_request(url, scraper)
        response.raise_for_status()
        response.encoding = response.encoding or "utf-8"
        return response.text

    def _graphql_request(
        self, scraper, query: str, variables: Dict[str, object]
    ) -> Dict:
        response = scraper.post(
            self._GRAPHQL_ENDPOINT,
            json={
                "query": query,
                "variables": variables,
                "operationName": "ComicDetails",
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover - unexpected server reply
            raise RuntimeError("GraphQL response was not JSON.") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected GraphQL payload type: {type(payload).__name__}")
        if payload.get("errors"):
            raise RuntimeError(
                f"GraphQL returned errors: {payload['errors']}"
            )
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected GraphQL data shape: {data!r}")
        return data

    def _extract_series_id(self, url: str) -> Optional[str]:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "series":
            candidate = parts[1]
            if candidate.isdigit():
                return candidate
        return None

    def _normalise_people_field(self, value) -> List[str]:
        if not value:
            return []
        items: Iterable[str]
        if isinstance(value, str):
            items = re.split(r"[,/;]", value)
        elif isinstance(value, Iterable):
            items = value
        else:
            return []
        cleaned: List[str] = []
        for item in items:
            text = (item or "").strip()
            if not text:
                continue
            cleaned.append(text)
        return cleaned

    def _extract_summary(self, data: Dict) -> Optional[str]:
        summaries = data.get("summary") or []
        for entry in summaries:
            text = (entry or {}).get("text")
            if text:
                return text.strip()
        return None

    def _chapter_seed_path(self, node_list: List[Dict]) -> Optional[str]:
        if not node_list:
            return None
        node = node_list[0] or {}
        data = node.get("data") or {}
        url_path = data.get("urlPath")
        if url_path:
            return url_path
        identifier = node.get("id")
        if identifier:
            return f"/chapter/{identifier}"
        return None

    def _build_comic_dict(
        self, url: str, gql_data: Dict
    ) -> Tuple[Dict, Optional[str]]:
        node = gql_data.get("get_content_comicNode") or {}
        data = node.get("data") or {}

        title = data.get("name") or self._fallback_slug(url)
        slug = data.get("slug") or self._fallback_slug(url)

        alt_names = self._normalise_people_field(data.get("altNames"))
        authors = self._normalise_people_field(data.get("authors"))
        artists = self._normalise_people_field(data.get("artists"))
        genres = self._normalise_people_field(data.get("genres"))

        cover = (
            data.get("urlCoverOri")
            or data.get("urlCover600")
            or data.get("urlCover300")
        )

        comic = {
            "title": title,
            "slug": slug,
            "hid": slug or node.get("id") or self._fallback_slug(url),
            "desc": self._extract_summary(data),
            "cover": cover,
            "authors": authors,
            "artists": artists,
            "alt_names": alt_names,
            "genres": genres,
            "language": data.get("tranLang") or data.get("origLang"),
            "_series_id": node.get("id"),
        }

        seed_path = self._chapter_seed_path(node.get("last_chapterNodes") or [])
        return comic, seed_path

    def _fallback_slug(self, url: str) -> str:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        return parts[-1] if parts else "batoto"

    # ------------------------------------------------------------------ context
    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        series_id = self._extract_series_id(url)
        if not series_id:
            raise RuntimeError(
                "Unable to determine Batoto series id from URL."
            )

        comic: Dict = {}
        seed_path: Optional[str] = None
        gql_error: Optional[Exception] = None
        try:
            gql_data = self._graphql_request(
                scraper,
                self._COMIC_DETAILS_QUERY,
                {"id": series_id, "last": 1},
            )
            comic, seed_path = self._build_comic_dict(url, gql_data)
        except Exception as exc:  # pragma: no cover - network path
            gql_error = exc
            comic = {}
            seed_path = None

        # Fetch the series page HTML once for metadata fallback and chapter seed.
        series_url = url
        if "/series/" not in urlparse(url).path:
            series_url = f"{self._BASE_URL}/series/{series_id}"
        html = self._fetch_html(series_url, scraper, make_request)
        soup = BeautifulSoup(html, "html.parser")

        html_meta = self._extract_series_meta_from_html(soup)
        for key, value in html_meta.items():
            if not comic.get(key) and value:
                comic[key] = value

        if not comic.get("title"):
            comic["title"] = self._fallback_slug(url)
        if not comic.get("slug"):
            comic["slug"] = self._fallback_slug(url)
        if not comic.get("hid"):
            comic["hid"] = comic["slug"]

        if seed_path:
            comic["_seed_chapter_url"] = urljoin(self._BASE_URL, seed_path)
        else:
            seed_url = self._find_first_chapter_url(soup)
            if seed_url:
                comic["_seed_chapter_url"] = seed_url
            elif gql_error:
                raise RuntimeError(
                    f"Unable to locate chapter list (GraphQL error: {gql_error})"
                )
            else:
                raise RuntimeError("Unable to locate chapter list for this series.")

        context = SiteComicContext(
            comic=comic,
            title=comic["title"],
            identifier=comic["slug"],
            soup=soup,
        )
        return context

    # ---------------------------------------------------------------- chapters
    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        seed_url = context.comic.get("_seed_chapter_url")
        if not seed_url:
            return []

        html = self._fetch_html(seed_url, scraper, make_request)
        soup = BeautifulSoup(html, "html.parser")

        chapters_by_id: Dict[str, Dict] = {}
        order_counter = 0

        for select in soup.find_all("select"):
            for option in select.find_all("option"):
                value = (option.get("value") or "").strip()
                if not value or not value.isdigit():
                    continue
                label = " ".join(option.get_text(" ", strip=True).split())
                if not label:
                    continue
                if value in chapters_by_id:
                    continue
                chap_number, title = self._parse_chapter_label(label, order_counter)
                chapter_url = f"{self._BASE_URL}/chapter/{value}"
                chapters_by_id[value] = {
                    "hid": f"{context.identifier}-{chap_number}",
                    "chap": chap_number,
                    "title": title,
                    "url": chapter_url,
                    "group_name": None,
                }
                order_counter += 1

        chapters: List[Dict] = list(chapters_by_id.values())
        chapters.sort(key=lambda ch: float(ch["chap"]))
        return chapters

    def _parse_chapter_label(
        self, label: str, fallback_index: int
    ) -> Tuple[str, Optional[str]]:
        normalized = label.strip()
        title: Optional[str] = None

        # Split common separators between chapter number and title.
        for sep in (" - ", " – ", " — ", ":", "–", "—"):
            if sep in normalized:
                left, right = normalized.split(sep, 1)
                normalized = left.strip()
                possible_title = right.strip()
                if possible_title:
                    title = possible_title
                break

        match = re.search(r"(?:ch(?:apter)?|ep|episode)\s*([\d]+(?:\.\d+)?)", normalized, re.I)
        if match:
            return match.group(1), title

        num_match = re.search(r"(\d+(?:\.\d+)?)", normalized)
        if num_match:
            return num_match.group(1), title

        lowered = normalized.lower()
        if "prologue" in lowered:
            return "0", title or "Prologue"
        if "epilogue" in lowered:
            return "9999", title or "Epilogue"

        # Fallback: ensure unique numeric ordering
        return str(fallback_index + 1), normalized if not title else title

    # --------------------------------------------------------------- page data
    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List:
        html = self._fetch_html(chapter["url"], scraper, make_request)

        image_urls = self._extract_img_https(html)
        if not image_urls:
            # fallback to legacy parsing
            soup = BeautifulSoup(html, "html.parser")
            candidates = [
                img.get("data-src") or img.get("src")
                for img in soup.find_all("img")
            ]
            image_urls = [
                self._absolutise_url(chapter["url"], url)
                for url in candidates
                if url and self._looks_like_image(url)
            ]

        seen: set[str] = set()
        deduped: List[str] = []
        for url in image_urls:
            if url not in seen:
                seen.add(url)
                deduped.append(url)

        entries: List = list(deduped)

        text_paragraphs = self._extract_text_paragraphs(html)
        if text_paragraphs:
            entries.append(
                {
                    "type": "text",
                    "paragraphs": text_paragraphs,
                    "title": chapter.get("title"),
                }
            )

        return entries

    def _extract_img_https(self, html: str) -> List[str]:
        pattern = re.compile(r"const\s+imgHttps\s*=\s*(\[[^\]]*\])", re.S)
        match = pattern.search(html)
        if not match:
            pattern_alt = re.compile(r"const\s+imgHttpsCdn\s*=\s*(\[[^\]]*\])", re.S)
            match = pattern_alt.search(html)
        if not match:
            return []

        array_literal = match.group(1)
        try:
            urls = json.loads(array_literal)
        except json.JSONDecodeError:
            return []

        cleaned: List[str] = []
        for url in urls:
            if not isinstance(url, str):
                continue
            absolute = self._absolutise_url(self._BASE_URL, url.strip())
            if absolute:
                cleaned.append(absolute)
        return cleaned

    def _absolutise_url(self, base: str, url: str) -> Optional[str]:
        if not url:
            return None
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("http"):
            return url
        return urljoin(base, url)

    def _looks_like_image(self, url: str) -> bool:
        lowered = url.lower()
        return lowered.endswith((".png", ".jpg", ".jpeg", ".webp", ".avif"))

    def _extract_text_paragraphs(self, html: str) -> List[str]:
        soup = BeautifulSoup(html, "html.parser")
        container = soup.select_one(".reader-text")
        if not container:
            return []

        paragraphs: List[str] = []
        for node in container.children:
            if isinstance(node, NavigableString):
                text = node.strip()
                if text:
                    paragraphs.append(text)
                continue
            if node.name == "br":
                paragraphs.append("")
                continue
            if node.name in {"p", "div", "span", "blockquote", "h2", "h3", "h4"}:
                text = node.get_text(" ", strip=True)
                if text:
                    paragraphs.append(text)
                continue
            if node.name == "ul":
                items = [
                    li.get_text(" ", strip=True)
                    for li in node.find_all("li", recursive=False)
                ]
                items = [item for item in items if item]
                if items:
                    paragraphs.extend(items)
        return paragraphs

    def _extract_series_meta_from_html(self, soup: BeautifulSoup) -> Dict[str, Optional[str]]:
        meta: Dict[str, Optional[str]] = {}

        meta_title = soup.find("meta", attrs={"property": "og:title"})
        if meta_title and meta_title.get("content"):
            meta["title"] = meta_title["content"].strip()

        meta_desc = soup.find("meta", attrs={"property": "og:description"})
        if meta_desc and meta_desc.get("content"):
            meta["desc"] = meta_desc["content"].strip()

        meta_img = soup.find("meta", attrs={"property": "og:image"})
        if meta_img and meta_img.get("content"):
            meta["cover"] = meta_img["content"].strip()

        return meta

    def _find_first_chapter_url(self, soup: BeautifulSoup) -> Optional[str]:
        for link in soup.find_all("a", href=True):
            href = link["href"]
            match = re.search(r"/chapter/\d+", href)
            if match:
                return urljoin(self._BASE_URL, match.group(0))
        return None


__all__ = ["BatoToSiteHandler"]
