from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .base import BaseSiteHandler, SiteComicContext

try:  # Optional dependency; only needed for Kagane
    from pywidevine.cdm import Cdm
    from pywidevine.device import Device
    from pywidevine.pssh import Pssh
except Exception:  # pragma: no cover - handled at runtime
    Cdm = None  # type: ignore
    Device = None  # type: ignore
    Pssh = None  # type: ignore


class KaganeSiteHandler(BaseSiteHandler):
    name = "kagane"
    domains = ("kagane.org", "www.kagane.org")

    _BASE_URL = "https://kagane.org"
    _API_URL = "https://api.kagane.org"
    _CERT_ENDPOINT = f"{_API_URL}/api/v1/static/bin.bin"
    _THUMBNAIL_TEMPLATE = f"{_API_URL}/api/v1/series/{{series_id}}/thumbnail"
    _INSTRUCTIONS_URL = (
        "https://github.com/zzyil/comick.io-Downloader/blob/main/docs/Widevine.md"
    )

    def __init__(self) -> None:
        super().__init__()
        self._certificate_bytes: Optional[bytes] = None
        self._cdm: Optional[Cdm] = None
        self._device_path: Optional[Path] = None
        self._token_cache: Dict[Tuple[str, str], Dict[str, object]] = {}

    # ------------------------------------------------------------------ helpers
    def _extract_series_id(self, url: str) -> str:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            raise RuntimeError("Unable to determine Kagane series id from URL.")
        if parts[0] == "series" and len(parts) > 1:
            return parts[1]
        return parts[-1]

    def _locate_wvd_file(self) -> Optional[Path]:
        env_path = os.environ.get("KAGANE_WVD")
        if env_path:
            candidate = Path(env_path).expanduser()
            if candidate.is_file():
                return candidate
        repo_root = Path(__file__).resolve().parents[1]
        for entry in repo_root.glob("*.wvd"):
            if entry.is_file():
                return entry
        return None

    def _ensure_widevine_ready(self) -> None:
        if self._cdm is not None:
            return
        if Cdm is None or Device is None or Pssh is None:
            raise RuntimeError(
                "Kagane downloads require the optional dependency 'pywidevine'. "
                "Install it via 'pip install pywidevine' and follow "
                f"{self._INSTRUCTIONS_URL}"
            )
        device_path = self._locate_wvd_file()
        if not device_path:
            raise RuntimeError(
                "Kagane requires a Widevine device file (*.wvd). "
                f"Place one in the project root or set KAGANE_WVD. "
                f"Instructions: {self._INSTRUCTIONS_URL}"
            )
        try:
            device = Device.load(device_path)
            self._cdm = Cdm.from_device(device)
            self._device_path = device_path
        except Exception as exc:  # pragma: no cover - external
            raise RuntimeError(
                f"Failed to load Widevine device '{device_path}': {exc}"
            ) from exc

    def _ensure_certificate(self, scraper, make_request) -> bytes:
        if self._certificate_bytes is None:
            resp = make_request(self._CERT_ENDPOINT, scraper)
            self._certificate_bytes = resp.content
        return self._certificate_bytes

    def _build_pssh(self, series_id: str, chapter_id: str) -> bytes:
        seed = hashlib.sha256(f"{series_id}:{chapter_id}".encode("utf-8")).digest()[:16]
        key_id = base64.b64decode("7e+LqXnWSs6jyCfc1R0h7Q==")
        zeroes = b"\x00\x00\x00\x00"
        info = bytes([18, len(seed)]) + seed
        info_size = len(info).to_bytes(4, "big")
        inner = zeroes + key_id + info_size + info
        outer_size = (len(inner) + 8).to_bytes(4, "big")
        return outer_size + b"pssh" + inner

    def _decode_jwt_exp(self, token: str) -> Optional[int]:
        try:
            payload = token.split(".")[1]
            padding = "=" * (-len(payload) % 4)
            data = base64.urlsafe_b64decode(payload + padding)
            parsed = json.loads(data.decode("utf-8"))
            exp = parsed.get("exp")
            return int(exp) if exp is not None else None
        except Exception:
            return None

    def _issue_token(
        self,
        series_id: str,
        chapter_id: str,
        scraper,
        make_request,
    ) -> Dict[str, object]:
        self._ensure_widevine_ready()
        certificate = self._ensure_certificate(scraper, make_request)

        assert self._cdm is not None  # for mypy

        session_id = self._cdm.open()
        try:
            self._cdm.set_service_certificate(session_id, certificate)
            pssh = Pssh(self._build_pssh(series_id, chapter_id))
            challenge = self._cdm.get_license_challenge(session_id, pssh)
        finally:
            self._cdm.close(session_id)

        payload = {
            "challenge": base64.b64encode(challenge).decode("ascii"),
        }

        url = (
            f"{self._API_URL}/api/v1/books/{series_id}/file/{chapter_id}"
        )

        resp = scraper.post(
            url,
            json=payload,
            headers={
                "Origin": self._BASE_URL,
                "Referer": f"{self._BASE_URL}/",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token")
        cache_url = data.get("cache_url")
        if not token or not cache_url:
            raise RuntimeError("Kagane token response missing fields.")
        expires_at = self._decode_jwt_exp(token)
        bundle = {
            "token": token,
            "cache_url": cache_url.rstrip("/"),
            "expires_at": expires_at,
        }
        return bundle

    def _get_token_bundle(
        self,
        series_id: str,
        chapter_id: str,
        scraper,
        make_request,
    ) -> Dict[str, object]:
        key = (series_id, chapter_id)
        bundle = self._token_cache.get(key)
        now = time.time()
        if bundle:
            expires = bundle.get("expires_at")
            if not expires or (expires - 30) > now:
                return bundle
        bundle = self._issue_token(series_id, chapter_id, scraper, make_request)
        self._token_cache[key] = bundle
        return bundle

    def _looks_like_image(self, data: bytes) -> bool:
        if len(data) >= 2 and data[0:2] in (b"\xFF\xD8", b"\xFF\x0A"):
            return True  # JPEG / JXL
        if (
            len(data) >= 12
            and data[0:4] == b"RIFF"
            and data[8:12] == b"WEBP"
        ):
            return True
        if len(data) >= 12 and data[0:12] == b"\x00\x00\x00\x0cJXL ":
            return True
        return False

    def _detect_extension(self, data: bytes) -> str:
        if data.startswith(b"\xFF\xD8"):
            return ".jpg"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return ".webp"
        if data.startswith(b"\xFF\x0A") or data.startswith(b"\x00\x00\x00\x0cJXL "):
            return ".jxl"
        return ".bin"

    def _generate_seed(self, series_id: str, chapter_id: str, page_index: int) -> int:
        seed_str = f"{series_id}:{chapter_id}:{page_index:04d}.jpg"
        digest = hashlib.sha256(seed_str.encode("utf-8")).digest()
        result = 0
        for byte in digest[:8]:
            result = (result << 8) | byte
        return result

    def _unscramble(
        self,
        data: bytes,
        mapping: Sequence[Tuple[int, int]],
        head_mode: bool = True,
    ) -> bytes:
        size = len(mapping)
        total = len(data)
        chunk = total // size if size else total
        remainder = total % size

        if head_mode:
            prefix = data[:remainder] if remainder else b""
            body = data[remainder:]
        else:
            prefix = data[-remainder:] if remainder else b""
            body = data[: total - remainder]

        chunks = [
            body[i * chunk : (i + 1) * chunk] for i in range(size)
        ]
        result_chunks = [b""] * size

        if head_mode:
            for dst, src in mapping:
                if dst < size and src < size:
                    result_chunks[dst] = chunks[src]
        else:
            for dst, src in mapping:
                if dst < size and src < size:
                    result_chunks[src] = chunks[dst]

        reordered = b"".join(result_chunks)
        return reordered + prefix if head_mode else prefix + reordered

    def _decrypt_payload(
        self,
        payload: bytes,
        series_id: str,
        chapter_id: str,
        page_index: int,
    ) -> bytes:
        if len(payload) < 140:
            raise RuntimeError("Kagane payload too small to decrypt.")
        iv = payload[128:140]
        ciphertext = payload[140:]
        key = hashlib.sha256(f"{series_id}:{chapter_id}".encode("utf-8")).digest()
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(iv, ciphertext, None)
        if self._looks_like_image(plaintext):
            return plaintext
        seed = self._generate_seed(series_id, chapter_id, page_index)
        mapping = Scrambler(seed, 10).get_mapping()
        descrambled = self._unscramble(plaintext, mapping, True)
        if not self._looks_like_image(descrambled):
            raise RuntimeError("Unable to unscramble Kagane image.")
        return descrambled

    def _fetch_series_api(self, series_id: str, scraper) -> Dict[str, object]:
        url = f"{self._API_URL}/api/v1/series/{series_id}"
        try:
            resp = scraper.get(url, headers={"Referer": f"{self._BASE_URL}/"})
            if resp.status_code != 200:
                return {}
            return resp.json()
        except Exception:
            return {}

    # ----------------------------------------------------------- Base overrides
    def configure_session(self, scraper, args) -> None:
        scraper.headers.setdefault("Referer", f"{self._BASE_URL}/")
        scraper.headers.setdefault("Origin", self._BASE_URL)
        scraper.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36",
        )

    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        series_id = self._extract_series_id(url)
        resp = make_request(url, scraper)
        soup = BeautifulSoup(resp.text, "html.parser")
        title = (soup.title.string or series_id).strip()
        desc_node = soup.find("meta", attrs={"name": "description"})
        description = desc_node["content"].strip() if desc_node else None

        comic: Dict[str, object] = {
            "hid": series_id,
            "title": title,
            "desc": description,
            "cover": self._THUMBNAIL_TEMPLATE.format(series_id=series_id),
            "_series_id": series_id,
        }

        details = self._fetch_series_api(series_id, scraper)
        authors = details.get("authors") or []
        genres = details.get("genres") or []
        alt_titles = [
            item.get("title")
            for item in details.get("alternate_titles", [])
            if isinstance(item, dict)
        ]

        if authors:
            comic["authors"] = authors
        if genres:
            comic["genres"] = genres
        if alt_titles:
            comic["alt_names"] = alt_titles
        status = details.get("status")
        if isinstance(status, str):
            comic["status"] = status

        return SiteComicContext(
            comic=comic,
            title=title,
            identifier=series_id,
            soup=None,
        )

    def get_chapters(
        self, context: SiteComicContext, scraper, language: str, make_request
    ) -> List[Dict]:
        series_id = context.comic.get("_series_id") or context.identifier
        url = f"{self._API_URL}/api/v1/books/{series_id}"
        resp = make_request(url, scraper)
        payload = resp.json()
        chapters: List[Dict] = []
        for entry in payload.get("content", []):
            chapter_id = entry.get("id")
            if not chapter_id:
                continue
            pages = entry.get("pages_count") or 0
            chap_num = entry.get("number_sort") or entry.get("number")
            chapters.append(
                {
                    "hid": chapter_id,
                    "chap": str(chap_num) if chap_num is not None else entry.get("title"),
                    "title": entry.get("title"),
                    "url": chapter_id,
                    "_series_id": series_id,
                    "_chapter_id": chapter_id,
                    "_pages": int(pages),
                    "uploaded": entry.get("release_date"),
                }
            )
        return chapters

    def get_group_name(self, chapter_version: Dict) -> Optional[str]:
        return None

    def get_chapter_images(
        self, chapter: Dict, scraper, make_request
    ) -> List[Dict]:
        series_id = chapter.get("_series_id")
        chapter_id = chapter.get("_chapter_id") or chapter.get("hid")
        pages = int(chapter.get("_pages") or 0)
        if not series_id or not chapter_id or pages <= 0:
            raise RuntimeError("Incomplete Kagane chapter metadata.")

        key = (series_id, chapter_id)
        bundle = self._get_token_bundle(series_id, chapter_id, scraper, make_request)
        cache_url = bundle["cache_url"]  # type: ignore[assignment]
        token = bundle["token"]  # type: ignore[assignment]

        images: List[Dict] = []
        for idx in range(1, pages + 1):
            image_url = (
                f"{cache_url}/api/v1/books/{series_id}/file/{chapter_id}/{idx}"
            )
            resp = scraper.get(
                image_url,
                params={"token": token},
                headers={"Referer": f"{self._BASE_URL}/"},
            )
            if resp.status_code == 401:
                bundle = self._issue_token(series_id, chapter_id, scraper, make_request)
                self._token_cache[key] = bundle
                cache_url = bundle["cache_url"]  # type: ignore
                token = bundle["token"]  # type: ignore
                image_url = (
                    f"{cache_url}/api/v1/books/{series_id}/file/{chapter_id}/{idx}"
                )
                resp = scraper.get(
                    image_url,
                    params={"token": token},
                    headers={"Referer": f"{self._BASE_URL}/"},
                )
            resp.raise_for_status()
            data = self._decrypt_payload(resp.content, series_id, chapter_id, idx)
            images.append(
                {
                    "type": "binary_image",
                    "data": data,
                    "extension": self._detect_extension(data),
                }
            )
        return images


# ------------------------------------------------------------------ DRM helpers
class Randomizer:
    MASK64 = (1 << 64) - 1
    MASK32 = (1 << 32) - 1
    MASK8 = 0xFF
    PRNG_MULT = 0x27BB2EE687B0B0FD
    RND_MULT_32 = 0x45D9F3B

    def __init__(self, seed_input: int, grid_size: int) -> None:
        self.size = grid_size * grid_size
        self.seed = seed_input & Randomizer.MASK64
        self.state = self._hash_seed(self.seed)
        self.entropy_pool = self._expand_entropy(self.seed)
        self.order = list(range(self.size))
        self._permute()

    def _hash_seed(self, seed: int) -> int:
        digest = hashlib.sha256(str(seed).encode("utf-8")).digest()
        return self._read_uint64(digest, 0) ^ self._read_uint64(digest, 8)

    def _read_uint64(self, data: bytes, offset: int) -> int:
        value = 0
        for i in range(8):
            value = (value << 8) | data[offset + i]
        return value

    def _expand_entropy(self, seed: int) -> bytes:
        return hashlib.sha512(str(seed).encode("utf-8")).digest()

    def _sbox(self, value: int) -> int:
        lut = [
            163,
            95,
            137,
            13,
            55,
            193,
            107,
            228,
            114,
            185,
            22,
            243,
            68,
            218,
            158,
            40,
        ]
        return lut[value & 15] ^ lut[(value >> 4) & 15]

    def prng(self) -> int:
        self.state ^= (self.state << 11) & Randomizer.MASK64
        self.state ^= self.state >> 19
        self.state ^= (self.state << 7) & Randomizer.MASK64
        self.state = (self.state * Randomizer.PRNG_MULT) & Randomizer.MASK64
        return self.state

    def _round_func(self, value: int, entropy: int) -> int:
        result = value ^ self.prng() ^ entropy
        rot = ((result << 5) | (result >> 3)) & Randomizer.MASK32
        result = (rot * Randomizer.RND_MULT_32) & Randomizer.MASK32
        sbox_val = self._sbox(result & Randomizer.MASK8)
        result ^= sbox_val
        result ^= result >> 13
        return result

    def _feistel(self, left: int, right: int, rounds: int) -> Tuple[int, int]:
        l = left
        r = right
        for round_idx in range(rounds):
            ent = self.entropy_pool[round_idx % len(self.entropy_pool)] & 0xFF
            l ^= self._round_func(r, ent)
            ent_second = ent ^ ((round_idx * 31) & 0xFF)
            r ^= self._round_func(l, ent_second)
        return l, r

    def _permute(self) -> None:
        half = self.size // 2
        size = self.size
        for idx in range(half):
            pair = idx + half
            r, i = self._feistel(idx, pair, 4)
            src = r % size
            dst = i % size
            self.order[src], self.order[dst] = self.order[dst], self.order[src]

        for idx in range(size - 1, 0, -1):
            ent = self.entropy_pool[idx % len(self.entropy_pool)] & 0xFF
            pos = (self.prng() + ent) % (idx + 1)
            self.order[idx], self.order[pos] = self.order[pos], self.order[idx]


class Scrambler:
    def __init__(self, seed: int, grid_size: int) -> None:
        self.grid_size = grid_size
        self.total = grid_size * grid_size
        self.randomizer = Randomizer(seed, grid_size)
        self.graph, self.in_degree = self._build_dependency_graph(seed)
        self.scramble_path = self._topological_sort()

    def _build_dependency_graph(self, seed: int) -> Tuple[Dict[int, List[int]], Dict[int, int]]:
        graph = {i: [] for i in range(self.total)}
        in_degree = {i: 0 for i in range(self.total)}

        rng = Randomizer(seed, self.grid_size)
        for node in range(self.total):
            count = (rng.prng() % 3) + 2
            for _ in range(int(count)):
                target = int(rng.prng() % self.total)
                if target != node and not self._would_cycle(graph, target, node):
                    graph[target].append(node)
                    in_degree[node] += 1

        for node in range(self.total):
            if in_degree[node] == 0:
                attempts = 0
                while attempts < 10:
                    source = int(rng.prng() % self.total)
                    if source != node and not self._would_cycle(graph, source, node):
                        graph[source].append(node)
                        in_degree[node] += 1
                        break
                    attempts += 1
        return graph, in_degree

    def _would_cycle(self, graph: Dict[int, List[int]], target: int, start: int) -> bool:
        stack = [start]
        visited = set()
        while stack:
            node = stack.pop()
            if node == target:
                return True
            if node in visited:
                continue
            visited.add(node)
            stack.extend(graph.get(node, []))
        return False

    def _topological_sort(self) -> List[int]:
        graph = {k: list(v) for k, v in self.graph.items()}
        in_degree = dict(self.in_degree)
        queue = [node for node, deg in in_degree.items() if deg == 0]
        order: List[int] = []
        while queue:
            node = queue.pop(0)
            order.append(node)
            for neighbor in graph.get(node, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        return order

    def get_mapping(self) -> List[Tuple[int, int]]:
        order = list(self.randomizer.order)
        if len(self.scramble_path) == self.total:
            temp = [0] * self.total
            for idx, val in enumerate(self.scramble_path):
                temp[idx] = order[val]
            order = temp
        return [(idx, order[idx]) for idx in range(self.total)]


__all__ = ["KaganeSiteHandler"]
