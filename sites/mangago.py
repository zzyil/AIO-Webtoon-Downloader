from __future__ import annotations

import base64
import re
from typing import Dict, List, Optional
from urllib.parse import quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .base import BaseSiteHandler, GroupInfo, SearchHit, SiteComicContext


class MangaGoSiteHandler(BaseSiteHandler):
    name = "mangago"
    domains = ("mangago.me", "www.mangago.me")

    _BASE_URL = "https://www.mangago.me"
    _AES_KEY = bytes.fromhex("e11adc3949ba59abbe56e057f20f883e")
    _AES_IV = bytes.fromhex("1234567890abcdef1234567890abcdef")

    _CHAPTER_RE = re.compile(r"\bCh\.?\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
    _IMGSRCS_RE = re.compile(
        r"\bvar\s+imgsrcs\s*=\s*(['\"])(?P<payload>.+?)\1",
        re.DOTALL,
    )
    _SERIES_HREF_RE = re.compile(r"/read-manga/([^/?#]+)/?$", re.IGNORECASE)

    def configure_session(self, scraper, args) -> None:
        scraper.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        scraper.headers.setdefault("Referer", self._BASE_URL + "/")
        scraper.headers.setdefault("Origin", self._BASE_URL)

    # ---------------------------------------------------------------- helpers
    def _make_soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html or "", "html.parser")

    def _absolute(self, value: Optional[str], base_url: Optional[str] = None) -> Optional[str]:
        if not value:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.startswith("//"):
            return "https:" + cleaned
        return urljoin(base_url or self._BASE_URL + "/", cleaned)

    def _series_slug_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "read-manga":
            return parts[1]
        return parts[-1] if parts else ""

    def _series_url_from_url(self, url: str) -> str:
        slug = self._series_slug_from_url(url)
        if not slug:
            return url
        return f"{self._BASE_URL}/read-manga/{slug}/"

    def _cover_from_soup(self, soup: BeautifulSoup, base_url: str) -> Optional[str]:
        for selector, attr in (
            ('meta[property="og:image"]', "content"),
            ('meta[name="twitter:image"]', "content"),
            (".cover img", "src"),
            (".cover img", "data-src"),
            ('img[src*="coverlink"]', "src"),
            ('img[data-src*="coverlink"]', "data-src"),
        ):
            node = soup.select_one(selector)
            if not node:
                continue
            value = node.get(attr)
            cover = self._absolute(value, base_url)
            if cover:
                return cover
        return None

    def _title_from_soup(self, soup: BeautifulSoup) -> str:
        node = soup.select_one("h1")
        if node:
            title = node.get_text(" ", strip=True)
            if title:
                return title
        meta = soup.select_one('meta[property="og:title"]')
        title = (meta.get("content") or "").strip() if meta else ""
        if not title and soup.title:
            title = soup.title.get_text(" ", strip=True)
        title = re.sub(r"\s+manga\s*-\s*Mangago\s*$", "", title, flags=re.IGNORECASE)
        return title.strip() or "Unknown"

    def _row_for_label(self, soup: BeautifulSoup, label_name: str):
        target = label_name.casefold().rstrip(":")
        for label in soup.select("table.left label"):
            label_text = label.get_text(" ", strip=True).casefold().rstrip(":")
            if label_text == target:
                return label.find_parent("td")
        return None

    def _row_text_without_label(self, row, label_name: str) -> str:
        if not row:
            return ""
        text = row.get_text(" ", strip=True)
        return re.sub(
            rf"^\s*{re.escape(label_name)}\s*:?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()

    def _split_alt_titles(self, text: str) -> List[str]:
        values: List[str] = []
        for part in re.split(r";|\n", text or ""):
            cleaned = re.sub(r"\s+", " ", part).strip()
            if cleaned and cleaned not in values:
                values.append(cleaned)
        return values

    def _extract_chapter_number(self, label: str) -> Optional[str]:
        match = self._CHAPTER_RE.search(label or "")
        if not match:
            return None
        value = match.group(1).strip()
        if value.endswith(".0"):
            return value[:-2]
        return value

    def _extract_group_name(self, label: str, url: str) -> Optional[str]:
        match = self._CHAPTER_RE.search(label or "")
        if match:
            suffix = label[match.end() :].strip(" :-\t\r\n")
            if suffix:
                return re.sub(r"\s+", " ", suffix).strip()
        if "/br_chapter-" in url:
            return "Official"
        return None

    def _chapter_identifier(self, url: str) -> str:
        parts = [p for p in urlparse(url).path.split("/") if p]
        for part in parts:
            if "chapter" in part:
                return part
        return parts[-1] if parts else url

    def _decode_imgsrcs(self, payload: str) -> List[str]:
        cleaned = (payload or "").strip()
        if not cleaned:
            return []
        missing_padding = len(cleaned) % 4
        if missing_padding:
            cleaned += "=" * (4 - missing_padding)
        encrypted = base64.b64decode(cleaned)
        decryptor = Cipher(
            algorithms.AES(self._AES_KEY),
            modes.CBC(self._AES_IV),
        ).decryptor()
        decrypted = decryptor.update(encrypted) + decryptor.finalize()
        text = decrypted.rstrip(b"\x00").decode("utf-8", "replace")
        images: List[str] = []
        for item in text.split(","):
            url = item.strip()
            if not url or "cspiclink" in url:
                continue
            absolute = self._absolute(url)
            if absolute:
                images.append(self._downloadable_image_url(absolute))
        return images

    def _downloadable_image_url(self, url: str) -> str:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        # Mangago's reader emits HTTPS URLs on hosts like
        # iweb_4.mangapicgallery.com. Python's TLS stack rejects those hostnames
        # because of the underscore even though browsers load them from the HAR.
        # The CDN serves the same files over HTTP, so downgrade only that invalid
        # hostname family and leave normal hosts/covers on HTTPS.
        if (
            parsed.scheme == "https"
            and "_" in host
            and host.endswith("mangapicgallery.com")
        ):
            return parsed._replace(scheme="http").geturl()
        return url

    def _fallback_images_from_soup(
        self,
        soup: BeautifulSoup,
        base_url: str,
    ) -> List[str]:
        images: List[str] = []
        seen: set[str] = set()
        for img in soup.select("img[src], img[data-src], img[data-original]"):
            src = img.get("data-src") or img.get("data-original") or img.get("src")
            url = self._absolute(src, base_url)
            if not url or url in seen:
                continue
            if "mangapicgallery.com" not in url and "newpiclink" not in url:
                continue
            seen.add(url)
            images.append(self._downloadable_image_url(url))
        return images

    def _search_title_from_anchor(self, anchor) -> str:
        title = (anchor.get("title") or "").strip()
        if not title:
            title = anchor.get_text(" ", strip=True)
        title = re.sub(r"\s+manga\s*$", "", title, flags=re.IGNORECASE).strip()
        return re.sub(r"\s+", " ", title)

    def _cover_near_anchor(self, anchor, base_url: str) -> Optional[str]:
        containers = [anchor]
        parent = anchor.parent
        for _ in range(5):
            if not parent:
                break
            containers.append(parent)
            parent = parent.parent
        for container in containers:
            img = container.select_one("img[src], img[data-src], img[data-original]")
            if not img:
                continue
            src = img.get("data-src") or img.get("data-original") or img.get("src")
            cover = self._absolute(src, base_url)
            if cover:
                return cover
        return None

    # ----------------------------------------------------------- Base overrides
    def fetch_comic_context(
        self,
        url: str,
        scraper,
        make_request,
    ) -> SiteComicContext:
        series_url = self._series_url_from_url(url)
        response = make_request(series_url, scraper)
        soup = self._make_soup(response.text)
        title = self._title_from_soup(soup)
        slug = self._series_slug_from_url(series_url)

        author_row = self._row_for_label(soup, "Author")
        authors = [
            a.get_text(" ", strip=True)
            for a in author_row.select("a")
            if a.get_text(" ", strip=True)
        ] if author_row else []
        genre_row = self._row_for_label(soup, "Genre(s)")
        genres = [
            a.get_text(" ", strip=True)
            for a in genre_row.select('a[href*="/genre/"]')
            if a.get_text(" ", strip=True)
        ] if genre_row else []
        alt_names = self._split_alt_titles(
            self._row_text_without_label(self._row_for_label(soup, "Alternative"), "Alternative")
        )
        status = self._row_text_without_label(self._row_for_label(soup, "Status"), "Status")
        if status:
            status = status.split(" RSS", 1)[0].strip()

        comic: Dict[str, object] = {
            "hid": slug,
            "title": title,
            "desc": "",
            "status": status or "Unknown",
            "cover": self._cover_from_soup(soup, series_url),
            "authors": authors,
            "genres": genres,
            "alt_names": alt_names,
            "url": series_url,
            "language": "en",
        }
        return SiteComicContext(
            comic=comic,
            title=title,
            identifier=slug,
            soup=soup,
        )

    def get_chapters(
        self,
        context: SiteComicContext,
        scraper,
        language: str,
        make_request,
    ) -> List[Dict]:
        soup = context.soup
        series_url = context.comic.get("url") or f"{self._BASE_URL}/read-manga/{context.identifier}/"
        if not soup:
            response = make_request(str(series_url), scraper)
            soup = self._make_soup(response.text)

        chapters: List[Dict] = []
        seen_urls: set[str] = set()
        links = soup.select("#chapter_table a.chico[href], #chapter_table a[href]")
        if not links:
            links = soup.select("a.chico[href]")
        chapters_by_url: Dict[str, Dict] = {}
        slug_marker = f"/read-manga/{context.identifier}/" if context.identifier else "/read-manga/"
        for link in links:
            href = link.get("href")
            if not href:
                continue
            url = self._absolute(href, str(series_url))
            if not url or "/pg-1/" not in url or slug_marker not in url:
                continue
            label = re.sub(r"\s+", " ", link.get_text(" ", strip=True)).strip()
            chap = self._extract_chapter_number(label)
            if not chap:
                continue
            row = link.find_parent("tr")
            uploaded = None
            uploader = None
            if row:
                no_cells = row.select("td.no")
                if no_cells:
                    uploader = no_cells[0].get_text(" ", strip=True) or None
                if len(no_cells) > 1:
                    uploaded = no_cells[-1].get_text(" ", strip=True) or None
            group_name = self._extract_group_name(label, url)
            if url in seen_urls:
                existing = chapters_by_url.get(url)
                if existing:
                    if uploaded and not existing.get("uploaded"):
                        existing["uploaded"] = uploaded
                    if uploader and not existing.get("uploader"):
                        existing["uploader"] = uploader
                continue
            seen_urls.add(url)
            # /br_chapter- is mangago's own official-release path (see
            # _extract_group_name), so flag it — that's what makes
            # `--group official` and the ranker's official tier work here.
            is_official = "/br_chapter-" in url
            chapter = {
                "hid": self._chapter_identifier(url),
                "chap": chap,
                "title": label or f"Ch.{chap}",
                "url": url,
                # Display string, NOT an epoch ("2 days ago"-shaped). The
                # ranker's recency tier rejects non-numeric `uploaded` on
                # purpose rather than risk a wrong parse silently reordering
                # picks — grep _coerce_epoch in sites/base.py.
                "uploaded": uploaded,
                "_groups": (
                    [GroupInfo(name=group_name, is_official=is_official)]
                    if group_name else []
                ),
                "group": group_name,
                "publisher": group_name,
                "uploader": uploader,
                "language": "en",
                # Hardcoded 0 for every chapter — mangago exposes no vote
                # count. base._build_rank_context treats an all-zero field as
                # "no data" and leaves the upvote tier inert.
                "up_count": 0,
            }
            chapters_by_url[url] = chapter
            chapters.append(chapter)
        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        url = chapter.get("url")
        if not isinstance(url, str) or not url:
            raise RuntimeError("MangaGo chapter is missing a URL.")
        scraper.headers["Referer"] = url
        response = make_request(url, scraper)
        html = response.text or ""
        match = self._IMGSRCS_RE.search(html)
        if match:
            images = self._decode_imgsrcs(match.group("payload"))
            if images:
                return images
        soup = self._make_soup(html)
        images = self._fallback_images_from_soup(soup, url)
        if images:
            return images
        raise RuntimeError("MangaGo reader did not expose any page images.")

    # ----------------------------------------------------------------- search
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
        url = f"{self._BASE_URL}/r/l_search/?name={quote_plus(clean)}"
        response = make_request(url, scraper)
        soup = self._make_soup(response.text)

        hits: List[SearchHit] = []
        seen: set[str] = set()
        anchors = soup.select('a[href*="/read-manga/"]')
        for idx, anchor in enumerate(anchors):
            href = anchor.get("href") or ""
            absolute = self._absolute(href, url)
            if not absolute:
                continue
            parsed_path = urlparse(absolute).path
            if "/pg-" in parsed_path or "chapter-" in parsed_path:
                continue
            if not self._SERIES_HREF_RE.search(parsed_path):
                continue
            series_url = self._series_url_from_url(absolute)
            if series_url in seen:
                continue
            title = self._search_title_from_anchor(anchor)
            if not title or self._CHAPTER_RE.search(title):
                continue
            seen.add(series_url)
            cover = self._cover_near_anchor(anchor, url)
            if not cover and len(hits) < 10:
                try:
                    series_response = make_request(series_url, scraper)
                    cover = self._cover_from_soup(
                        self._make_soup(series_response.text),
                        series_url,
                    )
                except Exception:
                    cover = None
            raw_score = max(0.05, 1.0 - (idx / max(1, len(anchors))))
            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=series_url,
                    cover=cover,
                    alt_titles=[],
                    year=None,
                    language="en",
                    chapter_count_hint=None,
                    raw_score=raw_score,
                )
            )
            if len(hits) >= limit:
                break
        return hits


__all__ = ["MangaGoSiteHandler"]
