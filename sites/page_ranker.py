"""Matched-page image-quality comparison: the pure math behind search ranking.

Owns: decoding a page once into a `PageCopy` (trimmed grayscale, thumbnail, chroma,
sha1), page matching across sources (thumbnail NCC), the pairwise comparator
(`measure` -> components, `score` -> one signed number), the colorized-edition call,
and the latent fit that turns pairwise scores into one 0..1 rating per source.
Also the webtoon inputs `strip_match` reads off each PageCopy (`small`, `edge_top`/
`edge_bot`, the kept `blob`) and the band helpers (`decode_gray`, `piece_copy`,
`textured_tiles`). A tall slice (h/w > TALL_ASPECT) keeps no full-size arrays:
`gray`/`thumb` are None and it is only ever compared through strip bands.
Read by: `sites/match_session.py` (the per-candidate coordinator that feeds it pages
and owns threads/deadlines), `sites/strip_match.py`. Depends on numpy + Pillow only: the Android build has no
scipy/OpenCV.

Why pairwise on matched pages: a single-image score cannot tell sharp detail from
codec noise or an enlarged copy from a real high-resolution scan, and rating each
site on its own random chapters compares different content. Two copies of the SAME
page, registered onto one grid and tone-matched, can be compared directly.

Provenance: the comparator is the P1 bake-off winner, ported from
`bench/image-ranker/page_compare.py` + `features.py` (gitignored harness; labeled corpus
and fit in `bench/image-ranker/`, decisions in `sync-temp/plans/image-ranker-rework.md`).
The math here must stay bit-for-bit equal to that harness, because the weights in
`WEIGHTS` were fitted on its numbers: change a primitive and the fit no longer
applies. `tests/test_page_ranker.py` pins the invariants; the bench's
`page_compare.py check` compares against the fitted cache.

Measurement rules (each was measured, see the plan's P1 outcome):
- The LARGER copy is always the registration reference, ties broken on (height,
  sha1). Registering in argument order biased sharpness ~7% and same-scan PSNR by up
  to 11 dB; this way `score(a, b) == -score(b, a)` exactly.
- No compression or noise term: the user's blind round saw grain and mid-quality
  codecs as invisible or slight, and every such term broke real-page ties.
- The enlargement test only means something within one scan family (it swings
  1-3 dB between different scans); the resolution credit is gated by it, bounded.
"""
from __future__ import annotations

import hashlib
import io
import math
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageOps

# Imported for its side effect: pillow-avif-plugin registers AVIF decode with PIL,
# so `Image.open` in the decoders below reads AVIF pages (hosts that serve AVIF
# would otherwise fail to decode and never get matched). Guarded because the
# plugin is a separate wheel that a build may lack (Pillow >= 11.2 can decode
# AVIF natively). jxlpy is the same idea for JXL, an optional dep that is not in
# requirements.txt. Both used to live in search_orchestrator's old scorer; they
# moved here with the scorer's removal so the decoder owns its own format support.
try:
    import pillow_avif  # noqa: F401
except ImportError:
    pass
try:
    import jxlpy  # noqa: F401
except ImportError:
    pass

# --------------------------------------------------------------------------- weights

# User decision 2026-10-18 (Railgun): sharpness dominates; a SMALL black term worth
# ~1.1x sharpness saturating at a 40-level black gap (the sweep-faithful 1.35x kept the
# reported MangaDex-over-atsu bug); resolution credit gated by the enlargement test at
# -0.5 dB; |score| < log(1.15) is a tie. Fitted on the bench corpus (objective 0.858
# with the user's codec-visibility verdicts). Keep equal to page_compare.W_DEFAULT.
WEIGHTS = dict(ws=1.0, gate=-0.5, wr=1.0, cap=math.log(1.1), sat=40.0)
TIE = math.log(1.15)

# fast registration (decimated global shift + capped tile grid): same P1 objective as
# the full fit, ~25% cheaper; too few structured tiles falls back to the full fit
FAST = True
_TILE = 192
_FAST_GRID = (6, 4)   # tile rows x cols in fast mode


# ------------------------------------------------------------------ pixel primitives

def _conv1(x, k, axis):
    r = len(k) // 2
    p = np.pad(x, [(r, r) if i == axis else (0, 0) for i in range(2)], mode="reflect")
    n = x.shape[axis]
    out = np.zeros_like(x)
    for i, c in enumerate(k):
        out += c * (p[i:i + n] if axis == 0 else p[:, i:i + n])
    return out


def gblur(x, s):
    r = max(1, int(math.ceil(3 * s)))
    # float32 taps: a float64 tap promotes every product to float64
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / s) ** 2)
    k = (k / k.sum()).astype(np.float32)
    return _conv1(_conv1(x, k, 0), k, 1)


def box(x, n):
    """n x n mean, reflect-padded like _conv1, by running sums (cost independent of n).
    float64 sums: float32 loses ~0.03 levels over a 2k-row column."""
    r = n // 2
    for ax in (0, 1):
        p = np.pad(x, [(r, r) if i == ax else (0, 0) for i in range(2)], mode="reflect").astype(np.float64)
        c = np.cumsum(p, axis=ax)
        c = np.concatenate([np.zeros_like(c[:1] if ax == 0 else c[:, :1]), c], axis=ax)
        m = x.shape[ax]
        x = ((c[n:n + m] - c[:m]) if ax == 0 else (c[:, n:n + m] - c[:, :m])).astype(np.float32) / n
    return x


def grad(x):
    gy, gx = np.gradient(x)
    return np.hypot(gx, gy)


def psnr(a, b):
    m = float(((a - b) ** 2).mean())
    return 10 * math.log10(255 ** 2 / max(m, 1e-6))


def hist_match(src, ref):
    """Map src's tones onto ref's distribution (rank -> ref quantile). Interpolates once
    per DISTINCT src value and scatters back: bit-identical to interpolating every
    pixel, but np.interp over ~3M unsorted queries measured 2-5 s per call against
    0.2-0.3 s this way."""
    u, inv, cnt = np.unique(src.ravel(), return_inverse=True, return_counts=True)
    s = np.repeat(u, cnt)
    r = np.sort(ref.ravel())
    q = np.interp(u, s, np.linspace(0, 1, s.size))
    return np.interp(q, np.linspace(0, 1, r.size), r).astype(np.float32)[inv].reshape(src.shape)


def trim(g):
    """Content box of a grayscale PIL image. Rows/cols count as content by their
    spread AFTER a sigma-2 blur: on raw pixels a grainy margin passes the spread test
    and the copy keeps its margins, so it is compared at the wrong scale and offset."""
    a = gblur(np.asarray(g, np.float32), 2.0)
    rows = np.where(a.std(axis=1) > 6)[0]
    cols = np.where(a.std(axis=0) > 6)[0]
    if len(rows) < 10 or len(cols) < 10:
        return g
    return g.crop((int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1))


def _to_w(g, w):
    return g if g.size[0] == w else g.resize((w, max(1, int(round(g.size[1] * w / g.size[0])))), Image.LANCZOS)


# --------------------------------------------------------------------- registration

def _shift(ref, mov):
    F = np.fft.rfft2(ref - ref.mean())
    G = np.fft.rfft2(mov - mov.mean())
    R = F * np.conj(G)
    R /= np.abs(R) + 1e-9
    r = np.fft.irfft2(R, s=ref.shape)
    y, x = np.unravel_index(np.argmax(r), r.shape)
    if y > r.shape[0] // 2:
        y -= r.shape[0]
    if x > r.shape[1] // 2:
        x -= r.shape[1]
    return int(y), int(x)


def _tile_shift(a, b):
    """Phase correlation of two equal tiles under a Hann window -> (dy, dx, peak): b's
    content sits at a's position minus (dy, dx). The integer peak is refined on a
    1/20-px grid of the inverse DFT around it, then a parabola: a parabola on the
    integer surface is biased toward whole pixels by ~0.15 px."""
    w = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1])).astype(np.float32)
    F = np.fft.fft2((a - a.mean()) * w)
    G = np.fft.fft2((b - b.mean()) * w)
    # half whitening: pure phase correlation gives a blurred copy's noise-only high
    # frequencies full weight (blur1.6 fit rms 0.55 px); |R|^0.5 measured rms 0.05 px
    R = F * np.conj(G)
    R /= np.sqrt(np.abs(R)) + 1e-9
    r = np.fft.ifft2(R).real
    r /= np.abs(r).sum() / 100   # peak as % of surface mass: comparable across tiles
    H, W = r.shape
    y, x = np.unravel_index(np.argmax(r), r.shape)
    pk = float(r[y, x])
    if y > H / 2:
        y -= H
    if x > W / 2:
        x -= W
    u, n = 20, 31
    oy = y + (np.arange(n) - n // 2) / u
    ox = x + (np.arange(n) - n // 2) / u
    Ey = np.exp(2j * np.pi * oy[:, None] * np.fft.fftfreq(H)[None, :])
    Ex = np.exp(2j * np.pi * np.fft.fftfreq(W)[:, None] * ox[None, :])
    up = (Ey @ R @ Ex).real
    i, j = np.unravel_index(np.argmax(up), up.shape)

    def par(m, c, p_):
        d = m - 2 * c + p_
        return 0.0 if abs(d) < 1e-12 else 0.5 * (m - p_) / d
    fy = par(up[i - 1, j], up[i, j], up[i + 1, j]) if 0 < i < n - 1 else 0.0
    fx = par(up[i, j - 1], up[i, j], up[i, j + 1]) if 0 < j < n - 1 else 0.0
    return oy[i] + fy / u, ox[j] + fx / u, pk


def _fit_affine(A, B, fast=False):
    """Affine map from A's pixel indices to B's (x_B = L x_A + t), both arrays the same
    nominal size: a global integer shift, sub-pixel phase correlation on a tile grid,
    then a weighted least-squares affine with outlier rejection. Affine, not one shift:
    two hosts' trims differ by a pixel or two, so after resizing to one width the
    copies differ in SCALE by ~0.1-0.3%, several pixels of drift top to bottom.
    Returns (L 2x2, t 2, stats). fast: the global shift on 4x-decimated copies (it only
    has to land each tile within T/4) and a capped grid; too few structured tiles
    reruns the full fit, because a 4-px-accurate shift must not be the answer."""
    hh = min(A.shape[0], B.shape[0])
    if fast:
        dy0, dx0 = _shift(A[:hh:4, ::4], B[:hh:4, ::4])
        dy0, dx0 = 4 * dy0, 4 * dx0
    else:
        dy0, dx0 = _shift(A[:hh], B[:hh])
    H, W = A.shape
    T = _TILE
    ny, nx = max(2, H // 220), max(2, W // 220)
    if fast:
        ny, nx = min(ny, _FAST_GRID[0]), min(nx, _FAST_GRID[1])
    pts, wts = [], []
    ys = np.linspace(T // 2 + 8, H - T // 2 - 8, ny).astype(int) if H > T + 16 else []
    xs = np.linspace(T // 2 + 8, W - T // 2 - 8, nx).astype(int) if W > T + 16 else []
    for cy in ys:
        for cx in xs:
            by, bx = cy - dy0, cx - dx0
            if by - T // 2 < 0 or bx - T // 2 < 0 or by + T // 2 > B.shape[0] or bx + T // 2 > B.shape[1]:
                continue
            ta = A[cy - T // 2:cy + T // 2, cx - T // 2:cx + T // 2]
            tb = B[by - T // 2:by + T // 2, bx - T // 2:bx + T // 2]
            if ta.std() < 8 or tb.std() < 8:
                continue
            ry, rx, pk = _tile_shift(ta, tb)
            if abs(ry) > T / 4 or abs(rx) > T / 4:
                continue
            pts.append((cx, cy, bx - rx, by - ry))
            wts.append(pk)
    L = np.eye(2)
    t = np.array([-dx0, -dy0], float)
    st = dict(reg_n=len(pts), reg_rms=float("nan"), reg_peak=float(np.median(wts)) if wts else 0.0)
    if len(pts) < 6:
        if fast:
            return _fit_affine(A, B, False)
        return L, t, st
    P = np.array(pts, float)
    wv = np.array(wts, float)
    keep = np.ones(len(P), bool)
    for _ in range(3):
        X = np.c_[P[keep, 0], P[keep, 1], np.ones(keep.sum())]
        sw = np.sqrt(wv[keep])[:, None]
        cx_, *_ = np.linalg.lstsq(X * sw, P[keep, 2] * sw[:, 0], rcond=None)
        cy_, *_ = np.linalg.lstsq(X * sw, P[keep, 3] * sw[:, 0], rcond=None)
        Xa = np.c_[P[:, 0], P[:, 1], np.ones(len(P))]
        res = np.hypot(Xa @ cx_ - P[:, 2], Xa @ cy_ - P[:, 3])
        nk = res <= max(1.0, 3 * np.median(res[keep]))
        if nk.sum() < 6 or (nk == keep).all():
            keep = nk if nk.sum() >= 6 else keep
            break
        keep = nk
    L = np.array([[cx_[0], cx_[1]], [cy_[0], cy_[1]]])
    t = np.array([cx_[2], cy_[2]])
    st.update(reg_n=int(keep.sum()), reg_rms=float(np.sqrt((res[keep] ** 2).mean())),
              reg_sx=float(L[0, 0]), reg_sy=float(L[1, 1]))
    return L, t, st


def _fshift(x, dy, dx):
    """Move x's content by (dy, dx) px: integer part by roll (bit-exact, so identical
    copies stay identical), fractional part as a Fourier phase ramp on a reflect-padded
    copy. Not bicubic/Lanczos: interpolators low-pass near Nyquist at half-pixel
    offsets, where screentone lives (a bicubic split warp measured 23 dB vs ~100)."""
    iy, ix = int(round(dy)), int(round(dx))
    fy, fx = dy - iy, dx - ix
    x = np.roll(np.roll(x, iy, 0), ix, 1)
    if abs(fy) < 0.02 and abs(fx) < 0.02:
        return x
    P = 16
    xp = np.pad(x, P, mode="reflect")
    H, W = xp.shape
    ph = np.exp(-2j * np.pi * (np.fft.fftfreq(H)[:, None] * fy + np.fft.rfftfreq(W)[None, :] * fx))
    return np.fft.irfft2(np.fft.rfft2(xp) * ph, s=xp.shape)[P:-P, P:-P].astype(np.float32)


def _box_resample(x, n_w, n_h, ax, bx, ay, by):
    """Lanczos-resample x so output index m maps to x's index (ax*m + bx, ay*m + by)
    per axis. PIL's resize box addresses pixel centres at i + 0.5."""
    x0 = bx + 0.5 - 0.5 * ax
    y0 = by + 0.5 - 0.5 * ay
    im = Image.fromarray(x.astype(np.float32), mode="F")
    return np.asarray(im.resize((n_w, n_h), Image.LANCZOS, box=(x0, y0, x0 + ax * n_w, y0 + ay * n_h)), np.float32)


def align(ga, gb, W, stats=None, fast=FAST):
    """Bring two trimmed native copies (PIL or float arrays) onto one grid at content
    width W, registered and cropped to the shared overlap. Returns (A, B) float arrays.

    A copy whose native width is W and needs no scale fix is never resampled (only
    rolled / Fourier-shifted); a copy that must change size is resampled ONCE from its
    native pixels with the fitted scale folded in. When neither changes width but the
    fit finds a real scale gap (> 0.2 px drift at the edges), BOTH are resampled half
    way so the interpolation low-pass hits them equally. Raises ValueError when the
    registered overlap is under 64 px (not the same page, or a crop mismatch)."""
    a = ga if isinstance(ga, np.ndarray) else np.asarray(ga, np.float32)
    b = gb if isinstance(gb, np.ndarray) else np.asarray(gb, np.float32)
    # a width within 1% of W is a trim difference, not a scale: crop/pad to W and let
    # the fit decide whether a scale gap is real
    tol = max(2, int(0.01 * W))

    def rw(x):
        if abs(x.shape[1] - W) <= tol:
            return x[:, :W] if x.shape[1] >= W else np.pad(x, ((0, 0), (0, W - x.shape[1])), mode="edge"), False
        return np.asarray(_to_w(Image.fromarray(x, mode="F"), W), np.float32), True
    (A0, ra), (B0, rb) = rw(a), rw(b)
    if not ra:
        a = A0
    if not rb:
        b = B0
    h = min(A0.shape[0], B0.shape[0])
    L, t, st = _fit_affine(A0, B0, fast)
    sx, sy = float(L[0, 0]), float(L[1, 1])
    drift = max(abs(sx - 1) * W, abs(sy - 1) * h) / 2
    mode = 1 if (ra or rb) else 0
    if drift > 0.2 and not (ra or rb):
        ra = rb = True
        mode = 2
    if ra or rb:
        s = np.array([sx, sy])
        tt = np.array([t[0], t[1]])
        if not ra:
            al, be = np.ones(2), np.zeros(2)
        elif not rb:
            al, be = 1 / s, -tt / s
        else:
            al = s ** -0.5
            c = np.array([W, h]) / 2
            be = c * (1 - al)

        def native_map(x, x0g, al_, be_):
            # per copy: frame m -> its W-grid index -> its native index
            k = np.array([x.shape[1] / W, x.shape[0] / x0g.shape[0]])
            return al_ * k, (be_ + 0.5) * k - 0.5
        Aa, Ba = native_map(a, A0, al, be)
        Ab, Bb = native_map(b, B0, s * al, s * be + tt)
        lo, hi = np.full(2, -np.inf), np.full(2, np.inf)
        for (A_, B_, x) in ((Aa, Ba, a), (Ab, Bb, b)):
            ext = np.array([x.shape[1], x.shape[0]]) - 3.0
            lo = np.maximum(lo, (2 - B_) / A_)
            hi = np.minimum(hi, (ext - B_) / A_)
        m0 = np.ceil(lo).astype(int)
        n = (np.floor(hi) - m0).astype(int)
        if (n < 64).any():
            raise ValueError("no overlap after registration")

        def take(x, A_, B_, resample):
            if not resample:
                return x[m0[1]:m0[1] + n[1], m0[0]:m0[0] + n[0]]
            return _box_resample(x, n[0], n[1], A_[0], B_[0] + A_[0] * m0[0], A_[1], B_[1] + A_[1] * m0[1])
        A1, B1 = take(a, Aa, Ba, ra), take(b, Ab, Bb, rb)
        L2, t2, st2 = _fit_affine(A1, B1, fast)
        c2 = np.array([A1.shape[1], A1.shape[0]]) / 2
        tr = (L2 - np.eye(2)) @ c2 + t2
    else:
        A1, B1 = A0[:h], B0[:h]
        c2 = np.array([W, h]) / 2
        tr = (L - np.eye(2)) @ c2 + t
    # residual translation: B index = A index + tr  ->  move B's content by -tr
    B1 = _fshift(B1, -tr[1], -tr[0])
    m = int(np.ceil(np.abs(tr).max())) + 8
    if stats is not None:
        stats.update(st)
        stats.update(reg_mode=mode, reg_res=float(np.hypot(*tr)) if mode else float("nan"))
    if A1.shape[0] <= 2 * m + 64 or A1.shape[1] <= 2 * m + 64:
        raise ValueError("no overlap after registration")
    return A1[m:-m, m:-m], B1[m:-m, m:-m]


# ------------------------------------------------------------------------ page copy

THUMB_W, THUMB_H = 48, 68
_CHROMA_W = 512
COLOR_T = 0.05   # chroma share at/above which a copy counts as colored (edition call)

# Webtoon support (plan "P3 detailed design"; strip logic in sites/strip_match.py).
# A page taller than TALL_ASPECT x its width is a strip slice: 0 of 980 manga corpus
# pages exceed 1.58, 79% of Solo Leveling's slices do. Such a page never gets the
# full-size float arrays `trim` builds (a 720x16000 slice peaked at 222 MB each, several
# workers at once: Solo Leveling's 1.2 GB search) and no thumbnail (a 48x68 resize
# squashes it ~16x into noise that matched unrelated slices). It joins clusters on
# sha1/pixhash identity only and is compared through strip bands instead.
TALL_ASPECT = 2.0
SMALL_W = 192    # width of the reduced copy every page keeps for strip profiles
EDGE_W = 256     # width the seam (art continues across a page cut) test reads rows at;
                 # the library/corpus measurements that tuned it used 256
# Decoded pixels in flight across the session's worker threads (prepare) and its
# band stage (decode_gray). Pillow's WebP decode of a 900x16000 slice peaks ~200 MB
# for 14 Mpx (decoder buffer + bytes copy + 4-byte RGB core), a whole prepare ~280 MB:
# two such decodes at once were Mount Hua's 631 MB session (p3/mem_session.py). A
# pixel budget bounds any mix of page sizes, where a count of tall pages did not;
# manga pages (1-3 Mpx) still decode 4-5 at a time.
DECODE_BUDGET_PX = 16_000_000


class _PixelBudget:
    """FIFO admission by pixel count: a decode waits until it is first in line and
    fits, so a stream of small pages cannot starve a large one. A page larger than
    the whole budget runs alone."""

    def __init__(self, cap: int):
        self.cap, self.used = cap, 0
        self.cv = threading.Condition()
        self.next_ticket = self.serving = 0

    @contextmanager
    def take(self, px: int):
        px = max(1, min(int(px), self.cap))
        with self.cv:
            t = self.next_ticket
            self.next_ticket += 1
            while t != self.serving or (self.used and self.used + px > self.cap):
                self.cv.wait()
            self.serving += 1
            self.used += px
            self.cv.notify_all()
        try:
            yield
        finally:
            with self.cv:
                self.used -= px
                self.cv.notify_all()


_DECODES = _PixelBudget(DECODE_BUDGET_PX)


@dataclass
class PageCopy:
    """One decoded page as served by one source: everything matching, dedupe and the
    edition call need, computed once per page however many peers it meets.
    `gray` is the trimmed grayscale content box the comparator registers; None for a
    tall page (strip slice) and once the session releases the pixels. `blob` (the
    served bytes), `small` and the edge rows feed sites/strip_match.py; the session
    drops `blob` as soon as no strip band can need it (grep _release_blobs)."""
    sha1: bytes
    gray: Optional[Image.Image]
    thumb: Optional[np.ndarray]  # zero-mean, unit-norm THUMB_H x THUMB_W float32; None when tall
    chroma: float                # share of mid-tone content pixels with RGB spread > 24
    pixhash: bytes               # sha1 of the decoded grayscale pixels (same pixels, other container)
    native_w: int = 0            # untrimmed decoded width
    native_h: int = 0
    meta: dict = field(default_factory=dict)
    tall: bool = False
    blob: Optional[bytes] = None
    small: Optional[np.ndarray] = None      # uint8, the whole page BOX-resized to SMALL_W wide
    edge_top: Optional[np.ndarray] = None   # float32 EDGE_W: mean of the top ~2/256 widths of rows
    edge_bot: Optional[np.ndarray] = None

    @property
    def width(self) -> int:
        return self.gray.size[0] if self.gray is not None else self.native_w

    @property
    def height(self) -> int:
        return self.gray.size[1] if self.gray is not None else self.native_h


def _thumb(g) -> np.ndarray:
    t = np.asarray(g.resize((THUMB_W, THUMB_H), Image.BILINEAR), dtype=np.float32)
    t = t - t.mean()
    n = float(np.linalg.norm(t))
    return t / n if n else t


def _chroma(rgb) -> float:
    """Share of content pixels whose chroma (max-min of RGB) exceeds 24, on a 512-px-wide
    copy. A B&W scan saved as RGB/JPEG stays well under 1% (JPEG chroma noise is small);
    a fan-colored or official color page is tens of percent."""
    im = rgb.resize((_CHROMA_W, max(1, int(rgb.size[1] * _CHROMA_W / rgb.size[0]))), Image.BILINEAR)
    full = np.asarray(im)
    # in row chunks: on a 720x16000 strip slice the whole-image int16/float64
    # temporaries alone were ~100 MB; the share is the same summed per chunk
    n_col = n_cont = 0
    for y in range(0, full.shape[0], 1024):
        a = full[y:y + 1024].astype(np.int16)
        c = a.max(axis=2) - a.min(axis=2)
        lum = a.mean(axis=2)
        content = (lum > 12) & (lum < 243)   # ignore pure paper / pure ink
        n_cont += int(content.sum())
        n_col += int((c[content] > 24).sum())
    return n_col / n_cont if n_cont else 0.0


def _edge(g, top: bool) -> np.ndarray:
    """Mean of the first/last ~2 EDGE_W-wide rows' worth of native rows, EDGE_W wide
    (what bench/image-ranker/p3/continuity.py read off a 256-wide resize)."""
    w, h = g.size
    r = max(1, min(h, int(round(2 * w / EDGE_W))))
    box_ = (0, 0, w, r) if top else (0, h - r, w, h)
    return np.asarray(g.crop(box_).resize((EDGE_W, 1), Image.BOX), np.float32)[0]


def _small(g) -> np.ndarray:
    w, h = g.size
    return np.asarray(g.resize((SMALL_W, max(1, int(round(h * SMALL_W / w)))), Image.BOX), np.uint8)


def prepare(blob: bytes) -> PageCopy:
    """Decode one served page. Raises on undecodable bytes (callers treat that as a
    failed fetch). Decoded pixels only: the container never reaches a score.
    A tall page (strip slice, see TALL_ASPECT) skips the trim and the thumbnail.
    Decodes under the pixel budget (_DECODES)."""
    im = Image.open(io.BytesIO(blob))
    with _DECODES.take(im.size[0] * im.size[1]):
        return _prepare(blob, im, im.size[1] > TALL_ASPECT * im.size[0])


def _prepare(blob: bytes, im, tall: bool) -> PageCopy:
    im.load()
    # convert("RGB") of an RGB image is a plain copy (57 MB on a 14 Mpx slice)
    rgb = im if im.mode == "RGB" else im.convert("RGB")
    g = ImageOps.grayscale(rgb)
    chroma = _chroma(rgb)
    del rgb
    tg = None if tall else trim(g)
    return PageCopy(
        sha1=hashlib.sha1(blob).digest(),
        gray=tg,
        thumb=None if tall else _thumb(tg),
        chroma=chroma,
        pixhash=hashlib.sha1(g.tobytes() + repr(g.size).encode()).digest(),
        native_w=im.size[0],
        native_h=im.size[1],
        tall=tall,
        blob=blob,
        small=_small(g),
        edge_top=_edge(g, True),
        edge_bot=_edge(g, False),
    )


def decode_gray(blob: bytes) -> Image.Image:
    """The full untrimmed grayscale page, for cutting strip bands (same conversion as
    prepare, so a band's pixels are the page's pixels)."""
    im = Image.open(io.BytesIO(blob))
    with _DECODES.take(im.size[0] * im.size[1]):
        im.load()
        return ImageOps.grayscale(im if im.mode == "RGB" else im.convert("RGB"))


def piece_copy(arr: np.ndarray, chroma: float) -> PageCopy:
    """A PageCopy for a band cut out of a stitched strip (uint8 rows x cols at the
    source's native width). Trimmed and thumbnailed like a page; sha1 and pixhash are
    both the hash of the pixels, so two sources whose bands hold identical pixels
    (same files, same rows) form one copy class and are never measured."""
    a = np.ascontiguousarray(arr, dtype=np.uint8)
    g = Image.fromarray(a, mode="L")
    tg = trim(g)
    h = hashlib.sha1(a.tobytes() + repr(a.shape).encode()).digest()
    return PageCopy(sha1=h, gray=tg, thumb=_thumb(tg), chroma=chroma, pixhash=h,
                    native_w=a.shape[1], native_h=a.shape[0])


def tile_capacity(h: int, w: int) -> int:
    """Upper bound on the tiles `_fit_affine` can keep for a reference of h x w: its
    grid (before the fast-mode cap; the fast fit falls back to the full grid). A
    pair whose larger copy cannot reach the session's tile gate never registers."""
    return max(2, h // 220) * max(2, w // 220)


def textured_tiles(a: np.ndarray) -> int:
    """Tiles of the `_fit_affine` grid over a (rows x cols uint8/float array) whose
    std reaches the fit's own threshold (8): a band with fewer than the session
    needs cannot register however well it is aligned (measured: located pieces that
    failed with 6 tiles at residual <= 0.1 px)."""
    H, W = a.shape
    T = _TILE
    ny, nx = max(2, H // 220), max(2, W // 220)
    ys = np.linspace(T // 2 + 8, H - T // 2 - 8, ny).astype(int) if H > T + 16 else []
    xs = np.linspace(T // 2 + 8, W - T // 2 - 8, nx).astype(int) if W > T + 16 else []
    n = 0
    for cy in ys:
        for cx in xs:
            if a[cy - T // 2:cy + T // 2, cx - T // 2:cx + T // 2].std() >= 8:
                n += 1
    return n


def ncc(a: PageCopy, b: PageCopy) -> float:
    """Normalized cross-correlation of the trimmed thumbnails, -1..1."""
    return float((a.thumb * b.thumb).sum())


# ------------------------------------------------------------------------ comparator

def _sharp(A, B):
    """(g1, g2, psnr_tone) for a registered same-size pair, ratios in A's favour.
    g1 = mean |grad| (the sweep's unit, the one `score` reads), g2 = RMS grad on the
    shared line-art mask (stored component only); each both ways round (each copy
    tone-matched onto the other), geometric mean."""
    Bt, At = hist_match(B, A), hist_match(A, B)
    gA, gB, gAt, gBt = grad(A), grad(B), grad(At), grad(Bt)

    def gm(x, y):
        return math.sqrt(x * y)
    g1 = gm(gA.mean() / max(gBt.mean(), 1e-6), gAt.mean() / max(gB.mean(), 1e-6))
    sm = box(np.minimum(grad(gblur(A, 1.5)), grad(gblur(Bt, 1.5))), 5)
    edge = sm > np.percentile(sm, 80)

    def rms(g):
        return float(np.sqrt((g[edge] ** 2).mean()))
    g2 = gm(rms(gA) / max(rms(gBt), 1e-6), rms(gAt) / max(rms(gB), 1e-6))
    return float(g1), g2, psnr(A, Bt)


def _orient_key(c: PageCopy):
    return (c.width, c.height, c.sha1)


def measure(a: PageCopy, b: PageCopy) -> Dict[str, float]:
    """Components of one matched page pair, in a's favour where directional:
      L_g1 / L_g2   tone-matched sharpness ratio (a over b) at the larger width
      psnr_tone     tone-matched PSNR at the larger width (same scan re-encoded
                    28-39 dB, different scans <= ~14, identical pixels very high)
      blk_a, blk_b  black level of each copy (2nd percentile of a 9x9 box mean;
                    lower = deeper blacks)
      wa, wb        trimmed content widths; a_is_big
      enl_gain      (widths differ) PSNR with the small copy enlarged minus PSNR with
                    the large copy reduced; very negative = the large copy holds real
                    extra resolution, >= 0 = it is an enlargement
      reg_rms       registration residual (px); > ~1.5 marks different crops/scans
      reg_n         tiles the registration kept; with reg_rms it is the match gate
                    (`registered`)
    Raises ValueError when the copies do not register (not the same page)."""
    a_ref = _orient_key(a) >= _orient_key(b)
    big, small = (a, b) if a_ref else (b, a)
    Wl, Ws = big.width, small.width
    st: dict = {}
    AL, BL = align(big.gray, small.gray, Wl, stats=st)
    g1, g2, p_up = _sharp(AL, BL)
    sgn = 1.0 if a_ref else -1.0
    m = dict(wa=a.width, wb=b.width, a_is_big=a.width >= b.width,
             L_g1=g1 ** sgn, L_g2=g2 ** sgn, psnr_tone=p_up,
             reg_rms=st.get("reg_rms", float("nan")), reg_n=int(st.get("reg_n", 0)))
    blk_big, blk_small = (float(np.percentile(box(X, 9), 2)) for X in (AL, BL))
    m["blk_a"], m["blk_b"] = (blk_big, blk_small) if a_ref else (blk_small, blk_big)
    if Wl != Ws:
        Sr, Dr = align(small.gray, big.gray, Ws)
        m["enl_gain"] = p_up - psnr(Sr, hist_match(Dr, Sr))
    return m


def score_parts(m: Dict[str, float], w=WEIGHTS) -> Dict[str, float]:
    """The three additive terms of `score`, in a's favour (log-ratio units):
    sharpness, gated resolution credit, bounded black-level term. Stored per source so
    a future levels fix can re-rank from cached components without re-probing."""
    sharp = w["ws"] * math.log(max(m["L_g1"], 1e-6))
    res = 0.0
    if m["wa"] != m["wb"] and "enl_gain" in m:
        lr = math.log(max(m["wa"], m["wb"]) / min(m["wa"], m["wb"]))
        lr *= max(0.0, min(1.0, (w["gate"] - m["enl_gain"]) / 1.5 + 0.5))
        res = w["wr"] * (lr if m["a_is_big"] else -lr)
    blk = w["cap"] * max(-1.0, min(1.0, (m["blk_b"] - m["blk_a"]) / w["sat"]))
    return dict(sharp=sharp, res=res, blk=blk)


def score(m: Dict[str, float], w=WEIGHTS) -> float:
    """Positive = a looks better. |score| < TIE is a tie."""
    p = score_parts(m, w)
    return p["sharp"] + p["res"] + p["blk"]


# ---------------------------------------------------------------------- edition call

def classify_editions(pages: Iterable[Dict[str, float]]) -> Dict[str, str]:
    """pages: per matched page, {source: chroma share} of the copies that page has.
    A copy disagrees when its color/gray call differs from the STRICT majority of that
    page's copies; a source is an alternate edition when it disagrees on at least half
    of the pages it voted on. Works both ways (fan-colored copy of a B&W series, gray
    copy of a color series) and flags nothing when all copies agree. A page with fewer
    than 3 copies or no strict majority casts no vote. Returns {source: "alternate" |
    "original"} for sources that voted at least once."""
    dis: Dict[str, int] = {}
    seen: Dict[str, int] = {}
    for pg in pages:
        calls = {k: v >= COLOR_T for k, v in pg.items()}
        n_color = sum(calls.values())
        n = len(calls)
        if n < 3 or 2 * n_color == n:
            continue
        major = 2 * n_color > n
        for k, c in calls.items():
            seen[k] = seen.get(k, 0) + 1
            dis[k] = dis.get(k, 0) + (c != major)
    return {k: "alternate" if 2 * dis.get(k, 0) >= seen[k] else "original" for k in seen}


# ---------------------------------------------------------------------- latent fit

RIDGE = 1.0   # page-equivalents pulling each source toward its group's mean


def fit_latent(edges: Sequence[Tuple[str, str, float, float]], ridge: float = RIDGE) -> Dict[str, float]:
    """edges: (source_i, source_j, s_ij, weight) with s_ij = score in i's favour
    (log-ratio units) and weight = pages the pair shares. Solves weighted least squares
    q_i - q_j ~ s_ij per connected component, with a ridge pulling each q toward the
    component mean (zero), so a source seen on one page is shrunk instead of trusted
    outright. Returns {source: q}, mean zero per component; a source in no edge is
    absent."""
    nodes = sorted({e[0] for e in edges} | {e[1] for e in edges})
    if not nodes:
        return {}
    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i, j, _, _ in edges:
        parent[find(i)] = find(j)
    comps: Dict[str, List[str]] = {}
    for n in nodes:
        comps.setdefault(find(n), []).append(n)
    out: Dict[str, float] = {}
    for members in comps.values():
        idx = {n: k for k, n in enumerate(members)}
        k = len(members)
        M = np.eye(k) * ridge
        rhs = np.zeros(k)
        for i, j, s, wt in edges:
            if i not in idx or j not in idx:
                continue
            a, b = idx[i], idx[j]
            M[a, a] += wt
            M[b, b] += wt
            M[a, b] -= wt
            M[b, a] -= wt
            rhs[a] += wt * s
            rhs[b] -= wt * s
        q = np.linalg.solve(M, rhs)
        q -= q.mean()
        for n, v in zip(members, q):
            out[n] = float(v)
    return out


def rating_from_deficit(d: float) -> float:
    """Absolute 0..1 rating from the deficit d = q_best - q (log-ratio units) to the
    best original-edition source of the same series. A tie (d < TIE) stays >= 0.75
    (the UI's green band starts at 0.7); a 1.5x softer copy lands ~0.51; 2.5x ~0.28,
    under the 0.4 fallback floor. Ratings are only ever compared within one series."""
    return 0.15 + 0.80 * math.exp(-max(0.0, d) / 0.5)


__all__ = [
    "WEIGHTS", "TIE", "PageCopy", "prepare", "ncc", "measure", "score", "score_parts",
    "classify_editions", "fit_latent", "rating_from_deficit", "align", "trim",
    "TALL_ASPECT", "SMALL_W", "EDGE_W", "decode_gray", "piece_copy", "tile_capacity",
    "textured_tiles",
]
