from __future__ import annotations

import json
import os
import re
import time

import random

def _mf_throttle(tag: str = "request") -> None:
    """Optional jittered delay to reduce burstiness when MangaFire/Cloudflare is sensitive.

    Env vars:
      - MANGAFIRE_DELAY_REQUEST (seconds, default 0.0)
      - MANGAFIRE_DELAY_CHAPTER (seconds, default 0.0)
    """
    try:
        if tag == "chapter":
            base = float(os.getenv("MANGAFIRE_DELAY_CHAPTER", "0.0"))
        else:
            base = float(os.getenv("MANGAFIRE_DELAY_REQUEST", "0.0"))
        if base <= 0:
            return
        time.sleep(base * random.uniform(0.7, 1.3))
    except Exception:
        return

from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BaseSiteHandler, SiteComicContext

try:
    from .mangafire_vrf_simple import get_vrf_generator
    VRF_AVAILABLE = True
except Exception:
    VRF_AVAILABLE = False


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _short(s: str, n: int = 240) -> str:
    s = (s or "").replace("\r", " ").replace("\n", " ")
    return s[:n] + ("…" if len(s) > n else "")


class MangaFireSiteHandler(BaseSiteHandler):
    name = "mangafire"
    domains = ("mangafire.to",)

    _BASE_URL = "https://mangafire.to"

    # Retry knobs (fixed delays; no exponential backoff)
    _JSON_RETRIES = _env_int("MANGAFIRE_JSON_RETRIES", 3)
    _JSON_RETRY_DELAY = _env_float("MANGAFIRE_JSON_RETRY_DELAY", 3.0)
    _VRF_RETRIES = _env_int("MANGAFIRE_CHAPTER_VRF_RETRIES", 3)
    _VRF_RETRY_DELAY = _env_float("MANGAFIRE_CHAPTER_VRF_RETRY_DELAY", 3.0)

    def configure_session(self, scraper, args) -> None:
        scraper.headers.update(
            {
                "Referer": self._BASE_URL + "/",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "X-Requested-With": "XMLHttpRequest",
            }
        )

    def _make_soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    def _extract_id_from_url(self, url: str) -> str:
        # URL format: https://mangafire.to/manga/name.id
        path = urlparse(url).path
        if "." in path:
            return path.split(".")[-1]
        return ""

    def _resp_diag(self, response) -> str:
        """A compact diagnostic string for logging."""
        try:
            status = getattr(response, "status_code", None)
        except Exception:
            status = None
        try:
            ctype = response.headers.get("content-type", "")
        except Exception:
            ctype = ""
        try:
            text = response.text or ""
        except Exception:
            text = ""
        head = _short(text, 180)
        return f"status={status} ctype={_short(ctype, 60)} body='{head}'"

    def _safe_json(self, response, *, label: str, url: str) -> Optional[Dict]:
        """Parse JSON with a clear log if it fails (often the server returns HTML while status=200)."""
        try:
            return response.json()
        except Exception as e:
            print(f"[!] {label}: JSON decode failed for {url}: {e}")
            print(f"    {self._resp_diag(response)}")
            return None

    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        _mf_throttle('request')
        response = make_request(url, scraper)

        # MangaFire sometimes returns JSON-wrapped HTML
        html_content = response.text
        if html_content.strip().startswith("{"):
            try:
                data = response.json()
                if data.get("status") == 200 and "result" in data:
                    html_content = data["result"]
            except Exception:
                pass  # Not JSON, use as-is

        soup = self._make_soup(html_content)

        manga_id = self._extract_id_from_url(url)

        title_node = soup.select_one("h1[itemprop='name']")
        title = title_node.get_text(strip=True) if title_node else "Unknown Title"

        cover_node = soup.select_one(".poster img[itemprop='image']")
        cover = cover_node.get("src") if cover_node else None

        desc = None
        desc_modal = soup.select_one("#synopsis")
        if desc_modal:
            close_btn = desc_modal.select_one(".modal-close")
            if close_btn:
                close_btn.decompose()
            desc = desc_modal.get_text(strip=True)
        else:
            desc_node = soup.select_one(".description")
            if desc_node:
                desc = desc_node.get_text(strip=True)

        status = None
        status_node = soup.select_one(".info p")
        if status_node:
            status_text = status_node.get_text(strip=True)
            if status_text in ["Releasing", "Ongoing"]:
                status = "Ongoing"
            elif status_text in ["Completed", "Finished"]:
                status = "Completed"
            else:
                status = status_text

        authors: List[str] = []
        for author_link in soup.select(".meta a[itemprop='author']"):
            author_name = author_link.get_text(strip=True)
            if author_name:
                authors.append(author_name)

        genres: List[str] = []
        for div in soup.select(".meta div"):
            span = div.select_one("span")
            if span and "Genres:" in span.get_text():
                for genre_link in div.select("a[href^='/genre/']"):
                    g = genre_link.get_text(strip=True)
                    if g:
                        genres.append(g)
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

    # ----------------------------- Chapters -----------------------------

    def get_chapters(self, context: SiteComicContext, scraper, language: str, make_request) -> List[Dict]:
        manga_id = context.identifier
        if not manga_id:
            return []

        lang_code = language if language else "en"

        # Primary endpoint (VRF-protected):
        # https://mangafire.to/ajax/read/{id}/chapter/{lang}?vrf=...
        read_ajax_path = f"/ajax/read/{manga_id}/chapter/{lang_code}"
        read_ajax_url = self._BASE_URL + read_ajax_path

        if VRF_AVAILABLE:
            try:
                vrf_gen = get_vrf_generator()
                init_reader_url = f"{self._BASE_URL}/read/manga.{manga_id}/{lang_code}/chapter-1"
                vrf = vrf_gen.ensure_vrf(read_ajax_path, init_url=init_reader_url)
                read_ajax_url = f"{read_ajax_url}?vrf={vrf}"
            except Exception as e:
                print(f"[!] Chapter list VRF failed: {e}")
                print("    (continuing without VRF; fallback endpoint may still work)")

        # Try read endpoint (with IDs)
        try:
            print(f"[*] Fetching chapters from: {read_ajax_url}")
            _mf_throttle('request')
            resp = make_request(read_ajax_url, scraper)
            data = self._safe_json(resp, label="chapter-list", url=read_ajax_url)
            if not data or data.get("status") != 200:
                raise RuntimeError(f"status={None if not data else data.get('status')}")

            html_content = None
            result = data.get("result")
            if isinstance(result, dict):
                html_content = result.get("html") or result.get("result") or result.get("data")
            elif isinstance(result, str):
                html_content = result
            if not html_content:
                raise RuntimeError("missing result HTML")

            soup = self._make_soup(html_content)
            chapters: List[Dict] = []
            for a in soup.select("a[data-id]"):
                chap_id = a.get("data-id")
                chap_num = a.get("data-number") or a.get("data-num") or a.get("data-chapter")
                title = a.get("title") or a.get_text(strip=True)
                href = a.get("href") or ""
                full_url = urljoin(self._BASE_URL, href)

                if chap_id:
                    chapters.append(
                        {
                            "hid": chap_id,
                            "chap": chap_num,
                            "title": title,
                            "url": full_url,
                            "uploaded": 0,
                        }
                    )
            if chapters:
                return chapters
            print("[!] Read endpoint returned no <a data-id> items; will fall back.")
        except Exception as e:
            print(f"[!] Read endpoint failed: {e}")

        # Fallback endpoint (often works, but lacks internal IDs)
        fallback_url = f"{self._BASE_URL}/ajax/manga/{manga_id}/chapter/{lang_code}"
        print(f"[*] Falling back to: {fallback_url}")

        try:
            _mf_throttle('request')
            resp = make_request(fallback_url, scraper)
            data = self._safe_json(resp, label="chapter-list-fallback", url=fallback_url)
            if not data or data.get("status") != 200:
                return []
            html = data.get("result")
            if not html:
                return []
            soup = self._make_soup(html)
        except Exception as e:
            print(f"[!] Fallback chapter list failed: {e}")
            return []

        chapters: List[Dict] = []
        for li in soup.select("li.item"):
            a_tag = li.select_one("a")
            if not a_tag:
                continue
            href = a_tag.get("href") or ""
            title = a_tag.get("title") or a_tag.get_text(strip=True)
            chap_num = li.get("data-number")
            full_url = urljoin(self._BASE_URL, href)
            chapters.append({"hid": chap_num, "chap": chap_num, "title": title, "url": full_url, "uploaded": 0})
        return chapters

    # ----------------------------- Images -----------------------------

    def _parse_images_from_result(self, result) -> List[str]:
        # result can be str(html) or dict(json)
        if isinstance(result, dict):
            images = result.get("images") or result.get("pages") or []
            if images and isinstance(images[0], list):
                images = [img[0] if isinstance(img, list) and img else img for img in images]
            return [u for u in images if isinstance(u, str) and u]

        if isinstance(result, str):
            soup = self._make_soup(result)
            images: List[str] = []

            for img in soup.select("img[data-url]"):
                u = img.get("data-url")
                if u:
                    images.append(u)

            if not images:
                for img in soup.select("img.page-img, img.img-fluid"):
                    u = img.get("src") or img.get("data-src")
                    if u:
                        images.append(u)

            if not images:
                # Look for inline JSON arrays in scripts
                for script in soup.find_all("script"):
                    st = script.string
                    if not st:
                        continue
                    if ("images" in st) or ("pages" in st):
                        m = re.search(r"(?:images|pages)\s*[:=]\s*(\[[^\]]+\])", st)
                        if m:
                            try:
                                arr = json.loads(m.group(1))
                                if isinstance(arr, list):
                                    images = [x[0] if isinstance(x, list) and x else x for x in arr]
                                    images = [u for u in images if isinstance(u, str) and u]
                                    break
                            except Exception:
                                continue

            return images

        return []

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        if not VRF_AVAILABLE:
            raise NotImplementedError(
                "MangaFire image downloading requires Playwright for VRF generation. "
                "Install with: pip install playwright && playwright install chromium"
            )

        chapter_id = chapter.get("hid")
        chapter_url = chapter.get("url")
        if not chapter_id:
            print("[!] Chapter missing ID; cannot fetch images.")
            return []

        ajax_path = f"/ajax/read/chapter/{chapter_id}"
        ajax_url_base = self._BASE_URL + ajax_path

        vrf_gen = get_vrf_generator()

        last_err: Optional[Exception] = None

        for attempt in range(1, self._JSON_RETRIES + 1):
            stage = "start"
            try:
                # 1) VRF
                stage = "vrf"
                if chapter_url:
                    print(f"[*] Capturing chapter VRF via navigation: {chapter_url}")
                    # ensure_vrf logs its own per-attempt reasons.
                    vrf = vrf_gen.ensure_vrf(ajax_path, page_url=chapter_url, init_url=chapter_url)
                else:
                    vrf = vrf_gen.ensure_vrf(ajax_path)
                ajax_url = f"{ajax_url_base}?vrf={vrf}"

                # 2) AJAX fetch
                stage = "ajax"
                print(f"[*] Fetching images for chapter {chapter_id} (attempt {attempt}/{self._JSON_RETRIES})…")
                _mf_throttle('request')
                resp = make_request(ajax_url, scraper)

                # 3) JSON parse
                stage = "json"
                data = self._safe_json(resp, label=f"chapter-{chapter_id}", url=ajax_url)
                if not data:
                    raise RuntimeError("non-json response")

                if data.get("status") != 200:
                    raise RuntimeError(f"api status={data.get('status')}")

                # 4) parse result
                stage = "parse"
                result = data.get("result")
                images = self._parse_images_from_result(result)
                if not images:
                    raise RuntimeError("parsed 0 images from result")

                return images

            except Exception as e:
                last_err = e
                print(f"[!] Chapter {chapter_id} attempt {attempt}/{self._JSON_RETRIES} failed at stage={stage}: {e}")
                if attempt < self._JSON_RETRIES:
                    time.sleep(self._JSON_RETRY_DELAY)
                    continue
                break

        # Final: help debugging
        print(f"[!] Giving up on chapter {chapter_id}. Last error: {last_err}")
        try:
            if hasattr(vrf_gen, "dump_state"):
                vrf_gen.dump_state()
        except Exception:
            pass
        return []
