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
    # pywidevine renamed the class from Pssh to PSSH at some point
    # (>= 1.9.0). Try the new name first, alias to Pssh so the rest of this
    # file uses one consistent symbol regardless of the installed version.
    try:
        from pywidevine.pssh import PSSH as Pssh  # type: ignore
    except ImportError:
        from pywidevine.pssh import Pssh  # type: ignore
except Exception:  # pragma: no cover - handled at runtime
    Cdm = None  # type: ignore
    Device = None  # type: ignore
    Pssh = None  # type: ignore


class KaganeSiteHandler(BaseSiteHandler):
    name = "kagane"
    domains = ("kagane.org", "www.kagane.org")

    # Per-process: only show the setup warning once even if the user feeds
    # multiple Kagane URLs in one run. Reset only on a fresh interpreter.
    _setup_warning_shown: bool = False

    _BASE_URL = "https://kagane.org"
    # 2026-05-13: api.kagane.org DNS gone; everything migrated to
    # yuzuki.kagane.org/api/v2/. The series-list/book-list endpoints all live
    # in the same v2 series payload (series_books field), and cover images are
    # served at /api/v2/image/{image_id} where image_id comes from the
    # series_covers entries inside the series payload — NOT from series_id.
    # The /compressed variant returns a thumbnail-sized webp (~67KB);
    # bare path returns full-res (~800KB+). We use /compressed for the
    # cover_thumb and full path elsewhere.
    _API_URL = "https://yuzuki.kagane.org"
    _CERT_ENDPOINT = f"{_API_URL}/api/v2/static/bin.bin"
    _IMAGE_TEMPLATE = f"{_API_URL}/api/v2/image/{{image_id}}"
    _IMAGE_COMPRESSED_TEMPLATE = f"{_API_URL}/api/v2/image/{{image_id}}/compressed"
    _INTEGRITY_ENDPOINT = "https://kagane.org/api/integrity"
    _INSTRUCTIONS_URL = (
        "https://github.com/zzyil/comick.io-Downloader/blob/main/docs/Widevine.md"
    )

    def __init__(self) -> None:
        super().__init__()
        self._certificate_bytes: Optional[bytes] = None
        self._cdm: Optional[Cdm] = None
        self._device_path: Optional[Path] = None
        self._token_cache: Dict[Tuple[str, str], Dict[str, object]] = {}
        # Integrity token cached across chapters; refresh when within 30s of
        # expiry. Required by yuzuki.kagane.org/api/v2/books POSTs (x-integrity-token header).
        self._integrity_token: Optional[str] = None
        self._integrity_token_exp: Optional[int] = None

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

    def _fetch_integrity_token(self, scraper) -> str:
        # 2026-05-13: yuzuki.kagane.org/api/v2/books rejects POSTs without
        # x-integrity-token. Service lives on the main kagane.org host
        # (NOT yuzuki) and returns {token, exp}. Cache + refresh 30s pre-exp.
        now = int(time.time())
        if (
            self._integrity_token
            and self._integrity_token_exp
            and self._integrity_token_exp - 30 > now
        ):
            return self._integrity_token
        resp = scraper.post(
            self._INTEGRITY_ENDPOINT,
            headers={
                "Content-Type": "application/json",
                "Referer": f"{self._BASE_URL}/",
                "Origin": self._BASE_URL,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("token")
        exp = data.get("exp")
        if not token:
            raise RuntimeError(
                "Kagane integrity token response missing 'token' field."
            )
        self._integrity_token = token
        self._integrity_token_exp = int(exp) if exp else None
        return token

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

        integrity_token = self._fetch_integrity_token(scraper)
        payload = {
            "challenge": base64.b64encode(challenge).decode("ascii"),
        }

        # 2026-05-13 v2: POST /api/v2/books/{chapter_id}?is_datasaver=false
        # series_id no longer appears in the path; chapter_id alone is the
        # book identifier. is_datasaver=false explicitly opts into full-res.
        url = f"{self._API_URL}/api/v2/books/{chapter_id}?is_datasaver=false"

        resp = scraper.post(
            url,
            json=payload,
            headers={
                "Origin": self._BASE_URL,
                "Referer": f"{self._BASE_URL}/",
                "Content-Type": "application/json",
                "x-integrity-token": integrity_token,
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
        # 2026-05-13: full v2 migration on yuzuki host — /api/v1/series and
        # /api/v1/books both 404 now. /api/v2/series/{id} returns the whole
        # series payload + the embedded `series_books` chapter list, so we
        # use it as the single source of truth for both context AND chapters.
        url = f"{self._API_URL}/api/v2/series/{series_id}"
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

    @staticmethod
    def _normalize_v2_genres(genre_blocks) -> List[str]:
        # v2 genres are dicts: {"genre_id":..., "genre_name":"Romance", "is_spoiler":false}
        names: List[str] = []
        if not isinstance(genre_blocks, list):
            return names
        for g in genre_blocks:
            if isinstance(g, dict):
                n = g.get("genre_name")
                if isinstance(n, str) and n:
                    names.append(n)
        return names

    @staticmethod
    def _normalize_v2_alt_titles(alt_blocks) -> List[str]:
        # v2 series_alternate_titles entries: {"label":"ja-Latn", "title":"..."}
        out: List[str] = []
        if not isinstance(alt_blocks, list):
            return out
        for it in alt_blocks:
            if isinstance(it, dict):
                t = it.get("title")
                if isinstance(t, str) and t:
                    out.append(t)
        return out

    @staticmethod
    def _v2_staff_to_authors(staff_blocks) -> List[str]:
        # series_staff is a list of staff entries; v2 leaves the staff array
        # empty for many series. When populated, each entry has fields like
        # role + name. We collect anything that looks like an author / artist
        # name; absent data → empty list (handler stays silent).
        out: List[str] = []
        if not isinstance(staff_blocks, list):
            return out
        for s in staff_blocks:
            if not isinstance(s, dict):
                continue
            nm = s.get("name") or s.get("staff_name") or s.get("title")
            if isinstance(nm, str) and nm:
                out.append(nm)
        return out

    def _check_kagane_setup(self) -> Optional[str]:
        """Return None if Kagane downloads can proceed, else a short reason
        string explaining what's missing. Drives the one-shot upfront warning
        in fetch_comic_context so users see the actionable problem before
        wasting time on chapter selection + retry passes.
        """
        if Cdm is None or Device is None or Pssh is None:
            return (
                "the 'pywidevine' package is missing or version-incompatible. "
                "Install with: pip install pywidevine"
            )
        if not self._locate_wvd_file():
            return (
                "no Widevine device file (*.wvd) found. Drop a *.wvd in the "
                "project root, or set KAGANE_WVD=<path-to-file.wvd>."
            )
        return None

    def _maybe_show_setup_warning(self) -> None:
        """Print a once-per-process notice if the Widevine setup is incomplete.

        Why up front instead of letting the chapter-image step fail: Kagane
        downloads are gated on Widevine DRM. Without pywidevine + a *.wvd
        device file, every chapter fetch will fail at the token-issue step,
        and the user only sees that after 'Selected N chapters' + 2 retry
        passes. Surfacing the actionable problem here saves ~15-30s per
        wasted attempt and points the user at the docs.
        """
        if KaganeSiteHandler._setup_warning_shown:
            return
        problem = self._check_kagane_setup()
        if not problem:
            return
        KaganeSiteHandler._setup_warning_shown = True
        # Plain prints (not log_verbose) so the warning appears regardless
        # of --verbose flag — this is a hard-fail condition the user
        # always needs to see.
        bar = "=" * 70
        print()
        print(bar)
        print("[!] Kagane downloads require Widevine DRM and will fail without setup:")
        print(f"    {problem}")
        print(f"    Setup guide: {self._INSTRUCTIONS_URL}")
        print("    Tip: try --multi-source to fall back to non-DRM mirrors.")
        print(bar)
        print()

    @staticmethod
    def _pick_cover_image_id(details: Dict) -> Optional[str]:
        # series_covers entries shape:
        #   {"chapter_number": "1", "cover_id": "<uuid>", "image_id": "<uuid>",
        #    "language": "en", "note": null, "volume_number": null}
        # Prefer en-language; fall back to first entry. The image_id is the
        # actual URL key; cover_id is metadata.
        covers = details.get("series_covers") if isinstance(details, dict) else None
        if not isinstance(covers, list) or not covers:
            return None
        en = next((c for c in covers if isinstance(c, dict) and c.get("language") == "en"), None)
        chosen = en or (covers[0] if isinstance(covers[0], dict) else None)
        if isinstance(chosen, dict):
            iid = chosen.get("image_id")
            if isinstance(iid, str) and iid:
                return iid
        return None

    def fetch_comic_context(
        self, url: str, scraper, make_request
    ) -> SiteComicContext:
        # Surface DRM-setup problems BEFORE chapter selection so the user
        # sees the actionable note rather than discovering it after every
        # chapter "Missed" + retries. See _maybe_show_setup_warning.
        self._maybe_show_setup_warning()
        series_id = self._extract_series_id(url)
        # 2026-05-13: skip HTML scrape entirely — Cloudflare interstitial
        # parses as "Just a moment..." title which corrupts downstream
        # filenames. The v2 series API gives a clean title + everything
        # else we need without ever touching the CF-fronted HTML page.
        details = self._fetch_series_api(series_id, scraper)
        title_raw = details.get("title") if isinstance(details, dict) else None
        title = (title_raw or series_id).strip() if isinstance(title_raw, str) else series_id

        # Cover image_id comes from series_covers — not the series_id (which
        # would 404 against /api/v2/image/). Fall back to no cover if the
        # payload didn't list any.
        cover_image_id = self._pick_cover_image_id(details if isinstance(details, dict) else {})
        cover_url = self._IMAGE_TEMPLATE.format(image_id=cover_image_id) if cover_image_id else None

        comic: Dict[str, object] = {
            "hid": series_id,
            "title": title,
            "_series_id": series_id,
            # Stash the full v2 payload so get_chapters can reuse it without
            # a second API call.
            "_v2_series_payload": details if isinstance(details, dict) else {},
        }
        if cover_url:
            comic["cover"] = cover_url

        if isinstance(details, dict) and details:
            description = details.get("description")
            if isinstance(description, str) and description.strip():
                comic["desc"] = description.strip()

            alt_names = self._normalize_v2_alt_titles(details.get("series_alternate_titles"))
            if alt_names:
                comic["alt_names"] = alt_names

            genres = self._normalize_v2_genres(details.get("genres"))
            if genres:
                comic["genres"] = genres

            authors = self._v2_staff_to_authors(details.get("series_staff"))
            if authors:
                comic["authors"] = authors

            status = details.get("publication_status")
            if isinstance(status, str) and status:
                comic["status"] = status

            year = details.get("start_year")
            if isinstance(year, int) and year > 0:
                comic["year"] = year

            fmt = details.get("format")
            if isinstance(fmt, str) and fmt:
                comic["type"] = fmt.lower()

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
        # Prefer the v2 payload stashed by fetch_comic_context to avoid a
        # second API call. Re-fetch only if context didn't carry it (defensive
        # — context.comic always contains it after fetch_comic_context).
        details = context.comic.get("_v2_series_payload")
        if not isinstance(details, dict) or not details:
            details = self._fetch_series_api(series_id, scraper) or {}

        chapters: List[Dict] = []
        # v2 series payload embeds the chapter list as `series_books`.
        # Each book has book_id, chapter_no (str), title, page_count,
        # published_on, groups (list of {group_id, title}).
        for entry in details.get("series_books", []):
            if not isinstance(entry, dict):
                continue
            chapter_id = entry.get("book_id")
            if not chapter_id:
                continue
            pages = entry.get("page_count") or 0
            chap_num = entry.get("chapter_no")
            # First group's title is the scanlation group name (most series
            # have exactly one group per chapter).
            group_name = None
            groups = entry.get("groups")
            if isinstance(groups, list) and groups and isinstance(groups[0], dict):
                gname = groups[0].get("title") or groups[0].get("group_name")
                if isinstance(gname, str) and gname:
                    group_name = gname
            chapters.append(
                {
                    "hid": chapter_id,
                    "chap": str(chap_num) if chap_num is not None else entry.get("title"),
                    "title": entry.get("title"),
                    "url": chapter_id,
                    "_series_id": series_id,
                    "_chapter_id": chapter_id,
                    "_pages": int(pages),
                    "uploaded": entry.get("published_on"),
                    "group_name": group_name,
                }
            )
        return chapters

    def get_group_name(self, chapter_version: Dict) -> Optional[str]:
        return chapter_version.get("group_name")

    def get_chapter_images(
        self, chapter: Dict, scraper, make_request
    ) -> List[Dict]:
        series_id = chapter.get("_series_id")
        chapter_id = chapter.get("_chapter_id") or chapter.get("hid")
        pages = int(chapter.get("_pages") or 0)
        if not series_id or not chapter_id or pages <= 0:
            raise RuntimeError(
                f"Kagane chapter metadata incomplete: series_id={series_id!r} "
                f"chapter_id={chapter_id!r} pages={pages}"
            )

        # Wrap the whole token-issue + image-fetch path with diagnostics.
        # Errors here used to bubble up as bare HTTPError / decryption traces
        # that aio-dl.py's chapter retry loop silently swallowed — making
        # "Missed N chapter(s)" the only user-visible signal. We surface the
        # failing step so the user can tell us which stage broke.
        key = (series_id, chapter_id)
        try:
            bundle = self._get_token_bundle(series_id, chapter_id, scraper, make_request)
        except RuntimeError:
            raise
        except Exception as e:
            from requests.exceptions import HTTPError
            status = None
            body_excerpt = ""
            if isinstance(e, HTTPError) and getattr(e, "response", None) is not None:
                status = e.response.status_code
                try:
                    body_excerpt = (e.response.text or "")[:200]
                except Exception:
                    body_excerpt = ""
            raise RuntimeError(
                f"Kagane token-issue failed for chapter {chapter_id!r} "
                f"(series {series_id!r}): "
                f"{type(e).__name__}: {e}"
                + (f" | http_status={status} body={body_excerpt!r}" if status else "")
            ) from e
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
            if resp.status_code >= 400:
                body_excerpt = ""
                try:
                    body_excerpt = (resp.text or "")[:200]
                except Exception:
                    body_excerpt = ""
                raise RuntimeError(
                    f"Kagane image fetch failed: chapter {chapter_id!r} page {idx}/{pages} "
                    f"-> http_status={resp.status_code} url={image_url} body={body_excerpt!r}"
                )
            try:
                data = self._decrypt_payload(resp.content, series_id, chapter_id, idx)
            except Exception as e:
                raise RuntimeError(
                    f"Kagane image decrypt failed: chapter {chapter_id!r} page {idx}/{pages} "
                    f"({len(resp.content)} bytes): {type(e).__name__}: {e}"
                ) from e
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
