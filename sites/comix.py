from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Any
from urllib.parse import parse_qsl, quote_plus, urlparse

from bs4 import BeautifulSoup

from .base import BaseSiteHandler, SearchHit, SiteComicContext


class ComixSiteHandler(BaseSiteHandler):
    name = "comix"
    domains = ("comix.to", "www.comix.to")

    def configure_session(self, scraper, args) -> None:
        scraper.headers.update({
            "Referer": "https://comix.to/",
            "Origin": "https://comix.to",
        })

    # ------------------------------------------------------------------ browser
    # As of 2026-05-13 comix.to API endpoints are token-gated with per-URL
    # HMAC signatures (`_=<sig>` param). Each unique API URL needs its OWN
    # capture — signatures don't transfer across URLs or sessions. The only
    # tractable path is letting Patchright navigate the page and either
    # capturing the outgoing request URL (for listing — we can replay with
    # cloudscraper) or reading the response body (for chapter detail —
    # we steal the JSON directly instead of replaying).
    #
    # Persistent browser instance kept alive for the handler lifetime so we
    # amortize the ~3-5s Patchright launch across N chapters. atexit hook
    # ensures cleanup at process exit. Sequential use only — the chapter
    # loop in aio-dl.py invokes get_chapter_images serially per handler.
    # Cross-file: same Patchright pattern used by sites/mangafire_vrf_simple.py
    # and sites/playwright_utils.py.
    _ChaptersTokenCache: Dict[str, str] = {}

    def __init__(self):
        super().__init__()
        # (pw_manager, browser, page) tuple or None when not yet started /
        # cleaned up. Started lazily by _ensure_browser on first need.
        self._browser_ctx: Optional[tuple] = None

    def _ensure_browser(self):
        """Start (once) and return the (pw, browser, page) tuple. Returns
        None if Patchright/Playwright unavailable, launch failed, or we're
        running in a non-main thread (Patchright's sync API requires an
        asyncio event loop, which background worker threads don't have).

        aio-dl.py's image-prefetch chain spawns background threads that
        call get_chapter_images for upcoming chapters in parallel — those
        threads silently degrade here. The main download thread still gets
        the real Patchright capture for the chapter it's actively downloading.
        Net effect: prefetch becomes a no-op (no speedup, no crash), and
        the sequential per-chapter capture still works.
        """
        if self._browser_ctx is not None:
            return self._browser_ctx
        # Reject non-main-thread callers up front — Patchright sync API
        # would raise "no running event loop" inside thread workers and
        # the resulting traceback floods the log.
        import threading
        if threading.current_thread() is not threading.main_thread():
            return None
        try:
            from patchright.sync_api import sync_playwright  # type: ignore
        except ImportError:
            try:
                from playwright.sync_api import sync_playwright  # type: ignore
            except ImportError:
                print("[!] Comix: patchright/playwright not installed; API capture unavailable.")
                return None
        try:
            pw = sync_playwright().start()
        except Exception as e:
            print(f"[!] Comix Playwright start failed: {e}")
            return None
        try:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            page = browser.new_page()
        except Exception as e:
            print(f"[!] Comix Playwright launch failed: {e}")
            try:
                pw.stop()
            except Exception:
                pass
            return None
        self._browser_ctx = (pw, browser, page)
        import atexit
        atexit.register(self._close_browser)
        return self._browser_ctx

    def _close_browser(self):
        ctx = self._browser_ctx
        self._browser_ctx = None
        if not ctx:
            return
        pw, browser, _ = ctx
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass

    @staticmethod
    def _is_main_thread() -> bool:
        import threading
        return threading.current_thread() is threading.main_thread()

    def _get_api_token(self, url: str) -> Optional[str]:
        """Capture the `_=<sig>&time=<ts>` query string for the chapter
        listing API (`/api/v1/manga/{hid}/chapters`) by navigating to the
        title URL and watching for the outgoing listing request.

        Caches per (title url) for the handler lifetime — list-API tokens
        are URL-bound, not chapter-id-bound, so one capture per title is
        enough for paginated chapter-list fetches.

        Returns the bare query string (no leading `?`) or None on failure.
        """
        cached = ComixSiteHandler._ChaptersTokenCache.get(url)
        if cached:
            return cached
        # Patchright sync API requires the asyncio loop of the main thread;
        # background prefetch threads have no loop and any call raises
        # "no running event loop". Cached token wins (returned above);
        # otherwise we can't capture a fresh one here.
        if not self._is_main_thread():
            return None
        ctx = self._ensure_browser()
        if not ctx:
            return None
        _, _, page = ctx
        token_query: Optional[str] = None

        def handle_req(request):
            nonlocal token_query
            if token_query:
                return
            u = request.url
            # Title page only fires the /chapters listing call (not /chapters/{id}).
            if "/chapters?" in u and "_=" in u:
                parts = u.split("?", 1)
                if len(parts) > 1:
                    token_query = parts[1]

        page.on("request", handle_req)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Poll up to ~5s for the listing XHR to fire.
            for _ in range(50):
                if token_query:
                    break
                page.wait_for_timeout(100)
        except Exception as e:
            print(f"[!] Comix token capture navigation failed: {e}")
            try:
                page.remove_listener("request", handle_req)
            except Exception:
                pass
            return None
        try:
            page.remove_listener("request", handle_req)
        except Exception:
            pass

        if token_query:
            ComixSiteHandler._ChaptersTokenCache[url] = token_query
        return token_query

    def _fetch_chapter_api_via_browser(self, chapter_url: str, chap_id) -> Optional[Dict]:
        """Steal the chapter-detail API response body by navigating to the
        chapter URL in Patchright. The site's JS fires /api/v1/chapters/{id}
        with a per-URL `_=<sig>` we can't reproduce — but we can read the
        response Patchright already received.

        Returns the parsed response dict (with `result.pages`) or None.
        """
        # Non-main threads can't drive Patchright's sync API (no event loop).
        # Background prefetch threads land here silently — main-thread
        # sequential fetch still works.
        if not self._is_main_thread():
            return None
        ctx = self._ensure_browser()
        if not ctx:
            return None
        _, _, page = ctx
        captured: Dict[str, Optional[Dict]] = {"data": None}
        target_path = f"/api/v1/chapters/{chap_id}"
        seen_responses: List[str] = []

        def handle_response(response):
            if captured["data"] is not None:
                return
            try:
                u = response.url
                if "/api/v1/" in u:
                    seen_responses.append(f"{response.status} {u[:120]}")
                if target_path in u and response.status == 200:
                    captured["data"] = response.json()
            except Exception:
                pass

        page.on("response", handle_response)
        try:
            page.goto(chapter_url, wait_until="domcontentloaded", timeout=30000)
            # Poll up to ~10s for the chapter-detail XHR to land. SPA loads
            # the viewer JS first, then fires the API call ~1-2s later.
            for _ in range(100):
                if captured["data"]:
                    break
                page.wait_for_timeout(100)
        except Exception as e:
            print(f"[!] Comix chapter browser fetch failed for {chap_id}: {type(e).__name__}: {e}", flush=True)
        finally:
            try:
                page.remove_listener("response", handle_response)
            except Exception:
                pass

        if not captured["data"]:
            # Diagnostic: surface what API responses we DID see so the user
            # can tell if the site changed structure vs. our listener missed.
            tail = seen_responses[-6:] if seen_responses else ["(none)"]
            print(f"[!] Comix: no chapter-API response captured for {chap_id}. Seen API responses (tail): {tail}", flush=True)
        return captured["data"]

    def _extract_next_data(self, html: str) -> List[Any]:
        """Extracts data pushed to self.__next_f."""
        data = []
        
        # Robust parsing instead of regex
        search_str = 'self.__next_f.push(['
        start_idx = 0
        
        while True:
            idx = html.find(search_str, start_idx)
            if idx == -1:
                break
            
            # Start parsing from after 'self.__next_f.push(['
            content_start = idx + len(search_str)
            current_idx = content_start
            
            balance = 1 # We are inside the first [
            in_string = False
            escape = False
            
            while current_idx < len(html):
                char = html[current_idx]
                
                if in_string:
                    if escape:
                        escape = False
                    elif char == '\\':
                        escape = True
                    elif char == '"':
                        in_string = False
                else:
                    if char == '"':
                        in_string = True
                    elif char == '[':
                        balance += 1
                    elif char == ']':
                        balance -= 1
                        if balance == 0:
                            break
                
                current_idx += 1
            
            if balance == 0:
                # We found the matching closing bracket
                arg_content = html[content_start:current_idx]
                
                # Try to parse as JSON list
                try:
                    # Wrap in brackets to make it a valid JSON list
                    json_str = f"[{arg_content}]"
                    args = json.loads(json_str)
                    
                    if len(args) >= 2:
                        data_str = args[1]
                        if isinstance(data_str, str):
                            # Parse the inner string
                            if data_str.startswith('c:'):
                                inner_json = data_str[2:]
                                try:
                                    data.append(json.loads(inner_json))
                                except json.JSONDecodeError:
                                    pass
                            elif data_str.startswith('0:'):
                                inner_json = data_str[2:]
                                try:
                                    data.append(json.loads(inner_json))
                                except json.JSONDecodeError:
                                    pass
                            else:
                                # Try parsing directly if it looks like JSON
                                try:
                                    data.append(json.loads(data_str))
                                except json.JSONDecodeError:
                                    pass
                except (json.JSONDecodeError, Exception):
                    pass
            
            start_idx = idx + 1

        return data

    def _find_key_recursive(self, obj: Any, key: str) -> Any:
        """Recursively searches for a key in a nested dictionary/list."""
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            for v in obj.values():
                res = self._find_key_recursive(v, key)
                if res is not None:
                    return res
        elif isinstance(obj, list):
            for item in obj:
                res = self._find_key_recursive(item, key)
                if res is not None:
                    return res
        return None

    def _normalize_named_list(self, value: Any) -> List[str]:
        """Converts mixed list/dict/string inputs into a clean list of names."""
        if not value:
            return []
        if not isinstance(value, list):
            value = [value]
        names: List[str] = []
        for item in value:
            name = None
            if isinstance(item, dict):
                name = item.get("title") or item.get("name")
            elif isinstance(item, str):
                name = item
            if name:
                name = name.strip()
                if name:
                    names.append(name)
        return names

    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        response = make_request(url, scraper)
        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        
        # First, extract hash_id from URL
        hash_id = None
        path = urlparse(url).path
        parts = path.split('/')
        if len(parts) >= 3 and parts[1] == 'title':
            slug_part = parts[2]
            if '-' in slug_part:
                hash_id = slug_part.split('-')[0]
            else:
                hash_id = slug_part
        
        manga_data = None
        
        # Try to fetch from API endpoint if we have hash_id
        if hash_id:
            try:
                # 2026-05-13: comix.to disabled /api/v2/manga/{hid} (returns
                # 404 with HTML body > 100 bytes, slipping past the warning
                # path in make_request). v1 is the documented public API per
                # their JS bundle's axios baseURL. Keep v2 as a fallback in
                # case they re-enable.
                api_url = f"https://comix.to/api/v1/manga/{hash_id}"
                api_response = make_request(api_url, scraper)
                if api_response.status_code == 404:
                    api_url = f"https://comix.to/api/v2/manga/{hash_id}"
                    api_response = make_request(api_url, scraper)
                api_data = api_response.json()

                # v1 returns status="ok"; v2 returned status=200. Accept both.
                if api_data.get("status") in (200, "ok") and api_data.get("result"):
                    manga_data = api_data["result"]
                    # Ensure hid is set
                    if "hid" not in manga_data:
                        manga_data["hid"] = manga_data.get("hash_id", hash_id)
            except Exception:
                # API failed, fall back to HTML extraction
                pass
        
        # Fallback: Extract Next.js data from HTML
        if not manga_data:
            next_data = self._extract_next_data(html)
            
            for item in next_data:
                found = self._find_key_recursive(item, "manga")
                if found:
                    manga_data = found
                    break
        
        if not manga_data:
            # Fallback: try to find it in the raw HTML
            match = re.search(r'"manga_id":(\d+)', html)
            if match:
                hash_match = re.search(r'"hash_id":"([^"]+)"', html)
                title_match = re.search(r'"title":"([^"]+)"', html)
                if hash_match and title_match:
                    manga_data = {
                        "manga_id": int(match.group(1)),
                        "hash_id": hash_match.group(1),
                        "title": title_match.group(1),
                        "hid": hash_match.group(1),
                    }

        if not manga_data:
             # Last resort: extract basic info from URL
             if hash_id:
                 title = slug_part.split('-', 1)[1].replace('-', ' ').title() if '-' in slug_part else slug_part
                 manga_data = {
                     "hash_id": hash_id,
                     "title": title,
                     "hid": hash_id,
                 }

        if not manga_data:
            raise RuntimeError("Could not find manga data in page.")

        # Ensure hid is present
        if "hid" not in manga_data:
            if "hash_id" in manga_data:
                manga_data["hid"] = manga_data["hash_id"]
            elif "slug" in manga_data:
                slug = manga_data["slug"]
                if "-" in slug:
                    manga_data["hid"] = slug.split("-")[0]
                else:
                    manga_data["hid"] = slug
            else:
                # Last resort: try to extract from URL
                if hash_id:
                    manga_data["hid"] = hash_id

        poster = manga_data.get("poster") or manga_data.get("_poster")
        if isinstance(poster, dict):
            cover_url = poster.get("large") or poster.get("medium") or poster.get("small")
            thumb_url = poster.get("medium") or poster.get("small") or cover_url
            if cover_url and not manga_data.get("cover"):
                manga_data["cover"] = cover_url
            if thumb_url and not manga_data.get("thumb"):
                manga_data["thumb"] = thumb_url
        if not manga_data.get("cover"):
            cover_tag = soup.find("meta", property="og:image")
            if cover_tag and cover_tag.get("content"):
                manga_data["cover"] = cover_tag["content"]

        synopsis = manga_data.get("synopsis")
        if synopsis and not manga_data.get("desc"):
            manga_data["desc"] = synopsis.strip()
        if not manga_data.get("desc"):
            desc_meta = soup.find("meta", attrs={"name": "description"})
            if desc_meta and desc_meta.get("content"):
                manga_data["desc"] = desc_meta["content"].strip()

        if url and not manga_data.get("url"):
            manga_data["url"] = url

        list_mappings = {
            "genres": ["genres", "genre"],
            "theme": ["theme"],
            "format": ["format"],
            "authors": ["authors", "author"],
            "artists": ["artists", "artist"],
            "alt_names": ["alt_names", "alt_titles", "altTitles", "aliases", "alternative_names"],
        }
        for target_key, source_keys in list_mappings.items():
            for source_key in source_keys:
                normalized = self._normalize_named_list(manga_data.get(source_key))
                if normalized:
                    manga_data[target_key] = normalized
                    break

        # Year may live under any of these depending on the comix.to API
        # version. Guard tightly: only int values > 0; non-int payloads are
        # silently dropped so downstream consumers always see a clean field.
        for year_key in ("year", "release_year", "year_of_release"):
            year_raw = manga_data.get(year_key)
            if isinstance(year_raw, int) and year_raw > 0:
                manga_data["year"] = year_raw
                break

        return SiteComicContext(
            comic=manga_data,
            title=manga_data.get("title", "Unknown"),
            identifier=manga_data.get("hid") or manga_data.get("hash_id"),
            soup=soup
        )

    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        hash_id = context.identifier
        if not hash_id:
             raise RuntimeError("Missing manga identifier (hash_id).")

        # Capture the `_=<sig>` query token up front. v1 chapters endpoint
        # returns 403 without it; v2 is permanently 404. Per probe, the sig
        # validates the request path only — page/limit/order can vary freely
        # while the same `_=` is reused. So we capture once, then paginate
        # with our own params.
        # When capture fails (no patchright/playwright, browser launch error)
        # we still attempt the bare API URLs — they'll 403, but the HTML
        # fallback in get_chapter_images may still get the user images.
        title_url = context.comic.get("url") or f"https://comix.to/title/{hash_id}"
        captured_qs = self._get_api_token(title_url) or ""
        sig = ""
        if captured_qs:
            for k, v in parse_qsl(captured_qs):
                if k == "_":
                    sig = v
                    break

        chapters = []
        page = 1
        # Server caps at limit=100 (limit=200 → 422 Unprocessable Entity);
        # 100 covers a 67-item title in one page and most series in 1-3
        # pages. limit is NOT part of the signature so we can pick any
        # accepted value without re-capturing the token.
        limit = 100

        while True:
            # 2026-05-13: v2 chapters endpoint 404s; v1 serves but requires
            # the captured token. Keep v2 as 404 fallback for forward-compat.
            api_url = (
                f"https://comix.to/api/v1/manga/{hash_id}/chapters"
                f"?order[number]=desc&limit={limit}&page={page}"
            )
            if sig:
                api_url += f"&_={sig}"
            response = make_request(api_url, scraper)
            if response.status_code == 404:
                api_url = (
                    f"https://comix.to/api/v2/manga/{hash_id}/chapters"
                    f"?order[number]=desc&limit={limit}&page={page}"
                )
                if sig:
                    api_url += f"&_={sig}"
                response = make_request(api_url, scraper)
            try:
                data = response.json()
            except json.JSONDecodeError:
                break

            # v1 status="ok"; v2 status=200. Accept both.
            if data.get("status") not in (200, "ok"):
                break
                
            items = data.get("result", {}).get("items", [])
            if not items:
                break
                
            for item in items:
                # Filter by language if needed, though the API seems to return 'en' mostly
                if language and item.get("language") != language:
                    continue
                    
                chap_num = item.get("number")
                # v1 uses `id`; v2 used `chapter_id`. Try v1 first.
                chap_id = item.get("id") or item.get("chapter_id")
                title = item.get("name") or f"Chapter {chap_num}"
                
                # Construct URL
                # Format: https://comix.to/title/{hash_id}-{slug}/{chapter_id}-chapter-{number}
                slug = context.comic.get("slug")
                
                # If we don't have the slug from API, try to get it from the context URL
                if not slug and context.comic.get("url"):
                     path = urlparse(context.comic["url"]).path
                     parts = path.split('/')
                     if len(parts) >= 3:
                         # This is likely the full slug (hash_id-slug)
                         slug = parts[2] 
                         # If we use this, we don't need to prepend hash_id again if it's already there
                         if slug.startswith(f"{hash_id}-"):
                             pass
                         else:
                             # This shouldn't happen if the URL is correct, but let's be safe
                             pass

                if not slug:
                    slug = "unknown"
                
                # Ensure slug starts with hash_id
                if not slug.startswith(f"{hash_id}-"):
                    slug = f"{hash_id}-{slug}"

                chap_url = f"https://comix.to/title/{slug}/{chap_id}-chapter-{chap_num}"
                
                # v1 uses `group`; v2 used `scanlation_group`. Try both.
                group_info = item.get("group") or item.get("scanlation_group") or {}
                group_name = group_info.get("name") if group_info else None

                chapters.append({
                    "url": chap_url,
                    "chap": str(chap_num),
                    "title": title,
                    "id": chap_id,
                    "group": group_name,
                    "up_count": item.get("votes", 0),
                })

            if len(items) < limit:
                break
            page += 1

        return chapters

    def get_group_name(self, chapter_version: Dict) -> Optional[str]:
        return chapter_version.get("group")

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        url = chapter.get("url")
        chap_id = chapter.get("id")
        images: List[str] = []

        # 2026-05-13: chapter HTML is now a ~6.7KB SPA shell with no embedded
        # images — JS calls /api/v1/chapters/{id}?_=<sig> client-side, where
        # `_=` is a per-URL HMAC we can't reproduce in Python. Patchright
        # navigates to the chapter URL and we steal the response body the
        # browser already received. Persistent browser (see _ensure_browser)
        # amortizes startup cost across chapters in the same download.
        if chap_id and url:
            data = self._fetch_chapter_api_via_browser(url, chap_id)
            if data:
                # 2026-05-13 v1 chapter-detail shape:
                #   result.pages = {"baseUrl": "<cdn-path-with-trailing-slash>",
                #                   "items": [{"url": "01.webp", "width": ..., "height": ...}, ...]}
                # Full image URL = baseUrl + items[i].url. Items typically all
                # webp; the baseUrl host rotates per chapter (anti-hotlink).
                result = data.get("result") or {}
                pages_obj = result.get("pages") or {}
                base_url = pages_obj.get("baseUrl") or ""
                items = pages_obj.get("items") or []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    rel = item.get("url")
                    if not isinstance(rel, str) or not rel:
                        continue
                    images.append(rel if rel.startswith("http") else (base_url + rel))

        if images:
            return images

        # ----- HTML-scrape fallback (kept for forward-compat if comix re-enables SSR) -----
        if not url:
            raise RuntimeError("Comix chapter is missing both id and url; cannot fetch images.")
        response = make_request(url, scraper)
        html = response.text

        next_data = self._extract_next_data(html)

        for item in next_data:
            # Look for "images" key which is a list of strings
            imgs = self._find_key_recursive(item, "images")
            if imgs and isinstance(imgs, list) and len(imgs) > 0 and isinstance(imgs[0], str):
                images = imgs
                break

        if not images:
             # Fallback: regex for "images":["url1", "url2"]
             match = re.search(r'"images":\[(.*?)\]', html)
             if match:
                 img_list_str = match.group(1)
                 # Extract URLs
                 images = re.findall(r'"(https?://[^"]+)"', img_list_str)

        if not images:
             # Fallback for escaped JSON (inside Next.js data string)
             # Matches \"images\":[\"url1\", \"url2\"]
             match = re.search(r'\\"images\\":\[(.*?)\]', html)
             if match:
                 img_list_str = match.group(1)
                 # Extract URLs (unescaped)
                 # The URLs will be like \"https://...\"
                 # We need to capture the URL inside the escaped quotes
                 # The regex r'\\"(https?://[^"]+)\\"' might fail if there are escaped chars inside the URL, but usually not.
                 # Safer: unescape the whole string first
                 try:
                     # Add brackets to make it a valid JSON list string: ["url1", "url2"]
                     # But img_list_str is like \"url1\",\"url2\"
                     # So we wrap it in brackets and unescape quotes? No.
                     # img_list_str is literally: \"https://...\",\"https://...\"
                     # We can just replace \" with " and then parse as JSON list
                     unescaped = "[" + img_list_str.replace('\\"', '"') + "]"
                     images = json.loads(unescaped)
                 except Exception:
                     # Regex fallback for escaped
                     images = re.findall(r'\\"(https?://[^"]+)\\"', img_list_str)

        if not images:
            raise RuntimeError("Could not find images in chapter page.")

        return images

    # ----------------------------------------------------------------- search
    # Comix uses /api/v1/manga?keyword=<query>. The /api/v1/search endpoint
    # in their JS bundle config IS a thing but returns 404 for unauth GETs;
    # /api/v1/manga is the public list endpoint with a 'keyword' filter that
    # behaves as substring/relevance match. (axios baseURL=/api/v1; bundle
    # exposes a top-level routes.search="/search" but that's a UI route, not
    # an API one.) The list endpoint is the supported public search path.
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
        url = (
            f"https://comix.to/api/v1/manga"
            f"?keyword={quote_plus(clean)}"
            f"&limit={int(limit)}"
        )
        response = make_request(url, scraper)
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError):
            return []
        if not isinstance(data, dict) or data.get("status") != "ok":
            return []
        items = (data.get("result") or {}).get("items") or []
        if not isinstance(items, list):
            return []

        hits: List[SearchHit] = []
        for idx, it in enumerate(items):
            hid = it.get("hid")
            title = (it.get("title") or "").strip()
            if not hid or not title:
                continue
            poster = it.get("poster") or {}
            cover = None
            if isinstance(poster, dict):
                cover = poster.get("large") or poster.get("medium") or poster.get("small")
            # latestChapter is float (e.g., 686.5 for half chapters); finalChapter
            # is the canonical end. Use finalChapter when available, else
            # int(latestChapter).
            chapter_count = it.get("finalChapter") or it.get("latestChapter")
            if isinstance(chapter_count, (int, float)):
                chapter_count = int(chapter_count)
            else:
                chapter_count = None
            year = it.get("year")
            if not isinstance(year, int):
                year = None
            # URL: /title/<hid> works without slug — verified live. The
            # fetch_comic_context handler takes hid from slug_part.split('-')[0]
            # so the no-slug form is parsed correctly.
            url_full = f"https://comix.to/title/{hid}"
            raw_score = max(0.05, 1.0 - (idx / max(1, len(items))))
            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=url_full,
                    cover=cover,
                    alt_titles=[],
                    year=year,
                    language=None,
                    chapter_count_hint=chapter_count,
                    raw_score=raw_score,
                )
            )
        return hits
