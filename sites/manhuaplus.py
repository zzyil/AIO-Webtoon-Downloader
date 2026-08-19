"""manhuaplus.com — a Madara WordPress site that does NOT subclass
MadaraSiteHandler (its chapter list + reader markup deviate enough that the
generic selectors miss), so it borrows the shared Madara helpers piecemeal:
`madara_search_via_admin_ajax` for search and `_normalize_madara_image_url`
for reader URLs. When fixing something here, check sites/madara.py first — the
attribute ladder + selector-break loop below are deliberate mirrors of
`MadaraSiteHandler.get_chapter_images` (grep reader_selectors).

Site-specific trap: this site's back catalogue is split across two image hosts.
Recent chapters serve from `cdn.manhuaplus.com`; old ones (roughly < ch.1000)
point at per-upload WordPress.com blogs — `anhanh1221.files.wordpress.com`,
`manhuaus5.files.wordpress.com`, … (the subdomain varies PER CHAPTER, so never
special-case one host).

WordPress.com content-negotiates those image URLs on the request's `Accept`:
ask for text/html and it answers **200 + `Content-Type: text/html` + a ~19 KB
attachment wrapper page**; ask for image/* and the same URL returns the real
JPEG. Since cloudscraper seeds every session with a document Accept, the whole
WordPress-hosted back catalogue used to download as markup — diagnosed at the
time as a retired host, which it is NOT (verified 2026-08-03: every one of
those URLs still serves its original bytes). The fix is request-side and
global: `sites/_image_io.py:IMAGE_ACCEPT`, sent per-request by
aio-dl.py:_try_download_url. `looks_like_real_image` (same module) still
backstops it, so a genuine wrapper page fails loudly instead of producing a
CBZ of HTML documents named 0001.jpg.
"""
from __future__ import annotations
from typing import Dict, List, Optional
from bs4 import BeautifulSoup
from .base import BaseSiteHandler, SearchHit, SiteComicContext
from .madara import madara_search_via_admin_ajax, _normalize_madara_image_url

class ManhuaPlusSiteHandler(BaseSiteHandler):
    name = "manhuaplus"
    domains = ("manhuaplus.com", "www.manhuaplus.com")
    _BASE_URL = "https://manhuaplus.com"

    # Tried in order; the FIRST one that yields any image wins. `.read-container`
    # wraps `.reading-content` on this site, so a single comma-union select would
    # make the two indistinguishable — matching MadaraSiteHandler.reader_selectors
    # keeps the narrower container authoritative.
    _READER_SELECTORS = (".reading-content img", ".read-container img")

    def configure_session(self, scraper, args) -> None:
        scraper.headers.update({"Referer": f"{self._BASE_URL}/"})
    def _make_soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")
    def search(self, query: str, scraper, make_request, *, language: str = "en", limit: int = 20) -> List[SearchHit]:
        # ManhuaPlus is a Madara WordPress site — reuse the shared admin-ajax
        # search helper (sites/madara.py:madara_search_via_admin_ajax).
        return madara_search_via_admin_ajax(
            base_url=self._BASE_URL, site_name=self.name, query=query, scraper=scraper, limit=limit,
        )
    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        soup = self._make_soup(make_request(url, scraper).text)
        # Madara renders promo chips ("Hot", "New") as a .manga-title-badges span
        # INSIDE div.post-title, ahead of the <h1>. get_text on that wrapper
        # therefore returned "HotApotheosis", which flows straight into AniList
        # matching (sites/external_metadata.py reads comic_data["title"]), so it
        # is not cosmetic. Drop the chips before reading any text out of the page;
        # nothing else here consumes them.
        for badge in soup.select(".manga-title-badges"):
            badge.decompose()
        title_node = soup.select_one(".post-title h1") or soup.select_one("h1, .post-title")
        title = title_node.get_text(strip=True) if title_node else "Unknown"
        desc = soup.select_one(".summary__content p, .description-summary p")
        description = desc.get_text(strip=True) if desc else ""
        # Same lazy-load ladder as the reader images: this theme's cover <img>
        # carries the real URL in data-src and leaves `src` pointing at Madara's
        # shared placeholder (themes/madara/images/dflazy.jpg), so reading `src`
        # gave EVERY series the same grey placeholder as its cover.jpg / UI
        # thumbnail / AniList site_cover fallback.
        cover_node = soup.select_one(".summary_image img")
        cover = self._img_src(cover_node) if cover_node else None
        if cover:
            cover = _normalize_madara_image_url(cover, url)
        genres = [a.get_text(strip=True) for a in soup.select(".genres-content a")]
        slug = url.rstrip("/").split("/")[-1]
        return SiteComicContext(comic={"hid": slug, "title": title, "desc": description, "cover": cover, "genres": genres, "url": url}, title=title, identifier=slug, soup=soup)
    def get_chapters(self, context: SiteComicContext, scraper, language: str, make_request) -> List[Dict]:
        soup = context.soup or self._make_soup(make_request(context.comic.get("url"), scraper).text)
        def clean_num(t):
            return self._chapter_number_from_text(t) or t
        return [{"hid": link.get("href"), "chap": clean_num(link.get_text(strip=True)), "title": link.get_text(strip=True), "url": link.get("href"), "uploaded": None} for li in soup.select(".wp-manga-chapter") if (link := li.select_one("a"))]

    @staticmethod
    def _img_src(img) -> Optional[str]:
        """First usable URL off one reader <img>, walking the lazy-load ladder
        this Madara theme uses. Mirrors sites/madara.py:834-861 — data-srcset is
        a candidate LIST ("url1 720w, url2 1080w"), so take the first entry's URL
        token, never the raw attribute. data: URIs are the lazy-load placeholder
        (precedent: sites/tapas.py:510), not a page."""
        for attr in ("data-src", "data-lazy-src", "data-cfsrc"):
            value = (img.get(attr) or "").strip()
            if value:
                return value
        srcset = (img.get("data-srcset") or "").strip()
        if srcset:
            first = srcset.split(",", 1)[0].strip()
            if first:
                candidate = first.split()[0]
                if candidate:
                    return candidate
        return (img.get("src") or "").strip() or None

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        chapter_url = chapter.get("url")
        if not chapter_url:
            raise RuntimeError("Chapter URL missing.")
        soup = self._make_soup(make_request(chapter_url, scraper).text)

        image_urls: List[str] = []
        for selector in self._READER_SELECTORS:
            for img in soup.select(selector):
                src = self._img_src(img)
                if not src or src.startswith("data:"):
                    continue
                src = _normalize_madara_image_url(src, chapter_url)
                if src and src not in image_urls:
                    image_urls.append(src)
            if image_urls:
                break

        # Fail loud rather than returning [] — an empty list reads as "chapter
        # has no pages" downstream and silently produces an empty CBZ.
        if not image_urls:
            raise RuntimeError("Unable to locate images for chapter.")
        return image_urls
