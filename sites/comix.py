from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .base import BaseSiteHandler, SiteComicContext


class ComixSiteHandler(BaseSiteHandler):
    name = "comix"
    domains = ("comix.to", "www.comix.to")

    def configure_session(self, scraper, args) -> None:
        scraper.headers.update({
            "Referer": "https://comix.to/",
            "Origin": "https://comix.to",
        })

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
                api_url = f"https://comix.to/api/v2/manga/{hash_id}"
                api_response = make_request(api_url, scraper)
                api_data = api_response.json()
                
                if api_data.get("status") == 200 and api_data.get("result"):
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
        }
        for target_key, source_keys in list_mappings.items():
            for source_key in source_keys:
                normalized = self._normalize_named_list(manga_data.get(source_key))
                if normalized:
                    manga_data[target_key] = normalized
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

        chapters = []
        page = 1
        limit = 100 # Use a reasonable limit
        
        while True:
            api_url = f"https://comix.to/api/v2/manga/{hash_id}/chapters?order[number]=desc&limit={limit}&page={page}"
            response = make_request(api_url, scraper)
            try:
                data = response.json()
            except json.JSONDecodeError:
                break
                
            if data.get("status") != 200:
                break
                
            items = data.get("result", {}).get("items", [])
            if not items:
                break
                
            for item in items:
                # Filter by language if needed, though the API seems to return 'en' mostly
                if language and item.get("language") != language:
                    continue
                    
                chap_num = item.get("number")
                chap_id = item.get("chapter_id")
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
                
                group_info = item.get("scanlation_group", {})
                group_name = group_info.get("name") if group_info else None

                chapters.append({
                    "url": chap_url,
                    "chap": str(chap_num),
                    "title": title,
                    "id": chap_id,
                    "group": group_name,
                    "up_count": item.get("votes", 0)
                })
            
            if len(items) < limit:
                break
            page += 1
            
        return chapters

    def get_group_name(self, chapter_version: Dict) -> Optional[str]:
        return chapter_version.get("group")

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        url = chapter.get("url")
        response = make_request(url, scraper)
        html = response.text
        
        next_data = self._extract_next_data(html)
        
        images = []
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
