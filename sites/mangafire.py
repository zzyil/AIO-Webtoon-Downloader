from __future__ import annotations

import json
import re
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BaseSiteHandler, SiteComicContext

try:
    from .mangafire_vrf_simple import get_vrf_generator
    VRF_AVAILABLE = True
except ImportError:
    VRF_AVAILABLE = False


class MangaFireSiteHandler(BaseSiteHandler):
    name = "mangafire"
    domains = ("mangafire.to",)

    _BASE_URL = "https://mangafire.to"

    def configure_session(self, scraper, args) -> None:
        scraper.headers.update(
            {
                "Referer": self._BASE_URL + "/",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "X-Requested-With": "XMLHttpRequest",
            }
        )

    def _make_soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def _get_manga_id(self, soup: BeautifulSoup) -> Optional[str]:
        # Try to find the manga ID from the HTML
        # It might be in a meta tag or a data attribute
        # Based on HAR, it seems to be used in AJAX calls like /ajax/manga/40624/views
        # But for the chapter list, it uses the slug or a different ID.
        # Let's look at the "Read Now" button or similar elements.
        
        # In the HAR, the chapter list call is: https://mangafire.to/ajax/manga/jjx8y/chapter/fr
        # "jjx8y" seems to be the ID or code.
        # The URL is https://mangafire.to/manga/hoegwija-sayongseolmyeongseo22.jjx8y
        # So it's the last part of the URL path after the dot.
        return None

    def _extract_id_from_url(self, url: str) -> str:
        # URL format: https://mangafire.to/manga/name.id
        path = urlparse(url).path
        if "." in path:
            return path.split(".")[-1]
        return ""

    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        response = make_request(url, scraper)

        # MangaFire sometimes returns JSON-wrapped HTML
        html_content = response.text
        if html_content.strip().startswith('{'):
            try:
                data = response.json()
                if data.get("status") == 200 and "result" in data:
                    html_content = data["result"]
            except (json.JSONDecodeError, KeyError):
                pass  # Not JSON, use as-is

        soup = self._make_soup(html_content)

        # Extract ID from URL
        manga_id = self._extract_id_from_url(url)

        # Title
        title_node = soup.select_one("h1[itemprop='name']")
        title = title_node.get_text(strip=True) if title_node else "Unknown Title"

        # Cover image
        cover_node = soup.select_one(".poster img[itemprop='image']")
        cover = cover_node.get("src") if cover_node else None

        # Description (full text from modal)
        desc = None
        desc_modal = soup.select_one("#synopsis")
        if desc_modal:
            # Get text from modal, removing the close button
            close_btn = desc_modal.select_one(".modal-close")
            if close_btn:
                close_btn.decompose()
            desc = desc_modal.get_text(strip=True)
        else:
            # Fallback to truncated description
            desc_node = soup.select_one(".description")
            if desc_node:
                desc = desc_node.get_text(strip=True)

        # Status
        status = None
        status_node = soup.select_one(".info p")
        if status_node:
            status_text = status_node.get_text(strip=True)
            # Normalize status text
            if status_text in ["Releasing", "Ongoing"]:
                status = "Ongoing"
            elif status_text in ["Completed", "Finished"]:
                status = "Completed"
            else:
                status = status_text

        # Authors
        authors = []
        author_links = soup.select(".meta a[itemprop='author']")
        for author_link in author_links:
            author_name = author_link.get_text(strip=True)
            if author_name:
                authors.append(author_name)

        # Genres
        genres = []
        genre_divs = soup.select(".meta div")
        for div in genre_divs:
            span = div.select_one("span")
            if span and "Genres:" in span.get_text():
                genre_links = div.select("a[href^='/genre/']")
                for genre_link in genre_links:
                    genre = genre_link.get_text(strip=True)
                    if genre:
                        genres.append(genre)
                break

        comic = {
            "hid": manga_id,
            "title": title,
            "desc": desc,
            "cover": cover,
            "authors": authors,
            "genres": genres,
            "status": status,
            "url": url,
        }

        return SiteComicContext(comic=comic, title=title, identifier=manga_id, soup=soup)

    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        manga_id = context.identifier
        if not manga_id:
            return []

        # Map language code if necessary. MangaFire uses 'en', 'fr', etc.
        # Assuming 'language' arg matches MangaFire's codes.
        lang_code = language if language else "en"

        chapters = []

        # Try the read endpoint which might contain IDs (VRF-protected)
        # URL: https://mangafire.to/ajax/read/{id}/chapter/{lang}?vrf={token}
        read_ajax_path = f"/ajax/read/{manga_id}/chapter/{lang_code}"
        read_ajax_url = self._BASE_URL + read_ajax_path

        try:
            print(f"Attempting to fetch chapters from: {read_ajax_url}")

            # Generate VRF token if available
            if VRF_AVAILABLE:
                try:
                    vrf_gen = get_vrf_generator()
                    # Construct a reader URL for initialization
                    # Try to use a valid reader URL pattern
                    init_reader_url = f"{self._BASE_URL}/read/manga.{manga_id}/{lang_code}/chapter-1"
                    vrf_token = vrf_gen.generate_vrf(read_ajax_path, init_url=init_reader_url)
                    read_ajax_url += f"?vrf={vrf_token}"
                    print(f"Using VRF token for request")
                except Exception as e:
                    print(f"Warning: Could not generate VRF token: {e}")
                    print("Attempting request without VRF...")

            response = make_request(read_ajax_url, scraper)
            data = response.json()
            print(f"Read endpoint status: {data.get('status')}")
            
            if data.get("status") == 200:
                html_content = data.get("result", {}).get("html") # It might be nested differently
                if isinstance(data.get("result"), str):
                     html_content = data.get("result")
                
                if html_content:
                    soup = self._make_soup(html_content)
                    # Parse with data-id
                    new_chapters = []
                    for a in soup.select("a[data-id]"):
                        chap_id = a.get("data-id")
                        chap_num = a.get("data-number")
                        title = a.get("title")
                        href = a.get("href")
                        full_url = urljoin(self._BASE_URL, href)
                        
                        new_chapters.append({
                            "hid": chap_id, # Use the internal ID!
                            "chap": chap_num,
                            "title": title,
                            "url": full_url,
                            "uploaded": 0
                        })
                    
                    if new_chapters:
                        print(f"Successfully fetched {len(new_chapters)} chapters with IDs from read endpoint.")
                        return new_chapters
                    else:
                        print("No chapters with data-id found in read endpoint HTML.")
                        
        except Exception as e:
            print(f"Failed to fetch from read endpoint: {e}")

        # Fallback to the original endpoint (which lacks IDs)
        print("Falling back to original chapter list endpoint...")
        ajax_url = f"{self._BASE_URL}/ajax/manga/{manga_id}/chapter/{lang_code}"
        
        try:
            response = make_request(ajax_url, scraper)
            data = response.json()
        except Exception as e:
            print(f"Error fetching chapters: {e}")
            return []
            
        if data.get("status") != 200:
            return []
            
        html_content = data.get("result")
        if not html_content:
            return []
            
        soup = self._make_soup(html_content)
        
        # Parse the list items
        for li in soup.select("li.item"):
            a_tag = li.select_one("a")
            if not a_tag:
                continue
                
            href = a_tag.get("href")
            title = a_tag.get("title")
            chap_num = li.get("data-number")
            
            full_url = urljoin(self._BASE_URL, href)
            
            chapters.append({
                "hid": chap_num, # Temporary ID
                "chap": chap_num,
                "title": title,
                "url": full_url,
                "uploaded": 0, # TODO: Parse date
            })
            
        return chapters

    def get_chapter_images(
        self, chapter: Dict, scraper, make_request
    ) -> List[str]:
        """Fetch image URLs for a chapter.

        Args:
            chapter: Chapter dict with 'hid' (chapter ID) and 'url' keys
            scraper: Scraper session
            make_request: Function to make HTTP requests

        Returns:
            List of image URLs
        """
        if not VRF_AVAILABLE:
            raise NotImplementedError(
                "MangaFire image downloading requires Playwright for VRF generation. "
                "Install with: pip install playwright && playwright install chromium"
            )

        chapter_id = chapter.get("hid")
        chapter_url = chapter.get("url")

        if not chapter_id:
            print(f"[!] Chapter missing ID, cannot fetch images")
            return []

        # Get images via AJAX endpoint
        # URL: /ajax/read/chapter/{chapter_id}?vrf={token}
        ajax_path = f"/ajax/read/chapter/{chapter_id}"
        ajax_url = self._BASE_URL + ajax_path

        try:
            # Generate VRF token by navigating to the chapter page
            vrf_gen = get_vrf_generator()

            # Navigate to chapter page to capture its specific VRF
            if chapter_url:
                print(f"[*] Loading chapter page to capture VRF: {chapter_url}")
                try:
                    # Add a method to capture VRF from a specific chapter
                    vrf_gen._navigate_and_capture(chapter_url, ajax_path)
                    vrf_token = vrf_gen.generate_vrf(ajax_path)
                except Exception as e:
                    print(f"[!] Failed to navigate to chapter: {e}")
                    # Try without navigation
                    vrf_token = vrf_gen.generate_vrf(ajax_path)
            else:
                vrf_token = vrf_gen.generate_vrf(ajax_path)

            ajax_url += f"?vrf={vrf_token}"

            print(f"Fetching images for chapter {chapter_id}...")
            response = make_request(ajax_url, scraper)
            data = response.json()

            if data.get("status") != 200:
                print(f"[!] Failed to fetch images: status {data.get('status')}")
                return []

            # Parse the HTML response containing image data
            html_content = data.get("result", {})

            if isinstance(html_content, str):
                # Sometimes the result is HTML
                soup = self._make_soup(html_content)

                # Look for image data in script tags or data attributes
                images = []

                # Try to find images in data-url attributes
                for img in soup.select("img[data-url]"):
                    img_url = img.get("data-url")
                    if img_url:
                        images.append(img_url)

                # Try to find images in src attributes
                if not images:
                    for img in soup.select("img.page-img, img.img-fluid"):
                        img_url = img.get("src") or img.get("data-src")
                        if img_url:
                            images.append(img_url)

                # Try to extract from inline JSON/JS
                if not images:
                    # Look for JavaScript containing image URLs
                    for script in soup.find_all("script"):
                        script_text = script.string
                        if script_text and ("images" in script_text or "pages" in script_text):
                            # Try to extract JSON
                            json_match = re.search(r'(?:images|pages)\s*[:=]\s*(\[[^\]]+\])', script_text)
                            if json_match:
                                try:
                                    images = json.loads(json_match.group(1))
                                    break
                                except json.JSONDecodeError:
                                    pass

                return images

            elif isinstance(html_content, dict):
                # Sometimes the result is a JSON object with image URLs
                images = html_content.get("images") or html_content.get("pages") or []

                # MangaFire returns images as: [[url, width, height], ...]
                # Extract just the URLs
                if images and isinstance(images[0], list):
                    images = [img[0] if isinstance(img, list) and len(img) > 0 else img for img in images]

                return images

            print(f"[!] Unexpected response format for chapter images")
            print(f"[!] Response type: {type(html_content)}")
            print(f"[!] Response keys: {html_content.keys() if isinstance(html_content, dict) else 'N/A'}")
            return []

        except Exception as e:
            print(f"[!] Error fetching images for chapter {chapter_id}: {e}")
            import traceback
            traceback.print_exc()
            return []

