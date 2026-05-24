from __future__ import annotations

import datetime as dt
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BaseSiteHandler, SearchHit, SiteComicContext


class DynastySiteHandler(BaseSiteHandler):
    name = "dynasty"
    domains = ("dynasty-scans.com", "www.dynasty-scans.com")

    _BASE_URL = "https://dynasty-scans.com"
    _SERIES_DIRS = {"series", "anthologies", "doujins", "issues"}
    _CHAPTER_DIR = "chapters"
    _CHAPTER_PAGE_LIMIT = 25

    def configure_session(self, scraper, args) -> None:
        scraper.headers.setdefault("Referer", self._BASE_URL + "/")
        scraper.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36",
        )

    # ------------------------------------------------------------------ helpers
    def _parse_path(self, url: str) -> Tuple[str, str]:
        parts = [p for p in urlparse(url).path.split("/") if p]
        if len(parts) < 2:
            raise RuntimeError("Dynasty URL missing directory/slug.")
        directory, slug = parts[0], parts[1]
        return directory, slug

    def _json_request(
        self,
        scraper,
        make_request,
        directory: str,
        slug: str,
        page: Optional[int] = None,
    ):
        path = f"/{directory}/{slug}.json"
        if page and page > 1:
            path = f"{path}?page={page}"
        resp = make_request(self._BASE_URL + path, scraper)
        return resp.json()

    def _to_timestamp(self, value: Optional[str]) -> int:
        if not value:
            return 0
        try:
            dt_obj = dt.datetime.strptime(value, "%Y-%m-%d")
            return int(dt.datetime.combine(dt_obj.date(), dt.time(), dt.timezone.utc).timestamp())
        except Exception:
            return 0

    def _clean_description(self, html: Optional[str]) -> Optional[str]:
        if not html:
            return None
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text("\n", strip=True)
        return text or None

    def _chapter_scanlator(self, tags: List[Dict]) -> Optional[str]:
        scanlators = [t["name"] for t in tags if t.get("type") == "Scanlator"]
        return ", ".join(scanlators) if scanlators else None

    # ----------------------------------------------------------- Base overrides
    def fetch_comic_context(
        self,
        url: str,
        scraper,
        make_request,
    ) -> SiteComicContext:
        directory, slug = self._parse_path(url)
        if directory not in self._SERIES_DIRS | {self._CHAPTER_DIR}:
            raise RuntimeError(f"Dynasty directory '{directory}' is not supported.")

        if directory == self._CHAPTER_DIR:
            data = self._json_request(scraper, make_request, directory, slug)
            title = data.get("title") or slug.replace("_", " ").title()
            comic = {
                "hid": slug,
                "title": title,
                "desc": f"Single chapter released on {data.get('released_on')}",
                "cover": urljoin(self._BASE_URL, (data.get("pages") or [{}])[0].get("url", "")),
                "_directory": directory,
                "_slug": slug,
            }
            return SiteComicContext(comic=comic, title=title, identifier=slug, soup=None)

        data = self._json_request(scraper, make_request, directory, slug)
        title = data.get("name") or slug.replace("_", " ").title()
        description = self._clean_description(data.get("description"))
        if data.get("aliases"):
            alias_text = "\n".join(f"• {alias}" for alias in data["aliases"])
            alias_block = f"Aliases:\n{alias_text}"
            description = f"{description}\n\n{alias_block}".strip() if description else alias_block

        # NOTE: Dynasty's /series/<slug>.json API does NOT return a `status`
        # field — only `tags`, `aliases`, `description`, `name`, `cover`,
        # `pages`. For Komikku-mode details.json, the status digit will be
        # "0" (Unknown). Per user direction 2026-05-19 we accept this rather
        # than HTML-scrape the series page for an extra request. See
        # dry_run_komikku_findings.md §C.
        comic = {
            "hid": slug,
            "title": title,
            "desc": description,
            "cover": self._cover_url(data),
            "genres": [tag["name"] for tag in data.get("tags", []) if tag.get("type") == "General"],
            "_directory": directory,
            "_slug": slug,
        }
        authors = [tag["name"] for tag in data.get("tags", []) if tag.get("type") == "Author"]
        if authors:
            comic["authors"] = authors
        artists = [tag["name"] for tag in data.get("tags", []) if tag.get("type") == "Artist"]
        if artists:
            comic["artists"] = artists

        # Aliases already merged into desc above for human-readable display;
        # also surface as a structured field for the Komikku/ComicInfo pipeline.
        aliases_raw = data.get("aliases")
        if isinstance(aliases_raw, list):
            cleaned = [a for a in aliases_raw if isinstance(a, str) and a]
            if cleaned:
                comic["alt_names"] = cleaned

        return SiteComicContext(comic=comic, title=title, identifier=slug, soup=None)

    def get_chapters(
        self,
        context: SiteComicContext,
        scraper,
        language: str,
        make_request,
    ) -> List[Dict]:
        directory = context.comic.get("_directory")
        slug = context.comic.get("_slug")
        if not directory or not slug:
            raise RuntimeError("Dynasty metadata missing.")

        if directory == self._CHAPTER_DIR:
            data = self._json_request(scraper, make_request, directory, slug)
            return [
                {
                    "hid": slug,
                    "chap": "1",
                    "title": data.get("title"),
                    "url": f"/{self._CHAPTER_DIR}/{slug}",
                    "uploaded": self._to_timestamp(data.get("released_on")),
                }
            ]

        first_page = self._json_request(scraper, make_request, directory, slug)
        taggings = list(first_page.get("taggings") or [])
        total_pages = first_page.get("total_pages") or 1

        page = 2
        while page <= total_pages and page <= self._CHAPTER_PAGE_LIMIT:
            more = self._json_request(scraper, make_request, directory, slug, page=page)
            taggings.extend(more.get("taggings") or [])
            page += 1

        chapters: List[Dict] = []
        header = None
        for item in taggings:
            if "header" in item:
                header = item.get("header")
                continue
            permalink = item.get("permalink")
            if not permalink:
                continue
            title = item.get("title") or permalink.replace("_", " ")
            chap_title = f"{header} {title}".strip() if header else title
            tags = item.get("tags") or []
            scanlator = self._chapter_scanlator(tags)
            chapters.append(
                {
                    "hid": permalink,
                    "chap": title,
                    "title": chap_title,
                    "url": f"/{self._CHAPTER_DIR}/{permalink}",
                    "uploaded": self._to_timestamp(item.get("released_on")),
                    "group_name": scanlator,
                }
            )

        if (first_page.get("type") or "").lower() != "doujin":
            chapters.reverse()
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
        url = chapter.get("url")
        if not url:
            raise RuntimeError("Dynasty chapter missing URL.")
        _, slug = self._parse_path(urljoin(self._BASE_URL, url))
        data = self._json_request(scraper, make_request, self._CHAPTER_DIR, slug)
        pages = data.get("pages") or []
        if not pages:
            raise RuntimeError("Dynasty chapter returned no pages.")
        return [urljoin(self._BASE_URL, page.get("url", "")) for page in pages if page.get("url")]

    # ----------------------------------------------------------------- helpers
    def _cover_url(self, data: Dict) -> Optional[str]:
        cover = data.get("cover")
        if cover:
            return urljoin(self._BASE_URL, cover)
        pages = (data.get("pages") or [])
        if pages:
            return urljoin(self._BASE_URL, pages[0].get("url", ""))
        return None

    # ----------------------------------------------------------------- search
    # Dynasty has no JSON search API: /search.json returns 500. Only the HTML
    # /search?q=<query>&classes[]=... path works. Search results live in
    # `<dd>` blocks, each containing `<a class="name" href="/<dir>/<slug>">Title</a>`
    # where <dir> ∈ {series, anthologies, doujins, issues}. There are no
    # cover thumbnails in the search HTML — the chapter-probe path fetches
    # /<dir>/<slug>.json which has the cover, so cover=None here is fine
    # (probe_sample_image's chapter path doesn't need hit.cover).
    _SEARCH_HREF_RE = re.compile(r"^/(series|anthologies|doujins|issues)/[^/?#]+/?$")

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
        # `classes[]` filter keeps results to series-like containers; without it,
        # /search returns chapter results (single uploads), tags, and authors
        # mixed in — those don't map to SiteComicContext-fetchable URLs.
        url = (
            f"{self._BASE_URL}/search"
            f"?q={quote_plus(clean)}"
            f"&classes%5B%5D=Series"
            f"&classes%5B%5D=Anthology"
            f"&classes%5B%5D=Doujin"
        )
        response = make_request(url, scraper)
        html = response.text
        if not html or len(html) < 200:
            return []
        soup = BeautifulSoup(html, "html.parser")

        anchors = [
            a for a in soup.select("dd a.name[href]")
            if self._SEARCH_HREF_RE.match((a.get("href") or "").strip())
        ]
        hits: List[SearchHit] = []
        seen: set = set()
        for idx, a in enumerate(anchors):
            if len(hits) >= limit:
                break
            href = (a.get("href") or "").strip().rstrip("/")
            abs_url = urljoin(self._BASE_URL, href)
            if abs_url in seen:
                continue
            seen.add(abs_url)
            title = a.get_text(strip=True)
            if not title:
                continue
            # Author is the next sibling anchor under /authors/. Used as an
            # alt_title hint so cross-site dedupe can still match when other
            # sites expose the romaji-only title (Dynasty consistently uses
            # the displayed/EN form so this rarely fires, but it's free).
            alt_titles: List[str] = []
            raw_score = max(0.05, 1.0 - (idx / max(1, len(anchors))))
            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=abs_url,
                    cover=None,
                    alt_titles=alt_titles,
                    year=None,
                    language=None,
                    chapter_count_hint=None,
                    raw_score=raw_score,
                )
            )
        return hits


__all__ = ["DynastySiteHandler"]
