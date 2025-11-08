from __future__ import annotations

import json
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from .base import BaseSiteHandler, SiteComicContext


class MangaHubSiteHandler(BaseSiteHandler):
    name = "mangahub"
    domains = ("mangahub.io", "www.mangahub.io")

    _BASE_URL = "https://mangahub.io"
    _GRAPHQL_URL = "https://api.mghcdn.com/graphql"
    _IMAGE_BASE = "https://imgx.mghcdn.com/"
    _THUMB_BASE = "https://thumb.mghcdn.com/"
    _ACCESS_TOKEN = "7d89de0ab95ce91ffa2621f124db8067"

    _MANGA_QUERY = """
    query MangaDetails($slug: String!) {
      manga(x:m01, slug:$slug) {
        id
        title
        slug
        status
        image
        latestChapter
        author
        artist
        genres
        description
        alternativeTitle
        mainSlug
        isYaoi
        isPorn
        isSoftPorn
        isLicensed
        updatedDate
        chapters {
          id
          number
          slug
          title
          date
        }
      }
    }
    """

    _CHAPTER_QUERY = """
    query ChapterPages($slug: String!, $number: Float!) {
      chapter(x:m01, slug:$slug, number:$number) {
        id
        number
        pages
      }
    }
    """

    def configure_session(self, scraper, args) -> None:
        scraper.headers.setdefault("Referer", self._BASE_URL + "/")
        scraper.headers.setdefault("Origin", self._BASE_URL)
        scraper.headers.setdefault("User-Agent", "Mozilla/5.0")
        scraper.headers.setdefault("x-mhub-access", self._ACCESS_TOKEN)

    # ------------------------------------------------------------------ helpers
    def _slug_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            raise RuntimeError("Invalid MangaHub URL.")
        if parts[0] == "manga":
            return parts[1]
        if parts[0] == "chapter":
            return parts[1]
        return parts[-1]

    def _post_graphql(self, scraper, query: str, variables: Dict) -> Dict:
        payload = {"query": query, "variables": variables}
        response = scraper.post(
            self._GRAPHQL_URL,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
        )
        response.raise_for_status()
        data = response.json()
        if "errors" in data:
            raise RuntimeError(f"MangaHub API error: {data['errors']}")
        return data.get("data") or {}

    def _absolute_thumb(self, image_path: Optional[str]) -> Optional[str]:
        if not image_path:
            return None
        if image_path.startswith("http"):
            return image_path
        return urljoin(self._THUMB_BASE, image_path)

    # ----------------------------------------------------------- Base overrides
    def fetch_comic_context(
        self,
        url: str,
        scraper,
        make_request,  # noqa: D401 - unused
    ) -> SiteComicContext:
        slug = self._slug_from_url(url)
        data = self._post_graphql(scraper, self._MANGA_QUERY, {"slug": slug})
        manga = data.get("manga")
        if not manga:
            raise RuntimeError("Unable to load MangaHub series data.")

        title = manga.get("title") or slug.replace("-", " ").title()
        comic: Dict[str, object] = {
            "hid": str(manga.get("id") or slug),
            "title": title,
            "desc": manga.get("description"),
            "cover": self._absolute_thumb(manga.get("image")),
            "authors": [a.strip() for a in (manga.get("author") or "").split(",") if a.strip()],
            "artists": [a.strip() for a in (manga.get("artist") or "").split(",") if a.strip()],
            "genres": manga.get("genres") or [],
            "alt_names": [a.strip() for a in (manga.get("alternativeTitle") or "").split(";") if a.strip()],
            "_slug": slug,
            "_chapters": manga.get("chapters") or [],
        }

        return SiteComicContext(comic=comic, title=title, identifier=str(manga.get("id") or slug), soup=None)

    def get_chapters(
        self,
        context: SiteComicContext,
        scraper,
        language: str,  # noqa: D401 - unused
        make_request,  # noqa: D401 - unused
    ) -> List[Dict]:
        slug = context.comic.get("_slug")
        if not isinstance(slug, str):
            raise RuntimeError("Missing MangaHub slug.")
        chapters = context.comic.get("_chapters") or []
        results: List[Dict] = []
        for chapter in chapters:
            number = chapter.get("number")
            if number is None:
                continue
            chap_slug = chapter.get("slug") or f"chapter-{number}"
            url = f"{self._BASE_URL}/chapter/{slug}/{chap_slug}"
            results.append(
                {
                    "hid": str(chapter.get("id") or f"{slug}-{number}"),
                    "chap": str(number),
                    "title": chapter.get("title") or f"Chapter {number}",
                    "url": url,
                    "uploaded": chapter.get("date"),
                    "_slug": slug,
                    "_number": number,
                }
            )
        results.sort(key=lambda c: float(c.get("chap") or 0))
        return results

    def get_chapter_images(
        self,
        chapter: Dict,
        scraper,
        make_request,  # noqa: D401 - unused
    ) -> List[str]:
        slug = chapter.get("_slug")
        number = chapter.get("_number")
        if not slug or number is None:
            raise RuntimeError("Chapter metadata incomplete for MangaHub.")
        data = self._post_graphql(scraper, self._CHAPTER_QUERY, {"slug": slug, "number": float(number)})
        info = data.get("chapter") or {}
        pages_raw = info.get("pages")
        if not pages_raw:
            raise RuntimeError("MangaHub chapter response missing pages.")
        try:
            pages_data = json.loads(pages_raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Unable to parse MangaHub page list.") from exc
        base_path = pages_data.get("p") or ""
        images = pages_data.get("i") or []
        if not images:
            raise RuntimeError("MangaHub returned an empty page list.")
        path_prefix = (base_path or "").lstrip("/")
        if path_prefix and not path_prefix.endswith("/"):
            path_prefix += "/"
        urls: List[str] = []
        for filename in images:
            if not filename:
                continue
            rel = path_prefix + filename.lstrip("/")
            urls.append(urljoin(self._IMAGE_BASE, rel))
        return urls


__all__ = ["MangaHubSiteHandler"]
