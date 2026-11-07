"""Webtoon strip matching: the geometry behind comparing differently sliced copies.

Owns: stitching one source's window of pages into a coarse strip profile
(`build_window`), locating two windows against each other along the strip
(`locate`), placing a slot's windows on one frame (`place`), choosing and cutting
comparison bands (`choose_bands`, `cut_band`), the seam test that tells a strip
from manga pages (`seam_stats`, `is_webtoon`), and episode labels in chapter titles
(`episode_number`).
Read by: `sites/match_session.py` (grep strip_match) - the session decides WHEN
(webtoon candidates only, pairs page matching left unlinked) and turns bands into
ordinary clusters; this module is pure numpy + Pillow (the Android build has no
scipy/OpenCV) and holds no session state.
Depends on: `page_ranker` (PageCopy fields `small`, `edge_top/bot`, `blob`,
`decode_gray`, `piece_copy`, `textured_tiles`).
Design + evidence: plan `sync-temp/plans/image-ranker-rework.md`, "P3 detailed
design"; scripts and logs in `bench/image-ranker/p3/` (gitignored).

Why strips: each site slices the same webtoon strip at its own heights (720x1280,
800x2900, 720x16000 ...), so a page is not a unit of content; position along the
strip, in units of the content width, is. Same-content windows locate at NCC
0.74-1.00 with a clear margin, different content <= 0.44 (ACCEPT_PEAK below).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from . import page_ranker as pr

# ---------------------------------------------------------------- tuning
PROFILE_W = 48           # coarse profile width; 1 profile row = content width / 48
MIN_RUN_WIDTHS = 4.0     # windows shorter than 4 content widths are never located
                         # (single/two-page probes gave false peaks up to 0.83)
ACCEPT_PEAK = 0.65       # windows of the same content measured 0.74-1.00 (ORV c256
                         # atsu vs mangakatana, two different scans: 0.74), different
                         # content <= 0.44 (bench/image-ranker/p3/eval_full_*.log;
                         # corpus windows <= 0.50)
ACCEPT_MARGIN = 0.15     # over the best offset outside the peak's own lobe
MIN_OVERLAP = 0.5        # of the shorter window
SCALES = tuple(round(1 + d / 100, 2) for d in range(-5, 6))   # B-over-A content scale:
                         # side trims differ by a few px between hosts, and a 1%
                         # scale error drifts ~10 rows over a 1000-row overlap

SEAM_NCC = 0.8           # an edge-row pair this similar = art continues across the cut
SEAM_MIN_STD = 6.0       # both edge rows need content to say anything
TALL_SHARE = 0.3         # candidate-level rule, see is_webtoon
MIN_PAGES = 10           # no call before this many pages: the session calls after EACH
                         # window and the call is sticky; one 3-page chapter with a tall
                         # credits page flagged a manga in some arrival orders (floor 6
                         # was the measured minimum; p3/sticky_rules.py)
SEAM_MIN = 2
SEAM_SHARE = 0.4

BAND_WIDTHS = 3.0        # band height in content widths (2,160 px at 720 wide): webtoon
                         # art has wide flat areas, and 2.2-width bands failed the
                         # 8-tile gate on well-aligned pieces (eval_full_tog: 3 of 18
                         # bands at 2.2, 1 of 18 at 3.0; eval3_*.log)
BAND_MAX_PX = 4_000_000  # per piece: keeps one measure() under ~200 MB
MAX_BANDS = 4            # per slot group
PAIR_BANDS = 2           # per source pair per slot: one chapter must not decide a pair
                         # (per-band spread inside a different-scan pair reached 0.24)
MIN_TEXTURED = 9         # page_ranker.textured_tiles each piece needs (the gate keeps 8)


# ---------------------------------------------------------------- windows

@dataclass
class StripWindow:
    """One source's window, stitched. Rows: `y0[i]` is page i's first row in the
    stitched NATIVE strip (untrimmed pages at the run's width); `profile` is the
    strip side-trimmed to columns [L, R) and resized to PROFILE_W, with each page
    placed at its exact fractional position (rounded per page boundary against the
    cumulative height, so rounding never accumulates: per-page resizing drifted ~5
    rows per 600 and hid real matches, 0.27 vs 0.94)."""
    src: str
    chap: int
    slot: int
    pages: List = field(default_factory=list)   # the session's _Page objects, by index
    width: int = 0
    L: int = 0
    R: int = 0
    y0: List[int] = field(default_factory=list)
    height: int = 0
    profile: Optional[np.ndarray] = None

    @property
    def scale(self) -> float:
        """Native rows per profile row."""
        return (self.R - self.L) / PROFILE_W

    def pixhashes(self) -> List[bytes]:
        return [p.copy.pixhash for p in self.pages]


def _runs(pages: Sequence) -> List[List]:
    """Runs of consecutive page indices at one native width."""
    ps = sorted((p for p in pages if p.copy is not None and p.copy.small is not None),
                key=lambda p: p.idx)
    out: List[List] = []
    for p in ps:
        if out and p.idx == out[-1][-1].idx + 1 and p.copy.native_w == out[-1][-1].copy.native_w:
            out[-1].append(p)
        else:
            out.append([p])
    return out


def _cols(small: np.ndarray) -> Optional[Tuple[int, int]]:
    """Content columns of one reduced page (column spread > 6 levels, as page_ranker.trim
    reads it); None for a blank page."""
    s = small.astype(np.float32).std(axis=0)
    c = np.where(s > 6)[0]
    if len(c) < 4:
        return None
    return int(c[0]), int(c[-1]) + 1


def build_window(src: str, chap: int, slot: int, pages: Sequence) -> Optional[StripWindow]:
    """The window's longest run, stitched and profiled; None when the run is shorter
    than MIN_RUN_WIDTHS widths. Side trim = the MEDIAN of the pages' content columns
    (one page whose art stops short must not narrow the window, which would change
    its content scale)."""
    runs = _runs(pages)
    if not runs:
        return None
    run = max(runs, key=lambda r: sum(p.copy.native_h for p in r))
    w = run[0].copy.native_w
    H = sum(p.copy.native_h for p in run)
    if H < MIN_RUN_WIDTHS * w:
        return None
    boxes = [b for b in (_cols(p.copy.small) for p in run) if b]
    if boxes:
        ls = int(np.median([b[0] for b in boxes]))
        rs = int(np.median([b[1] for b in boxes]))
    else:
        ls, rs = 0, pr.SMALL_W
    if rs - ls < pr.SMALL_W // 4:
        ls, rs = 0, pr.SMALL_W
    L = int(np.floor(ls * w / pr.SMALL_W))
    R = int(np.ceil(rs * w / pr.SMALL_W))
    s = PROFILE_W / (R - L)     # profile rows per native row
    rows, y0, y = [], [], 0
    for p in run:
        y0.append(y)
        a, b = int(round(y * s)), int(round((y + p.copy.native_h) * s))
        y += p.copy.native_h
        if b <= a:
            continue
        sm = p.copy.small[:, ls:rs]
        rows.append(np.asarray(Image.fromarray(sm).resize((PROFILE_W, b - a), Image.BOX), np.float32))
    if not rows:
        return None
    return StripWindow(src=src, chap=chap, slot=slot, pages=list(run), width=w, L=L, R=R,
                       y0=y0, height=H, profile=np.concatenate(rows, 0))


# ---------------------------------------------------------------- locating

@dataclass
class Located:
    """B placed on A: B's profile row r sits at A's profile row `off + k * r`."""
    peak: float
    margin: float
    off: float
    k: float
    overlap: int      # profile rows

    @property
    def accepted(self) -> bool:
        return self.peak >= ACCEPT_PEAK and self.margin >= ACCEPT_MARGIN


def _ncc_all(A: np.ndarray, B: np.ndarray, minov: int):
    """NCC of B at every vertical offset in A's rows (B row 0 at A row o), per-strip
    mean removed (the comparison is of structure, not of overall tone), FFT for the
    cross term, running sums for the energies. Returns (offsets, ncc) for offsets
    whose overlap is >= minov."""
    A = A - A.mean()
    B = B - B.mean()
    ha, hb = len(A), len(B)
    n = 1 << int(np.ceil(np.log2(ha + hb)))
    num = np.fft.irfft(np.fft.rfft(A, n, axis=0) * np.conj(np.fft.rfft(B, n, axis=0)), n, axis=0).sum(1)
    ea = np.r_[0.0, np.cumsum((A.astype(np.float64) ** 2).sum(1))]
    eb = np.r_[0.0, np.cumsum((B.astype(np.float64) ** 2).sum(1))]
    o = np.arange(-(hb - minov), ha - minov + 1)
    a0, a1 = np.maximum(0, o), np.minimum(ha, o + hb)
    ok = (a1 - a0) >= minov
    o, a0, a1 = o[ok], a0[ok], a1[ok]
    den = np.sqrt((ea[a1] - ea[a0]) * (eb[a1 - o] - eb[a0 - o]))
    v = np.where(den > 0, num[o % n] / np.maximum(den, 1e-9), -1.0)
    return o, v


def _lobe_margin(o: np.ndarray, v: np.ndarray, i: int) -> float:
    """Peak minus the best value outside the peak's lobe (the run of offsets around
    the peak where the curve keeps falling): a smooth strip's peak has wide shoulders,
    so 'the second best offset' would be its own neighbour."""
    lo = i
    while lo > 0 and v[lo - 1] <= v[lo]:
        lo -= 1
    hi = i
    while hi < len(v) - 1 and v[hi + 1] <= v[hi]:
        hi += 1
    rest = np.r_[v[:lo], v[hi + 1:]]
    return float(v[i] - rest.max()) if len(rest) else float(v[i])


def locate(A: StripWindow, B: StripWindow) -> Located:
    """Best placement of B on A over every offset and the SCALES search."""
    best: Optional[Located] = None
    for k in SCALES:
        hb = max(1, int(round(len(B.profile) * k)))
        Bp = B.profile if hb == len(B.profile) else np.asarray(
            Image.fromarray(B.profile, mode="F").resize((PROFILE_W, hb), Image.BILINEAR), np.float32)
        minov = max(8, int(MIN_OVERLAP * min(len(A.profile), hb)))
        if minov > min(len(A.profile), hb):
            continue
        o, v = _ncc_all(A.profile, Bp, minov)
        if not len(v):
            continue
        i = int(np.argmax(v))
        if best is None or v[i] > best.peak:
            ov = int(min(len(A.profile), o[i] + hb) - max(0, o[i]))
            best = Located(peak=float(v[i]), margin=_lobe_margin(o, v, i), off=float(o[i]), k=k, overlap=ov)
    return best or Located(peak=-1.0, margin=0.0, off=0.0, k=1.0, overlap=0)


def snap_identical(A: StripWindow, B: StripWindow, loc: Located) -> Located:
    """Two windows that share a page (same pixhash, same width) are the same files:
    replace the coarse estimate with the exact offset that page gives, so bands cut
    from both hold identical pixels and tie as one copy class (coarse rounding put
    such offsets ~9 px off)."""
    if A.width != B.width or (A.L, A.R) != (B.L, B.R):
        return loc
    pos_a = {h: A.y0[i] for i, h in enumerate(A.pixhashes())}
    for j, h in enumerate(B.pixhashes()):
        if h in pos_a:
            return Located(peak=loc.peak, margin=loc.margin, off=(pos_a[h] - B.y0[j]) / A.scale,
                           k=1.0, overlap=loc.overlap)
    return loc


# ---------------------------------------------------------------- placement

@dataclass
class Placed:
    """A window on its group's frame: frame row = pos + k * (window profile row)."""
    win: StripWindow
    pos: float
    k: float


def place(windows: Sequence[StripWindow], links: Iterable[Tuple[int, int, Located]]) -> List[List[Placed]]:
    """Connected groups of windows (accepted links only), each placed on one frame by a
    maximum spanning tree on peak, walked from the group's longest window. Cycles are
    ignored (the tree's strongest links win), so one weak contradictory link cannot
    move a window. links: (i, j, Located of windows[j] on windows[i])."""
    links = sorted((l for l in links if l[2].accepted), key=lambda l: -l[2].peak)
    parent = list(range(len(windows)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    adj: Dict[int, List[Tuple[int, float, float]]] = {}
    for i, j, loc in links:
        ri, rj = find(i), find(j)
        if ri == rj:
            continue
        parent[ri] = rj
        # j on i: j_row r -> i_row off + k r ; i on j: i_row r -> j_row (r - off) / k
        adj.setdefault(i, []).append((j, loc.off, loc.k))
        adj.setdefault(j, []).append((i, -loc.off / loc.k, 1.0 / loc.k))
    groups: Dict[int, List[int]] = {}
    for i in range(len(windows)):
        groups.setdefault(find(i), []).append(i)
    out: List[List[Placed]] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        root = max(members, key=lambda i: (len(windows[i].profile), windows[i].src))
        pos = {root: (0.0, 1.0)}
        stack = [root]
        while stack:
            i = stack.pop()
            pi, ki = pos[i]
            for j, off, k in adj.get(i, ()):
                if j in pos:
                    continue
                pos[j] = (pi + ki * off, ki * k)
                stack.append(j)
        out.append([Placed(windows[i], *pos[i]) for i in members])
    return out


# ---------------------------------------------------------------- bands

@dataclass
class Band:
    f0: float          # frame rows
    f1: float
    members: List[Placed]


def _span(p: Placed) -> Tuple[float, float]:
    return p.pos, p.pos + p.k * len(p.win.profile)


def choose_bands(group: Sequence[Placed], need: Dict[Tuple[str, str], int],
                 max_bands: int = MAX_BANDS) -> List[Band]:
    """Up to max_bands non-overlapping bands on one placed group, each lying fully
    inside every member it lists (1 profile row of margin), greedily by: source pairs
    still needing a comparison (`need`: pair -> bands already given, < PAIR_BANDS),
    then structure (row spread of the covering profiles). `need` is updated."""
    if len(group) < 2:
        return []
    lo = min(_span(p)[0] for p in group)
    hi = max(_span(p)[1] for p in group)
    # band height in frame rows (content-width units of the group's root window),
    # capped so no member's piece exceeds BAND_MAX_PX: a member's native rows per
    # frame row are scale / k
    bh = BAND_WIDTHS * PROFILE_W
    bh = min([bh] + [BAND_MAX_PX / p.win.width * p.k / p.win.scale for p in group])
    if bh < PROFILE_W:   # under one content width: nothing worth registering
        return []
    cands: List[Tuple[float, List[Placed]]] = []
    f = lo + 1
    while f + bh <= hi - 1:
        cov = [p for p in group if _span(p)[0] + 1 <= f and f + bh <= _span(p)[1] - 1]
        if len(cov) >= 2:
            cands.append((f, cov))
        f += bh / 2
    chosen: List[Band] = []
    while len(chosen) < max_bands and cands:
        def gain(c):
            f0, cov = c
            srcs = sorted({p.win.src for p in cov})
            pairs = [(a, b) for i, a in enumerate(srcs) for b in srcs[i + 1:]
                     if (a, b) in need and need[(a, b)] < PAIR_BANDS]
            struct = float(np.mean([_band_struct(p, f0, f0 + bh) for p in cov]))
            return (len(pairs), struct)
        best = max(cands, key=gain)
        if gain(best)[0] == 0:
            break
        f0, cov = best
        srcs = sorted({p.win.src for p in cov})
        for i, a in enumerate(srcs):
            for b in srcs[i + 1:]:
                if (a, b) in need:
                    need[(a, b)] += 1
        chosen.append(Band(f0, f0 + bh, list(cov)))
        cands = [c for c in cands if c[0] + bh <= f0 or c[0] >= f0 + bh]
    return chosen


def _band_struct(p: Placed, f0: float, f1: float) -> float:
    r0 = int(max(0, (f0 - p.pos) / p.k))
    r1 = int(min(len(p.win.profile), (f1 - p.pos) / p.k))
    seg = p.win.profile[r0:r1]
    return float(seg.std(axis=1).mean()) if len(seg) else 0.0


def native_rows(p: Placed, f0: float, f1: float) -> Tuple[int, int]:
    """The band's rows in p's stitched native strip."""
    s = p.win.scale
    return int(round((f0 - p.pos) / p.k * s)), int(round((f1 - p.pos) / p.k * s))


def cut_band(p: Placed, f0: float, f1: float, decoded: Dict[int, np.ndarray]) -> Optional[np.ndarray]:
    """Native uint8 rows of the band from p's window pages, full page width (each
    piece is trimmed like a page by page_ranker.piece_copy, so identical files give
    identical pieces). decoded: id(page) -> full gray array, filled lazily from each
    page's blob; None when a page's blob was already released."""
    y0, y1 = native_rows(p, f0, f1)
    parts = []
    for page, ys in zip(p.win.pages, p.win.y0):
        ye = ys + page.copy.native_h
        if ye <= y0 or ys >= y1:
            continue
        key = id(page)
        if key not in decoded:
            if page.copy.blob is None:
                return None
            decoded[key] = np.asarray(pr.decode_gray(page.copy.blob), np.uint8)
        a = decoded[key]
        parts.append(a[max(0, y0 - ys):min(page.copy.native_h, y1 - ys)])
    if not parts:
        return None
    return np.concatenate(parts, 0)


# ---------------------------------------------------------------- webtoon test

def seam_stats(pages: Sequence) -> Tuple[int, int]:
    """(continuing cuts, informative cuts) over consecutive page indices of one window:
    page i's bottom edge row vs page i+1's top. Informative = both rows have content;
    continuing = their NCC > SEAM_NCC (the art runs across the cut)."""
    ps = sorted((p for p in pages if p.copy is not None and p.copy.edge_top is not None), key=lambda p: p.idx)
    cont = inf = 0
    for a, b in zip(ps, ps[1:]):
        if b.idx != a.idx + 1:
            continue
        x, y = a.copy.edge_bot, b.copy.edge_top
        if x.std() < SEAM_MIN_STD or y.std() < SEAM_MIN_STD:
            continue
        inf += 1
        x, y = x - x.mean(), y - y.mean()
        if float(x @ y / (np.linalg.norm(x) * np.linalg.norm(y))) > SEAM_NCC:
            cont += 1
    return cont, inf


def is_webtoon(n_pages: int, n_tall: int, cont: int, inf: int) -> bool:
    """Candidate-level call over every window seen so far. Measured on the user's
    library (111 series, bench/image-ranker/p3/lib_rules.py): 0 manga flagged, 0
    webtoons missed. Per-window rules failed there: 'any tall page' flagged a manga
    (one 17x-tall credits page) and missed webtoons sliced at 1.6x; 'one continuing
    cut' flagged 5 manga."""
    if n_pages < MIN_PAGES:
        return False
    if n_tall >= TALL_SHARE * n_pages:
        return True
    return cont >= SEAM_MIN and cont >= SEAM_SHARE * inf


# ---------------------------------------------------------------- episode labels

_EP = re.compile(r"\bep(?:isode)?\.?\s*(\d+)", re.I)


def episode_number(title: Optional[str]) -> Optional[int]:
    """'Ep. 164', '[S2] Ep. 164', 'Episode 53', 'Chapter 244: [S2] Ep. 164' -> the
    episode number; None when the title names none (asura/weebcentral titles don't)."""
    if not title:
        return None
    m = _EP.search(str(title))
    return int(m.group(1)) if m else None


__all__ = [
    "StripWindow", "Located", "Placed", "Band", "build_window", "locate", "snap_identical",
    "place", "choose_bands", "cut_band", "native_rows", "seam_stats", "is_webtoon",
    "episode_number",
]
