import re
import json
from urllib.parse import urljoin, urlparse
from .base import BaseSiteHandler

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
        return SiteComicContext(
            comic={
                'hid': series_slug,
                'build_id': build_id,
                'series_id': series_id,
                'series_slug': series_slug,
                'authors': authors,
                'cover': cover_url,
                'desc': description
            },
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
                    print(f"Failed to fetch chapter page: {response.status_code}")
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
