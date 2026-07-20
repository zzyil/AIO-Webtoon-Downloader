from __future__ import annotations

from typing import Dict, List, Optional
from urllib.parse import quote_plus, urljoin, urlparse

from .base import BaseSiteHandler, SearchHit, SiteComicContext


class AtsumaruSiteHandler(BaseSiteHandler):
    name = "atsumaru"
    domains = ("atsu.moe", "www.atsu.moe")

    _BASE_URL = "https://atsu.moe"
    _SEARCH_URL = "/collections/manga/documents/search"

    # Probe atsumaru's 8 breadth-sample chapters in ONE wave, not 2. Each sample
    # is a full-res color WebP CDN download (atsu.moe is WebP-native), so at the
    # base width of 4 the 8-chapter image-quality probe needs 2 waves and
    # routinely blew PROBE_SOURCE_BUDGET_S, timing out half the samples and
    # (pre-v5.2) scoring them 0.0 → a ~0.8 source read as ~0.10. atsu.moe is a
    # plain JSON API + CDN and real downloads already run ~8-wide image workers,
    # so 8 concurrent probe fetches is within its normal load. Combined with the
    # v5.2 timeout-exclusion in base._probe_chapter_aggregate this keeps the
    # probe measuring real quality; it does NOT (and is not meant to) drop the
    # probe under the 30s slow_probe threshold — atsumaru stays honestly flagged
    # "slow" in the search-health system (user decision). Grep
    # PROBE_BREADTH_CONCURRENCY.
    PROBE_BREADTH_CONCURRENCY = 8

    def __init__(self) -> None:
        super().__init__()
        self._api_headers = {
            "Accept": "*/*",
            "Referer": f"{self._BASE_URL}/",
            "Host": urlparse(self._BASE_URL).netloc,
        }

    # ----------------------------------------------------------------- helpers
    def _slug_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            return parsed.netloc
        if parts[0] == "manga":
            return parts[-1]
        return parts[-1]

    def _api_get(self, scraper, path: str, params: Optional[Dict[str, str]] = None):
        url = urljoin(self._BASE_URL, path)
        response = scraper.get(url, params=params, headers=self._api_headers)
        response.raise_for_status()
        return response

    def _parse_cover(self, payload: Dict) -> Optional[str]:
        poster = payload.get("poster") or payload.get("image")
        if isinstance(poster, dict):
            poster = poster.get("image")
        if isinstance(poster, str):
            poster = poster.lstrip("/")
            if poster.startswith("static/"):
                poster = poster[len("static/") :]
            return f"{self._BASE_URL}/static/{poster}"
        return None

    def _parse_people(self, entries: Optional[List[Dict]]) -> List[str]:
        if not entries:
            return []
        return [entry.get("name") for entry in entries if entry.get("name")]

    # ----------------------------------------------------------- Base overrides
    def configure_session(self, scraper, args) -> None:
        scraper.headers.setdefault("Referer", f"{self._BASE_URL}/")

    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        slug = self._slug_from_url(url)
        payload = self._api_get(scraper, "/api/manga/page", params={"id": slug}).json()
        manga = payload.get("mangaPage") or {}

        title = manga.get("title") or slug
        description = manga.get("synopsis")
        cover = self._parse_cover(manga)

        comic: Dict[str, object] = {
            "hid": slug,
            "title": title,
            "desc": description,
            "cover": cover,
            "genres": [tag.get("name") for tag in manga.get("tags") or [] if tag.get("name")],
            "url": url,
            # Store embedded chapters from the manga page as a fallback.
            # For adult-content manga, /api/manga/chapters returns empty without
            # an authenticated session, but /api/manga/page always includes the
            # first batch of chapters regardless of login state.
            "_embedded_chapters": manga.get("chapters") or [],
            "_has_more_chapters": bool(manga.get("hasMoreChapters")),
        }

        authors = self._parse_people(manga.get("authors"))
        if authors:
            comic["authors"] = authors

        comic["language"] = "en"

        return SiteComicContext(comic=comic, title=title, identifier=slug, soup=None)

    def _fetch_all_chapters(self, slug: str, scraper) -> List[Dict]:
        """Fetch all chapters via /api/manga/allChapters.

        This endpoint returns every chapter for the manga in a single response,
        avoiding the pagination gaps that /api/manga/page has (which shows only
        the newest and oldest chapters, hiding the middle ones behind a
        "Show All" button on the website).
        """
        try:
            response = self._api_get(
                scraper,
                "/api/manga/allChapters",
                params={"mangaId": slug},
            )
            payload = response.json()
            return payload.get("chapters") or []
        except Exception:
            return []

    def _fetch_chapter_batch(self, slug: str, page: int, scraper) -> Dict:
        response = self._api_get(
            scraper,
            "/api/manga/chapters",
            params={
                "id": slug,
                "filter": "all",
                "sort": "desc",
                "page": str(page),
            },
        )
        return response.json()

    def _fetch_manga_page_chapter_batch(self, slug: str, index: int, scraper) -> Dict:
        """Fetch chapters via /api/manga/page with an index offset.

        atsu.moe uses ``index`` (0-based position in descending order) to
        paginate chapters through this endpoint, which works for adult-content
        manga even without an authenticated session.
        """
        response = self._api_get(
            scraper,
            "/api/manga/page",
            params={"id": slug, "index": str(index)},
        )
        payload = response.json()
        manga = payload.get("mangaPage") or {}
        return {
            "chapters": manga.get("chapters") or [],
            "hasMoreChapters": bool(manga.get("hasMoreChapters")),
        }

    def _parse_chapter_entry(self, slug: str, entry: Dict, fallback_index: int = 0) -> Dict:
        """Convert a raw chapter dict from either API source into a normalised chapter dict."""
        chapter_id = entry.get("id")
        chap_number = entry.get("number")
        title = entry.get("title")
        date = entry.get("createdAt")
        # Shared ISO-8601 'Z' parser (grep _parse_iso_z_timestamp); None on a
        # miss -> keep the 0 default this entry path expects.
        uploaded = 0
        if isinstance(date, str):
            uploaded = self._parse_iso_z_timestamp(date) or 0

        # Determine the chapter number string used for filtering.
        # If chap_number is set, use it directly. Otherwise fall back to the
        # title string. If the title is also non-numeric (common for adult
        # content where chapters use descriptive titles), assign a sequential
        # index so the main filtering logic does not silently discard it.
        if chap_number is not None:
            chap_str = str(chap_number)
        elif title is not None:
            # Try to parse a number out of the title first
            num = self._chapter_number_from_text(str(title))
            if num is not None:
                chap_str = num
            else:
                # Non-numeric title — use a positional index so the chapter
                # isn't discarded by the float-parsing filter in aio-dl.py.
                chap_str = str(fallback_index)
        else:
            chap_str = str(fallback_index)

        return {
            "hid": f"{slug}-{chapter_id}",
            "chap": chap_str,
            "title": title,
            "url": f"/read/{slug}/{chapter_id}",
            "_slug": slug,
            "_chapter_id": chapter_id,
            "uploaded": uploaded,
        }

    def get_chapters(
        self,
        context: SiteComicContext,
        scraper,
        language: str,
        make_request,
    ) -> List[Dict]:
        slug = context.identifier
        page = 0
        chapters: List[Dict] = []
        while True:
            batch = self._fetch_chapter_batch(slug, page, scraper)
            entries = batch.get("chapters") or []
            for i, entry in enumerate(entries):
                chapters.append(self._parse_chapter_entry(slug, entry, fallback_index=len(chapters) + 1))
            pages_total = batch.get("pages")
            current_page = batch.get("page", page)
            has_next = (
                isinstance(pages_total, int)
                and isinstance(current_page, int)
                and current_page + 1 < pages_total
            )
            if not has_next or not entries:
                break
            page = current_page + 1

        if chapters:
            return chapters

        # --- Fallback for adult-content manga ---
        # /api/manga/chapters returns empty without an authenticated session.
        # First try /api/manga/allChapters which returns every chapter in one
        # shot — this avoids the pagination gaps that /api/manga/page has
        # (it only shows the newest and oldest chapters, hiding the middle
        # ones behind a "Show All" button on the website).
        all_entries = self._fetch_all_chapters(slug, scraper)
        if all_entries:
            for entry in all_entries:
                chapters.append(self._parse_chapter_entry(slug, entry, fallback_index=len(chapters) + 1))
            return chapters

        # Last resort: use embedded chapters from the manga page response,
        # then paginate via the manga page endpoint if hasMoreChapters is set.
        embedded: List[Dict] = list(context.comic.get("_embedded_chapters") or [])
        has_more: bool = bool(context.comic.get("_has_more_chapters"))

        for entry in embedded:
            chapters.append(self._parse_chapter_entry(slug, entry, fallback_index=len(chapters) + 1))

        if has_more and embedded:
            # Paginate using the last chapter's index field as the offset.
            # The API returns chapters in descending order; we pass the index
            # of the last chapter we received so the server continues from
            # the next batch.
            last_index = embedded[-1].get("index")
            seen_ids = {c.get("id") for c in embedded if c.get("id")}
            
            while has_more and last_index is not None:
                try:
                    batch = self._fetch_manga_page_chapter_batch(slug, last_index, scraper)
                except Exception:
                    break
                next_entries = batch.get("chapters") or []
                has_more = bool(batch.get("hasMoreChapters"))
                if not next_entries:
                    break
                    
                added_any = False
                for entry in next_entries:
                    eid = entry.get("id")
                    if eid and eid not in seen_ids:
                        seen_ids.add(eid)
                        chapters.append(self._parse_chapter_entry(slug, entry, fallback_index=len(chapters) + 1))
                        added_any = True
                
                if not added_any:
                    break
                last_index = next_entries[-1].get("index")

        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        slug = chapter.get("_slug")
        chapter_id = chapter.get("_chapter_id")
        if not slug or not chapter_id:
            # fallback to parsing from URL
            url = chapter.get("url", "")
            parts = [p for p in url.split("/") if p]
            if len(parts) >= 3:
                slug = parts[-2]
                chapter_id = parts[-1]
        if not slug or not chapter_id:
            raise RuntimeError("Atsumaru chapter identifiers missing.")

        response = self._api_get(
            scraper,
            "/api/read/chapter",
            params={"mangaId": slug, "chapterId": chapter_id},
        )
        payload = response.json()
        pages = ((payload or {}).get("readChapter") or {}).get("pages") or []
        images: List[str] = []
        for page in pages:
            image = page.get("image")
            if not image:
                continue
            images.append(urljoin(self._BASE_URL, image))
        if not images:
            raise RuntimeError("No images returned for Atsumaru chapter.")
        return images

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
        params = (
            f"?q={quote_plus(clean)}"
            f"&query_by=title,englishTitle,otherNames"
            f"&per_page={int(limit)}"
        )
        response = make_request(urljoin(self._BASE_URL, self._SEARCH_URL + params), scraper)
        try:
            data = response.json()
        except ValueError:
            return []
        hits_raw = data.get("hits") if isinstance(data, dict) else None
        if not isinstance(hits_raw, list):
            return []

        hits: List[SearchHit] = []
        for idx, item in enumerate(hits_raw):
            doc = item.get("document") if isinstance(item, dict) else None
            if not isinstance(doc, dict):
                continue
            slug = doc.get("id")
            title = (doc.get("englishTitle") or doc.get("title") or "").strip()
            if not slug or not title:
                continue
            alt_titles = []
            for value in (doc.get("title"), *(doc.get("otherNames") or [])):
                if isinstance(value, str) and value.strip() and value.strip() != title:
                    alt_titles.append(value.strip())
            cover = self._parse_cover(doc)
            chapter_count = doc.get("chaptersCount") or doc.get("chapterCount")
            if not isinstance(chapter_count, int):
                chapter_count = None
            year = doc.get("year")
            if not isinstance(year, int):
                year = None
            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=urljoin(self._BASE_URL, f"/manga/{slug}"),
                    cover=cover,
                    alt_titles=alt_titles,
                    year=year,
                    chapter_count_hint=chapter_count,
                    raw_score=max(0.05, 1.0 - (idx / max(1, len(hits_raw)))),
                )
            )
            if len(hits) >= limit:
                break
        return hits


__all__ = ["AtsumaruSiteHandler"]
