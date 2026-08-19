from __future__ import annotations

import re
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BaseSiteHandler, SearchHit, SiteComicContext


class WeebCentralSiteHandler(BaseSiteHandler):
    name = "weebcentral"
    domains = ("weebcentral.com", "www.weebcentral.com")

    _BASE_URL = "https://weebcentral.com"
    _SERIES_HREF_RE = re.compile(r"/series/[A-Z0-9]+/")

    # ----------------------------------------------------------------- helpers

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
        # Thin wrapper over the shared BaseSiteHandler._parse_iso_z_timestamp
        # (grep it); the strip is kept here because this call site historically
        # trimmed whitespace before parsing (the <time datetime=…> attr).
        if not iso_text:
            return None
        return self._parse_iso_z_timestamp(iso_text.strip())

    def _extract_chapter_number(self, text: str) -> Optional[str]:
        return self._chapter_number_from_text(text)

    def _fetch_html(self, url: str, scraper, make_request, what: str) -> str:
        """Fetch *url*, diverting to the shared CF rescue when turned away.

        Ladder: cloudscraper → crawlee_utils.rescue_cf_html → raise.

        Turn-away is TWO tests — a 403/429/503 status, and is_cf_challenge on
        the body, which is what catches the HTTP-200 interstitial. Getting by
        with only the first is what let a challenge page reach the parser here.

        The rescue itself is deliberately NOT a ladder in this file any more.
        It used to be one (embedder browser → impit → CF solver), ordered by
        hand so that impit stayed ahead of the window-opening solver on desktop
        while the Android WebView — the only rung that exists there — went
        first. All of that is now expressed once, inside rescue_cf_html, whose
        tiers are impit → embedder WebView → zendriver with the last two gated
        on an opt-in permission. A per-file copy of that policy is exactly how
        sites/manhwaread.py and sites/madara.py drifted apart.

        Not a zstd problem. The comment here used to blame cloudscraper
        advertising zstd it cannot decode; that was measured false — cloudscraper
        sends `Accept-Encoding: gzip, deflate, br` (and strips `br` unless
        allow_brotli), and urllib3 only offers zstd when backports.zstd is
        installed. What actually fires this path is the 403/429/503 Cloudflare
        branch below.
        """
        from .crawlee_utils import (
            is_cf_challenge,
            rescue_cf_html,
            warn_cf_rescue,
        )

        errors: List[str] = []
        try:
            resp = make_request(url, scraper)
            if resp.status_code in (403, 429, 503):
                raise RuntimeError(f"HTTP {resp.status_code}")
            # BOTH tests, because they catch different things and this file
            # shipped with only the first. A status check alone misses the
            # JS-redirect interstitial, which CF serves with **HTTP 200** — that
            # body was being returned verbatim and parsed as the series page,
            # i.e. exactly the defect the ladder below exists to prevent, still
            # live inside the function rewritten to fix it. is_cf_challenge
            # covers the 200 variant (grep its `status_code == 200` branch);
            # the status check stays because a bare 403/429/503 with no CF
            # phrases is still a turn-away worth escalating.
            if is_cf_challenge(resp.status_code, resp.text):
                raise RuntimeError("Cloudflare interstitial served with HTTP 200")
            return resp.text
        except Exception as exc:
            errors.append(f"cloudscraper: {exc}")

        # ONE rung now, not three. rescue_cf_html owns the tier order itself
        # (impit -> embedder WebView -> zendriver, with the two human-facing
        # tiers gated on interactive_solve_allowed()), so the hand-rolled
        # ladder that used to live here — including the embedder_only= dance
        # that existed purely to keep impit ahead of a window-opening solver on
        # desktop — is now a second copy of a policy that has a home. Two copies
        # of a rescue policy is how sites/manhwaread.py drifted from
        # sites/madara.py; grep the tier list in crawlee_utils.rescue_cf_html.
        # Unconditional: rescue_cf_html's first tier is impit, which needs no
        # solver at all, so there is no capability question left to ask here.
        try:
            return rescue_cf_html(url, base_url=self._BASE_URL, scraper=scraper)
        except Exception as exc:
            errors.append(f"rescue: {exc}")

        warn_cf_rescue(url, "; ".join(errors))
        raise RuntimeError(
            f"WeebCentral {what} fetch failed: " + "; ".join(errors)
        )

    # ----------------------------------------------------------- Base overrides
    def configure_session(self, scraper, args) -> None:
        scraper.headers.setdefault("Referer", self._BASE_URL + "/")

    def fetch_comic_context(self, url: str, scraper, make_request) -> SiteComicContext:
        # Same ladder as the other two fetches. This one used to be a bare
        # make_request, which on a content-bearing 403 hands the interstitial
        # straight to the parser: title degrades to the URL slug, no cover, no
        # description — and get_chapters raises seconds later anyway. Failing
        # here with the ladder's own message beats shipping junk metadata.
        html = self._fetch_html(url, scraper, make_request, "series page")
        soup = self._make_soup(html)

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
        # WeebCentral's series template MAY expose a separate "Artist(s)"
        # row in the .post_content_item list. When present, surface it for
        # Komikku's details.json. When absent (the dominant case — WeebCentral
        # typically conflates author + artist into the Author row), `artists`
        # stays empty and the field is documented as a per-site limitation.
        # See dry_run_komikku_findings.md §A.
        artists = self._extract_list_values(
            hero or soup, ["artist", "illustrator"]
        )
        tags = self._extract_list_values(hero or soup, ["tag", "type"])
        status_values = self._extract_list_values(hero or soup, ["status"])
        alt_values = self._extract_list_values(
            hero or soup, ["associated names", "alternative", "alias"]
        )
        year_values = self._extract_list_values(hero or soup, ["released", "year"])

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
        if artists:
            comic["artists"] = artists
        if tags:
            comic["genres"] = tags
        if status_values:
            comic["status"] = status_values[0]
        if alt_values:
            comic["alt_names"] = alt_values
        if year_values:
            year_match = re.search(r"\b(\d{4})\b", year_values[0])
            if year_match:
                comic["year"] = int(year_match.group(1))

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
        chapter_html = self._fetch_html(
            chapter_url, scraper, make_request, "chapter list"
        )
        soup = self._make_soup(chapter_html)

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
            # No group/scanlator field: WeebCentral does not credit scanlators
            # anywhere in the chapter list. It used to derive one from an
            # inline `svg[stroke]` hex (#d8b4fe -> "Official", #4C4D54 ->
            # "Unknown"), but the markup is now an <img src=".../
            # chapter-badge.svg"> with no inline SVG at all, so that selector
            # matched nothing and every chapter got scanlator=None. Removed
            # rather than repaired — there is no real name to recover, and the
            # bogus "Official" string would now be read as a genuine
            # is_official signal by base.group_matches_filter.
            chapters.append(
                {
                    "hid": abs_url.rstrip("/"),
                    "chap": self._extract_chapter_number(title) or title,
                    "title": title,
                    "url": abs_url,
                    "uploaded": uploaded,
                }
            )
        return chapters

    def get_chapter_images(self, chapter: Dict, scraper, make_request) -> List[str]:
        chapter_url = chapter.get("url")
        if not chapter_url:
            raise RuntimeError("Chapter missing URL.")
        base = chapter_url.rstrip("/")
        # Ensure we request the full list
        images_url = f"{base}/images?is_prev=False&current_page=1&reading_style=long_strip"
        images_html = self._fetch_html(images_url, scraper, make_request, "images")
        soup = self._make_soup(images_html)
        
        images: List[str] = []
        # The images usually have class "maw-w-full" (max-width: full)
        # Fallback to all images if specific class not found, but filter out small icons
        candidates = soup.select("img.maw-w-full") or soup.select("img")
        
        for img in candidates:
            src = img.get("src") or img.get("data-src")
            if not src:
                continue
            # Filter out likely non-content images based on keywords or size if possible
            # But for now, just filtering by extension or path might be enough if needed.
            # The inspection showed valid images are like .../0001-001.png
            
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = urljoin(images_url, src)
            elif not src.startswith("http"):
                src = urljoin(images_url, src)
            
            # Basic filtering to avoid site logos/icons if we fell back to "img"
            if "static/images" in src or "brand" in src:
                continue
                
            if src not in images:
                images.append(src)
                
        if not images:
            raise RuntimeError("Unable to locate images for chapter.")
        return images

    # WeebCentral's /search page is JS-driven; /search/data returns the rendered
    # results as an HTMX fragment of <article class="bg-base-300"> blocks, each
    # with an <a href="/series/<UUID>/<slug>"> wrapping the cover <img alt="… cover">.
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
        from urllib.parse import quote_plus

        url = (
            f"{self._BASE_URL}/search/data"
            f"?text={quote_plus(clean)}"
            f"&sort=Best+Match&order=Descending&official=Any&anime=Any"
            f"&adult=Any&display_mode=Full+Display&series_status=Any"
        )
        # The FOURTH bare fetch in this file, and the one the earlier CF sweep
        # missed because it lives in search() rather than the three data
        # fetches. Same treatment now: a challenged body must never be parsed
        # into "0 results", which is indistinguishable from a genuine miss and
        # is what made this class of failure invisible. Interactive solving is
        # already denied here by default (crawlee_utils' opt-in permission), so
        # this buys the headless tiers without ever facing a human.
        html = self._fetch_html(url, scraper, make_request, "search")
        if not html or len(html) < 100:
            return []

        soup = self._make_soup(html)
        articles = [
            article
            for article in soup.select("article.bg-base-300, article")
            if article.find("a", href=self._SERIES_HREF_RE)
        ]
        hits: List[SearchHit] = []
        seen: set[str] = set()
        for idx, article in enumerate(articles):
            if len(hits) >= limit:
                break
            anchor = article.find("a", href=self._SERIES_HREF_RE)
            if not anchor:
                continue
            href = (anchor.get("href") or "").strip()
            abs_url = href if href.startswith("http") else urljoin(self._BASE_URL, href)
            abs_url = abs_url.split("?")[0].split("#")[0]
            if abs_url in seen:
                continue
            seen.add(abs_url)

            img = article.select_one("img[alt]")
            title: Optional[str] = None
            if img:
                alt = (img.get("alt") or "").strip()
                if alt.lower().endswith(" cover"):
                    alt = alt[:-len(" cover")].strip()
                if alt:
                    title = alt
            if not title:
                disp = article.select_one(".text-ellipsis")
                if disp:
                    title = disp.get_text(strip=True)
            if not title:
                slug = abs_url.rstrip("/").rsplit("/", 1)[-1]
                title = slug.replace("-", " ").strip() or slug

            cover: Optional[str] = None
            source = article.select_one("source[srcset]")
            if source:
                srcset = (source.get("srcset") or "").strip()
                if srcset:
                    cover = srcset.split()[0]
            if not cover and img:
                src = img.get("src")
                if src:
                    cover = src

            alt_titles: List[str] = []
            slug = abs_url.rstrip("/").rsplit("/", 1)[-1]
            slug_alt = slug.replace("-", " ").strip()
            if slug_alt and slug_alt.lower() != (title or "").lower():
                alt_titles.append(slug_alt)

            hits.append(
                SearchHit(
                    site=self.name,
                    title=title,
                    url=abs_url,
                    cover=cover,
                    alt_titles=alt_titles,
                    raw_score=max(0.05, 1.0 - (idx / max(1, len(articles)))),
                )
            )
        return hits


__all__ = ["WeebCentralSiteHandler"]
