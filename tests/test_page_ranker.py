"""sites/page_ranker.py: the matched-page comparator's invariants, on synthetic pages.

The fitted numbers themselves are checked against the bench harness
(bench/image-ranker/parity_port.py, gitignored); these tests pin the properties the
ranking relies on, so a refactor that breaks one fails here without the corpus:
antisymmetry, identical pixels tie whatever the container, a softened copy loses, an
enlarged copy gains nothing, page matching separates pages, the edition majority rule,
and the latent fit.
"""
import io
import math
import random

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from sites import page_ranker as pr


def _page(seed=0, w=900, h=1300):
    """A deterministic manga-like page: panel borders, line strokes, a screentone patch,
    solid blacks, text-like glyph rows."""
    rng = random.Random(seed)
    im = Image.new("L", (w, h), 245)
    d = ImageDraw.Draw(im)
    for _ in range(6):
        x0, y0 = rng.randrange(20, w - 300), rng.randrange(20, h - 400)
        d.rectangle((x0, y0, x0 + rng.randrange(200, 280), y0 + rng.randrange(250, 380)), outline=10, width=4)
    for _ in range(220):
        x, y = rng.randrange(w), rng.randrange(h)
        d.line((x, y, x + rng.randrange(-90, 90), y + rng.randrange(-90, 90)), fill=rng.randrange(0, 60), width=rng.choice((1, 2, 3)))
    tx, ty = rng.randrange(40, w - 340), rng.randrange(40, h - 340)
    for yy in range(ty, ty + 300, 6):
        for xx in range(tx + (yy // 6) % 2 * 3, tx + 300, 6):
            d.ellipse((xx, yy, xx + 2, yy + 2), fill=40)
    bx, by = rng.randrange(20, w - 200), rng.randrange(20, h - 180)
    d.rectangle((bx, by, bx + 160, by + 140), fill=8)
    gx, gy = rng.randrange(20, w - 220), rng.randrange(20, h - 200)
    for row in range(8):
        for col in range(14):
            if rng.random() < 0.8:
                x, y = gx + col * 14, gy + row * 20
                d.rectangle((x, y, x + 8, y + 12), outline=20, width=1)
    return im


def _enc(im, fmt="PNG", **kw):
    b = io.BytesIO()
    im.save(b, fmt, **kw)
    return b.getvalue()


@pytest.fixture(scope="module")
def base():
    return _page(1)


def test_identical_pixels_tie_whatever_the_container(base):
    a = pr.prepare(_enc(base, "PNG"))
    b = pr.prepare(_enc(base, "WEBP", lossless=True))
    assert a.sha1 != b.sha1 and a.pixhash == b.pixhash
    assert abs(pr.score(pr.measure(a, b))) < 1e-9


def test_score_is_antisymmetric(base):
    soft = base.filter(ImageFilter.GaussianBlur(1.2))
    a, b = pr.prepare(_enc(base)), pr.prepare(_enc(soft, "JPEG", quality=90))
    assert pr.score(pr.measure(a, b)) == pytest.approx(-pr.score(pr.measure(b, a)), abs=1e-12)


def test_softened_copy_loses_beyond_a_tie(base):
    soft = base.filter(ImageFilter.GaussianBlur(1.6))
    a, b = pr.prepare(_enc(base)), pr.prepare(_enc(soft))
    s = pr.score(pr.measure(a, b))
    assert s > pr.TIE


def test_enlarged_copy_gains_nothing(base):
    small = base.resize((600, int(1300 * 600 / 900)), Image.LANCZOS)
    big = small.resize((900, 1300), Image.LANCZOS)
    a, b = pr.prepare(_enc(big)), pr.prepare(_enc(small))
    m = pr.measure(a, b)
    assert m["enl_gain"] > -0.5          # the test sees no real extra resolution
    assert pr.score_parts(m)["res"] == pytest.approx(0.0, abs=0.05)
    assert abs(pr.score(m)) < pr.TIE


def test_real_extra_resolution_is_credited(base):
    small = base.resize((600, int(1300 * 600 / 900)), Image.LANCZOS)
    a, b = pr.prepare(_enc(base)), pr.prepare(_enc(small))
    m = pr.measure(a, b)
    assert m["enl_gain"] < -0.5
    assert pr.score(m) > pr.TIE


def test_score_parts_sum_to_score(base):
    soft = base.filter(ImageFilter.GaussianBlur(0.8)).resize((700, 1011), Image.LANCZOS)
    m = pr.measure(pr.prepare(_enc(base)), pr.prepare(_enc(soft)))
    assert sum(pr.score_parts(m).values()) == pytest.approx(pr.score(m), abs=1e-12)


def test_ncc_separates_same_page_from_other_pages(base):
    same = pr.prepare(_enc(base.resize((700, 1011), Image.LANCZOS), "JPEG", quality=70))
    other = pr.prepare(_enc(_page(2)))
    a = pr.prepare(_enc(base))
    assert pr.ncc(a, same) > 0.9
    assert pr.ncc(a, other) < 0.5


def test_unrelated_pages_do_not_register(base):
    a, b = pr.prepare(_enc(base)), pr.prepare(_enc(_page(3)))
    st = {}
    try:
        pr.align(a.gray, b.gray, a.width, stats=st)
    except ValueError:
        return
    assert not (st.get("reg_n", 0) >= 8 and st.get("reg_rms", 99) < 1.0)


def test_chroma_and_edition_majority():
    gray = _page(4).convert("RGB")
    col = Image.merge("RGB", (gray.getchannel(0), gray.getchannel(1).point(lambda v: v * 0.6), gray.getchannel(2)))
    assert pr.prepare(_enc(gray)).chroma < pr.COLOR_T <= pr.prepare(_enc(col)).chroma
    pages = [{"a": 0.0, "b": 0.01, "c": 0.4}, {"a": 0.0, "b": 0.0, "c": 0.5}, {"a": 0.0, "c": 0.3}]
    assert pr.classify_editions(pages) == {"a": "original", "b": "original", "c": "alternate"}
    # all agree -> nothing flagged; a lone pair never votes
    assert set(pr.classify_editions([{"a": 0.6, "b": 0.7, "c": 0.5}]).values()) == {"original"}
    assert pr.classify_editions([{"a": 0.0, "b": 0.6}]) == {}


def test_fit_latent_recovers_consistent_differences_and_shrinks_thin_evidence():
    q = {"x": 0.3, "y": 0.0, "z": -0.2}
    edges = [(i, j, q[i] - q[j], 50.0) for i, j in (("x", "y"), ("y", "z"), ("x", "z"))]
    fit = pr.fit_latent(edges)
    for i in q:
        for j in q:
            assert fit[i] - fit[j] == pytest.approx(q[i] - q[j], abs=0.01)
    thin = pr.fit_latent([("x", "y", 0.4, 1.0)])
    assert 0 < thin["x"] - thin["y"] < 0.4
    # separate components are fitted separately, each mean zero
    two = pr.fit_latent([("a", "b", 0.2, 5.0), ("c", "d", -0.2, 5.0)])
    assert two["a"] + two["b"] == pytest.approx(0.0, abs=1e-12)
    assert two["c"] < two["d"]


def test_rating_map_bands():
    assert pr.rating_from_deficit(0.0) == pytest.approx(0.95)
    assert pr.rating_from_deficit(pr.TIE) >= 0.7
    assert 0.45 < pr.rating_from_deficit(math.log(1.5)) < 0.55
    assert pr.rating_from_deficit(math.log(2.5)) < 0.4
    assert pr.rating_from_deficit(-1.0) == pytest.approx(0.95)


def test_decode_budget_bounds_pixels_in_flight_and_keeps_arrival_order():
    """Decodes hold at most the budget at once (one page larger than it runs alone),
    and a large page queued behind small ones is not overtaken by later small ones."""
    import threading
    import time
    b = pr._PixelBudget(10)
    lock = threading.Lock()
    state = {"in": 0, "max": 0}
    order = []

    def job(name, px, hold):
        with b.take(px):
            with lock:
                state["in"] += min(px, 10)
                state["max"] = max(state["max"], state["in"])
                order.append(name)
            time.sleep(hold)
            with lock:
                state["in"] -= min(px, 10)

    ts = [threading.Thread(target=job, args=("s1", 4, 0.15))]
    ts[0].start()
    time.sleep(0.03)
    ts.append(threading.Thread(target=job, args=("big", 50, 0.05)))   # over the cap: alone
    ts[-1].start()
    time.sleep(0.03)
    ts.append(threading.Thread(target=job, args=("s2", 4, 0.01)))     # fits now, but waits its turn
    ts[-1].start()
    for t in ts:
        t.join()
    assert state["max"] <= 10
    assert order == ["s1", "big", "s2"]


@pytest.mark.parametrize("fmt", ["WEBP", "AVIF"])
def test_webp_and_avif_pages_decode(base, fmt):
    """Hosts serve WebP and AVIF pages; a format PIL cannot open would read as a
    failed fetch for every page. AVIF comes from Pillow >= 11.2 or the guarded
    pillow_avif import at the top of page_ranker (the only AVIF decode check
    since the old scorer's format tests went)."""
    from PIL import features
    if fmt == "AVIF" and not features.check("avif"):
        pytest.skip("this Pillow build has no AVIF codec")
    c = pr.prepare(_enc(base, fmt, quality=90))
    assert (c.native_w, c.native_h) == base.size
