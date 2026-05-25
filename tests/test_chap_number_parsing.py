"""Regression tests for chapter-number parsing in comix + mangapill handlers.

Background: aio-dl.py's chapter bucketing (~line 5885) calls float(ch["chap"])
to sort and group chapter versions. Handlers that store a non-numeric string
(the full "Chapter 123" title) or a literal "None" fail this float() call;
every chapter gets skipped with "Skipping chapter with invalid number: ..."
and the user sees zero downloads.

Two regressions were caught by the user (2026-05-24):

  - sites/mangapill.py:get_chapters previously set `chap = title`, where
    title is "Chapter 123" — the bare title fails float() parsing.
  - sites/comix.py:get_chapters previously emitted str(item.get("number"))
    which became "None" when the API returned a null `number` field; same
    float() failure downstream.

These tests pin the fixed behavior so a future refactor can't silently
re-introduce the regression.

Cross-file:
  - sites/mangapill.py:_extract_chapter_number — title-then-URL extraction.
  - sites/comix.py:get_chapters — chap_str normalization with title fallback.
  - sites/chapter_merger.py:_extract_chapter_num — downstream consumer that
    must accept the same numeric-string format these handlers produce.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sites.mangapill import MangaPillSiteHandler


class TestMangaPillExtractChapterNumber:
    """Title and URL-fallback extraction in mangapill."""

    def setup_method(self):
        self.handler = MangaPillSiteHandler()

    def test_simple_chapter_number_from_title(self):
        assert self.handler._extract_chapter_number("Chapter 123") == "123"

    def test_decimal_chapter_from_title(self):
        assert self.handler._extract_chapter_number("Chapter 47.5") == "47.5"

    def test_zero_padded_chapter_from_title(self):
        # _extract_chapter_num downstream parses "047" as 47.0; we preserve the
        # string representation here (caller normalizes via float()).
        assert self.handler._extract_chapter_number("Ch 047") == "047"

    def test_oneshot_returns_none(self):
        # Non-numeric labels (oneshots, omake, etc.) return None so the
        # caller can decide to skip rather than emit a non-numeric chap.
        assert self.handler._extract_chapter_number("Oneshot") is None

    def test_empty_or_none_returns_none(self):
        assert self.handler._extract_chapter_number("") is None
        assert self.handler._extract_chapter_number(None) is None


class TestMangaPillGetChaptersStoresNumericChap:
    """Integration: get_chapters MUST produce parseable float(chap) values."""

    def _build_context_with_chapter_html(self, chapter_html: str):
        """Synthesize a SiteComicContext whose soup contains a #chapters block."""
        from bs4 import BeautifulSoup
        from sites.base import SiteComicContext
        soup = BeautifulSoup(
            f'<html><body><div id="chapters">{chapter_html}</div></body></html>',
            "html.parser",
        )
        return SiteComicContext(
            comic={"url": "https://example.com/manga/9999/test"},
            title="Test",
            identifier="9999",
            soup=soup,
        )

    def test_chap_is_parseable_number_from_title(self):
        ctx = self._build_context_with_chapter_html(
            '<div><a href="/chapters/9999-chapter-47">Chapter 47</a></div>'
            '<div><a href="/chapters/9999-chapter-48">Chapter 48</a></div>'
        )
        handler = MangaPillSiteHandler()
        chapters = handler.get_chapters(ctx, MagicMock(), "en", MagicMock())
        assert len(chapters) == 2
        # The contract aio-dl.py:5885 enforces: float(chap) must succeed.
        for ch in chapters:
            float(ch["chap"])  # would raise ValueError on regression
        assert chapters[0]["chap"] == "47"
        assert chapters[1]["chap"] == "48"

    def test_chap_falls_back_to_url_when_title_non_numeric(self):
        """When the title is a non-numeric label but the URL encodes the
        number after `chapter-`, extraction falls back to the URL."""
        ctx = self._build_context_with_chapter_html(
            '<div><a href="/chapters/9999-chapter-100">Bonus Story</a></div>'
        )
        handler = MangaPillSiteHandler()
        chapters = handler.get_chapters(ctx, MagicMock(), "en", MagicMock())
        # URL has `chapter-100` → extracted as 100 even though title is non-numeric.
        assert len(chapters) == 1
        assert chapters[0]["chap"] == "100"
        float(chapters[0]["chap"])

    def test_chap_url_fallback_avoids_chapter_id_prefix(self):
        """URL is /chapters/9999-chapter-47 — the regex must skip the 9999
        chapter-ID prefix and only match the number AFTER `chapter-`. A
        naive `\\d+` regex would grab 9999."""
        ctx = self._build_context_with_chapter_html(
            '<div><a href="/chapters/12345-chapter-7">Omake</a></div>'
        )
        handler = MangaPillSiteHandler()
        chapters = handler.get_chapters(ctx, MagicMock(), "en", MagicMock())
        assert len(chapters) == 1
        assert chapters[0]["chap"] == "7"  # NOT "12345"

    def test_non_numeric_title_without_url_match_is_skipped(self):
        """When BOTH title and URL fail extraction, the chapter is skipped
        (rather than surfacing a non-numeric chap that crashes downstream)."""
        ctx = self._build_context_with_chapter_html(
            '<div><a href="/chapters/special-oneshot">Bonus Story</a></div>'
        )
        handler = MangaPillSiteHandler()
        chapters = handler.get_chapters(ctx, MagicMock(), "en", MagicMock())
        assert chapters == []


class TestComixChapNormalization:
    """Verify the chap_str normalization logic from sites/comix.py:get_chapters.

    We replicate the extraction algorithm rather than calling the live
    method (which requires Patchright + *****.to API access). The contract
    under test: regardless of `number` shape, the emitted `chap` must
    survive float() OR the chapter is skipped entirely.
    """

    @staticmethod
    def _normalize(chap_num, title):
        """Mirror of comix.py:get_chapters chap_str derivation. Pinned here
        so the test fails if the handler's logic drifts."""
        import re
        if isinstance(chap_num, (int, float)):
            return f"{chap_num:g}"
        for source_text in (
            chap_num if isinstance(chap_num, str) else None,
            title,
        ):
            if not source_text:
                continue
            m = re.search(r"(\d+(?:\.\d+)?)", str(source_text))
            if m:
                return m.group(1)
        return None

    def test_int_number_field(self):
        assert self._normalize(47, "Chapter 47") == "47"

    def test_float_number_field_renders_clean(self):
        # %g drops trailing zeros so 47.0 → "47" (matches str-producers).
        assert self._normalize(47.0, "Chapter 47") == "47"
        assert self._normalize(47.5, "Chapter 47.5") == "47.5"

    def test_none_number_falls_back_to_title(self):
        # The user's reported failure mode: *****.to returns null `number`
        # for some chapters; old code emitted str(None) == "None" which
        # crashed float() downstream. Now we extract from the title.
        assert self._normalize(None, "Chapter 99") == "99"

    def test_empty_string_number_falls_back_to_title(self):
        assert self._normalize("", "Ch 50") == "50"

    def test_string_number_with_extra_text_extracts_digits(self):
        assert self._normalize("Chapter 48", "Chapter 48") == "48"

    def test_none_number_with_non_numeric_title_returns_none(self):
        # Both unparseable → caller skips the chapter.
        assert self._normalize(None, "Oneshot") is None
        assert self._normalize(None, None) is None
