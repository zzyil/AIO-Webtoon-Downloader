"""A failed chapter is retried on the PRIMARY first, keeping the pages it already
downloaded; only then do other sites get a look (plan
sync-temp/plans/multisource-offset.md D1/D2, user decisions 2026-10-25).

WHAT BROKE. A failed attempt wiped its temp dir, so every retry re-downloaded the
whole chapter, and the rescue ran BEFORE the long retries: a 120-page LINE
chapter that lost 2 pages to a CDN stall was replaced by a 103-page re-host, and
by-number rescues filled 21 of 23 unOrdinary chapters with the wrong chapter.

Two layers:
  - `_resumable_pages` (module level in aio-dl.py): which kept pages a retry may
    reuse. Pure function, unit-tested here.
  - the strict wrapper's order, driven through the real main() with the offline
    fake sites of tests/test_multisource_rescue.py (fixtures `aio`, `env`).

Cross-file: aio-dl.py — `_process_chapter_strict` (retry order, _OUTAGE_BREAKER),
`_process_chapter_impl(resume_pages=)`, `_PAGES_MANIFEST`, `_handler_incomplete`;
sites/base.py — IncompleteChapterError.pages; sites/chapter_fingerprint.py —
pages_from_blobs.
"""

from __future__ import annotations

import os

import pytest

from tests.test_multisource_rescue import (  # noqa: F401  (fixtures)
    A_SLICE, ALT_URL, P_SLICE, PRIMARY_URL, _cbz_for, _pages, _pages_of, _run,
    _sites, aio, env,
)


# ── _resumable_pages ───────────────────────────────────────────────────────

def _tasks(urls):
    return [(i, u, "dir", f"5_{i:04d}.jpg") for i, u in enumerate(urls, 1)]


def _manifest(urls, site="s", count=None):
    return {"site": site, "count": len(urls) if count is None else count,
            "urls": {str(i): u for i, u in enumerate(urls, 1)}}


FILES = ["5_0001.jpg", "5_0002.webp", "5_0003.png", ".pending_5_0004", ".pages.json"]


def test_unchanged_list_reuses_every_finished_page(aio):
    urls = [f"https://cdn/x/{i}.jpg" for i in range(1, 5)]
    got = aio._resumable_pages(_manifest(urls), "s", 4, _tasks(urls), FILES, "5")
    # Page 4 was still a `.pending_*` file: not finished, not reused.
    assert got == {1: "5_0001.jpg", 2: "5_0002.webp", 3: "5_0003.png"}


def test_another_site_or_another_page_count_reuses_nothing(aio):
    # An alt rescue writes into the same ch_{n} dir under the primary's number.
    urls = [f"https://cdn/x/{i}.jpg" for i in range(1, 5)]
    assert aio._resumable_pages(_manifest(urls, site="alt"), "s", 4, _tasks(urls), FILES, "5") == {}
    assert aio._resumable_pages(_manifest(urls, count=5), "s", 4, _tasks(urls), FILES, "5") == {}


def test_rotated_query_tokens_match_by_path_when_paths_are_distinct(aio):
    old = [f"https://cdn/x/{i}.jpg?tok=a" for i in range(1, 5)]
    new = [f"https://cdn/x/{i}.jpg?tok=b" for i in range(1, 5)]
    got = aio._resumable_pages(_manifest(old), "s", 4, _tasks(new), FILES, "5")
    assert sorted(got) == [1, 2, 3]


def test_a_proxy_list_with_one_path_matches_only_by_full_url(aio):
    # `/?url=...`: every page shares the path "/", so a path match would pair
    # any old page with any new one.
    old = [f"https://proxy/?url=https://cdn/{i}.jpg&t=a" for i in range(1, 5)]
    new = [f"https://proxy/?url=https://cdn/{i}.jpg&t=b" for i in range(1, 5)]
    assert aio._resumable_pages(_manifest(old), "s", 4, _tasks(new), FILES, "5") == {}
    assert sorted(aio._resumable_pages(_manifest(old), "s", 4, _tasks(old), FILES, "5")) == [1, 2, 3]


def test_a_reordered_list_reuses_only_the_indices_that_still_match(aio):
    old = [f"https://cdn/x/{i}.jpg" for i in range(1, 5)]
    new = [old[1], old[0], old[2], old[3]]       # pages 1 and 2 swapped
    got = aio._resumable_pages(_manifest(old), "s", 4, _tasks(new), FILES, "5")
    assert got == {3: "5_0003.png"}


def test_data_uris_keep_nothing(aio):
    urls = ["data:image/png;base64,AAAA"] + [f"https://cdn/x/{i}.jpg" for i in range(2, 5)]
    got = aio._resumable_pages(_manifest(urls), "s", 4, _tasks(urls), FILES, "5")
    assert 1 not in got and sorted(got) == [2, 3]


def test_page_names_of_another_chapter_never_match(aio):
    # "15_0001.jpg" must not be read as chapter 5's page 1.
    urls = [f"https://cdn/x/{i}.jpg" for i in range(1, 3)]
    got = aio._resumable_pages(_manifest(urls), "s", 2, _tasks(urls), ["15_0001.jpg"], "5")
    assert got == {}


# ── the order, through main() ──────────────────────────────────────────────

def _flaky(env, fails):
    """Make primary pages fail a set number of times: {(label, page): n}."""
    primary = env["sites"][PRIMARY_URL]
    real = primary.page_bytes

    def page_bytes(label, i):
        if fails.get((label, i), 0) > 0:
            fails[(label, i)] -= 1
            return None
        return real(label, i)

    primary.page_bytes = page_bytes
    return primary


@pytest.fixture
def downloads(aio, env, monkeypatch):
    """Every image URL the run fetched, in order."""
    calls = []
    inner = aio.dl_image

    def counting(url, folder, name, scraper, cleanup=True):
        calls.append(url)
        return inner(url, folder, name, scraper, cleanup)

    monkeypatch.setattr(aio, "dl_image", counting)
    monkeypatch.setattr(aio, "_LIST_RETRY_BACKOFF", 0.0)
    return calls


FAST = ("--inline-chapter-backoff", "0")


def test_a_retry_fetches_only_the_pages_the_last_attempt_missed(aio, env, monkeypatch, downloads, capsys):
    _sites(env)
    _flaky(env, {("5", 8): 1})
    _run(aio, monkeypatch, extra=FAST)
    ch5 = [u for u in downloads if "primary.test/img/5/" in u]
    # 10 pages once, then page 8 again: nothing else re-downloaded.
    assert sorted(ch5) == sorted([f"https://primary.test/img/5/{i}" for i in range(1, 11)]
                                 + ["https://primary.test/img/5/8"])
    assert "kept 9/10 page(s) from the previous attempt, fetching 1" in capsys.readouterr().out
    pages, ci = _pages_of(_cbz_for(env["out"], 5))
    assert pages == _pages(5, P_SLICE, "PNG")       # the primary's own pages
    assert "<AioRescue" not in ci


def test_primary_retries_run_before_any_other_site(aio, env, monkeypatch, downloads):
    # Fails the first attempt AND the near-miss retry; the first long retry
    # gets it. No alternative is consulted on the way.
    _sites(env)
    _flaky(env, {("5", 8): 2})
    _run(aio, monkeypatch, extra=(*FAST, "--inline-chapter-retries", "1"))
    alt = env["sites"][ALT_URL]
    assert alt.image_calls == []
    pages, _ci = _pages_of(_cbz_for(env["out"], 5))
    assert pages == _pages(5, P_SLICE, "PNG")


def test_a_failed_page_list_gets_one_short_retry(aio, env, monkeypatch, downloads):
    _sites(env)
    primary = env["sites"][PRIMARY_URL]
    real = primary.get_chapter_images
    failures = {"5": 1}

    def images(ch, s, mr):
        if failures.get(ch["chap"], 0) > 0:
            failures[ch["chap"]] -= 1
            raise RuntimeError("list endpoint 500")
        return real(ch, s, mr)

    primary.get_chapter_images = images
    _run(aio, monkeypatch, extra=FAST)
    assert primary.image_calls.count("5") == 1      # the call that got through
    pages, _ci = _pages_of(_cbz_for(env["out"], 5))
    assert pages == _pages(5, P_SLICE, "PNG")
    assert env["sites"][ALT_URL].image_calls == []


def test_a_page_list_that_fails_twice_skips_the_long_ladder(aio, env, monkeypatch, downloads, capsys):
    _sites(env)
    primary = env["sites"][PRIMARY_URL]
    real = primary.get_chapter_images

    def images(ch, s, mr):
        if ch["chap"] == "5":
            raise RuntimeError("list endpoint 500")
        return real(ch, s, mr)

    primary.get_chapter_images = images
    _run(aio, monkeypatch, extra=(*FAST, "--inline-chapter-retries", "2"))
    out = capsys.readouterr().out
    ch5 = [ln for ln in out.splitlines() if ln.strip().startswith("[!] Chapter 5 ")]
    assert sum("page list failed" in ln for ln in ch5) == 1
    assert not any("long retry" in ln for ln in ch5), ch5


def test_handler_pages_from_an_incomplete_error_prove_the_rescue(aio, env, monkeypatch, downloads):
    # A handler that downloads in-process (MangaDex MD@H shape) gives up with 7
    # of 10 pages: those bytes are the own-pages proof.
    from sites.base import IncompleteChapterError

    _sites(env)
    primary = env["sites"][PRIMARY_URL]
    real = primary.get_chapter_images
    story5 = _pages(5, P_SLICE, "PNG")

    def images(ch, s, mr):
        if ch["chap"] == "5":
            raise IncompleteChapterError(
                pages_ok=7, pages_total=10, host="primary.test", reason="incomplete",
                pages={i: story5[i - 1] for i in range(1, 8)},
            )
        return real(ch, s, mr)

    primary.get_chapter_images = images
    _run(aio, monkeypatch, extra=FAST)
    pages, ci = _pages_of(_cbz_for(env["out"], 5))
    assert pages == _pages(5, A_SLICE, "JPEG")       # alt "3" holds story 5
    assert 'tier="own-pages"' in ci


def test_unservable_chapters_that_are_not_in_a_row_do_not_trip_the_breaker(aio, env, monkeypatch, downloads, capsys):
    # 4 and 5 fail for good, 6 downloads, 7 fails: never three in a row. Each is
    # placed from the alt (4's right neighbour 5 fails its probe too, so 6 is
    # the one checked), so the run reaches 8.
    _sites(env, primary_missing=[(str(c), i) for c in (4, 5, 7) for i in range(1, 11)])
    _run(aio, monkeypatch, extra=FAST)
    out = capsys.readouterr().out
    assert "in a row that" not in out
    for c in (4, 5, 7):
        pages, ci = _pages_of(_cbz_for(env["out"], c))
        assert pages == _pages(c, A_SLICE, "JPEG") and 'tier="placement"' in ci
    assert _cbz_for(env["out"], 8)
