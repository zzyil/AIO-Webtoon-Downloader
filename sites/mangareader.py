from __future__ import annotations

import io
import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
from PIL import Image

from .base import BaseSiteHandler, SiteComicContext


class MangaReaderSiteHandler(BaseSiteHandler):
    name = "mangareader"
    domains = ("mangareader.to", "www.mangareader.to")

    _BASE_URL = "https://mangareader.to"
    _AJAX_BASE = f"{_BASE_URL}/ajax"
    _PIECE_SIZE = 200
    _SCRAMBLE_KEY = "staystay"

    def configure_session(self, scraper, args) -> None:
        scraper.headers.setdefault("Referer", self._BASE_URL + "/")
        scraper.headers.setdefault("Origin", self._BASE_URL)
        scraper.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36",
        )

    # ------------------------------------------------------------------ helpers
    def _extract_series_id(self, url: str) -> Tuple[str, str]:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        slug = ""
        if parts:
            if parts[0] == "manga":
                slug = parts[1]
            elif parts[0] == "read":
                slug = parts[1]
        if not slug:
            raise RuntimeError("Unable to determine MangaReader slug.")
        match = re.search(r"-(\d+)$", slug)
        if not match:
            raise RuntimeError("Unable to determine MangaReader series id.")
        return slug, match.group(1)

    def _fetch_series_page(self, url: str, scraper, make_request) -> BeautifulSoup:
        response = make_request(url, scraper)
        return BeautifulSoup(response.text, "html.parser")

    def _absolute(self, href: str) -> str:
        return urljoin(self._BASE_URL, href)

    def _quality_param(self) -> str:
        env = os.environ.get("MANGAREADER_QUALITY", "medium").lower()
        if env not in {"low", "medium", "high"}:
            return "medium"
        return env

    # ----------------------------------------------------------- Base overrides
    def fetch_comic_context(
        self,
        url: str,
        scraper,
        make_request,
    ) -> SiteComicContext:
        slug, series_id = self._extract_series_id(url)
        series_url = self._absolute(f"/manga/{slug}")
        soup = self._fetch_series_page(series_url, scraper, make_request)

        title_node = soup.select_one("h1.page__title") or soup.select_one("h1:not(.h4)")
        title = title_node.get_text(strip=True) if title_node else slug.replace("-", " ").title()

        cover_node = soup.select_one(".media img, .item-thumb img, .cover img")
        cover = None
        if cover_node:
            cover = (
                cover_node.get("data-src")
                or cover_node.get("data-cfsrc")
                or cover_node.get("src")
            )
            if cover:
                cover = self._absolute(cover)

        desc_node = soup.select_one(".description, .content, .summaries")
        description = desc_node.get_text("\n", strip=True) if desc_node else None

        genres = [
            a.get_text(strip=True)
            for a in soup.select(".genres a, .categories a, .tag-list a")
            if a.get_text(strip=True)
        ]

        comic: Dict[str, object] = {
            "hid": series_id,
            "title": title,
            "desc": description,
            "cover": cover,
            "genres": genres,
            "_slug": slug,
            "_series_id": series_id,
        }

        return SiteComicContext(comic=comic, title=title, identifier=series_id, soup=None)

    def get_chapters(
        self,
        context: SiteComicContext,
        scraper,
        language: str,
        make_request,
    ) -> List[Dict]:
        series_id = context.comic.get("_series_id") or context.identifier
        resp = scraper.get(
            f"{self._AJAX_BASE}/manga/reading-list/{series_id}",
            params={"readingBy": "chap"},
            headers={"Referer": self._BASE_URL + "/"},
        )
        resp.raise_for_status()
        data = resp.json()
        html = data.get("html")
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        lang_code = (language or "en").lower()
        container = soup.select_one(f"#{lang_code}-chapters")
        if container is None:
            container = soup.select_one("div[id$='-chapters']")
        if container is None:
            return []

        chapters: List[Dict] = []
        for anchor in container.select("a.item-link[href]"):
            href = anchor["href"]
            abs_url = self._absolute(href)
            text = anchor.get("data-shortname") or anchor.get_text(strip=True)
            chap_num = self._parse_chapter_number(text)
            chapters.append(
                {
                    "hid": abs_url,
                    "chap": chap_num or text,
                    "title": text,
                    "url": abs_url,
                    "_series_id": series_id,
                }
            )
        return chapters

    def get_group_name(self, chapter_version: Dict) -> Optional[str]:
        return None

    def get_chapter_images(
        self,
        chapter: Dict,
        scraper,
        make_request,
    ) -> List[Dict]:
        read_url = chapter.get("url")
        if not read_url:
            raise RuntimeError("Chapter URL missing for MangaReader.")
        resp = make_request(read_url, scraper)
        soup = BeautifulSoup(resp.text, "html.parser")
        wrapper = soup.select_one("#wrapper")
        if not wrapper:
            raise RuntimeError("Unable to locate MangaReader reader metadata.")
        reading_by = wrapper.get("data-reading-by") or "chap"
        reading_id = wrapper.get("data-reading-id")
        if not reading_id:
            raise RuntimeError("Unable to determine MangaReader chapter id.")

        ajax_resp = scraper.get(
            f"{self._AJAX_BASE}/image/list/{reading_by}/{reading_id}",
            params={
                "mode": "vertical",
                "quality": self._quality_param(),
                "hozPageSize": "1",
            },
            headers={"Referer": read_url},
        )
        ajax_resp.raise_for_status()
        data = ajax_resp.json()
        html = data.get("html") or ""
        soup = BeautifulSoup(html, "html.parser")

        results: List[Dict] = []
        for idx, card in enumerate(soup.select(".iv-card")):
            image_url = card.get("data-url")
            if not image_url:
                continue
            img_resp = scraper.get(
                image_url,
                headers={"Referer": self._BASE_URL + "/"},
            )
            img_resp.raise_for_status()
            blob = img_resp.content
            classes = card.get("class") or []
            shuffled = any(cls == "shuffled" for cls in classes)
            if shuffled:
                blob = self._descramble(blob)
            ext = self._infer_extension(image_url, blob)
            results.append(
                {
                    "type": "binary_image",
                    "data": blob,
                    "extension": ext,
                    "name": f"{chapter.get('chap','ch')}_{idx+1:04d}{ext}",
                }
            )
        return results

    # ----------------------------------------------------------------- parsing
    def _parse_chapter_number(self, text: str) -> Optional[str]:
        match = re.search(r"(\d+(?:\.\d+)?)", text or "")
        return match.group(1) if match else None

    def _infer_extension(self, url: str, data: bytes) -> str:
        ext = os.path.splitext(urlparse(url).path)[1].lower()
        if ext in {".jpg", ".jpeg", ".png", ".webp"}:
            return ext
        if data.startswith(b"\x89PNG"):
            return ".png"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return ".webp"
        return ".jpg"

    # -------------------------------------------------------------- descramble
    def _descramble(self, blob: bytes) -> bytes:
        image = Image.open(io.BytesIO(blob))
        image = image.convert("RGB")
        width, height = image.size
        canvas = Image.new("RGB", (width, height))

        pieces: List[_Piece] = []
        for y in range(0, height, self._PIECE_SIZE):
            for x in range(0, width, self._PIECE_SIZE):
                w = min(self._PIECE_SIZE, width - x)
                h = min(self._PIECE_SIZE, height - y)
                pieces.append(_Piece(x, y, w, h))

        groups: Dict[Tuple[int, int], List[_Piece]] = {}
        for piece in pieces:
            groups.setdefault((piece.w, piece.h), []).append(piece)

        for group in groups.values():
            perm = self._permutation(len(group))
            for idx, original_idx in enumerate(perm):
                src_piece = group[idx]
                dst_piece = group[original_idx]
                region = image.crop(
                    (src_piece.x, src_piece.y, src_piece.x + src_piece.w, src_piece.y + src_piece.h)
                )
                canvas.paste(region, (dst_piece.x, dst_piece.y))

        output = io.BytesIO()
        canvas.save(output, format=image.format or "JPEG", quality=95)
        return output.getvalue()

    def _permutation(self, size: int) -> Sequence[int]:
        memo = getattr(self, "_perm_cache", {})
        if size in memo:
            return memo[size]
        rng = _SeedRandom(self._SCRAMBLE_KEY)
        indices = list(range(size))
        perm = []
        for _ in range(size):
            choice = int(rng.next_double() * len(indices))
            perm.append(indices.pop(choice))
        memo[size] = perm
        self._perm_cache = memo
        return perm


# ------------------------------------------------------------------ helpers
@dataclass
class _Piece:
    x: int
    y: int
    w: int
    h: int


class _SeedRandom:
    _WIDTH = 256

    def __init__(self, key: str) -> None:
        algorithm = algorithms.ARC4(key.encode("utf-8"))
        cipher = Cipher(algorithm, mode=None)
        self._encryptor = cipher.encryptor()
        self._buffer = bytearray(self._encryptor.update(bytes(self._WIDTH)))
        self._pos = self._WIDTH

    def _next_byte(self) -> int:
        if self._pos == self._WIDTH:
            self._buffer = bytearray(self._encryptor.update(bytes(self._WIDTH)))
            self._pos = 0
        value = self._buffer[self._pos]
        self._pos += 1
        return value

    def next_double(self) -> float:
        num = self._next_byte()
        exp = 8
        while num < (1 << 52):
            num = (num << 8) | self._next_byte()
            exp += 8
        while num >= (1 << 53):
            num >>= 1
            exp -= 1
        return num / float(1 << exp)


__all__ = ["MangaReaderSiteHandler"]
