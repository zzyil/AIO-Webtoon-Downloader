from __future__ import annotations

import datetime as dt
import re
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, FeatureNotFound

from .base import BaseSiteHandler, SiteComicContext


class WeebCentralSiteHandler(BaseSiteHandler):
    name = "weebcentral"
    domains = ("weebcentral.com", "www.weebcentral.com")

    _BASE_URL = "https://weebcentral.com"

    def __init__(self) -> None:
        super().__init__()
        try:
            import lxml  # type: ignore  # noqa: F401

            self._parser = "lxml"
        except Exception:
            self._parser = "html.parser"

    # ----------------------------------------------------------------- helpers
    def _make_soup(self, html: str) -> BeautifulSoup:
        try:
            return BeautifulSoup(html, self._parser)
        except FeatureNotFound:
            return BeautifulSoup(html, "html.parser")

    def _extract_slug(self, url: str) -> str:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        return parts[-1] if parts else parsed.netloc

    def _source_image(self, container: Optional[BeautifulSoup], base_url: str) -> Optional[str]:
        if container is None:
            return None
        source = container.select_one("source")
        if source:
            srcset = source.get("srcset")
            if srcset:
                src = srcset.replace("small", "normal").strip()
                return urljoin(base_url, src)
        img = container.select_one("img")
        if not img:
            return None
        src = img.get("src")
        if not src:
            return None
        src = src.strip()
        if src.startswith("//"):
            return "https:" + src
        if src.startswith("/"):
            return urljoin(base_url, src)
        if src.startswith("http"):
            return src
        return urljoin(base_url, src)

    def _extract_list_values(self, section: BeautifulSoup, keywords: List[str]) -> List[str]:
        values: List[str] = []
        for item in section.select("li"):
            label = item.find("strong")
            if not label:
                continue
            label_text = label.get_text(strip=True).lower()
            if not any(k in label_text for k in keywords):
                continue
            anchors = item.select("a")
            if anchors:
                values.extend(a.get_text(strip=True) for a in anchors if a.get_text(strip=True))
            else:
                text = item.get_text(" ", strip=True)
                if text:
                    cleaned = re.sub(r"^.*?:", "", text).strip()
                    if cleaned:
                        values.append(cleaned)
        deduped: List[str] = []
        for value in values:
            if value and value not in deduped:
                deduped.append(value)
        return deduped

    def _extract_description(self, section: Optional[BeautifulSoup]) -> Optional[str]:
        if section is None:
            return None
        desc = []
        li_desc = None
        for item in section.select("li"):
            label = item.find("strong")
            if not label:
                continue
            label_text = label.get_text(strip=True).lower()
            if "description" in label_text:
                li_desc = item
                break
        if li_desc:
            para = li_desc.find("p")
            if para:
                desc.append(para.get_text(strip=True))

        def _append_list(title: str, keyword: str) -> None:
            for item in section.select("li"):
                label = item.find("strong")
                if not label:
                    continue
                if keyword not in label.get_text(strip=True).lower():
                    continue
                entries = [li.get_text(strip=True) for li in item.select("li")]
                if entries:
                    desc.append(f"{title}:")
                    desc.extend(f"• {entry}" for entry in entries if entry)

        _append_list("Related Series", "related")
        _append_list("Associated Names", "associated")

        text = "\n\n".join([part for part in desc if part])
        return text or None

    def _build_chapter_list_url(self, url: str) -> str:
        parsed = urlparse(urljoin(self._BASE_URL, url))
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 3 and parts[0] == "series":
            base_parts = parts[:3]
            base_parts[-1] = "full-chapter-list"
            path = "/".join(base_parts)
        else:
            path = "/".join(parts + ["full-chapter-list"])
        return urljoin(self._BASE_URL, "/" + path)

    def _extract_datetime(self, iso_text: Optional[str]) -> Optional[int]:
        if not iso_text:
            return None
        iso_text = iso_text.strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return int(dt.datetime.strptime(iso_text, fmt).timestamp())
            except ValueError:
                continue
        return None

    def _extract_chapter_number(self, text: str) -> Optional[str]:
        match = re.search(r"(\d+(?:\.\d+)?)", text)
        return match.group(1) if match else None

    # ----------------------------------------------------------- Base overrides
    def configure_session(self, scraper, args) -> None:
        scraper.headers.setdefault("Referer", self._BASE_URL + "/")

    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        response = make_request(url, scraper)
        soup = self._make_soup(response.text)

        sections = soup.select("section[x-data] > section")
        hero = sections[0] if sections else None
        details = sections[1] if len(sections) > 1 else sections[0] if sections else None

        title = None
        if details:
            heading = details.select_one("h1")
            if heading:
                title = heading.get_text(strip=True)
        title = title or self._extract_slug(url)

        authors = self._extract_list_values(hero or soup, ["author"])
        tags = self._extract_list_values(hero or soup, ["tag", "type"])
        status_values = self._extract_list_values(hero or soup, ["status"])

        desc = self._extract_description(details)
        cover = self._source_image(hero, url)

        slug = self._extract_slug(url)
        comic: Dict[str, object] = {
            "hid": slug,
            "title": title,
            "desc": desc,
            "cover": cover,
            "url": url,
        }
        if authors:
            comic["authors"] = authors
        if tags:
            comic["genres"] = tags
        if status_values:
            comic["status"] = status_values[0]

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
        info_url = context.comic.get("url")
        if isinstance(info_url, str) and info_url:
            series_url = info_url
        else:
            series_url = urljoin(self._BASE_URL + "/", f"series/{context.identifier}")

        chapter_url = self._build_chapter_list_url(series_url)
        soup = self._make_soup(make_request(chapter_url, scraper).text)

        chapters: List[Dict] = []
        for anchor in soup.select("div[x-data] > a"):
            title_node = anchor.select_one("span.flex > span")
            if not title_node:
                continue
            title = title_node.get_text(strip=True)
            href = anchor.get("href")
            if not href:
                continue
            abs_url = urljoin(self._BASE_URL, href)
            time_node = anchor.select_one("time[datetime]")
            uploaded = self._extract_datetime(time_node.get("datetime") if time_node else None)
            scanlator = None
            svg = anchor.select_one("svg[stroke]")
            if svg:
                stroke = svg.get("stroke")
                if stroke == "#d8b4fe":
                    scanlator = "Official"
                elif stroke == "#4C4D54":
                    scanlator = "Unknown"
            chapters.append(
                {
                    "hid": abs_url.rstrip("/"),
                    "chap": self._extract_chapter_number(title) or title,
                    "title": title,
                    "url": abs_url,
                    "uploaded": uploaded,
                    "scanlator": scanlator,
                }
            )
        return chapters

    def get_group_name(self, chapter_version: Dict) -> Optional[str]:
        group = chapter_version.get("scanlator")
        return group if isinstance(group, str) else None

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        chapter_url = chapter.get("url")
        if not chapter_url:
            raise RuntimeError("Chapter missing URL.")
        base = chapter_url.rstrip("/")
        images_url = f"{base}/images?is_prev=False&reading_style=long_strip"
        response = make_request(images_url, scraper)
        soup = self._make_soup(response.text)
        images: List[str] = []
        for img in soup.select("section[x-data~=scroll] img"):
            src = img.get("src") or img.get("data-src")
            if not src:
                continue
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = urljoin(images_url, src)
            elif not src.startswith("http"):
                src = urljoin(images_url, src)
            images.append(src)
        if not images:
            raise RuntimeError("Unable to locate images for chapter.")
        return images


__all__ = ["WeebCentralSiteHandler"]
