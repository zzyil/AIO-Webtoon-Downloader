from __future__ import annotations

import re
from typing import Dict, List, Optional
from urllib.parse import quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BaseSiteHandler, SearchHit, SiteComicContext


class MangaPillSiteHandler(BaseSiteHandler):
    name = "mangapill"
    domains = ("mangapill.com", "www.mangapill.com")
    
    _BASE_URL = "https://mangapill.com"

    def configure_session(self, scraper, args) -> None:
        scraper.headers.update(
            {
                "Referer": "https://mangapill.com/",
            }
        )

    # -- Helpers -----------------------------------------------------
    def _make_soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def _slug_from_url(self, url: str) -> str:
        # URL: https://mangapill.com/manga/123/title-slug
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]

        if len(path_parts) >= 2 and path_parts[0] == "manga":
            return path_parts[1] # Return ID as slug/identifier
        return path_parts[-1]

    # Extract the first numeric token from a chapter title or URL path.
    # Mirrors sites/madara.py:_extract_chapter_number / weebcentral.py.
    # Without this, get_chapters() below stored the full "Chapter 123"
    # title as `chap`, and aio-dl.py's bucketing at ~line 5885 calls
    # float(chap) which raises ValueError → every chapter logged as
    # "Skipping chapter with invalid number: Chapter 123" → zero
    # chapters downloaded. The regex matches both integer and decimal
    # chapters ("Chapter 47.5" → "47.5"). Returns None when no numeric
    # token is present (oneshots, omake, side stories) so the caller
    # can decide whether to skip or surface them as non-numeric.
    _CHAP_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")

    def _extract_chapter_number(self, text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        match = self._CHAP_NUM_RE.search(text)
        return match.group(1) if match else None

    # -- Base overrides ----------------------------------------------
    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        response = make_request(url, scraper)
        soup = self._make_soup(response.text)
        
        # Title: div.container > div:first-child > div:last-child > div:nth-child(2) > h1
        # Or just h1
        title = soup.select_one("h1")
        title = title.get_text(strip=True) if title else "Unknown"
        
        # Description: div.container > div:first-child > div:last-child > div:nth-child(2) > p
        desc_node = soup.select_one("div.container > div:first-child > div:last-child > div:nth-child(2) > p")
        description = desc_node.get_text(strip=True) if desc_node else ""
        
        # Cover: div.container > div:first-child > div:first-child > img
        cover_node = soup.select_one("div.container > div:first-child > div:first-child > img")
        cover = cover_node.get("data-src") or cover_node.get("src") if cover_node else None
        
        # Genres: a[href*=genre]
        genres = [a.get_text(strip=True) for a in soup.select("a[href*=genre]")]
        
        # Status
        status_node = soup.select_one("div.container > div:first-child > div:last-child > div:nth-child(3) > div:nth-child(2) > div")
        status = status_node.get_text(strip=True) if status_node else "Unknown"
        
        # Authors / year / alt names live in the metadata block
        # (`div.container > div:first-child > div:last-child > div:nth-child(3)`)
        # alongside status. The existing nth-child selector for status only
        # picks one specific cell; walk the entire metadata block for the
        # rest. Robust to label-row reordering, no-op when no label matches.
        authors: List[str] = []
        artists: List[str] = []
        year: Optional[int] = None
        alt_names: List[str] = []
        meta_block = soup.select_one(
            "div.container > div:first-child > div:last-child > div:nth-child(3)"
        )
        if meta_block:
            for row in meta_block.find_all("div", recursive=False):
                children = list(row.find_all("div", recursive=False))
                if len(children) < 2:
                    continue
                label = children[0].get_text(strip=True).lower()
                value_node = children[1]
                value = value_node.get_text(" ", strip=True)
                if not value:
                    continue
                if "author" in label:
                    anchors = [
                        a.get_text(strip=True)
                        for a in value_node.find_all("a")
                        if a.get_text(strip=True)
                    ]
                    authors = anchors or [value]
                elif "artist" in label or "illustrator" in label:
                    # Komikku's details.json wants `artist` independently of
                    # `author`. MangaPill MAY expose this in some series'
                    # metadata block; surface when present, leave empty when
                    # not (the common case — MangaPill typically only lists
                    # an Author row). dry_run_komikku_findings.md §A.
                    anchors = [
                        a.get_text(strip=True)
                        for a in value_node.find_all("a")
                        if a.get_text(strip=True)
                    ]
                    artists = anchors or [value]
                elif "year" in label or "released" in label:
                    year_match = re.search(r"\b(\d{4})\b", value)
                    if year_match:
                        year = int(year_match.group(1))
                elif "alternative" in label or label.startswith("alt"):
                    alt_names = [
                        p.strip() for p in re.split(r"[;,/]", value) if p.strip()
                    ]

        slug = self._slug_from_url(url)

        comic = {
            "hid": slug,
            "title": title,
            "desc": description,
            "status": status,
            "cover": cover,
            "genres": genres,
            "url": url,
        }
        if authors:
            comic["authors"] = authors
        if artists:
            comic["artists"] = artists
        if year is not None:
            comic["year"] = year
        if alt_names:
            comic["alt_names"] = alt_names

        return SiteComicContext(
            comic=comic,
            title=title,
            identifier=slug,
            soup=soup,
        )

    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        soup = context.soup
        if not soup:
            response = make_request(context.comic.get("url"), scraper)
            soup = self._make_soup(response.text)
            
        chapters = []
        # Selector: #chapters > div > a
        for link in soup.select("#chapters > div > a"):
            href = link.get("href")
            if not href:
                continue

            title = link.get_text(strip=True)
            url = urljoin(self._BASE_URL, href)

            # Extract numeric chapter portion. The previous code stored the
            # full title ("Chapter 123") as `chap`; aio-dl.py:5885 calls
            # float(chap) for bucketing and ValueErrors on the non-numeric
            # text, skipping every chapter. Try title first; on failure,
            # extract the segment after `chapter-` in the URL (MangaPill
            # chapter URLs are /chapters/<id>-chapter-<num>, so the bare
            # _CHAP_NUM_RE would mis-grab the leading <id>). When NEITHER
            # surfaces a number, skip the entry — surfacing a non-numeric
            # `chap` would just trigger the same skip downstream with a
            # misleading log line.
            chap_num = self._extract_chapter_number(title)
            if chap_num is None and href:
                m = re.search(r"chapter-(\d+(?:\.\d+)?)", href)
                if m:
                    chap_num = m.group(1)
            if chap_num is None:
                continue

            chapters.append({
                "hid": href,
                "chap": chap_num,
                "title": title,
                "url": url,
                "uploaded": None, # No date in list
            })

        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        url = chapter.get("url")
        response = make_request(url, scraper)
        soup = self._make_soup(response.text)

        images = []
        # Selector: picture img
        for img in soup.select("picture img"):
            src = img.get("data-src") or img.get("src")
            if src:
                images.append(src)

        return images

    # -- Cross-site search ------------------------------------------
    # MangaPill search: GET /search?q=<query>. Result cards are
    # <a class="mb-2" href="/manga/<id>/<slug>"> wrapping two divs:
    #   - first div: primary title (e.g. "Sousou no Frieren")
    #   - second div: alt title / EN romanization
    # The cover lives in a separate <a> with same href but no text content;
    # we filter to title-bearing anchors only and grab the cover by walking
    # the row container.
    _RESULT_HREF_RE = re.compile(r"^/manga/\d+/[\w\-]+")

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
        url = f"{self._BASE_URL}/search?q={quote_plus(clean)}"
        # HTTP errors propagate; orchestrator records the host in the
        # probe-failure cache.
        response = make_request(url, scraper)
        html = response.text
        if not html or len(html) < 200:
            return []

        soup = self._make_soup(html)
        hits: List[SearchHit] = []
        seen: set = set()
        anchors = [
            a for a in soup.select("a[href]")
            if self._RESULT_HREF_RE.match((a.get("href") or "").strip())
        ]
        # Each result has two anchors with the same href (cover + title).
        # Keep only the title-bearing one (has child divs with text).
        for idx, a in enumerate(anchors):
            href = a.get("href") or ""
            divs = [d for d in a.find_all("div", recursive=False) if d.get_text(strip=True)]
            if not divs:
                # cover-only anchor; skip
                continue
            abs_url = urljoin(self._BASE_URL, href).split("?")[0].split("#")[0]
            if abs_url in seen:
                continue
            seen.add(abs_url)

            primary = divs[0].get_text(strip=True)
            alt_titles: List[str] = []
            for d in divs[1:]:
                t = d.get_text(strip=True)
                if t and t != primary:
                    alt_titles.append(t)
            if not primary:
                continue

            # Cover lives on the sibling cover-anchor — find it by href match.
            cover: Optional[str] = None
            for sib in anchors:
                if sib is a or (sib.get("href") or "") != href:
                    continue
                img = sib.select_one("img")
                if img:
                    src = img.get("data-src") or img.get("src")
                    if src:
                        cover = src if src.startswith("http") else urljoin(self._BASE_URL, src)
                        break

            raw_score = max(0.05, 1.0 - (idx / max(1, len(anchors))))
            hits.append(
                SearchHit(
                    site=self.name,
                    title=primary,
                    url=abs_url,
                    cover=cover,
                    alt_titles=alt_titles,
                    year=None,
                    language=None,
                    chapter_count_hint=None,
                    raw_score=raw_score,
                )
            )
            if len(hits) >= limit:
                break
        return hits
