"""Base handler for MangaThemesia-based sites."""

from __future__ import annotations
from typing import Dict, List, Optional, Callable
from urllib.parse import urljoin, quote
import re
from bs4 import BeautifulSoup, NavigableString
from .base import BaseSiteHandler, SearchHit, SiteComicContext
from .mangathemesia_utils import (
    extract_ts_reader_images,
    extract_ts_reader_payload,
)
try:
    from .playwright_utils import fetch_html_playwright, PLAYWRIGHT_AVAILABLE
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    def fetch_html_playwright(*args, **kwargs):
        raise ImportError("Playwright not available")

# Zendriver-based CF cookie path (ported from upstream/main 2026-05-13).
# Used by handlers that set use_zendriver=True in sites/__init__.py — these
# sites' WAF challenges can't be solved by cloudscraper but Zendriver +
# headless Chrome can. fetch_html_zendriver fetches the page; sync_cf_cookies
# copies the live cf_clearance cookie into the scraper session so subsequent
# plain requests (image fetches, chapter pages) inherit the solved challenge.
# Graceful fallback when zendriver isn't installed — affected handlers will
# still load but will fail on actual fetch attempts with a clear error.
try:
    from .crawlee_utils import (
        fetch_html_with_cf_cookies,
        fetch_html_zendriver,
        sync_cf_cookies,
        ZENDRIVER_AVAILABLE,
    )
except ImportError:
    ZENDRIVER_AVAILABLE = False
    def fetch_html_with_cf_cookies(*args, **kwargs):
        raise ImportError("zendriver not available")
    def fetch_html_zendriver(*args, **kwargs):
        raise ImportError("zendriver not available")
    def sync_cf_cookies(*args, **kwargs):
        pass

class MangaThemesiaSiteHandler(BaseSiteHandler):
    """Base handler for MangaThemesia framework sites."""
    
    def __init__(
        self,
        name: str,
        display_name: str,
        base_url: str,
        domains: tuple,
        url_normalizer: Optional[Callable[[str], str]] = None,
        chapter_filter: Optional[str] = None,
        use_playwright: bool = False,
        use_zendriver: bool = False,
        chapter_selector: Optional[str] = None,
        verify_ssl: bool = True,
    ):
        self.name = name
        self.display_name = display_name
        self.base_url = base_url
        self.domains = domains
        self._url_normalizer = url_normalizer
        self._chapter_filter = chapter_filter
        self.use_playwright = use_playwright
        self.use_zendriver = use_zendriver
        self.chapter_selector = chapter_selector
        self.verify_ssl = verify_ssl
        self.chapter_ajax = None
    
    def configure_session(self, scraper, args) -> None:
        scraper.headers.update({"Referer": f"{self.base_url}/"})
        scraper.verify = self.verify_ssl
        
        if not self.verify_ssl:
            # Mount a standard adapter to avoid SSL context issues with cloudscraper/Python 3.14
            from requests.adapters import HTTPAdapter
            adapter = HTTPAdapter()
            for domain in self.domains:
                scraper.mount(f"https://{domain}", adapter)
    
    def _make_soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def _normalize_url(self, url: str) -> str:
        """Normalize URL if custom normalizer is provided."""
        if self._url_normalizer:
            return self._url_normalizer(url)
        return url

    def _extract_imptdt_values(self, soup: BeautifulSoup, label: str) -> List[str]:
        """Extract values from a MangaThemesia `.imptdt` label/value row.

        Real MangaThemesia (the WordPress theme this base class is named for)
        renders series metadata as `<div class="imptdt">Label <i>Value</i></div>`
        rows — one each for Status, Type, Released, Author, Artist, Serialization,
        Posted By. The label is plain text at the start; values are wrapped in
        `<a>` or `<i>` elements (or the bare remainder text when neither).

        This is the canonical extractor. The existing `.author-content`,
        `.genres-content`, `.post-status` selectors in `fetch_comic_context`
        below are Madara-theme selectors that happen to also work on a handful
        of MT sites that mix themes; we keep them as a layered fallback. See
        sites/tecnoxmoon.py:106-126 for the original implementation this is
        ported from — that handler had to roll its own enrichment because the
        base parser didn't read `.imptdt` rows.

        Returns a dedup-preserving list (first occurrence wins). Empty list
        when no matching row exists.
        """
        # Match the label only when followed by `:`, whitespace, or end-of-
        # string. The previous bare `startswith(label.lower())` accepted
        # 'Author Note:', 'Authored By:', 'Artist Statement:', 'Status
        # Update:' — any of which would feed wrong content back through the
        # `if imptdt_authors: authors = imptdt_authors` assignments below and
        # silently overwrite the canonical author/artist/status lists parsed
        # by the Madara-style selectors. The character class `[:\s]` covers
        # the realistic delimiters MangaThemesia variants render between the
        # label and its value (`:`, space, NBSP — `\s` covers NBSP per
        # re.UNICODE which is the default in Py3).
        label_lower = label.lower()
        boundary_pattern = re.compile(
            r"^" + re.escape(label_lower) + r"(?:[:\s]|$)"
        )
        for row in soup.select(".imptdt"):
            text = row.get_text(" ", strip=True)
            text_lower = text.lower()
            if not boundary_pattern.match(text_lower):
                continue
            values: List[str] = []
            for node in row.select("a, i"):
                value = node.get_text(strip=True)
                if value:
                    values.append(value)
            if not values:
                # Strip the label prefix AND any trailing `:` / whitespace
                # before re-using the remainder. `len(label)` alone left
                # entries like ": Real Name" after the boundary-aware match
                # promoted a `Status:`-prefixed row to a value of `: Real`.
                remainder = text[len(label):].lstrip(":").strip()
                if remainder:
                    values.append(remainder)
            if values:
                seen: List[str] = []
                for val in values:
                    if val not in seen:
                        seen.append(val)
                return seen
        return []

    # ---------------------------------------------------------------- search
    # MangaThemesia framework search: GET /?s=<query> returns the standard
    # WordPress search-results page with manga cards under .listupd .bs.
    # Each card structure:
    #   <div class="bs">
    #     <div class="bsx">
    #       <a href="/manga/<slug>/" title="<Title>">
    #         <div class="limit">
    #           <img class="ts-post-image" src="<cover>" />
    #         </div>
    #         <div class="bigor">
    #           <div class="tt"><h2 itemprop="headline"><Title></h2></div>
    #         </div>
    #       </a>
    #     </div>
    #   </div>
    # Single implementation covers ~50 sites in MANGATHEMESIA_SITES at once.
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
        url = f"{self.base_url.rstrip('/')}/?s={quote(clean)}"
        # HTTP errors propagate to orchestrator's _run_one for probe-cache
        # tracking. Sites that return tiny bodies (some MangaThemesia sites
        # serve a JS-only search and return ~24 bytes from the static path)
        # produce empty results without raising.
        response = make_request(url, scraper)
        html = response.text or ""
        if len(html) < 200:
            return []
        soup = self._make_soup(html)

        # Primary: cards in .listupd .bs .bsx > a. Fallback: any .bs with
        # a /manga/ link if the .bsx wrapper changed.
        cards = soup.select(".listupd .bs, .bs")
        if not cards:
            return []

        hits: List[SearchHit] = []
        seen: set = set()
        for idx, card in enumerate(cards):
            if len(hits) >= limit:
                break
            link = card.select_one("a[href]")
            if not link:
                continue
            href = (link.get("href") or "").strip()
            if not href:
                continue
            # Filter to series/manga URLs only — skip nav links, ads, etc.
            if not re.search(r"/(manga|series|comic|comics)/[^/?#]+", href):
                continue
            abs_url = href if href.startswith("http") else urljoin(self.base_url, href)
            abs_url = abs_url.split("?")[0].split("#")[0]
            if abs_url in seen:
                continue
            seen.add(abs_url)

            # Title preference: <a title="..."> attribute → <h2 itemprop>
            # → fallback to slug-derived. The title attribute is the most
            # reliable across MangaThemesia variants.
            title = (link.get("title") or "").strip()
            if not title:
                t = card.select_one(".tt h2, h2[itemprop='headline'], .tt, h2, h3")
                if t:
                    title = t.get_text(strip=True)
            if not title:
                continue

            # Cover from img inside the card (data-src for lazy-load).
            cover: Optional[str] = None
            img = card.select_one("img")
            if img is not None:
                src = (
                    img.get("data-src")
                    or img.get("data-lazy-src")
                    or img.get("data-cfsrc")
                    or img.get("src")
                )
                if src:
                    cover = src if src.startswith("http") else urljoin(self.base_url, src)

            raw_score = max(0.05, 1.0 - (idx / max(1, len(cards))))
            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=abs_url,
                    cover=cover,
                    alt_titles=[],
                    year=None,
                    language=None,
                    chapter_count_hint=None,
                    raw_score=raw_score,
                )
            )
        return hits

    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        url = self._normalize_url(url)
        if self.use_zendriver:
            # Zendriver-based path: fetch via headless Chrome to solve CF
            # challenges, then sync the captured cf_clearance cookie back
            # into the scraper so subsequent plain HTTP calls (image fetches,
            # chapter listing) inherit the solved challenge. wait_selector
            # waits for the series title element to ensure the page actually
            # rendered before we parse. Stashed raw HTML lets get_chapters
            # reuse it without re-launching Chrome.
            html = fetch_html_zendriver(
                url,
                wait_selector="h1.entry-title, h1.series-title, h1.post-title",
            )
            sync_cf_cookies(scraper, url)
            soup = BeautifulSoup(html, "html.parser")
        elif self.use_playwright:
            html = fetch_html_playwright(url)
            soup = BeautifulSoup(html, "html.parser")
        else:
            response = make_request(url, scraper)
            html = response.text
            soup = BeautifulSoup(html, "html.parser")
        
        # Standard MangaThemesia selectors
        title_node = soup.select_one("h1.entry-title, h1.series-title, h1.post-title")
        if not title_node:
            # Fallback to page title for sites like OmegaScans
            if soup.title:
                title = soup.title.get_text(strip=True).split("-")[0].strip()
            else:
                title = "Unknown"
        else:
            title = title_node.get_text(strip=True)
        
        desc_node = soup.select_one(".entry-content p, .summary__content p")
        desc = desc_node.get_text(strip=True) if desc_node else None
        # Real-MangaThemesia desc fallback: itemprop="description" is the
        # canonical schema.org tag. Sites that strip the `.entry-content`
        # wrapper but keep the structured-data markup land here.
        if not desc:
            itemprop_desc = soup.select_one("[itemprop='description']")
            if itemprop_desc is not None:
                text = itemprop_desc.get_text(" ", strip=True)
                if text:
                    desc = text
        
        # Cover image
        cover_node = soup.select_one(".thumb img, .summary_image img")
        cover = None
        if cover_node:
            cover = (
                cover_node.get("src")
                or cover_node.get("data-src")
                or cover_node.get("data-lazy-src")
            )
        if not cover:
            bg_container = soup.select_one(".thumb, .summary_image")
            if bg_container:
                style = bg_container.get("style") or ""
                # Single-escape parens are correct here: in a raw string,
                # `\\(` is two characters (backslash + paren) and the regex
                # looks for a literal backslash, which never appears in
                # `style="background-image: url(...)"`. The previous
                # double-escape made cover-from-style detection always fail
                # silently — handlers fell through to the WP REST API path.
                match = re.search(r"url\(['\"]?(.*?)['\"]?\)", style)
                if match:
                    cover = match.group(1)
        
        # Authors — Madara-style selectors first (a small minority of MT
        # sites render Madara markup); imptdt rows are the real MangaThemesia
        # surface and override when present.
        authors = []
        author_nodes = soup.select(".author-content a, .artist-content a")
        for node in author_nodes:
            text = node.get_text(strip=True)
            if text:
                authors.append(text)
        imptdt_authors = self._extract_imptdt_values(soup, "Author")
        if imptdt_authors:
            authors = imptdt_authors

        # Artists — real MangaThemesia exposes a separate Artist row. Komikku's
        # details.json requires the artist field; we now extract it here. Before
        # this fix the base parser had no `artists` extraction at all (see
        # dry_run_komikku_findings.md §A — rizzfables/rizzcomic/violetscans).
        artists: List[str] = []
        imptdt_artists = self._extract_imptdt_values(soup, "Artist")
        if imptdt_artists:
            artists = imptdt_artists

        # Genres — Madara-style fallback first, then the canonical `.mgen a`
        # used by real MangaThemesia.
        genres = []
        genre_nodes = soup.select(".genres-content a")
        for node in genre_nodes:
            text = node.get_text(strip=True)
            if text:
                genres.append(text)
        if not genres:
            for node in soup.select(".mgen a"):
                text = node.get_text(strip=True)
                if text:
                    genres.append(text)

        # Status — Madara fallback first, then imptdt Status row.
        status_node = soup.select_one(".post-status .summary-content, .status-content")
        status = status_node.get_text(strip=True) if status_node else "Unknown"
        if not status or status == "Unknown":
            imptdt_status = self._extract_imptdt_values(soup, "Status")
            if imptdt_status:
                status = imptdt_status[0]

        # Alt titles — MT skins surface them under `.seriestualt`. Splits on
        # the usual separator zoo; deduped while preserving order. Skip year:
        # MT's "Posted On" is the series-page creation date, not series start.
        alt_titles: List[str] = []
        for el in soup.select(".seriestualt"):
            text = el.get_text(" ", strip=True)
            if not text:
                continue
            for piece in re.split(r"[,;/|]", text):
                p = piece.strip()
                if p and p not in alt_titles:
                    alt_titles.append(p)

        slug = url.rstrip("/").split("/")[-1]
        post_id_from_html = self._extract_post_id(html)
        api_data = None
        if title == "Unknown" or not desc or not cover or not post_id_from_html:
            api_data = self._fetch_series_metadata_via_api(slug, scraper)
            if api_data:
                title = api_data.get("title") or title
                desc = api_data.get("desc") or desc
                cover = api_data.get("cover") or cover
                post_id_from_html = post_id_from_html or api_data.get("post_id")
        
        comic = {
            "hid": slug,
            "title": title,
            "desc": desc,
            "cover": cover,
            "authors": authors,
            # `artists` was missing from the base before the 2026-05-19
            # Komikku-metadata pass. aio-dl.py:6042 joins this list into
            # details.json's `artist` field; emit it even when empty so the
            # Komikku writer treats it as "no artist" rather than KeyError.
            "artists": artists,
            "genres": genres,
            "status": status,
            "url": url,
        }
        if alt_titles:
            comic["alt_names"] = alt_titles

        comic["_raw_html"] = html
        if post_id_from_html:
            comic["_post_id"] = str(post_id_from_html)

        return SiteComicContext(comic=comic, title=title, identifier=slug, soup=soup)
    
    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        url = context.comic["url"]

        if self.use_zendriver:
            # Reuse the HTML already fetched by fetch_comic_context (stashed
            # in context.comic["_raw_html"] above) to avoid launching a
            # second browser window for the same series. CF cookies were
            # cached on the first Zendriver call so subsequent plain make_request
            # calls also inherit the solved challenge.
            cached_html = context.comic.get("_raw_html")
            if cached_html:
                soup = BeautifulSoup(cached_html, "html.parser")
            else:
                # Fallback: cookies should already be cached from fetch_comic_context
                sync_cf_cookies(scraper, url)
                response = make_request(url, scraper)
                soup = BeautifulSoup(response.text, "html.parser")
        elif self.use_playwright:
            # Use custom selector for waiting if available
            wait_sel = self.chapter_selector if self.chapter_selector else "#chapterlist li, .eplister li"
            html = fetch_html_playwright(url, wait_selector=wait_sel)
            soup = BeautifulSoup(html, "html.parser")
        else:
            # Re-fetch page to ensure we have fresh content (sometimes context soup is enough, but safe to refetch)
            # Actually, we can reuse context.soup if it's full page, but let's follow pattern
            response = make_request(url, scraper)
            soup = BeautifulSoup(response.text, "html.parser")

        chapters = []
        
        # Common selectors for MangaThemesia chapter lists
        # Added .chapter-list li for some sites
        if self.chapter_selector:
            selector = self.chapter_selector
        else:
            selector = "#chapterlist li, .eplister li, .chapter-list li"
            
        if self._chapter_filter:
            # Apply filter to selector(s)
            # This is a bit complex if selector is a list string, but assuming simple cases
            pass 
        
        for item in soup.select(selector):
            # Check if item is 'a' or 'li'
            if item.name == 'a':
                link = item
            else:
                link = item.select_one("a")
                
            if not link:
                continue
            
            href = link.get("href")
            if not href:
                # Skip chapters without URL (locked/paid)
                continue
                
            # Ensure absolute URL
            if not href.startswith("http"):
                from urllib.parse import urljoin
                href = urljoin(self.base_url, href)
                
            if self._url_normalizer:
                href = self._url_normalizer(href)
            
            title_node = link.select_one(".chapternum, .epl-num")
            if title_node:
                title = title_node.get_text(strip=True)
            else:
                # Use separator to prevent merging text (e.g. "Chapter 1" + "Date" -> "Chapter 1Date")
                title = link.get_text(separator=" ", strip=True)
            
            # Extract numeric chapter number
            # First try from URL as it's often cleaner for sites like OmegaScans
            # /chapter-123
            import re
            chap = None
            url_match = re.search(r'chapter-(\d+(?:\.\d+)?)', href)
            if url_match:
                chap = float(url_match.group(1))
            
            if chap is None:
                # Fallback to title extraction
                chap_match = re.search(r'Chapter\s+(\d+(?:\.\d+)?)', title, re.IGNORECASE)
                if chap_match:
                    chap = float(chap_match.group(1))
                else:
                    # Fallback to first number found
                    chap_match = re.search(r'(\d+(?:\.\d+)?)', title)
                    chap = float(chap_match.group(1)) if chap_match else 0.0
            
            date_text = ""
            date_node = link.select_one(".chapterdate, .epl-date")
            if date_node:
                date_text = date_node.get_text(strip=True)
            
            chapters.append({
                "hid": href,
                "chap": chap,
                "title": title,
                "url": href,
                "uploaded": date_text,
            })
        
        # Reverse to get oldest first (MangaThemesia returns newest first)
        chapters.reverse()
        
        return chapters
    
    def get_chapter_images(
        self, chapter: Dict, scraper, make_request
    ) -> List:
        url = chapter["url"]

        html: Optional[str]
        soup: Optional[BeautifulSoup]
        if self.use_zendriver:
            # Reuse CF cookies captured in fetch_comic_context. The scraper
            # session already carries the solved cf_clearance, so a plain
            # make_request works — no need to open yet another Chrome
            # window per chapter (which would 5-10× the per-chapter latency).
            sync_cf_cookies(scraper, url)
            response = make_request(url, scraper)
            html = response.text
            soup = BeautifulSoup(html, "html.parser")
        elif self.use_playwright:
            html = fetch_html_playwright(url, wait_selector="img.ts-main-image")
            soup = BeautifulSoup(html, "html.parser")
        else:
            response = make_request(url, scraper)
            html = response.text
            soup = self._make_soup(html)

        ts_payload = extract_ts_reader_payload(html or "")
        if ts_payload and ts_payload.get("is_novel"):
            paragraphs = self._extract_novel_paragraphs(soup)
            if paragraphs:
                return [
                    {
                        "type": "text",
                        "paragraphs": paragraphs,
                        "title": chapter.get("title"),
                    }
                ]

        if self.use_playwright and soup:
            dom_images: List[str] = []
            for img in soup.select("img.ts-main-image, .reader-area img"):
                src = img.get("src") or img.get("data-src")
                if src:
                    dom_images.append(src)
            dom_images = self._finalize_image_urls(dom_images, url)
            if dom_images:
                return dom_images

        images = extract_ts_reader_images(html or "", ts_payload)
        images = self._finalize_image_urls(images, url)
        if images:
            return images

        if soup:
            html_images: List[str] = []
            for img in soup.select("#readerarea img, .reading-content img"):
                src = img.get("src") or img.get("data-src")
                if src:
                    html_images.append(src)
            html_images = self._finalize_image_urls(html_images, url)
            if html_images:
                return html_images

            paragraphs = self._extract_novel_paragraphs(soup)
            if paragraphs:
                return [
                    {
                        "type": "text",
                        "paragraphs": paragraphs,
                        "title": chapter.get("title"),
                    }
                ]

        return []

    def _parse_chapter_elements(self, elements) -> List[Dict]:
        chapters: List[Dict] = []
        if not elements:
            return chapters

        for item in elements:
            if item.name == "option":
                href = item.get("value")
                title = item.get_text(strip=True)
                date_text = ""
                link = None
            else:
                if item.name == "a":
                    link = item
                else:
                    link = item.select_one("a")
                if not link:
                    continue
                href = link.get("href")
                if not href:
                    continue
                title_node = link.select_one(".chapternum, .epl-num")
                if title_node:
                    title = title_node.get_text(strip=True)
                else:
                    title = link.get_text(separator=" ", strip=True)
                date_node = link.select_one(".chapterdate, .epl-date")
                date_text = date_node.get_text(strip=True) if date_node else ""

            if not href:
                continue
            if not href.startswith("http"):
                href = urljoin(self.base_url, href)
            if self._url_normalizer:
                href = self._url_normalizer(href)

            chap = None
            url_match = re.search(r'chapter-(\d+(?:\.\d+)?)', href)
            if url_match:
                chap = float(url_match.group(1))
            if chap is None:
                chap_match = re.search(r'Chapter\s+(\d+(?:\.\d+)?)', title, re.IGNORECASE)
                if chap_match:
                    chap = float(chap_match.group(1))
                else:
                    chap_match = re.search(r'(\d+(?:\.\d+)?)', title)
                    chap = float(chap_match.group(1)) if chap_match else 0.0

            chapters.append({
                "hid": href,
                "chap": chap,
                "title": title,
                "url": href,
                "uploaded": date_text,
            })

        return chapters

    def _fetch_chapters_via_ajax(self, context: SiteComicContext, scraper, page_html: Optional[str]) -> List[Dict]:
        if not self.chapter_ajax:
            return []

        html_source = page_html or context.comic.get("_raw_html", "")
        post_id = context.comic.get("_post_id") or self._extract_post_id(html_source or "")
        if not post_id:
            return []

        ajax_url = (
            self.chapter_ajax.get("url")
            or self._extract_ajax_url(html_source or "")
            or urljoin(self.base_url, "/wp-admin/admin-ajax.php")
        )
        payload = dict(self.chapter_ajax.get("payload", {}))
        id_param = self.chapter_ajax.get("id_param", "id")
        payload[id_param] = post_id
        if self.chapter_ajax.get("action"):
            payload.setdefault("action", self.chapter_ajax["action"])

        response = scraper.post(ajax_url, data=payload)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        option_elements = soup.select("option[data-id]")
        return self._parse_chapter_elements(option_elements)

    def _extract_post_id(self, html: Optional[str]) -> Optional[str]:
        if not html:
            return None
        patterns = [
            r"var\s+post_id\s*=\s*(\d+)",
            r"data-post-id=\"(\d+)\"",
            r"manga_id\s*[:=]\s*\"?(\d+)\"?",
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                return match.group(1)
        return None

    def _extract_ajax_url(self, html: Optional[str]) -> Optional[str]:
        if not html:
            return None
        match = re.search(r"var\s+ajaxurl\s*=\s*[\"']([^\"']+)", html)
        if match:
            return match.group(1)
        return None

    def _fetch_series_metadata_via_api(self, slug: str, scraper) -> Optional[Dict]:
        if not scraper:
            return None
        try:
            encoded_slug = quote(slug.strip("/"))
            api_path = f"/wp-json/wp/v2/manga?slug={encoded_slug}&per_page=1"
            api_url = urljoin(self.base_url, api_path)
            response = scraper.get(api_url)
            response.raise_for_status()
            data = response.json()
        except Exception:
            return None

        if not isinstance(data, list) or not data:
            return None

        post = data[0]
        info: Dict[str, Optional[str]] = {}
        post_id = post.get("id")
        if post_id is not None:
            info["post_id"] = str(post_id)

        wp_title = (
            post.get("yoast_head_json", {}).get("og_title")
            or post.get("title", {}).get("rendered")
        )
        if wp_title:
            info["title"] = self._clean_wp_text(wp_title)

        excerpt = (
            post.get("excerpt", {}).get("rendered")
            or post.get("content", {}).get("rendered")
        )
        if excerpt:
            info["desc"] = self._clean_wp_text(excerpt)

        cover = (
            post.get("better_featured_image", {}).get("source_url")
            or post.get("jetpack_featured_media_url")
            or post.get("featured_media_url")
        )
        if cover:
            info["cover"] = cover

        return info or None

    def _clean_wp_text(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        soup = BeautifulSoup(value, "html.parser")
        text = soup.get_text(" ", strip=True)
        return text or None

    def _finalize_image_urls(self, urls: List[str], base_url: str) -> List[str]:
        finalized: List[str] = []
        for src in urls:
            if not src:
                continue
            src = src.strip()
            if not src:
                continue
            if not src.startswith("http"):
                src = urljoin(base_url, src)
            if self._url_normalizer:
                src = self._normalize_url(src)
            finalized.append(src)
        return finalized

    def _extract_novel_paragraphs(self, soup: Optional[BeautifulSoup]) -> List[str]:
        if not soup:
            return []
        container = soup.select_one("#readerarea, .readerarea, .reading-content")
        if not container:
            return []

        paragraphs: List[str] = []
        for node in container.children:
            if isinstance(node, NavigableString):
                text = node.strip()
                if text:
                    paragraphs.append(text)
                continue
            if not getattr(node, "name", None):
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
                for li in node.find_all("li", recursive=False):
                    text = li.get_text(" ", strip=True)
                    if text:
                        paragraphs.append(f"• {text}")

        if not paragraphs:
            text = container.get_text("\n", strip=True)
            if text:
                paragraphs = [
                    line.strip() for line in text.splitlines() if line.strip()
                ]
        return paragraphs
