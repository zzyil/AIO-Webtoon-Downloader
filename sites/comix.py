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
        # Regex to find self.__next_f.push calls
        # Matches: self.__next_f.push([1, "string"])
        # We capture the list content
        matches = re.findall(r'self\.__next_f\.push\(\[(.*?)\]\)', html)
        for match in matches:
            try:
                # The match is like: 1, "string"
                # We wrap it in brackets to make it a valid JSON list: [1, "string"]
                # But the string might contain escaped quotes, so simple wrapping might fail if not careful.
                # However, the match is a string representation of arguments.
                # Let's try to parse it as a JSON array if we can.
                # Since it's JS code, it might not be valid JSON (e.g. single quotes).
                # But looking at the HAR, it uses double quotes.
                
                # A safer way might be to extract the string part.
                # The format seems to be: index, "string_content"
                parts = match.split(',', 1)
                if len(parts) == 2:
                    json_str = parts[1].strip()
                    if json_str.startswith('"') and json_str.endswith('"'):
                        # It's a JSON string. Deserialize it.
                        inner_str = json.loads(json_str)
                        # The inner string often starts with "c:" or similar prefixes for React Server Components
                        # e.g. "c:[\"$\",\"$L15\",null,{\"manga\":{...}}]"
                        if inner_str.startswith('c:'):
                            inner_json = inner_str[2:]
                            try:
                                data.append(json.loads(inner_json))
                            except json.JSONDecodeError:
                                pass
                        elif inner_str.startswith('0:'):
                             # Sometimes it's 0:{"P":null,...}
                            inner_json = inner_str[2:]
                            try:
                                data.append(json.loads(inner_json))
                            except json.JSONDecodeError:
                                pass
            except Exception:
                continue
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
        
        # Extract Next.js data
        next_data = self._extract_next_data(html)
        
        manga_data = None
        for item in next_data:
            found = self._find_key_recursive(item, "manga")
            if found:
                manga_data = found
                break
        
        if not manga_data:
            # Fallback: try to find it in the raw HTML if regex failed
            # Look for "manga_id":
            match = re.search(r'"manga_id":(\d+)', html)
            if match:
                # If we found manga_id, we might be able to find the hash_id too
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
             # Try to extract from URL if all else fails
             # URL: https://comix.to/title/0kx0d-eternally-regressing-knight
             path = urlparse(url).path
             parts = path.split('/')
             if len(parts) >= 3 and parts[1] == 'title':
                 slug_part = parts[2]
                 if '-' in slug_part:
                     hash_id = slug_part.split('-')[0]
                     title = slug_part.split('-', 1)[1].replace('-', ' ').title()
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
                # Try to extract from slug if hash_id is missing
                # slug might be "0kx0d-eternally-regressing-knight"
                slug = manga_data["slug"]
                if "-" in slug:
                    manga_data["hid"] = slug.split("-")[0]
                else:
                    manga_data["hid"] = slug # Fallback
            else:
                # Last resort: try to extract from URL
                path = urlparse(url).path
                parts = path.split('/')
                if len(parts) >= 3 and parts[1] == 'title':
                     slug_part = parts[2]
                     if '-' in slug_part:
                         manga_data["hid"] = slug_part.split('-')[0]
                     else:
                         manga_data["hid"] = slug_part

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
            raise RuntimeError("Could not find images in chapter page.")
            
        return images
