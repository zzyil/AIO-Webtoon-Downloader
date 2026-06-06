from __future__ import annotations

import json
import re
from html import unescape
from typing import Dict, List, Optional
from urllib.parse import quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BaseSiteHandler, SearchHit, SiteComicContext
from .hardening import configure_throttling


class AsuraSiteHandler(BaseSiteHandler):
    name = "asura"
    domains = (
        "asuracomic.net",
        "www.asuracomic.net",
        "asurascans.net",
        "www.asurascans.net",
        "asurascans.com",
        "www.asurascans.com",
        "asurascans.org",
        "www.asurascans.org",
        "asuracomic.com",
        "www.asuracomic.com",
    )
    _BASE_URL = "https://asurascans.com"
    _COMICS_HREF_RE = re.compile(r"^/comics/[a-z0-9\-]+/?$")

    def configure_session(self, scraper, args) -> None:
        if "Referer" not in scraper.headers:
            scraper.headers.update(
                {
                    "Referer": "https://asurascans.com/",
                    "Origin": "https://asurascans.com",
                }
            )
        
        # Asura is notoriously sensitive to bots (Cloudflare Turnstile + hidden captchas).
        # We increase the page delays slightly to avoid "Are you human?" checks.
        configure_throttling(
            scraper,
            domains=self.domains,
            gaps={
                "default": 1.5,
                "ajax": 2.0,
                "page": 3.0, # Highly restricted
                "image": 0.5, # Images usually fine once pageloaded
            },
            jitter=1.0 # High jitter to look human
        )

    # -- Helpers -----------------------------------------------------
    def _make_soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def _fetch_html(self, url: str, scraper, make_request) -> str:
        response = make_request(url, scraper)
        response.encoding = response.encoding or "utf-8"
        return response.text

    def _unwrap_rsc(self, value):
        """Unwrap React Server Component wire format [type, value] pairs recursively."""
        if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
            return self._unwrap_rsc(value[1])
        if isinstance(value, list):
            return [self._unwrap_rsc(item) for item in value]
        if isinstance(value, dict):
            return {k: self._unwrap_rsc(v) for k, v in value.items()}
        return value

    def _extract_props_json(self, html: str) -> Optional[Dict]:
        """Extract the RSC props JSON from page HTML.

        The site embeds chapter/series data in a custom element attribute like:
            props="{&quot;seriesSlug&quot;:[0,&quot;...&quot;], ...}"
        We decode and parse this.
        """
        # Look for 'props="{' pattern (HTML-encoded JSON in a props attribute)
        match = re.search(r'props="(\{&quot;.*?})"', html, re.DOTALL)
        if not match:
            return None
        raw = unescape(match.group(1))
        try:
            data = json.loads(raw)
            return self._unwrap_rsc(data)
        except (json.JSONDecodeError, ValueError):
            return None

    def _extract_chapters_from_html(self, html: str) -> List[Dict]:
        """Extract chapter list from the comic page HTML.

        Chapters are <a> tags inside a scrollable container:
            div.max-h-[500px] > a[href*="/chapter/"]
        """
        soup = BeautifulSoup(html, "html.parser")
        chapters = []

        # Primary: links inside the scrollable chapter list
        chapter_links = soup.select('a[href*="/chapter/"]')
        seen = set()
        for a in chapter_links:
            href = a.get("href", "")
            if not href or href in seen:
                continue
            seen.add(href)

            # Extract chapter number from URL: /comics/{slug}/chapter/{number}
            ch_match = re.search(r'/chapter/(\d+(?:\.\d+)?)', href)
            if not ch_match:
                continue
            chap_num = ch_match.group(1)

            # Title: try to get it from the link text
            text = a.get_text(strip=True)
            # Typical text: "Chapter109Choicelast week" — extract meaningful part
            title_match = re.match(r'Chapter\s*\d+(?:\.\d+)?\s*(.*?)(?:\d+\s*\w+\s*ago|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)', text, re.IGNORECASE)
            title = title_match.group(1).strip() if title_match else ""
            if not title:
                title = f"Chapter {chap_num}"

            chapters.append({
                "chap": chap_num,
                "title": title,
                "href": href,
            })

        return chapters

    def _extract_title_from_html(self, html: str) -> str:
        """Extract comic title from the page HTML."""
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find("h1")
        if h1:
            return h1.get_text(strip=True)
        return ""

    def _extract_images_from_html(self, html: str) -> List[str]:
        """Extract chapter page image URLs from the HTML.

        Strategy 1: Parse RSC props JSON embedded in the page (has all images).
        Strategy 2: Regex for CDN image URLs in the HTML (fallback).
        """
        images = []

        # Strategy 1: RSC props JSON
        props = self._extract_props_json(html)
        if props and isinstance(props.get("pages"), list):
            for page in props["pages"]:
                if isinstance(page, dict) and page.get("url"):
                    images.append(page["url"])
            if images:
                return images

        # Strategy 2: Regex for all CDN chapter image URLs
        pattern = r'https://cdn\.asurascans\.com/asura-images/chapters/[^"&\s]+'
        # Decode &quot; first for HTML-encoded contexts
        decoded_html = unescape(html)
        urls = re.findall(pattern, decoded_html)
        seen = set()
        for url in urls:
            if url not in seen:
                seen.add(url)
                images.append(url)

        return images

    def _extract_people_from_html(self, html: str) -> Dict[str, List[str]]:
        soup = BeautifulSoup(html, "html.parser")
        authors: List[str] = []
        artists: List[str] = []
        # Look for author/artist info sections
        for h3 in soup.find_all("h3"):
            text = h3.get_text(strip=True).lower()
            next_el = h3.find_next_sibling()
            if not next_el:
                # Try parent's next h3
                parent = h3.parent
                if parent:
                    siblings = parent.find_all("h3")
                    if len(siblings) >= 2 and siblings[0] == h3:
                        next_el = siblings[1]
            if not next_el:
                continue
            value = next_el.get_text(strip=True)
            if "author" in text or "writer" in text:
                authors = [p.strip() for p in re.split(r'[,/]', value) if p.strip()]
            elif "artist" in text or "illustrator" in text:
                artists = [p.strip() for p in re.split(r'[,/]', value) if p.strip()]
        result: Dict[str, List[str]] = {}
        if authors:
            result["authors"] = authors
        if artists:
            result["artists"] = artists
            if not authors:
                result.setdefault("authors", artists)
        return result

    def _slug_from_url(self, url: str) -> str:
        path = urlparse(url).path
        parts = [part for part in path.split("/") if part]
        if not parts:
            return ""
        # New format: /comics/<slug> or /comics/<slug>/chapter/<n>
        if parts[0] == "comics" and len(parts) > 1:
            return parts[1]
        # Old format: /series/<slug> or /series/<slug>/chapter/<n>
        if parts[0] == "series" and len(parts) > 1:
            return parts[1]
        return parts[0]

    def _chapter_url(self, base: str, slug: str, chapter_value: str) -> str:
        return f"{base}/comics/{slug}/chapter/{chapter_value}"

    # -- Base overrides ----------------------------------------------
    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        html = self._fetch_html(url, scraper, make_request)

        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        slug = self._slug_from_url(url)

        # Extract title
        title = self._extract_title_from_html(html)
        if not title:
            title = slug

        # Try RSC props first for metadata
        props = self._extract_props_json(html)

        comic: Dict = {}
        if props:
            comic = {
                "name": props.get("seriesName") or title,
                "slug": props.get("seriesSlug") or slug,
                "id": props.get("seriesId"),
                "cover": props.get("seriesCover"),
            }
        else:
            comic = {
                "name": title,
                "slug": slug,
            }

        comic.setdefault("slug", slug)
        comic.setdefault("name", title)
        # Stabilize the series hid against Asura's rotating slug hash. Asura
        # bakes a rotating hex suffix into the slug
        # ("sss-class-suicide-hunter-46f09241") and the prop-embedded seriesId
        # is unreliable here (the generic props extractor grabs the nav block,
        # not the series object — comic.get("id") is almost always None). Using
        # the raw slug made the hid drift across crashes/resumes/site migrations
        # (asurascans.com -> asuracomic.net, /comics/ -> /series/), which spawned
        # duplicate "(hid=...)" folders and broke resume. Strip the trailing hash
        # so one series maps to one stable folder. The downloader keys folders
        # and resume off context.identifier (aio-dl.py:6877:
        # `hid, title = context.identifier, ...`), NOT comic["hid"], so we
        # return stable_hid as the identifier below. The FULL slug stays in
        # comic["slug"] and is what get_chapters uses to build
        # /comics/<full-slug>/chapter/<n> URLs.
        # Migration: allocate_series_output_dir reuses pre-fix full-slug folders
        # via its hash-tolerant _marker_matches (same -[0-9a-f]{6,}$ strip).
        stable_hid = re.sub(r"-[0-9a-f]{6,}$", "", slug) or slug
        comic["hid"] = stable_hid
        comic["slug"] = slug  # full slug (with hash) for chapter URL building
        if comic.get("cover") and not comic.get("thumb"):
            comic["thumb"] = comic["cover"]
        comic["_base_url"] = base_url

        # Extract people metadata
        extra_people = self._extract_people_from_html(html)
        for key, value in extra_people.items():
            if value:
                comic[key] = value

        # Extract chapter list from HTML
        chapter_list = self._extract_chapters_from_html(html)
        comic["_chapter_list"] = chapter_list

        return SiteComicContext(
            comic=comic,
            title=comic["name"],
            identifier=stable_hid,
            soup=None,
        )

    def extract_additional_metadata(
        self, context: SiteComicContext
    ) -> Dict[str, List[str]]:
        comic = context.comic or {}
        metadata: Dict[str, List[str]] = {}

        description = comic.get("description") or comic.get("summary")
        if description:
            comic["desc"] = description

        genres = comic.get("genres")
        if isinstance(genres, list):
            metadata["genres"] = [g["name"] for g in genres if isinstance(g, dict) and g.get("name")]

        for key, target in (("authors", "authors"), ("artists", "artists")):
            if key in comic and isinstance(comic[key], list):
                if comic[key] and isinstance(comic[key][0], dict):
                    metadata[target] = [
                        item["name"]
                        for item in comic[key]
                        if isinstance(item, dict) and item.get("name")
                    ]
                else:
                    metadata[target] = [str(item).strip() for item in comic[key] if str(item).strip()]

        return metadata

    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        comic = context.comic or {}
        # Full slug (with hash) for chapter URLs — context.identifier is now the
        # hash-stripped stable hid, so build URLs from comic["slug"] instead.
        slug = comic.get("slug") or context.identifier
        base_url = comic.get("_base_url") or "https://asurascans.com"
        chapter_list: List[Dict] = comic.get("_chapter_list", [])
        chapters: List[Dict] = []

        def chapter_sort_key(item):
            try:
                return float(item["chap"])
            except (ValueError, KeyError):
                return float("inf")

        for entry in sorted(chapter_list, key=chapter_sort_key):
            chap_num = entry.get("chap", "")
            href = entry.get("href", "")
            title = entry.get("title", f"Chapter {chap_num}")

            # Build full URL
            if href.startswith("/"):
                chapter_url = f"{base_url}{href}"
            elif href.startswith("http"):
                chapter_url = href
            else:
                chapter_url = self._chapter_url(base_url, slug, chap_num)

            chapters.append(
                {
                    "hid": f"{slug}-{chap_num}",
                    "chap": chap_num,
                    "title": title,
                    "url": chapter_url,
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
        images = self._extract_images_from_html(html)

        if not images:
            raise RuntimeError(
                f"No images found for Asura chapter: {chapter_url}"
            )
        return images

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
        url = f"{self._BASE_URL}/browse?search={quote_plus(clean)}"
        response = make_request(url, scraper)
        html = response.text or ""
        if len(html) < 1000:
            return []
        soup = self._make_soup(html)

        by_href: Dict[str, List] = {}
        for anchor in soup.select('a[href^="/comics/"]'):
            href = (anchor.get("href") or "").strip()
            if not self._COMICS_HREF_RE.match(href):
                continue
            by_href.setdefault(href, []).append(anchor)

        hits: List[SearchHit] = []
        for idx, (href, anchors) in enumerate(by_href.items()):
            if len(hits) >= limit:
                break
            title = ""
            cover = None
            for anchor in anchors:
                if not title:
                    h3 = anchor.select_one("h3")
                    if h3:
                        title = h3.get_text(strip=True)
                if not cover:
                    img = anchor.select_one("img")
                    if img:
                        if not title:
                            title = (img.get("alt") or "").strip()
                        src = (img.get("src") or "").strip()
                        if src:
                            cover = src if src.startswith("http") else urljoin(self._BASE_URL, src)
            if not title:
                continue
            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=urljoin(self._BASE_URL, href),
                    cover=cover,
                    raw_score=max(0.05, 1.0 - (idx / max(1, len(by_href)))),
                )
            )
        return hits


__all__ = ["AsuraSiteHandler"]
