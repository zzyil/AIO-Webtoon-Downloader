"""Chapter fingerprints: proving that two copies of a chapter hold the same content.

Owns: the per-page grayscale "smalls" a chapter is reduced to (`page_small`,
`ChapterPages`), their storage on disk (`save_pages` / `load_pages`, the hidden
`<series>/.aio_fp/` folder via `fp_path` and `FingerprintWriter`), the stitched
strip fingerprint built from them (`from_smalls`, `Fingerprint`), the band search
that locates one fingerprint in another (`verdict`, `match`), the two
multi-source rescue proofs (`prove_partial`: a failed chapter's own pages found in
an alt chapter; `head_matches`: the cheap first-pages pre-check), and the checks
a fill by placement rests on (`middle_pages` / `probe_names`: the identifying
probe that says which of our chapters an alt entry is; `holds_other`: the ±15
veto).
Read by: `aio-dl.py` (grep chapter_fingerprint): the chapter routine saves every
downloaded chapter's smalls and the pages of every failed attempt, the strict
wrapper's rescue search runs `head_matches` / `prove_partial` before an alt
chapter may be stored as the primary's chapter, and `_place` runs the probes
and checks; `sites/rescue_pairing.py` places, this file tests content.
Depends on: numpy + Pillow only (the Android build has no scipy/OpenCV), and
`page_ranker._DECODES` (the shared decode pixel budget) for every decode.
Design + evidence: plan `sync-temp/plans/multisource-offset.md` (Design 2 and 3,
"Rev 1.1 delta"); the P0 prototype and replay are in `bench/ms-offset/`
(gitignored: fp.py, replay.py, discrim.py).

Why a strip and not pages: sites slice a webtoon strip at different heights and
manga scans differ in crop and size, so a page is not a unit of content; position
down the stitched chapter, in units of the content width, is. Every quantity here
is derived from the stored smalls, so the matcher can be re-tuned without
re-downloading anything.

Measured (P0, unOrdinary LINE vs mangakatana/atsumaru, Berserk, Chainsaw Man,
Tower of God): the same chapter covers 0.94-1.0 of its bands both ways; any other
chapter, recaps and side stories included, <= 0.09. Different scans of the same
chapter can fall to 0.25-0.69, so COVER stays at 0.6 and a miss there is
"unproven", never a fill.
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageOps

from . import page_ranker as pr

# ---------------------------------------------------------------- tuning
SMALL_W = 96             # stored page width; every derived view comes from these
PROFILE_W = 48           # strip profile width; 1 profile row = content width / 48
TALL_ASPECT = 2.0

BAND = 96                # profile rows per band = 2 content widths
STRIDE = 48
MAX_BANDS = 32           # evenly spaced among the informative bands
STD_FLOOR = 12.0         # band std (0-255) below which a band says nothing: black,
                         # white and text-card pages give no evidence either way

BAND_T = 0.90            # band peak that counts as a hit
LINE_TOL = 6.0           # rows, plus 1% of the distance along A
K_RANGE = (0.9, 1.1)     # B-over-A scale; scans of one chapter measured 0.95-1.05
MIN_HITS = 3
COVER = 0.6              # share of A's informative bands that must sit on the line

MIN_PARTIAL_BANDS = 8    # a failed attempt's pages must carry at least this many
                         # informative bands (~16 content widths) before they may
                         # prove anything: 3 bands is enough to separate chapters
                         # in P0, 8 leaves margin for runs cut short by gaps
HEAD_PAGES = 6           # pages of an alt candidate fetched for the pre-check
PREFIX_SLACK = 8 * PROFILE_W   # a run that starts at page 1 must land within 8
                               # widths of the candidate's top: room for a
                               # scanlator's credit pages, not for a whole earlier
                               # chapter merged in front (those run 50-280 widths)


# ---------------------------------------------------------------- pages

def page_small(blob: bytes, width: int = SMALL_W) -> Tuple[np.ndarray, int, int]:
    """(small uint8 HxW, native_w, native_h) for one page's bytes. JPEG decodes at
    the smallest DCT scale that still covers 2x the target width (Image.draft: 3 ms
    vs 10 ms a page in P0). Raises on undecodable bytes; callers treat that page as
    missing."""
    im = Image.open(io.BytesIO(blob))
    nw, nh = im.size
    with pr._DECODES.take(nw * nh):
        if im.format == "JPEG":
            im.draft("L", (width * 2, max(1, int(nh * width * 2 / max(1, nw)))))
        im = ImageOps.grayscale(im if im.mode in ("L", "RGB") else im.convert("RGB"))
        h = max(1, int(round(nh * width / max(1, nw))))
        sm = np.asarray(im.resize((width, h), Image.BOX), np.uint8)
    return sm, int(nw), int(nh)


@dataclass
class ChapterPages:
    """Smalls of the pages one copy of a chapter delivered, keyed by 1-based page
    index. `pages_total` is how many pages the copy has (pages may be missing:
    a failed attempt keeps what it got). `meta` rides along to disk (site, chap,
    url, rescue provenance)."""
    pages: Dict[int, Tuple[np.ndarray, int, int]] = field(default_factory=dict)
    pages_total: int = 0
    meta: Dict[str, object] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return self.pages_total > 0 and len(self.pages) >= self.pages_total

    def merge(self, other: "ChapterPages") -> None:
        """Union with another attempt at the SAME copy. A different page count means
        the source re-sliced the chapter, so the old pages no longer index the same
        content and are dropped."""
        if other.pages_total != self.pages_total:
            self.pages = {}
            self.pages_total = other.pages_total
        self.pages.update(other.pages)

    def runs(self) -> List[List[int]]:
        """Contiguous runs of present page indices, in order."""
        out: List[List[int]] = []
        for i in sorted(self.pages):
            if out and i == out[-1][-1] + 1:
                out[-1].append(i)
            else:
                out.append([i])
        return out

    def fingerprint(self, idxs: Optional[Sequence[int]] = None) -> "Fingerprint":
        idxs = sorted(self.pages) if idxs is None else list(idxs)
        return from_smalls([self.pages[i][0] for i in idxs],
                           [(self.pages[i][1], self.pages[i][2]) for i in idxs])


def pages_from_files(paths: Sequence[Tuple[int, str]], pages_total: int,
                     meta: Optional[Dict[str, object]] = None) -> ChapterPages:
    """ChapterPages from (page_index, file) pairs; unreadable files are skipped."""
    cp = ChapterPages(pages_total=pages_total, meta=dict(meta or {}))
    for idx, p in paths:
        try:
            with open(p, "rb") as fh:
                cp.pages[int(idx)] = page_small(fh.read())
        except Exception:
            continue
    return cp


def pages_from_blobs(blobs: Dict[int, bytes], pages_total: int,
                     meta: Optional[Dict[str, object]] = None) -> ChapterPages:
    """ChapterPages from {page_index: bytes} (a handler's in-memory pages, e.g.
    IncompleteChapterError.pages); undecodable blobs are skipped."""
    cp = ChapterPages(pages_total=pages_total, meta=dict(meta or {}))
    for idx, blob in blobs.items():
        try:
            cp.pages[int(idx)] = page_small(blob)
        except Exception:
            continue
    return cp


def save_pages(path: str, cp: ChapterPages) -> None:
    """Write smalls + page geometry + meta as one compressed npz (~190 KB for a
    120-page webtoon chapter in P0). Written to a temp name then renamed, so a
    reader never sees half a file."""
    idx = sorted(cp.pages)
    smalls = [cp.pages[i][0] for i in idx]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        np.savez_compressed(
            fh,
            smalls=(np.concatenate(smalls, 0) if smalls else np.zeros((0, SMALL_W), np.uint8)),
            heights=np.asarray([s.shape[0] for s in smalls], np.int32),
            natives=np.asarray([(cp.pages[i][1], cp.pages[i][2]) for i in idx], np.int32).reshape(-1, 2),
            index=np.asarray(idx, np.int32),
            pages_total=np.asarray(cp.pages_total, np.int32),
            meta=np.asarray(json.dumps(cp.meta, ensure_ascii=False, default=str)),
        )
    os.replace(tmp, path)


def load_pages(path: str) -> Optional[ChapterPages]:
    """Inverse of save_pages; None when the file is missing or unreadable."""
    try:
        z = np.load(path, allow_pickle=False)
        S, H, N, I = z["smalls"], z["heights"], z["natives"], z["index"]
        cp = ChapterPages(pages_total=int(z["pages_total"]),
                          meta=json.loads(str(z["meta"])) if "meta" in z.files else {})
    except Exception:
        return None
    y = 0
    for i, h, (w_, h_) in zip(I, H, N):
        cp.pages[int(i)] = (S[y:y + int(h)], int(w_), int(h_))
        y += int(h)
    return cp


_SAFE = re.compile(r"[^0-9A-Za-z._-]+")


def chap_key(chap) -> str:
    """Filesystem-safe form of a chapter label ("12.5" stays "12.5")."""
    return _SAFE.sub("_", str(chap)) or "_"


def fp_path(series_dir: str, site: str, chap) -> str:
    """`<series>/.aio_fp/<site>/<chap>.npz`. Hidden on purpose: device sync skips
    dotfiles (sync/planner.js, pc-inventory.js) and readers ignore the folder."""
    return os.path.join(series_dir, ".aio_fp", chap_key(site), chap_key(chap) + ".npz")


class FingerprintWriter:
    """Decodes and saves fingerprints off the chapter thread. The caller hands over
    page BYTES (read before later steps convert or delete the files), so the next
    chapter is not held up by decoding. At most `max_pending` chapters queue
    (each holds its bytes in memory); `submit` blocks beyond that. `close` waits
    for everything queued. Errors are swallowed: a fingerprint is evidence for a
    later rescue, never a reason to fail a download."""

    def __init__(self, max_pending: int = 2):
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aio-fp")
        self._slots = threading.BoundedSemaphore(max_pending)
        self._futures: List[Future] = []

    def drain(self) -> None:
        """Wait for everything submitted so far, and keep accepting work (unlike
        `close`). Placement reads the fingerprints of chapters this run just
        downloaded, so their files must exist first."""
        pending, self._futures = self._futures, []
        for f in pending:
            try:
                f.result()
            except Exception:
                pass

    def submit(self, path: str, blobs: Sequence[Tuple[int, bytes]], pages_total: int,
               meta: Dict[str, object]) -> Future:
        self._slots.acquire()
        self._futures = [f for f in self._futures if not f.done()]

        def job():
            try:
                cp = ChapterPages(pages_total=pages_total, meta=dict(meta))
                for idx, b in blobs:
                    try:
                        cp.pages[int(idx)] = page_small(b)
                    except Exception:
                        continue
                if cp.pages:
                    save_pages(path, cp)
            except Exception:
                pass
            finally:
                self._slots.release()

        fut = self._pool.submit(job)
        self._futures.append(fut)
        return fut

    def close(self) -> None:
        self._pool.shutdown(wait=True)


# ---------------------------------------------------------------- fingerprint

@dataclass
class Fingerprint:
    profile: np.ndarray                       # float32 rows x PROFILE_W
    page_rows: List[Tuple[float, float]]      # each page's [y0, y1) in profile rows
    n_pages: int = 0
    _bands: Optional[List[int]] = field(default=None, repr=False)

    @property
    def length_widths(self) -> float:
        return len(self.profile) / PROFILE_W

    def bands(self) -> List[int]:
        """Start rows of informative bands, at most MAX_BANDS, evenly spaced."""
        if self._bands is None:
            P = self.profile
            starts = [y for y in range(0, max(1, len(P) - BAND + 1), STRIDE)
                      if len(P) >= BAND and P[y:y + BAND].std() >= STD_FLOOR]
            if len(starts) > MAX_BANDS:
                pick = np.linspace(0, len(starts) - 1, MAX_BANDS).round().astype(int)
                starts = [starts[i] for i in pick]
            self._bands = starts
        return self._bands

    @property
    def informative(self) -> bool:
        return len(self.bands()) >= MIN_HITS


def _cols(sm: np.ndarray) -> Optional[Tuple[int, int]]:
    s = sm.astype(np.float32).std(axis=0)
    c = np.where(s > 6)[0]
    if len(c) < 4:
        return None
    return int(c[0]), int(c[-1]) + 1


def _frame(box: Optional[Tuple[int, int]]) -> Tuple[int, int]:
    """A content box usable as a crop: too narrow (a lone bubble, a page number)
    or absent (a blank page) means the full width."""
    if box is None or box[1] - box[0] < SMALL_W // 4:
        return 0, SMALL_W
    return box


def from_smalls(smalls: Sequence[np.ndarray], natives: Sequence[Tuple[int, int]]) -> Fingerprint:
    """Stitch pages into the strip profile, each page landing at its exact
    fractional row so rounding never accumulates down a 300-page chapter (same
    rule as strip_match.build_window).

    Side margins: every page is cropped to ONE frame, the content's outer extent
    over the pages (leftmost content column to rightmost), with the n//10 most
    extreme pages each side dropped so one wide credit page cannot set it.
    Outer extent, not the median frame used before: manga gutters alternate
    sides page to page, so the median sat between two modes and moved with the
    page set. A failed attempt's pages 4-20 and the alt's pages 1-20 of the same
    scan were cropped 2 columns apart and every band peaked below BAND_T (HxH
    Ch 72, tcbscans vs weebcentral, 2026-11-01); the outer extent of a run
    holding both gutter sides is the same in every copy. Tried and killed: each
    page cropped to its own frame (fixes manga, but short webtoon slices, e.g.
    mangakatana's, then take a frame from wherever the slice was cut: unOrdinary
    84 -> 56 of 85 proven) and a median per alternate-page group (manga only
    half fixed). Regression harness + numbers: local bench/ms-offset/trim_regress.py,
    bench/hxh-ch72/regress_*.log."""
    boxes = [b for b in (_cols(s) for s in smalls) if b]
    if boxes:
        k = len(boxes) // 10
        frame = _frame((sorted(b[0] for b in boxes)[k], sorted(b[1] for b in boxes)[-1 - k]))
    else:
        frame = (0, SMALL_W)
    ls, rs = frame
    rows, spans, y = [], [], 0.0
    for sm, (nw, nh) in zip(smalls, natives):
        # profile rows this page spans: its small-row height scaled so its
        # cropped width becomes PROFILE_W
        hp = nh * SMALL_W / max(1, nw) * PROFILE_W / (rs - ls)
        a, b = int(round(y)), int(round(y + hp))
        spans.append((y, y + hp))
        y += hp
        if b <= a:
            continue
        rows.append(np.asarray(Image.fromarray(sm[:, ls:rs]).resize((PROFILE_W, b - a), Image.BOX),
                               np.float32))
    prof = np.concatenate(rows, 0) if rows else np.zeros((0, PROFILE_W), np.float32)
    return Fingerprint(profile=prof, page_rows=spans, n_pages=len(smalls))


# ---------------------------------------------------------------- band search

class _Strip:
    """B prepared once (FFT + cumulative sums) for many band searches."""

    def __init__(self, P: np.ndarray):
        self.P = P.astype(np.float64)
        self.h = len(P)
        self.n = 1 << int(np.ceil(np.log2(max(2, self.h + BAND))))
        self.F = np.fft.rfft(self.P, self.n, axis=0)
        self.cs = np.r_[0.0, np.cumsum(self.P.sum(1))]
        self.cq = np.r_[0.0, np.cumsum((self.P ** 2).sum(1))]

    def search_many(self, bands: np.ndarray) -> List[Tuple[int, float]]:
        """Best local-mean NCC of each band (m x BAND x PROFILE_W) over every
        full-overlap offset of the strip -> [(offset, peak)]."""
        if self.h < BAND or not len(bands):
            return [(0, -1.0)] * len(bands)
        b0 = bands.astype(np.float64) - bands.mean(axis=(1, 2), keepdims=True)
        eb = (b0 ** 2).sum(axis=(1, 2))
        num = np.fft.irfft(self.F[None] * np.conj(np.fft.rfft(b0, self.n, axis=1)),
                           self.n, axis=1).sum(2)
        o = np.arange(0, self.h - BAND + 1)
        N = BAND * PROFILE_W
        sa = self.cs[o + BAND] - self.cs[o]
        qa = self.cq[o + BAND] - self.cq[o]
        ea = qa - sa * sa / N
        # A flat window has no structure to agree with, and FFT round-off over ~0
        # variance produced "NCC" up to 68 in P0 (false placements on white gutters).
        flat = ea < (STD_FLOOR / 2) ** 2 * N
        v = num[:, o] / np.sqrt(np.maximum(ea, 1e-9)[None] * np.maximum(eb, 1e-9)[:, None])
        v[:, flat] = -1.0
        i = np.argmax(v, axis=1)
        return [(int(o[j]), float(v[r, j]) if eb[r] > 0 else -1.0) for r, j in enumerate(i)]


def band_hits(a: Fingerprint, b: Fingerprint) -> List[Tuple[int, int, float]]:
    """[(a_row, b_row, peak)] for every informative band of A searched over B."""
    ys = a.bands()
    if not ys:
        return []
    res = _Strip(b.profile).search_many(np.stack([a.profile[y:y + BAND] for y in ys]))
    return [(y, o, p) for y, (o, p) in zip(ys, res)]


def collinear(hits, band_t: float = BAND_T):
    """Largest set of (a_row, b_row) hits on one line b = off + k*a, k in K_RANGE.
    Exhaustive over pairs (<= MAX_BANDS^2 lines)."""
    pts = [(a, b) for a, b, p in hits if p >= band_t]
    if not pts:
        return [], 1.0, 0.0
    best, bk, bo = [pts[0]], 1.0, float(pts[0][1] - pts[0][0])
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            (a1, b1), (a2, b2) = pts[i], pts[j]
            if a2 == a1:
                continue
            k = (b2 - b1) / (a2 - a1)
            if not (K_RANGE[0] <= k <= K_RANGE[1]):
                continue
            off = b1 - k * a1
            inl = [(a, b) for a, b in pts if abs(b - (off + k * a)) <= LINE_TOL + 0.01 * abs(a - a1)]
            if len(inl) > len(best):
                best, bk, bo = inl, k, off
    return best, bk, bo


@dataclass
class Verdict:
    hits: int            # collinear band hits
    bands: int           # informative bands of A
    cov_a: float         # share of A's informative bands on the line
    k: float
    off: float           # B row where A's row 0 lands

    def proven(self) -> bool:
        return self.hits >= MIN_HITS and self.cov_a >= COVER


def verdict(a: Fingerprint, b: Fingerprint) -> Verdict:
    """How much of A lies, in order, inside B. Missing evidence (no informative
    band) gives cov_a 0, i.e. unproven, never a pass."""
    hits = band_hits(a, b)
    inl, k, off = collinear(hits)
    return Verdict(hits=len(inl), bands=len(hits),
                   cov_a=(len(inl) / len(hits)) if hits else 0.0, k=k, off=off)


def match(a: Fingerprint, b: Fingerprint) -> Tuple[bool, Verdict, Verdict]:
    """Same chapter both ways: most of A lies inside B AND most of B inside A. The
    second direction is what rejects a recap quoting old panels and an episode
    that opens with the previous one's last panel."""
    ab, ba = verdict(a, b), verdict(b, a)
    return ab.proven() and ba.proven(), ab, ba


# ---------------------------------------------------------------- rescue proofs

@dataclass
class Proof:
    ok: bool
    why: str                                  # one line for the log / abort message
    runs: List[Tuple[int, int, float]] = field(default_factory=list)  # (first, last, cov)


def _crop(fp: Fingerprint, lo: float, hi: float) -> Fingerprint:
    lo_i, hi_i = max(0, int(lo)), max(0, int(hi))
    return Fingerprint(profile=fp.profile[lo_i:hi_i], page_rows=[])


def prove_partial(partial: ChapterPages, cand: Fingerprint) -> Proof:
    """Does the alt chapter `cand` hold the content of the pages this chapter's
    failed attempts DID deliver? (plan tier T1.)

    The present pages split into contiguous runs at the gaps; stitching across a
    gap would invent a seam no copy has. Every run with enough bands to say
    anything must be found in the candidate (coverage >= COVER, collinear), the runs
    must land in page order, a run starting at page 1 must land at the
    candidate's top, and the stretch of the candidate each run covers must in turn
    lie inside that run (both directions, as in `match`). Runs too short to be
    informative are ignored; if the informative ones together carry fewer than
    MIN_PARTIAL_BANDS bands, the answer is unproven.

    Not checked here: a candidate that is LONGER than the chapter (an alt entry
    holding N and N+1 together). That stores extra content, not wrong content; the
    one-to-many case is plan P2's."""
    runs = partial.runs()
    fps = [(r, partial.fingerprint(r)) for r in runs]
    useful = [(r, f) for r, f in fps if f.informative]
    total = sum(len(f.bands()) for _, f in useful)
    if total < MIN_PARTIAL_BANDS:
        return Proof(False, f"own pages too few to prove ({total} informative bands, "
                            f"{len(partial.pages)}/{partial.pages_total} pages)")
    last_off = -1.0
    out = []
    for r, f in useful:
        v = verdict(f, cand)
        if not v.proven():
            return Proof(False, f"pages {r[0]}-{r[-1]} not found in candidate "
                                f"(coverage {v.cov_a:.2f})", out)
        if r[0] == 1 and v.off > PREFIX_SLACK:
            return Proof(False, f"chapter start lands {v.off / PROFILE_W:.1f} widths "
                                f"into the candidate", out)
        if v.off < last_off:
            return Proof(False, f"pages {r[0]}-{r[-1]} land out of order", out)
        back = verdict(_crop(cand, v.off, v.off + v.k * len(f.profile)), f)
        if not back.proven():
            return Proof(False, f"candidate stretch for pages {r[0]}-{r[-1]} not in "
                                f"them (coverage {back.cov_a:.2f})", out)
        last_off = v.off
        out.append((r[0], r[-1], round(v.cov_a, 2)))
    return Proof(True, f"own pages found in order ({len(useful)} run(s), {total} bands)", out)


def head_can_judge(partial: ChapterPages) -> bool:
    """Can `head_matches` ever answer for this chapter's own pages, whatever the
    candidate? Needs page 1 and an opening run informative and longer than the
    credit-page slack. False lets the rescue search skip fetching candidate
    heads it could only answer None for (aio-dl.py, grep head_can_judge)."""
    if 1 not in partial.pages:
        return False
    b = partial.fingerprint(partial.runs()[0])
    return b.informative and b.length_widths >= PREFIX_SLACK / PROFILE_W


def head_matches(partial: ChapterPages, head: ChapterPages) -> Optional[bool]:
    """Cheap pre-check before downloading a whole alt candidate: do the candidate's
    first pages appear in the opening run of this chapter's own pages? None when it
    cannot tell (page 1 not among the own pages, or either side uninformative);
    the caller then pays for the full candidate and `prove_partial` decides.
    Only a False skips a candidate, and False means the candidate's opening
    content is absent from this chapter's opening, which no neighbouring chapter
    and no recap passes (P0: other chapters <= 0.09 coverage). No offset rule: a
    title card one copy has and the other lacks shifts the start legitimately."""
    if not head.pages or not head_can_judge(partial):
        return None
    a = head.fingerprint()
    b = partial.fingerprint(partial.runs()[0])
    if not a.informative:
        return None
    # The head can only be absent from the opening run if that run is long enough
    # to have held it; a run cut short by a gap says nothing (plus slack for the
    # credit pages an alt adds in front).
    if b.length_widths < a.length_widths + PREFIX_SLACK / PROFILE_W:
        return None
    return verdict(a, b).proven()


# ---------------------------------------------------------------- placement checks
# Plan rev 3 D3: a chapter the primary delivered no pages for is PLACED: the alt
# entry the count from its neighbours' copies points at, downloaded and checked.
# These are the content tests placement rests on.

PROBE_PAGES = 4          # an identifying probe: this many consecutive middle pages
PROBE_PAGES_WIDE = 6     # ... widened to this many when they carry < MIN_HITS bands
                         # (M1: opening pages are ambiguous, recaps quote old panels)


def middle_pages(n: int, k: int) -> List[int]:
    """1-based indices of the k consecutive pages at the middle of an n-page
    chapter (all of them when n <= k). The k = PROBE_PAGES_WIDE run holds the
    k = PROBE_PAGES one, so widening fetches two more pages, not six."""
    k = min(k, n)
    st = max(0, n // 2 - k // 2)
    return list(range(st + 1, st + k + 1))


def probe_path(series_dir: str, site: str, chap) -> str:
    """`<series>/.aio_fp/<site>/<chap>.probe.npz`: the middle pages a placement
    fetched of a primary chapter not downloaded yet (or downloaded before
    fingerprints existed). Never read as the chapter's full fingerprint."""
    return os.path.join(series_dir, ".aio_fp", chap_key(site), chap_key(chap) + ".probe.npz")


def probe_names(probe: Fingerprint, ours: Fingerprint, ours_is_probe: bool) -> bool:
    """Does an alt entry's middle-page probe show it is our chapter `ours`? Its
    pages must lie inside our copy; when our side is itself only a probe (4
    middle pages of a chapter not downloaded yet), our probe lying inside theirs
    counts too, since two 4-page runs from re-sliced sites rarely cover the same
    stretch. Placement asks this of every chapter within
    rescue_pairing.IDENTIFY_WINDOW and accepts only a unique yes."""
    return verdict(probe, ours).proven() or (ours_is_probe and verdict(ours, probe).proven())


def holds_other(cand: Fingerprint, other: Fingerprint) -> bool:
    """The ±15 veto's test: is most of `cand` inside the primary chapter `other`,
    or most of `other` inside `cand`? Either means the candidate is (or holds)
    another chapter's content. One direction is enough on purpose: an alt entry
    that merges two chapters, or a short entry that is a slice of a long one,
    must not fill a third. `other` may be a 4-page probe: it then lies inside a
    candidate that holds that chapter."""
    return verdict(cand, other).proven() or verdict(other, cand).proven()
