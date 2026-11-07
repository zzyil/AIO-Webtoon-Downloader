"""sites/chapter_fingerprint.py: the content proof behind multi-source rescue.

Synthetic strips stand in for chapters: seeded blurred noise, sliced at a
different page height per "site", encoded JPEG/PNG/WebP, optionally side-cropped
(a 3% content-scale difference between scans). Each test pins one property the
rescue relies on:

  - the same chapter matches both ways across slicing, format and crop;
  - a different chapter, a recap built from two chapters, and blank pages do not;
  - own pages with gaps prove the right candidate and reject the next chapter and
    a candidate with a whole earlier chapter merged in front;
  - the cheap first-pages check rules out wrong candidates, and says "cannot
    tell" (None) rather than "no" when the own pages could not have held them;
  - storage round-trips and a re-sliced retry resets the partial record.

P0 evidence for the thresholds (real chapters): bench/ms-offset/ (gitignored).
End-to-end coverage through main(): tests/test_multisource_rescue.py.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image, ImageFilter

from sites import chapter_fingerprint as cf

W = 400
H = 12000            # 30 content widths


def _strip(seed: int, h: int = H) -> Image.Image:
    rng = np.random.default_rng(seed)
    small = rng.integers(0, 256, (h // 40, W // 40), dtype=np.uint8)
    return Image.fromarray(small).resize((W, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(3))


def _slice(strip: Image.Image, page_h: int, fmt: str = "JPEG", crop: int = 0) -> list:
    out = []
    w, h = strip.size
    for y in range(0, h, page_h):
        piece = strip.crop((crop, y, w - crop, min(h, y + page_h))).convert("RGB")
        buf = io.BytesIO()
        piece.save(buf, fmt, quality=88)
        out.append(buf.getvalue())
    return out


def _cp(blobs, keep=None) -> cf.ChapterPages:
    cp = cf.ChapterPages(pages_total=len(blobs))
    for i, b in enumerate(blobs, 1):
        if keep is None or i in keep:
            cp.pages[i] = cf.page_small(b)
    return cp


@pytest.fixture(scope="module")
def chapters():
    return {s: _strip(s) for s in (1, 2, 3)}


def test_same_chapter_matches_across_slicing_format_and_crop(chapters):
    a = _cp(_slice(chapters[1], 1280, "PNG")).fingerprint()
    b = _cp(_slice(chapters[1], 2900, "JPEG", crop=6)).fingerprint()
    c = _cp(_slice(chapters[1], 16000, "WEBP")).fingerprint()
    for x, y in ((a, b), (a, c), (b, c)):
        ok, ab, ba = cf.match(x, y)
        assert ok, (ab, ba)


def test_different_chapter_does_not_match(chapters):
    a = _cp(_slice(chapters[1], 1280)).fingerprint()
    b = _cp(_slice(chapters[2], 2900)).fingerprint()
    ok, ab, ba = cf.match(a, b)
    assert not ok
    assert ab.cov_a < 0.2 and ba.cov_a < 0.2


def test_recap_quoting_two_chapters_matches_neither(chapters):
    recap = Image.new("L", (W, H))
    recap.paste(chapters[1].crop((0, 0, W, H // 3)), (0, 0))
    recap.paste(chapters[2].crop((0, H // 2, W, H // 2 + H // 3)), (0, H // 3))
    recap.paste(_strip(9, H - 2 * (H // 3)), (0, 2 * (H // 3)))
    r = _cp(_slice(recap, 1280)).fingerprint()
    for s in (1, 2):
        ok, _, _ = cf.match(r, _cp(_slice(chapters[s], 1280)).fingerprint())
        assert not ok


def test_blank_pages_are_no_evidence():
    blank = Image.new("L", (W, H), 255)
    a = _cp(_slice(blank, 1280)).fingerprint()
    assert not a.informative
    ok, ab, _ = cf.match(a, a)
    assert not ok and ab.cov_a == 0.0
    assert not cf.prove_partial(_cp(_slice(blank, 1280)), a).ok


def test_own_pages_with_a_gap_prove_the_right_candidate(chapters):
    own = _cp(_slice(chapters[1], 1280, "PNG"), keep=set(range(1, 11)) - {4, 5})
    right = _cp(_slice(chapters[1], 2900, "JPEG", crop=6)).fingerprint()
    proof = cf.prove_partial(own, right)
    assert proof.ok, proof.why
    assert len(proof.runs) == 2


def test_own_pages_reject_the_next_chapter(chapters):
    own = _cp(_slice(chapters[1], 1280), keep=set(range(1, 9)))
    assert not cf.prove_partial(own, _cp(_slice(chapters[2], 2900)).fingerprint()).ok


def test_own_pages_reject_a_candidate_with_an_earlier_chapter_in_front(chapters):
    merged = Image.new("L", (W, 2 * H))
    merged.paste(chapters[3], (0, 0))
    merged.paste(chapters[1], (0, H))
    own = _cp(_slice(chapters[1], 1280), keep=set(range(1, 9)))
    proof = cf.prove_partial(own, _cp(_slice(merged, 2900)).fingerprint())
    assert not proof.ok
    assert "widths into the candidate" in proof.why


def test_too_few_own_pages_prove_nothing(chapters):
    own = _cp(_slice(chapters[1], 1280), keep={1})
    proof = cf.prove_partial(own, _cp(_slice(chapters[1], 2900)).fingerprint())
    assert not proof.ok and "too few" in proof.why


def test_head_check_rules_out_wrong_candidates_and_abstains_when_it_cannot_tell(chapters):
    own = _cp(_slice(chapters[1], 1280), keep=set(range(1, 9)) | {10})
    right_head = _cp(_slice(chapters[1], 1000)[: cf.HEAD_PAGES])
    wrong_head = _cp(_slice(chapters[2], 1000)[: cf.HEAD_PAGES])
    assert cf.head_matches(own, right_head) is True
    assert cf.head_matches(own, wrong_head) is False
    # Page 1 missing: no opening to compare against.
    assert cf.head_matches(_cp(_slice(chapters[1], 1280), keep=set(range(2, 9))), wrong_head) is None
    # Opening run too short to have held the candidate's head.
    assert cf.head_matches(_cp(_slice(chapters[1], 1280), keep={1, 2, 5, 6}), wrong_head) is None


def _manga_pages(strip: Image.Image, width: int = W) -> list:
    """Book pages cut from a strip: page-shaped (h/w 1.5), the content inset with
    a white binding gutter that alternates sides, as in a scanned tankobon.
    `width` re-scales the whole page (another site's copy of the same scan)."""
    out = []
    cw, ch, gut = W - 40, 540, 40
    for k, y in enumerate(range(0, strip.size[1] - ch + 1, ch)):
        page = Image.new("L", (W, 600), 255)
        page.paste(strip.crop((0, y, W, y + ch)).resize((cw, ch)), (gut if k % 2 == 0 else 0, 30))
        if width != W:
            page = page.resize((width, round(600 * width / W)))
        buf = io.BytesIO()
        page.convert("RGB").save(buf, "JPEG", quality=88)
        out.append(buf.getvalue())
    return out


def test_manga_pages_with_alternating_gutters_prove_with_a_page_missing(chapters):
    # HxH Ch 72 (2026-11-01): the primary lost page 3, so its pages 4-20 were
    # compared against the alt's pages 1-20 of the same scan. The old median
    # frame sat between the two gutter sides and moved with the page set, so
    # nothing matched; the outer content extent is the same for both page sets.
    own = _cp(_manga_pages(chapters[1]), keep=set(range(1, 21)) - {3})
    same = _cp(_manga_pages(chapters[1], width=W + 1)).fingerprint()
    other = _cp(_manga_pages(chapters[2], width=W + 1)).fingerprint()
    proof = cf.prove_partial(own, same)
    assert proof.ok, proof.why
    assert not cf.prove_partial(own, other).ok


def test_head_check_knows_up_front_when_it_cannot_judge(chapters):
    pages = _slice(chapters[1], 1280)
    assert cf.head_can_judge(_cp(pages, keep=set(range(1, 9))))
    assert not cf.head_can_judge(_cp(pages, keep=set(range(2, 9))))      # no page 1
    assert not cf.head_can_judge(_cp(pages, keep={1, 2} | set(range(4, 9))))  # opening too short


def test_pages_round_trip_and_a_resliced_retry_resets(tmp_path, chapters):
    cp = _cp(_slice(chapters[1], 1280), keep={1, 2, 3})
    cp.meta = {"site": "s", "chap": "12.5"}
    path = cf.fp_path(str(tmp_path), "s", "12.5")
    assert path.endswith(".aio_fp\\s\\12.5.npz") or path.endswith(".aio_fp/s/12.5.npz")
    cf.save_pages(path, cp)
    back = cf.load_pages(path)
    assert back.pages_total == cp.pages_total and sorted(back.pages) == [1, 2, 3]
    assert back.meta == cp.meta
    assert np.array_equal(back.pages[2][0], cp.pages[2][0])
    back.merge(_cp(_slice(chapters[1], 1280), keep={4}))
    assert sorted(back.pages) == [1, 2, 3, 4]
    back.merge(_cp(_slice(chapters[1], 2000), keep={1}))   # different page count
    assert sorted(back.pages) == [1] and back.pages_total == 6
    assert cf.load_pages(str(tmp_path / "missing.npz")) is None
