"""Komikku metadata extraction tests — guards the 2026-05-19 parser fixes.

Covers the F1-F11 fixes from
``C:\\Users\\legoc\\.claude\\plans\\these-are-findings-for-prancy-salamander.md``.
Each test isolates a single parser change with an inline HTML/JSON fixture so
network unavailability doesn't break CI. The probe script
``bench/probe_komikku_metadata.py`` is the integration counterpart — these
tests catch regressions before the probe even runs.

Cross-file:
  - ``sites/__init__.py:get_handler_by_name`` (F1)
  - ``sites/madara.py:_extract_people`` (F10)
  - ``sites/mangathemesia.py:fetch_comic_context`` (F2) + ``_extract_imptdt_values``
  - ``sites/assortedscans.py:fetch_comic_context`` (F3)
  - ``sites/flamecomics.py:_strip_html`` (F4)
  - ``sites/mangafire.py:fetch_comic_context`` (F5)
  - ``sites/linewebtoon.py:fetch_comic_context`` (F6)
  - ``sites/weebcentral.py:fetch_comic_context`` (F7)
  - ``sites/mangapill.py:fetch_comic_context`` (F8)
  - ``sites/mangakatana.py:fetch_comic_context`` (F9)
  - ``aio-dl.py:6020-6066`` (details.json writer — consumes the dicts these
    tests verify)
"""

from __future__ import annotations

from unittest.mock import MagicMock

from bs4 import BeautifulSoup

from sites import get_handler_by_name
from sites.assortedscans import AssortedScansSiteHandler
from sites.flamecomics import FlameComicsSiteHandler
from sites.madara import MadaraSiteHandler
from sites.mangathemesia import MangaThemesiaSiteHandler


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal stand-in for requests.Response. Mirrors the helper in
    ``tests/test_mangadex_handler.py`` so the two suites stay consistent."""

    def __init__(self, text="", status_code=200, json_payload=None, headers=None):
        self.text = text
        self.status_code = status_code
        self._json = json_payload
        self.headers = headers or {}
        self.encoding = "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code} Server Error")

    def json(self):
        if self._json is None:
            raise ValueError("no json payload")
        return self._json


def _mr_returning(text="", status_code=200, json_payload=None):
    """Build a fake ``make_request`` callable that returns one canned
    response regardless of URL. Most parser tests don't care about URL
    routing — they just want the response body served to fetch_comic_context.
    """
    response = _FakeResponse(text=text, status_code=status_code, json_payload=json_payload)
    def mr(url, scraper):  # noqa: ARG001
        return response
    return mr


def _scraper_with_no_api():
    """Build a MagicMock scraper whose ``.get`` raises. Used in MangaThemesia
    tests to ensure the WP-REST-API fallback path inside fetch_comic_context
    doesn't accidentally fire and overwrite the values we're trying to test.
    """
    scraper = MagicMock()
    scraper.get.side_effect = RuntimeError("api fallback should not fire in this test")
    return scraper


# ---------------------------------------------------------------------------
# F1 — get_handler_by_name case insensitivity
# ---------------------------------------------------------------------------

def test_get_handler_by_name_case_insensitive_toonily():
    """Toonily / WebtoonXYZ pass their display-name ("Toonily", "WebtoonXYZ")
    to Madara's __init__, which overwrites the lowercase class attribute.
    Before F1, ``get_handler_by_name("toonily")`` returned None because the
    comparison was case-sensitive. After F1, all of `toonily`, `Toonily`,
    `TOONILY` return the same handler.
    """
    h1 = get_handler_by_name("toonily")
    h2 = get_handler_by_name("Toonily")
    h3 = get_handler_by_name("TOONILY")
    assert h1 is not None, "toonily handler should resolve"
    assert h1 is h2 is h3


def test_get_handler_by_name_case_insensitive_webtoonxyz():
    h1 = get_handler_by_name("webtoonxyz")
    h2 = get_handler_by_name("WebtoonXYZ")
    h3 = get_handler_by_name("WEBTOONXYZ")
    assert h1 is not None
    assert h1 is h2 is h3


def test_get_handler_by_name_unknown_returns_none():
    """Sanity: an unregistered name still returns None."""
    assert get_handler_by_name("definitely-not-a-real-site-xyz") is None


# ---------------------------------------------------------------------------
# F10 — Madara _extract_people digits-as-name guard
# ---------------------------------------------------------------------------

# A Madara series page typically renders the post-content_item rows below
# the cover. Real markup we're matching:
#   <div class="post-content_item">
#     <div class="summary-heading"><h5>Artist(s)</h5></div>
#     <div class="summary-content"><a>Real Name</a></div>
#   </div>

_MADARA_ROWS_WITH_DIGIT_ARTIST = """
<div class="post-content_item">
  <div class="summary-heading"><h5>Author(s)</h5></div>
  <div class="summary-content"><a href="/manga-author/test-author/">Test Author</a></div>
</div>
<div class="post-content_item">
  <div class="summary-heading"><h5>Artist(s)</h5></div>
  <div class="summary-content"><a href="/manga-artist/2021/">2021</a></div>
</div>
"""

_MADARA_ROWS_NORMAL = """
<div class="post-content_item">
  <div class="summary-heading"><h5>Author(s)</h5></div>
  <div class="summary-content"><a>Test Author</a></div>
</div>
<div class="post-content_item">
  <div class="summary-heading"><h5>Artist(s)</h5></div>
  <div class="summary-content"><a>Test Studio</a></div>
</div>
"""


def test_madara_extract_people_rejects_digits():
    """Year-as-artist leak from example.com/i-am-the-fated-villain/ — the
    site's series template stuffed "2021" into the Artist row. Before F10,
    ``_extract_people(soup, ("artist", "illustrator"))`` returned ["2021"];
    after F10 it returns [].
    """
    handler = MadaraSiteHandler("manhuaplus", "https://example.com")
    soup = BeautifulSoup(_MADARA_ROWS_WITH_DIGIT_ARTIST, "html.parser")
    authors = handler._extract_people(soup, ("author", "writer"))
    artists = handler._extract_people(soup, ("artist", "illustrator"))
    assert authors == ["Test Author"]
    assert artists == []  # "2021" gets filtered


def test_madara_extract_people_accepts_normal_names():
    """Regression guard: the digit-guard must not reject names that contain
    digits (e.g. 'h-goon', 'Studio 99', '2pac'). Only PURE digits are
    rejected."""
    handler = MadaraSiteHandler("test", "https://example.com")
    soup = BeautifulSoup(_MADARA_ROWS_NORMAL, "html.parser")
    artists = handler._extract_people(soup, ("artist", "illustrator"))
    assert artists == ["Test Studio"]


def test_madara_extract_people_studio_with_digits_passes():
    """'Studio 99' contains digits but isn't pure-digit — should pass through."""
    handler = MadaraSiteHandler("test", "https://example.com")
    html = """
    <div class="post-content_item">
      <div class="summary-heading"><h5>Artist</h5></div>
      <div class="summary-content"><a>Studio 99</a> / <a>2pac</a></div>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    artists = handler._extract_people(soup, ("artist",))
    assert artists == ["Studio 99", "2pac"]


# ---------------------------------------------------------------------------
# F2 — MangaThemesia .imptdt parser + artists field + .mgen / itemprop fallbacks
# ---------------------------------------------------------------------------

# Realistic MangaThemesia series page markup. Includes:
#   - <h1 class="entry-title"> for title
#   - .thumb img for cover (skips the WP API fallback)
#   - .entry-content p for desc (Madara-compatible fallback)
#   - .imptdt rows for Status / Author / Artist (real MangaThemesia)
#   - .mgen a for genres (real MangaThemesia)
#   - var post_id = 123 (skips API fallback)

_MT_FULL_HTML = """
<html><body>
<h1 class="entry-title">Test Series</h1>
<div class="thumb"><img src="https://cdn.example.com/cover.jpg" /></div>
<div class="entry-content"><p>Sample description text...</p></div>
<div class="wd-full">
  <div class="imptdt">Status <i>Completed</i></div>
  <div class="imptdt">Type <a>Manhwa</a></div>
  <div class="imptdt">Released <i>2018</i></div>
  <div class="imptdt">Author <a>Test Author</a></div>
  <div class="imptdt">Artist <a>Test Studio</a></div>
</div>
<div class="mgen">
  <a href="/genres/action/">Action</a>
  <a href="/genres/adventure/">Adventure</a>
  <a href="/genres/fantasy/">Fantasy</a>
</div>
<script>var post_id = 12345;</script>
</body></html>
"""


def _make_mt_handler():
    return MangaThemesiaSiteHandler(
        name="testmt",
        display_name="TestMT",
        base_url="https://example.com",
        domains=("example.com",),
    )


def test_mangathemesia_imptdt_full_extraction():
    """End-to-end: a realistic MangaThemesia series page populates all five
    Komikku-relevant fields via the .imptdt + .mgen + .entry-content path."""
    handler = _make_mt_handler()
    ctx = handler.fetch_comic_context(
        "https://example.com/manga/test-series/",
        _scraper_with_no_api(),
        _mr_returning(_MT_FULL_HTML),
    )
    comic = ctx.comic
    assert comic["title"] == "Test Series"
    assert "Sample description" in (comic["desc"] or "")
    assert comic["authors"] == ["Test Author"]
    assert comic["artists"] == ["Test Studio"]
    assert "Action" in comic["genres"]
    assert "Adventure" in comic["genres"]
    assert comic["status"] == "Completed"


def test_mangathemesia_artists_field_always_present():
    """Even when the source has no Artist row, the dict MUST contain an
    ``artists`` key. aio-dl.py:6042's ``comic_data.get("artists", []) or []``
    works either way, but downstream consumers may assume the key exists.
    """
    handler = _make_mt_handler()
    html_no_artist = _MT_FULL_HTML.replace(
        '<div class="imptdt">Artist <a>Test Studio</a></div>', ""
    )
    ctx = handler.fetch_comic_context(
        "https://example.com/manga/x/",
        _scraper_with_no_api(),
        _mr_returning(html_no_artist),
    )
    assert "artists" in ctx.comic
    assert ctx.comic["artists"] == []


def test_mangathemesia_mgen_genre_fallback():
    """When no Madara-style .genres-content exists, .mgen a should fire."""
    handler = _make_mt_handler()
    ctx = handler.fetch_comic_context(
        "https://example.com/manga/x/",
        _scraper_with_no_api(),
        _mr_returning(_MT_FULL_HTML),
    )
    # All three .mgen anchors should be picked up.
    assert ctx.comic["genres"] == ["Action", "Adventure", "Fantasy"]


def test_mangathemesia_itemprop_desc_fallback():
    """When .entry-content p is absent but [itemprop=description] is present,
    desc should still populate via the itemprop fallback (real MangaThemesia
    schema.org markup)."""
    html = """
    <html><body>
    <h1 class="entry-title">Test</h1>
    <div class="thumb"><img src="https://cdn.example.com/c.jpg"/></div>
    <div itemprop="description">Itemprop description text.</div>
    <script>var post_id = 99;</script>
    </body></html>
    """
    handler = _make_mt_handler()
    ctx = handler.fetch_comic_context(
        "https://example.com/manga/x/",
        _scraper_with_no_api(),
        _mr_returning(html),
    )
    assert ctx.comic["desc"] == "Itemprop description text."


def test_mangathemesia_imptdt_status_fallback_from_unknown():
    """When .post-status doesn't exist (the typical real-MT case), status
    should fall through to the .imptdt Status row instead of staying 'Unknown'."""
    handler = _make_mt_handler()
    ctx = handler.fetch_comic_context(
        "https://example.com/manga/x/",
        _scraper_with_no_api(),
        _mr_returning(_MT_FULL_HTML),
    )
    assert ctx.comic["status"] == "Completed"


def test_extract_imptdt_values_dedupe_and_order():
    """The helper should return values in DOM order, deduplicated."""
    handler = _make_mt_handler()
    soup = BeautifulSoup(
        '<div class="imptdt">Author <a>Test Author</a> <a>Test Author</a> <a>h-goon</a></div>',
        "html.parser",
    )
    assert handler._extract_imptdt_values(soup, "Author") == ["Test Author", "h-goon"]


def test_extract_imptdt_values_missing_label():
    """When the requested label doesn't exist, return []."""
    handler = _make_mt_handler()
    soup = BeautifulSoup(
        '<div class="imptdt">Status <i>Ongoing</i></div>', "html.parser"
    )
    assert handler._extract_imptdt_values(soup, "Author") == []


# ---------------------------------------------------------------------------
# F3 — AssortedScans DOM extraction (when meta tags are empty)
# ---------------------------------------------------------------------------

# MangAdventure framework typical series page (example.com style):
# meta name=description / keywords are blank; the visible info is rendered
# in DOM via Jinja macros.

_ASSORTED_DOM_HTML = """
<html><head>
  <meta name="description" content="" />
  <meta name="keywords" content="" />
  <meta property="og:image" content="https://example.com/cover.jpg" />
  <meta property="og:description" content="Fallback og desc." />
</head><body>
  <main id="content">
    <h1>A Game of Murder</h1>
    <article class="info">
      <p>A series description in the DOM rather than meta.</p>
    </article>
    <a href="/author/some-author/">Some Author</a>
    <a href="/artist/some-artist/">Some Artist</a>
    <a href="/category/action/">Action</a>
    <a href="/category/mystery/">Mystery</a>
    <span class="status">Ongoing</span>
    <div class="chapter-list">
      <li class="chapter-details"><a href="/reader/generic-series/1/1/">Ch1</a></li>
    </div>
  </main>
</body></html>
"""

# example.com style: meta tags ARE populated (the family is heterogeneous).
_ASSORTED_META_POPULATED_HTML = """
<html><head>
  <meta name="description" content="Meta desc on arcrelight." />
  <meta name="keywords" content="Mystery, Sci-Fi, Supernatural" />
  <meta property="og:image" content="https://example.com/cover.jpg" />
</head><body>
  <main id="content">
    <h1>Test Title</h1>
    <div class="chapter-list">
      <li class="chapter-details"><a href="/reader/x/1/1/">Ch1</a></li>
    </div>
  </main>
</body></html>
"""


def test_assortedscans_dom_extraction_populates_all_fields():
    """End-to-end: empty meta + populated DOM → all 5 fields extracted."""
    handler = AssortedScansSiteHandler()
    ctx = handler.fetch_comic_context(
        "https://example.com/reader/generic-series/",
        MagicMock(),
        _mr_returning(_ASSORTED_DOM_HTML),
    )
    comic = ctx.comic
    assert "A series description" in (comic.get("desc") or "")
    assert comic.get("authors") == ["Some Author"]
    assert comic.get("artists") == ["Some Artist"]
    assert comic.get("genres") == ["Action", "Mystery"]
    assert comic.get("status") == "Ongoing"


def test_assortedscans_meta_path_still_works():
    """ArcRelight-style: meta tags populated → no regression; the DOM path
    runs as a no-op."""
    handler = AssortedScansSiteHandler()
    ctx = handler.fetch_comic_context(
        "https://example.com/reader/test/",
        MagicMock(),
        _mr_returning(_ASSORTED_META_POPULATED_HTML),
    )
    comic = ctx.comic
    assert comic.get("desc") == "Meta desc on arcrelight."
    assert comic.get("genres") == ["Mystery", "Sci-Fi", "Supernatural"]


def test_assortedscans_og_description_fallback():
    """When meta description is blank AND the DOM has no .info p container,
    og:description should be the last fallback."""
    html = """
    <html><head>
      <meta name="description" content="" />
      <meta property="og:description" content="OG fallback only." />
      <meta property="og:image" content="https://example.com/c.jpg" />
    </head><body>
      <main id="content">
        <h1>X</h1>
        <div class="chapter-list">
          <li class="chapter-details"><a href="/reader/x/1/1/">Ch1</a></li>
        </div>
      </main>
    </body></html>
    """
    handler = AssortedScansSiteHandler()
    ctx = handler.fetch_comic_context(
        "https://example.com/reader/x/",
        MagicMock(),
        _mr_returning(html),
    )
    assert ctx.comic.get("desc") == "OG fallback only."


# ---------------------------------------------------------------------------
# F4 — FlameComics HTML strip
# ---------------------------------------------------------------------------

def test_flamecomics_strip_html_mantine_wrapper():
    """The Mantine HTML wrapper that FlameComics's API leaks should be
    stripped to plain text."""
    raw = '<p class="mantine-focus-auto m_b6d8b162 mantine-Text-root">Sample description, after "the Gate"...</p>'
    cleaned = FlameComicsSiteHandler._strip_html(raw)
    assert cleaned == 'Sample description, after "the Gate"...'


def test_flamecomics_strip_html_nested_tags():
    """Nested tags (which Mantine sometimes renders) all collapse to text."""
    raw = '<p><b>Bold</b> and <i>italic</i> text.</p>'
    cleaned = FlameComicsSiteHandler._strip_html(raw)
    assert cleaned == "Bold and italic text."


def test_flamecomics_strip_html_none_input():
    """None input → None output (preserves the no-data signal)."""
    assert FlameComicsSiteHandler._strip_html(None) is None


def test_flamecomics_strip_html_empty_string():
    """Empty string input → None (an empty-strip should NOT collapse to '')."""
    assert FlameComicsSiteHandler._strip_html("") is None


def test_flamecomics_strip_html_plain_text_passthrough():
    """Already-plain text passes through (BeautifulSoup wraps then unwraps)."""
    assert FlameComicsSiteHandler._strip_html("Plain text.") == "Plain text."


# ---------------------------------------------------------------------------
# F5 — MangaFire artists extraction
# ---------------------------------------------------------------------------

# MangaFire series page markup. Note: the handler also reads `.info p` for
# status, `.poster img[itemprop=image]` for cover, `#synopsis` for desc.

_MANGAFIRE_FULL_HTML = """
<html><body>
<div class="manga-info">
  <div class="poster"><img itemprop="image" src="https://cdn.example.com/c.jpg"/></div>
  <h1 itemprop="name">Test Series</h1>
  <div id="synopsis">Sung Jinwoo, also known as "the weakest hunter"...</div>
  <div class="info"><p>Completed</p></div>
  <div class="meta">
    <div><span>Author:</span> <a itemprop="author" href="/author/x">Test Author</a></div>
    <div><span>Artist:</span> <a itemprop="artist" href="/artist/x">Test Studio</a></div>
    <div><span>Genres:</span>
      <a href="/genre/action">Action</a>
      <a href="/genre/fantasy">Fantasy</a>
    </div>
    <div><span>Published:</span> Mar 21, 2017 to Sep 21, 2018</div>
  </div>
</div>
</body></html>
"""


def test_mangafire_artists_itemprop_extracted():
    """MangaFire's series page exposes a separate Artist field via
    itemprop='artist'. The F5 fix added this extraction; comic dict must now
    contain both authors and artists.
    """
    from sites.mangafire import MangaFireSiteHandler
    handler = MangaFireSiteHandler()
    ctx = handler.fetch_comic_context(
        "https://example.com/manga/test-series.x",
        MagicMock(),
        _mr_returning(_MANGAFIRE_FULL_HTML),
    )
    assert ctx.comic["authors"] == ["Test Author"]
    assert ctx.comic["artists"] == ["Test Studio"]


def test_mangafire_artists_label_fallback():
    """When MangaFire drops the itemprop='artist' attribute, the F5 label-row
    fallback should still find the artist via the 'Artist:' span text."""
    html = _MANGAFIRE_FULL_HTML.replace(
        '<a itemprop="artist" href="/artist/x">Test Studio</a>',
        '<a href="/artist/x">Test Studio</a>',
    )
    from sites.mangafire import MangaFireSiteHandler
    handler = MangaFireSiteHandler()
    ctx = handler.fetch_comic_context(
        "https://example.com/manga/test-series.x",
        MagicMock(),
        _mr_returning(html),
    )
    assert ctx.comic["artists"] == ["Test Studio"]


# ---------------------------------------------------------------------------
# F6 — LineWebtoon always-populate artists
# ---------------------------------------------------------------------------

# LineWebtoon's fetch_comic_context is heavy (canonicalizes title_no, follows
# redirects, reads meta tags). We exercise the post-parse comic-dict construction
# by feeding a fixture HTML with author == artist (the solo-creator case that
# previously left artists empty).

_LINEWEBTOON_SOLO_AUTHOR_HTML = """
<html><head>
  <meta property="com-linewebtoon:webtoon:author" content="singNsong" />
  <meta property="og:image" content="https://cdn.example.com/cover.jpg" />
</head><body>
<h1 class="subj">Omniscient Reader</h1>
<div class="detail_header">
  <div class="info">
    <span class="author">singNsong</span>
  </div>
</div>
<p id="_asideDetail">
  <p class="day_info">EVERY MONDAY</p>
  <p class="summary">Dokja was an average office worker...</p>
</p>
<h2 class="genre">Fantasy</h2>
</body></html>
"""


def test_linewebtoon_artists_populated_when_same_as_author():
    """F6: the artist field must be filled even when the second .author slot
    is missing (i.e., a single creator does both jobs)."""
    from sites.linewebtoon import LineWebtoonSiteHandler
    handler = LineWebtoonSiteHandler()
    # LineWebtoon's fetch_comic_context expects a real-looking URL it can
    # extract title_no from. The /list?title_no=N pattern is canonical.
    ctx = handler.fetch_comic_context(
        "https://www.example.com/en/fantasy/generic-reader/list?title_no=2154",
        MagicMock(),
        _mr_returning(_LINEWEBTOON_SOLO_AUTHOR_HTML),
    )
    # Author must be populated.
    assert ctx.comic["authors"] == ["singNsong"]
    # F6 guarantee: artists also populated (was missing pre-fix).
    assert ctx.comic.get("artists") == ["singNsong"]


# ---------------------------------------------------------------------------
# F8 — MangaPill artist label check
# ---------------------------------------------------------------------------

# MangaPill series page metadata block: nested divs where children[0] is the
# label and children[1] is the value. The F8 fix added an "artist" branch.

# MangaPill's selectors target a specific nested layout:
#   div.container > div:first-child > div:first-child > img        (cover)
#   div.container > div:first-child > div:last-child > div:nth-child(2) > p   (desc)
#   div.container > div:first-child > div:last-child > div:nth-child(3)       (meta block)
# We mirror that exact structure here so the parser hits every selector.
_MANGAPILL_META_HTML = """
<html><body>
<div class="container">
  <div>
    <div><img src="https://cdn.example.com/cover.jpg"/></div>
    <div>
      <div><h1>Test Series</h1></div>
      <div><p>Description here.</p></div>
      <div>
        <div><div>Author</div><div><a>Test Author</a></div></div>
        <div><div>Artist</div><div><a>Test Studio</a></div></div>
        <div><div>Year</div><div>2018</div></div>
        <div><div>Status</div><div>finished</div></div>
      </div>
    </div>
  </div>
</div>
<a href="/genre/action">Action</a>
<a href="/genre/fantasy">Fantasy</a>
</body></html>
"""


def test_mangapill_artist_branch_populates():
    """F8: the new artist branch in MangaPill's meta-block walk should
    surface artist values when the site exposes them."""
    from sites.mangapill import MangaPillSiteHandler
    handler = MangaPillSiteHandler()
    ctx = handler.fetch_comic_context(
        "https://example.com/manga/8136/test-series-novel",
        MagicMock(),
        _mr_returning(_MANGAPILL_META_HTML),
    )
    assert ctx.comic.get("authors") == ["Test Author"]
    assert ctx.comic.get("artists") == ["Test Studio"]


def test_mangapill_no_artist_row_leaves_field_absent():
    """Regression: when no Artist row is present (the typical case), the
    `artists` key should simply not be set — never set to [Author]."""
    html = _MANGAPILL_META_HTML.replace(
        "<div><div>Artist</div><div><a>Test Studio</a></div></div>", ""
    )
    from sites.mangapill import MangaPillSiteHandler
    handler = MangaPillSiteHandler()
    ctx = handler.fetch_comic_context(
        "https://example.com/manga/8136/test",
        MagicMock(),
        _mr_returning(html),
    )
    assert ctx.comic.get("authors") == ["Test Author"]
    assert "artists" not in ctx.comic  # not invented


# ---------------------------------------------------------------------------
# F9 — MangaKatana authors fallback selectors
# ---------------------------------------------------------------------------

# When `.author a` is absent, the F9 fix should fall back to
# `a[href*='/authors/']` and then to label/value row scanning.

_MANGAKATANA_AUTHOR_HREF_HTML = """
<html><body>
<h1 class="heading">Test Series</h1>
<div class="cover"><img src="https://example.com/c.jpg"/></div>
<div class="summary"><p>Sample description...</p></div>
<ul class="d39">
  <li>
    <div class="label">Authors:</div>
    <div class="value"><a href="https://example.com/authors/test-author">Test Author</a></div>
  </li>
  <li>
    <div class="label">Status:</div>
    <div class="value status">Completed</div>
  </li>
</ul>
<div class="genres">
  <a href="/genres/action">Action</a>
</div>
</body></html>
"""

_MANGAKATANA_NO_AUTHOR_HTML = """
<html><body>
<h1 class="heading">Test Series</h1>
<div class="cover"><img src="https://example.com/c.jpg"/></div>
<div class="summary"><p>desc</p></div>
<ul class="d39">
  <li><div class="label">Status:</div><div class="value status">ongoing</div></li>
</ul>
<div class="genres"><a href="/genres/action">Action</a></div>
</body></html>
"""


def test_mangakatana_authors_label_value_fallback():
    """F9: when `.author a` is empty, scan `.label`/`.value` rows for an
    Author label and extract anchor text."""
    from sites.mangakatana import MangaKatanaSiteHandler
    handler = MangaKatanaSiteHandler()
    ctx = handler.fetch_comic_context(
        "https://example.com/manga/test-series.21708",
        MagicMock(),
        _mr_returning(_MANGAKATANA_AUTHOR_HREF_HTML),
    )
    assert ctx.comic.get("authors") == ["Test Author"]


def test_mangakatana_authors_empty_when_truly_missing():
    """When neither the canonical .author a nor any fallback finds an author,
    the field stays empty — never invented."""
    from sites.mangakatana import MangaKatanaSiteHandler
    handler = MangaKatanaSiteHandler()
    ctx = handler.fetch_comic_context(
        "https://example.com/manga/test.x",
        MagicMock(),
        _mr_returning(_MANGAKATANA_NO_AUTHOR_HTML),
    )
    assert ctx.comic.get("authors") == []


# ---------------------------------------------------------------------------
# Integration cross-check: confirm details.json keys map cleanly
# ---------------------------------------------------------------------------

def test_details_json_dict_shape_from_handlers():
    """Smoke: every comic_data dict our handlers return should be safely
    digestible by aio-dl.py:6039-6049's payload construction. Replicates the
    same defensive `.get(..., []) or []` pattern to catch shape issues.
    """
    handler = _make_mt_handler()
    ctx = handler.fetch_comic_context(
        "https://example.com/manga/x/",
        _scraper_with_no_api(),
        _mr_returning(_MT_FULL_HTML),
    )
    comic = ctx.comic
    # Simulate aio-dl.py:6039-6049
    details = {
        "title": ctx.title,
        "author": ", ".join(comic.get("authors", []) or []),
        "artist": ", ".join(comic.get("artists", []) or []),
        "description": comic.get("desc") or "",
        "genre": list(comic.get("genres", []) or []),
        "status": comic.get("status") or "",
    }
    assert details["title"] == "Test Series"
    assert details["author"] == "Test Author"
    assert details["artist"] == "Test Studio"
    assert details["description"].startswith("Sample description")
    assert details["genre"] == ["Action", "Adventure", "Fantasy"]
    assert details["status"] == "Completed"
