from __future__ import annotations

import json
import re
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString

from .base import BaseSiteHandler, SiteComicContext


class MangaParkSiteHandler(BaseSiteHandler):
    name = "mangapark"
    domains: Tuple[str, ...] = (
        "mangapark.net",
        "www.mangapark.net",
        "v3x.mangapark.net",
    )

    _BASE_URL = "https://mangapark.net"
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

    _CHAPTER_LIST_QUERY = """
    query ChapterList($comicId: ID!) {
      get_comicChapterList(comicId: $comicId) {
        id
        data {
          id
          comicId
          isFinal
          volume
          serial
          dname
          title
          urlPath
          sfw_result
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
    def _extract_comic_id(self, url: str) -> Optional[str]:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            return None
        try:
            title_part = parts[1] if parts[0] == "title" and len(parts) > 1 else parts[0]
        except IndexError:
            title_part = parts[0]
        match = re.match(r"(\d+)", title_part)
        return match.group(1) if match else None

    def _graphql_request(
        self, scraper, query: str, variables: Dict[str, object], operation: str
    ) -> Dict:
        response = scraper.post(
            self._GRAPHQL_ENDPOINT,
            json={
                "query": query,
                "variables": variables,
                "operationName": operation,
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("GraphQL response was not JSON.") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Unexpected GraphQL payload type: {type(payload).__name__}"
            )
        if payload.get("errors"):
            raise RuntimeError(f"GraphQL returned errors: {payload['errors']}")
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected GraphQL data shape: {data!r}")
        return data

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
            if text:
                cleaned.append(text)
        return cleaned

    def _extract_summary(self, data: Dict) -> Optional[str]:
        summaries = data.get("summary") or []
        for entry in summaries:
            text = (entry or {}).get("text")
            if text:
                return text.strip()
        return None

    def _fallback_slug(self, url: str) -> str:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        return parts[-1] if parts else "mangapark"

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

        cover_raw = (
            data.get("urlCoverOri")
            or data.get("urlCover600")
            or data.get("urlCover300")
        )
        cover = self._absolutise_url(self._BASE_URL, cover_raw) if cover_raw else None

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

        seed_path = None
        chapter_nodes = node.get("last_chapterNodes") or []
        if chapter_nodes:
            chapter = chapter_nodes[0] or {}
            chap_data = chapter.get("data") or {}
            url_path = chap_data.get("urlPath")
            if url_path:
                seed_path = url_path

        return comic, seed_path

    def _extract_series_meta_from_html(self, soup: BeautifulSoup) -> Dict[str, Optional[str]]:
        meta: Dict[str, Optional[str]] = {}
        meta_title = soup.find("meta", attrs={"property": "og:title"})
        if meta_title and meta_title.get("content"):
            meta["title"] = meta_title["content"].strip()

        meta_desc = soup.find("meta", attrs={"name": "description"})
        if not meta_desc:
            meta_desc = soup.find("meta", attrs={"property": "og:description"})
        if meta_desc and meta_desc.get("content"):
            meta["desc"] = meta_desc["content"].strip()

        meta_img = soup.find("meta", attrs={"property": "og:image"})
        if meta_img and meta_img.get("content"):
            absolute = self._absolutise_url(self._BASE_URL, meta_img["content"].strip())
            meta["cover"] = absolute
            if absolute:
                meta_img["content"] = absolute

        return meta

    def _find_first_chapter_url(self, soup: BeautifulSoup) -> Optional[str]:
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if "/chapter-" in href:
                return urljoin(self._BASE_URL, href)
        return None

    def _fetch_html(self, url: str, scraper, make_request) -> str:
        response = make_request(url, scraper)
        response.raise_for_status()
        response.encoding = response.encoding or "utf-8"
        return response.text

    # ------------------------------------------------------------------ context
    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        comic_id = self._extract_comic_id(url)
        if not comic_id:
            raise RuntimeError("Unable to determine MangaPark comic id from URL.")

        comic: Dict = {"_series_id": comic_id}
        seed_path: Optional[str] = None
        gql_error: Optional[Exception] = None
        try:
            gql_data = self._graphql_request(
                scraper,
                self._COMIC_DETAILS_QUERY,
                {"id": comic_id, "last": 1},
                "ComicDetails",
            )
            comic, seed_path = self._build_comic_dict(url, gql_data)
            if not comic.get("_series_id"):
                comic["_series_id"] = comic_id
        except Exception as exc:  # pragma: no cover - network path
            gql_error = exc
            comic = {"_series_id": comic_id}
            seed_path = None

        # Fetch the series page HTML for fallback metadata and seed chapter.
        series_url = url
        parsed = urlparse(url)
        if "/chapter" in parsed.path:
            series_url = f"{self._BASE_URL}/title/{comic_id}"
        html = self._fetch_html(series_url, scraper, make_request)
        soup = BeautifulSoup(html, "html.parser")

        html_meta = self._extract_series_meta_from_html(soup)
        for key, value in html_meta.items():
            if value and not comic.get(key):
                comic[key] = value

        # Attempt to gather chapter list and page data from embedded script first.
        embedded = self._extract_embedded_state(html)
        if embedded:
            comic.setdefault("_embedded_state", embedded)
            if embedded.get("chapters"):
                comic["_embedded_chapters"] = embedded["chapters"]
            if embedded.get("pages"):
                comic["_embedded_pages"] = embedded["pages"]

        if not comic.get("title"):
            comic["title"] = self._fallback_slug(url)
        if not comic.get("slug"):
            comic["slug"] = self._fallback_slug(url)
        if not comic.get("hid"):
            comic["hid"] = f"{comic['slug']}"

        if seed_path:
            comic["_seed_chapter_url"] = urljoin(self._BASE_URL, seed_path)
        else:
            fallback_seed = self._find_first_chapter_url(soup)
            if fallback_seed:
                comic["_seed_chapter_url"] = fallback_seed
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
        if "_embedded_chapters" in context.comic:
            chapters_raw = context.comic["_embedded_chapters"]
            from_embedded = True
        else:
            comic_id = context.comic.get("_series_id")
            if not comic_id:
                comic_id = self._extract_comic_id(context.comic.get("_seed_chapter_url", ""))
            if not comic_id:
                return []
            data = self._graphql_request(
                scraper,
                self._CHAPTER_LIST_QUERY,
                {"comicId": comic_id},
                "ChapterList",
            )
            chapters_raw = data.get("get_comicChapterList") or []
            from_embedded = False

        chapters: List[Dict] = []
        for entry in chapters_raw:
            if from_embedded:
                chap_data = entry or {}
                url_path = chap_data.get("url")
                serial_value = chap_data.get("serial") or chap_data.get("number")
                title = chap_data.get("title")
                display = chap_data.get("display")
            else:
                chap_data = (entry or {}).get("data") or {}
                url_path = chap_data.get("urlPath")
                serial_value = chap_data.get("serial")
                title = chap_data.get("title")
                display = chap_data.get("dname")
            if not url_path:
                continue
            chapter_url = urljoin(self._BASE_URL, url_path)
            chap_number = self._normalise_chapter_number(
                serial_value,
                display,
                title,
            )
            title_clean = title or display or None
            chapters.append(
                {
                    "hid": f"{context.identifier}-{chap_number}",
                    "chap": chap_number,
                    "title": title_clean,
                    "url": chapter_url,
                    "group_name": None,
                }
            )
        chapters.sort(key=lambda ch: float(ch["chap"]))
        return chapters

    def _normalise_chapter_number(
        self,
        serial_value,
        dname: Optional[str],
        title: Optional[str],
    ) -> str:
        if isinstance(serial_value, (int, float)):
            if isinstance(serial_value, float) and not serial_value.is_integer():
                return str(serial_value).rstrip("0").rstrip(".")
            return str(int(serial_value))
        candidate_sources = [dname, title]
        for source in candidate_sources:
            if not source:
                continue
            match = re.search(r"(\d+(?:\.\d+)?)", source)
            if match:
                return match.group(1)
        return "0"

    # --------------------------------------------------------------- page data
    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List:
        html = self._fetch_html(chapter["url"], scraper, make_request)
        soup = BeautifulSoup(html, "html.parser")
        images: List[str] = []
        for img in soup.find_all("img"):
            src = img.get("data-src") or img.get("src") or ""
            src = src.strip()
            if not src:
                continue
            if self._looks_like_non_page_asset(src):
                continue
            absolute = self._absolutise_url(chapter["url"], src)
            if absolute and absolute not in images:
                images.append(absolute)

        if not images:
            for match in re.findall(
                r"https?://[^\s\"']+/media/mpup/[^\s\"']+\.(?:webp|png|jpg|jpeg|avif)",
                html,
            ):
                absolute = self._absolutise_url(chapter["url"], match)
                if absolute and absolute not in images:
                    images.append(absolute)

        entries: List = list(images)
        text_paragraphs = self._extract_text_paragraphs(soup)
        if text_paragraphs:
            entries.append(
                {
                    "type": "text",
                    "paragraphs": text_paragraphs,
                    "title": chapter.get("title"),
                }
            )
        return entries

    def _looks_like_non_page_asset(self, url: str) -> bool:
        lowered = url.lower()
        if lowered.startswith("//"):
            lowered = "https:" + lowered
        if any(part in lowered for part in ("/static-assets/", "/favicon", "/logo")):
            return True
        if "/media/mpav/" in lowered or "/media/mpim/" in lowered:
            return True
        if not lowered.startswith("http"):
            return False
        # Manga pages typically live under /media/mpup/...
        return "/media/mpup/" not in lowered

    def _absolutise_url(self, base: str, url: str) -> Optional[str]:
        if not url:
            return None
        url = url.strip()
        if not url:
            return None
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("http"):
            return url
        return urljoin(base, url)

    def _extract_text_paragraphs(self, soup: BeautifulSoup) -> List[str]:
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

    # ----------------------------------------------------------- embedded state
    def _extract_embedded_state(self, html: str) -> Optional[Dict]:
        """
        MangaPark embeds Qwik JSON with compressed keys. We look for a script tag
        with type qwik/json and attempt to parse a simplified structure.
        """
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", attrs={"type": "qwik/json"})
        if not script or not script.string:
            return None

        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            return None

        # The Qwik structure is deeply nested and uses short references.
        # We attempt to locate chapter list and page list heuristically.
        refs = data.get("refs") or {}
        embedded: Dict[str, object] = {}

        chapters = []
        for key, value in refs.items():
            if isinstance(value, str) and "/chapter" in value and "/title/" in value:
                chapters.append({"url": value})
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and "/chapter" in item and "/title/" in item:
                        chapters.append({"url": item})
        if chapters:
            embedded["chapters"] = chapters

        pages = []
        for key, value in refs.items():
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, str) and "/media/mpup/" in item:
                    pages.append(item)
        if pages:
            embedded["pages"] = pages

        return embedded if embedded else None


__all__ = ["MangaParkSiteHandler"]
