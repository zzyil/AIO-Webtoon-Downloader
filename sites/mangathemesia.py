"""Base handler for MangaThemesia-based sites."""

from __future__ import annotations
from typing import Dict, List, Optional, Callable
from urllib.parse import urljoin, quote
import re
from bs4 import BeautifulSoup, NavigableString
from .base import BaseSiteHandler, SiteComicContext
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
    
    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        url = self._normalize_url(url)
        if self.use_playwright:
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
                match = re.search(r"url\\(['\"]?(.*?)['\"]?\\)", style)
                if match:
                    cover = match.group(1)
        
        # Authors
        authors = []
        author_nodes = soup.select(".author-content a, .artist-content a")
        for node in author_nodes:
            authors.append(node.get_text(strip=True))
            
        # Genres
        genres = []
        genre_nodes = soup.select(".genres-content a")
        for node in genre_nodes:
            genres.append(node.get_text(strip=True))

        # Status
        status_node = soup.select_one(".post-status .summary-content, .status-content")
        status = status_node.get_text(strip=True) if status_node else "Unknown"

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
            "genres": genres,
            "status": status,
            "url": url,
        }
        
        comic["_raw_html"] = html
        if post_id_from_html:
            comic["_post_id"] = str(post_id_from_html)

        return SiteComicContext(comic=comic, title=title, identifier=slug, soup=soup)
    
    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        url = context.comic["url"]
        
        if self.use_playwright:
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
        if self.use_playwright:
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
