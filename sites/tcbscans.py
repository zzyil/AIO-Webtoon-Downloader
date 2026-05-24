from __future__ import annotations

import re
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BaseSiteHandler, SearchHit, SiteComicContext


class TCBScansSiteHandler(BaseSiteHandler):
    # NOTE: TCBScans's series template exposes only title + description + cover.
    # No genres/authors/artists/status anywhere on the page. Komikku's
    # details.json for tcbscans-sourced series will populate `description`
    # only — the other four fields stay empty (Komikku falls back to
    # status=0/Unknown). This is a site-side limitation, not a parser bug.
    # See dry_run_komikku_findings.md §C and bench/probe_komikku_metadata.py's
    # EXPECTED_MISSING entry (~line 124).
    name = "tcbscans"
    domains = ("tcbonepiecechapters.com", "www.tcbonepiecechapters.com", "tcbscans.com", "www.tcbscans.com")
    _BASE_URL = "https://tcbonepiecechapters.com"

    def configure_session(self, scraper, args) -> None:
        # TCB Scans often requires standard headers
        pass

    # -- Helpers -----------------------------------------------------
    def _fetch_html(self, url: str, scraper, make_request) -> str:
        response = make_request(url, scraper)
        response.encoding = response.encoding or "utf-8"
        return response.text

    def _make_soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    # -- Base overrides ----------------------------------------------
    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        html = self._fetch_html(url, scraper, make_request)
        soup = self._make_soup(html)

        # Selector: div.order-1
        # Title: h1
        # Desc: p
        # Thumb: img src
        
        info_div = soup.select_one("div.order-1")
        if not info_div:
            # Fallback or error
            # Try to find any h1
            title = soup.select_one("h1")
            title = title.get_text(strip=True) if title else "Unknown"
        else:
            title_node = info_div.select_one("h1")
            title = title_node.get_text(strip=True) if title_node else "Unknown"
            
        slug = self._slug_from_url(url)
        
        comic = {
            "hid": slug,
            "title": title,
            "url": url,
        }
        
        if info_div:
            desc_node = info_div.select_one("p")
            if desc_node:
                comic["desc"] = desc_node.get_text(strip=True)
            
            img_node = info_div.select_one("img")
            if img_node:
                src = img_node.get("src")
                if src:
                    comic["cover"] = urljoin(url, src)

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
             html = self._fetch_html(context.comic["url"], scraper, make_request)
             soup = self._make_soup(html)
             
        # Selector: div.grid a
        chapter_links = soup.select("div.grid a")
        
        chapters = []
        for link in chapter_links:
            href = link.get("href")
            if not href:
                continue
            
            url = urljoin(context.comic["url"], href)
            
            # Title extraction
            # Kotlin: element.select("div.font-bold:not(.flex)").text()
            title_node = link.select_one("div.font-bold:not(.flex)")
            raw_title = title_node.get_text(strip=True) if title_node else ""
            
            # Description/Chapter Name
            # Kotlin: element.selectFirst(".text-gray-500")
            desc_node = link.select_one(".text-gray-500")
            desc = desc_node.get_text(strip=True) if desc_node else ""
            
            # Parse chapter number
            # Regex: \d+.?\d+$
            match = re.search(r"(\d+(?:\.\d+)?)", raw_title)
            chap_num = match.group(1) if match else "0"
            
            # Construct full title
            full_title = raw_title
            if desc:
                full_title += f": {desc}"
                
            chapters.append({
                "hid": url, # Use URL as ID
                "chap": chap_num,
                "title": full_title,
                "url": url,
            })
            
        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        url = chapter["url"]
        html = self._fetch_html(url, scraper, make_request)
        soup = self._make_soup(html)
        
        # Selector: picture img, .image-container img
        images = soup.select("picture img, .image-container img")
        
        image_urls = []
        for img in images:
            src = img.get("src")
            if src:
                image_urls.append(urljoin(url, src))
                
        return image_urls

    def _slug_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        return parts[-1] if parts else "unknown"

    # ----------------------------------------------------------------- search
    # TCBScans has no /search endpoint. Their /projects page is the entire
    # catalog (~38 series — TCB only does One Piece + a small curated set
    # of weekly Shonen Jump titles). Client-side filter on title; the catalog
    # is small enough that we don't need pagination or relevance scoring
    # beyond substring match. Same pattern as flamecomics/zeroscans.
    #
    # Domain fallback: tcbscans.com is geo-blocked from some networks
    # ("Supplied countryName is invalid" 200-byte response); use
    # tcbonepiecechapters.com as the canonical search domain. The handler's
    # domains tuple includes both so resolved URLs still match the right
    # handler downstream.
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
        url = f"{self._BASE_URL}/projects"
        response = make_request(url, scraper)
        html = response.text or ""
        if len(html) < 200:
            return []
        soup = self._make_soup(html)

        # Each project lives under TWO anchors (image-only and title-text)
        # both pointing to /mangas/<id>/<slug>. We dedupe by href and pick
        # the title-text anchor when present (image-only has no text content).
        anchors = soup.select('a[href*="/mangas/"]')
        by_href: Dict[str, List] = {}
        for a in anchors:
            href = (a.get("href") or "").strip()
            if not href.startswith("/mangas/"):
                continue
            by_href.setdefault(href, []).append(a)

        ql = clean.lower()
        query_tokens = set(t for t in ql.split() if t)

        scored: List = []
        for href, anchor_list in by_href.items():
            # Pick the title-text anchor (has non-empty text); fall back to
            # the image's alt attribute on the image-only anchor.
            text_anchor = next(
                (a for a in anchor_list if a.get_text(strip=True)), None
            )
            img_anchor = next(
                (a for a in anchor_list if a.find("img")), None
            )
            title = ""
            cover = None
            if text_anchor:
                title = text_anchor.get_text(strip=True)
            if img_anchor:
                img = img_anchor.find("img")
                if img:
                    if not title:
                        title = (img.get("alt") or "").strip()
                    src = img.get("src")
                    if src:
                        cover = src if src.startswith("http") else urljoin(self._BASE_URL, src)
            if not title:
                continue
            tl = title.lower()
            if ql in tl:
                relevance = 1.0
            elif query_tokens and all(tok in tl for tok in query_tokens):
                relevance = 0.7
            else:
                continue
            scored.append((relevance, title, href, cover))

        scored.sort(key=lambda x: -x[0])

        hits: List[SearchHit] = []
        for idx, (relevance, title, href, cover) in enumerate(scored[:limit]):
            url_full = urljoin(self._BASE_URL, href)
            raw_score = max(0.05, relevance * (1.0 - (idx / max(1, len(scored)))))
            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=url_full,
                    cover=cover,
                    alt_titles=[],
                    year=None,
                    language=None,
                    chapter_count_hint=None,
                    raw_score=raw_score,
                )
            )
        return hits


__all__ = ["TCBScansSiteHandler"]
