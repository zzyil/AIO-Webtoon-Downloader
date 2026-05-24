import re
import json
import sys
from urllib.parse import quote_plus, urljoin, urlparse
from .base import BaseSiteHandler, SearchHit

class OmegaScansSiteHandler(BaseSiteHandler):
    name = "omegascans"
    domains = ("omegascans.org", "www.omegascans.org")

    def fetch_comic_context(self, url, scraper, make_request):
        response = scraper.get(url)
        if response.status_code != 200:
            raise Exception(f"Failed to load series page: {response.status_code}")
            
        html_content = response.text
        
        # Extract buildId and series_id
        build_id = None
        series_id = None
        # Extra metadata from the Next.js series payload — heancms exposes
        # status, tags, release_year, alternative_names server-rendered.
        # All optional; absent fields stay None and don't enter the comic dict.
        ns_status = None
        ns_year = None
        ns_genres = []
        ns_alt = []

        # Try __NEXT_DATA__ first as it's most reliable
        next_data_match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html_content)
        if next_data_match:
            try:
                data = json.loads(next_data_match.group(1))
                build_id = data.get('buildId')
                # series_id might be in pageProps -> series -> id
                series_data = data.get('props', {}).get('pageProps', {}).get('series', {})
                if series_data:
                    series_id = series_data.get('id')
                    raw_status = series_data.get('status')
                    if isinstance(raw_status, str) and raw_status:
                        ns_status = raw_status
                    raw_year = series_data.get('release_year') or series_data.get('year')
                    if isinstance(raw_year, int) and raw_year > 0:
                        ns_year = raw_year
                    for tag in series_data.get('tags') or []:
                        name = tag.get('name') if isinstance(tag, dict) else tag
                        if isinstance(name, str) and name and name not in ns_genres:
                            ns_genres.append(name)
                    for nm in series_data.get('alternative_names') or []:
                        if isinstance(nm, str) and nm and nm not in ns_alt:
                            ns_alt.append(nm)
            except Exception as e:
                pass

        # Fallback for buildId
        if not build_id:
            # Handle escaped quotes in JSON string
            build_id_match = re.search(r'\\"buildId\\":\\"(.*?)\\"', html_content)
            if not build_id_match:
                # Try unescaped just in case
                build_id_match = re.search(r'"buildId":"(.*?)"', html_content)
            
            if build_id_match:
                build_id = build_id_match.group(1)
            else:
                raise Exception("Could not find buildId")
        
        # Fallback for series_id
        if not series_id:
            series_id_match = re.search(r'\\"series_id\\":(\d+)', html_content)
            if not series_id_match:
                series_id_match = re.search(r'"series_id":(\d+)', html_content)
            
            if series_id_match:
                series_id = series_id_match.group(1)
            else:
                raise Exception("Could not find series_id")
        
        # Extract series_slug from URL
        parsed_url = urlparse(url)
        path_parts = parsed_url.path.strip("/").split("/")
        if len(path_parts) >= 2 and path_parts[0] == "series":
            series_slug = path_parts[1]
        else:
            raise Exception("Could not extract series slug from URL")
            
        # Extract title
        import html
        title_match = re.search(r'<title>(.*?)</title>', html_content)
        title = title_match.group(1) if title_match else series_slug
        title = html.unescape(title).replace(" - Omega Scans", "").strip()

        # Extract author
        author_match = re.search(r'>Author</span>.*?<span[^>]*>(.*?)</span>', html_content)
        authors = []
        if author_match:
            author_str = html.unescape(author_match.group(1)).strip()
            authors = [a.strip() for a in re.split(r',|&', author_str) if a.strip()]

        # Extract cover image
        cover_match = re.search(r'<meta property="og:image" content="(.*?)"', html_content)
        cover_url = cover_match.group(1) if cover_match else None

        # Extract description
        desc_match = re.search(r'<meta property="og:description" content="(.*?)"', html_content)
        description = html.unescape(desc_match.group(1)).strip() if desc_match else None

        from .base import SiteComicContext
        comic_dict = {
            'hid': series_slug,
            'build_id': build_id,
            'series_id': series_id,
            'series_slug': series_slug,
            'authors': authors,
            'cover': cover_url,
            'desc': description,
        }
        if ns_status:
            comic_dict['status'] = ns_status
        if ns_year:
            comic_dict['year'] = ns_year
        if ns_genres:
            comic_dict['genres'] = ns_genres
        if ns_alt:
            comic_dict['alt_names'] = ns_alt
        return SiteComicContext(
            comic=comic_dict,
            title=title,
            identifier=series_slug
        )

    def get_chapters(self, context, scraper, language, make_request):
        comic_data = context.comic
        series_id = comic_data['series_id']
        series_slug = comic_data['series_slug']
        build_id = comic_data['build_id']

        # 2. Fetch chapters from API
        chapters = []
        page = 1
        while True:
            api_url = f"https://api.omegascans.org/chapter/query?page={page}&perPage=30&series_id={series_id}"
            try:
                api_res = scraper.get(api_url)
                if api_res.status_code != 200:
                    break
                
                data = api_res.json()
                page_chapters = data.get('data', [])
                if not page_chapters:
                    break
                    
                for chap in page_chapters:
                    if chap.get('price', 0) > 0:
                        continue # Skip paid chapters
                        
                    chapter_number = chap.get('chapter_name', '').replace('Chapter ', '').strip()
                    
                    chapters.append({
                        'id': str(chap['id']),
                        'chap': chapter_number,
                        'title': chap.get('chapter_title') or chap.get('chapter_name'),
                        'slug': chap.get('chapter_slug'),
                        'url': f"https://omegascans.org/series/{series_slug}/{chap.get('chapter_slug')}",
                        'build_id': build_id,
                        'series_slug': series_slug
                    })
                
                # Check if we reached the last page
                meta = data.get('meta', {})
                if page >= meta.get('last_page', 1):
                    break
                    
                page += 1
                
            except Exception as e:
                pass
                break
                
        return chapters

    def get_chapter_images(self, chapter, scraper, make_request):
        # Construct _next/data URL
        build_id = chapter.get('build_id')
        series_slug = chapter.get('series_slug')
        chapter_slug = chapter.get('slug')
        
        if not (build_id and series_slug and chapter_slug):
            pass
            return []

        next_data_url = f"https://omegascans.org/_next/data/{build_id}/series/{series_slug}/{chapter_slug}.json"
        
        # print(f"Fetching chapter data from: {next_data_url}")
        data = None
        
        try:
            response = scraper.get(next_data_url)
            if response.status_code == 200:
                try:
                    data = response.json()
                except Exception:
                    pass
            else:
                pass
        except Exception as e:
            pass

        # If _next/data failed, try direct URL
        if not data:
            direct_url = chapter.get('url')
            if not direct_url:
                direct_url = f"https://omegascans.org/series/{series_slug}/{chapter_slug}"
            
            # print(f"Fallback: Fetching direct URL: {direct_url}")
            try:
                response = scraper.get(direct_url)
                if response.status_code != 200:
                    print(f"Failed to fetch chapter page: {response.status_code}", file=sys.stderr)
                    return []
                
                html = response.text
                
                # Try __NEXT_DATA__
                next_data_match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
                if next_data_match:
                    try:
                        data = json.loads(next_data_match.group(1))
                    except Exception as e:
                        pass
                
                if not data:
                    # Fallback 2: Regex for all image URLs in the HTML
                    # print("Debug: __NEXT_DATA__ not found or invalid, trying regex for images")
                    images = re.findall(r'(https://media\.omegascans\.org/file/[^"\\]+\.(?:jpg|jpeg|png|webp))', html)
                    if images:
                        # Remove duplicates while preserving order
                        seen = set()
                        unique_images = []
                        for img in images:
                            if img not in seen:
                                seen.add(img)
                                unique_images.append(img)
                        # print(f"Debug: Found {len(unique_images)} images via regex")
                        return unique_images
                    
                    # print("Could not extract chapter data from HTML")
                    return []
                    
            except Exception as e:
                # print(f"Error fetching direct URL: {e}")
                return []

        # Extract images from data (either from _next/data or __NEXT_DATA__)
        try:
            # Path: pageProps -> chapter -> chapter_data -> images
            # Note: The structure might vary slightly between _next/data and __NEXT_DATA__
            # In __NEXT_DATA__, it's usually under props -> pageProps
            
            page_props = data.get('pageProps')
            if not page_props:
                page_props = data.get('props', {}).get('pageProps', {})
                
            images = page_props.get('chapter', {}).get('chapter_data', {}).get('images', [])
            
            if not images:
                pass
                
            return images
        except Exception as e:
            # print(f"Error extracting images from data: {e}")
            return []

    # -- Cross-site search ------------------------------------------
    # OmegaScans is a heancms-framework site. The public search endpoint is
    # /query on api.omegascans.org with query_string + paging params. Same
    # shape as other heancms sites (asuracomic.net, reaperscans, etc.) so
    # other heancms handlers added later in Phase D can crib this. Series URL
    # constructed from the slug returned by the API; cover from `thumbnail`
    # which is either a relative path or a fully-qualified media.omegascans.org
    # URL — we normalize both shapes.
    _API_BASE = "https://api.omegascans.org"
    _SERIES_BASE = "https://omegascans.org/series"
    _MEDIA_BASE = "https://media.omegascans.org"

    def search(self, query, scraper, make_request, *, language="en", limit=20):
        clean = (query or "").strip()
        if not clean:
            return []
        # heancms /query: adult=true keeps mature series visible (the user's
        # ranking/feedback layer can downscore those if they don't want them).
        # series_type=Comic excludes Novel results which the handler wouldn't
        # know how to download anyway. orderBy=total_views gives a stable
        # relevance proxy when the substring match is the only signal.
        url = (
            f"{self._API_BASE}/query"
            f"?adult=true"
            f"&page=1"
            f"&perPage={int(limit)}"
            f"&query_string={quote_plus(clean)}"
            f"&order=desc"
            f"&orderBy=total_views"
            f"&series_type=Comic"
        )
        response = make_request(url, scraper)
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError):
            return []
        items = data.get("data") or []
        if not isinstance(items, list):
            return []

        hits = []
        for idx, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            slug = it.get("series_slug") or it.get("slug")
            title = (it.get("title") or "").strip()
            if not slug or not title:
                continue
            # thumbnail can be a relative filename ("foo.webp") or a full URL
            # (`https://media.omegascans.org/file/...`) depending on heancms
            # version. Normalize.
            thumb = it.get("thumbnail") or it.get("cover")
            cover = None
            if isinstance(thumb, str) and thumb:
                if thumb.startswith("http"):
                    cover = thumb
                else:
                    cover = f"{self._MEDIA_BASE}/cover/{thumb.lstrip('/')}"
            year = it.get("year")
            if not isinstance(year, int):
                year = None
            url_full = f"{self._SERIES_BASE}/{slug}"
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
                    chapter_count_hint=None,
                    raw_score=raw_score,
                )
            )
        return hits
