from __future__ import annotations
from typing import Dict, List
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from .base import BaseSiteHandler, SiteComicContext

class ManhuaPlusSiteHandler(BaseSiteHandler):
    name = "manhuaplus"
    domains = ("manhuaplus.com", "www.manhuaplus.com")
    _BASE_URL = "https://manhuaplus.com"
    def configure_session(self, scraper, args) -> None:
        scraper.headers.update({"Referer": f"{self._BASE_URL}/"})
    def _make_soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")
    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        soup = self._make_soup(make_request(url, scraper).text)
        title = soup.select_one("h1, .post-title")
        title = title.get_text(strip=True) if title else "Unknown"
        desc = soup.select_one(".summary__content p, .description-summary p")
        description = desc.get_text(strip=True) if desc else ""
        cover = soup.select_one(".summary_image img")
        cover = cover.get("src") if cover else None
        genres = [a.get_text(strip=True) for a in soup.select(".genres-content a")]
        slug = url.rstrip("/").split("/")[-1]
        return SiteComicContext(comic={"hid": slug, "title": title, "desc": description, "cover": cover, "genres": genres, "url": url}, title=title, identifier=slug, soup=soup)
    def get_chapters(self, context: SiteComicContext, scraper, language: str, make_request) -> List[Dict]:
        soup = context.soup or self._make_soup(make_request(context.comic.get("url"), scraper).text)
        return [{"hid": link.get("href"), "chap": link.get_text(strip=True), "title": link.get_text(strip=True), "url": link.get("href"), "uploaded": None} for li in soup.select(".wp-manga-chapter") if (link := li.select_one("a"))]
    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        url = chapter.get("url")
        soup = self._make_soup(make_request(url, scraper).text)
        return [urljoin(url, img.get("src") or img.get("data-src")) for img in soup.select(".read-container img, .reading-content img") if img.get("src") or img.get("data-src")]
