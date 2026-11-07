"""Item kinds: what a download run is made of (chapters or native volumes).

What this module owns
---------------------
The ONE place that decides how a run's item kind turns into on-disk names:
tmp dir, per-item file name, raw-image folder name, and which pipeline
features apply to that kind. A run downloads exactly one kind, picked from
``--download-volumes`` right after argparse (grep ``space_for`` in aio-dl.py),
and every storage key comes from that choice. So "volume 3 written over
chapter 3" cannot be expressed by the caller; it isn't something a later check
has to catch.

Why one kind per run, not a kind tag on every item: the pool already comes
from either ``get_chapters`` or ``get_volumes`` (never both), and the downstream
pipeline reads ``ch["chap"]`` in hundreds of places. Deriving every key from
the run's kind keeps those reads correct as they are. The per-item ``_kind``
tag (stamped by ``BaseSiteHandler.list_volumes``) is used only for image
routing (``BaseSiteHandler.get_item_images``) and for one consistency assert.

Who reads it
------------
- aio-dl.py: tmp dir, filenames, keep-images folder, coercions (grep ``space.``)
- library_state.py: volume file scan (``parse_volume_number_from_name``)
- series_ledger.py: the kind names used as ledger namespaces
- aio_android.py: tmp dir scan for resume (``parse_tmp_dir_name``)
- JS twins: UI-source/electron/series-ledger.js (parseTmpDirName,
  VOLUME_FILE_RE). Keep the two in step; the shared cases live in
  tests/fixtures/series_ledger_cases.json.

Depends on: stdlib only. It is imported by the Android engine and by
library_state, neither of which can pull in aio-dl.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

CHAPTER = "chapter"
VOLUME = "volume"
KINDS = (CHAPTER, VOLUME)


def tilde_decimal(label: str) -> str:
    """Rewrite the LAST ``digit.digit`` in *label* as ``digit~digit``.

    Same rule as aio-dl.py's ``format_chap_for_filename`` (grep
    ``_DECIMAL_DOT_LAST_RE``): ``~`` makes lexical order match numeric order
    ("1" < "1~5" < "2"). Readers reverse it (library.js, library_state.py).
    """
    return re.sub(r"(\d)\.(\d)(?!.*\d\.\d)", r"\1~\2", str(label).strip(), count=1)


def _join_name(*parts: str) -> str:
    # Same normalization as aio-dl.py main()'s join_name closure: single spaces,
    # no trailing dots/spaces (Windows rejects them).
    s = " ".join(p for p in parts if p)
    s = re.sub(r"\s+", " ", s).strip()
    return s.rstrip(" .")


def _pad_volume(label: str) -> str:
    """``"1"`` -> ``"01"``, ``"117"`` -> ``"117"``, ``"2.5"`` -> ``"02~5"``.

    Two-digit padding keeps the usual 1-99 range in lexical order. The
    integer part is padded and the decimal part is kept after ``~``.
    A non-numeric label is passed through unchanged, though
    ``BaseSiteHandler.list_volumes`` already drops those.
    """
    text = str(label).strip()
    m = re.fullmatch(r"(\d+)(?:\.(\d+))?", text)
    if not m:
        return tilde_decimal(text)
    whole = m.group(1).lstrip("0") or "0"
    out = whole.zfill(2)
    if m.group(2):
        out += "~" + m.group(2)
    return out


@dataclass(frozen=True)
class ItemSpace:
    """Everything that differs between a chapter run and a volume run.

    The feature flags say which pipeline stages apply to this kind. They are
    data, not checks, so a stage can't forget to ask:
      combined_archive: build the series-wide `{title}.{fmt}` archive.
        Volume runs never do. Volumes are already whole books, and sharing that
        file name would collide with the chapter archive.
      multi_source: cross-site discovery and rescue. Other sites list chapters,
        not this site's volume scans, so there is nothing to rescue from.
      collapse_splits: the chapter-fragment merger. Volume numbers have no
        fragments.
      fingerprints: `.aio_fp/<site>/<n>.npz`. Their only reader is multi-source
        rescue, so volumes skip them; writing them would overwrite chapter n's
        evidence.
      prefetch_next: prefetch the next item's images during the current one.
        Off for volumes: overlapping two 200-page fetches doubles the load for
        little gain.
    """

    kind: str
    noun: str                 # "Chapter" / "Volume" (log + progress lines)
    tmp_prefix: str           # "tmp_" / "tmpvol_"
    raw_image_dir_prefix: str  # "Chapter_" / "Volume_" under images/
    combined_archive: bool
    multi_source: bool
    collapse_splits: bool
    fingerprints: bool
    prefetch_next: bool

    @property
    def is_volume(self) -> bool:
        return self.kind == VOLUME

    def tmp_dir_name(self, hid: str) -> str:
        return f"{self.tmp_prefix}{hid}"

    def raw_image_dir_name(self, label: str) -> str:
        return f"{self.raw_image_dir_prefix}{label}"

    def item_filename(self, base: str, label: str, fmt: str) -> str:
        """Standard (non-Komikku) per-item archive name.

        chapter: ``"{base} Ch {n}.{fmt}"`` (same as aio-dl.py's ch_filename)
        volume:  ``"{base} Vol.{NN}.{fmt}"``

        The volume form has no "Ch" anywhere, so neither chapter parser
        (library_state.CHAPTER_FILE_RE, library.js KOMIKKU_CH_RE and the
        " Ch " marker) can read it as a chapter. Komikku runs never reach the
        volume branch, because --komikku forces volume mode off.
        """
        if self.is_volume:
            stem = _join_name(base, f"Vol.{_pad_volume(label)}")
        else:
            stem = _join_name(base, f"Ch {tilde_decimal(label)}")
        return f"{stem}.{fmt}"


CHAPTERS = ItemSpace(
    kind=CHAPTER,
    noun="Chapter",
    tmp_prefix="tmp_",
    raw_image_dir_prefix="Chapter_",
    combined_archive=True,
    multi_source=True,
    collapse_splits=True,
    fingerprints=True,
    prefetch_next=True,
)

VOLUMES = ItemSpace(
    kind=VOLUME,
    noun="Volume",
    tmp_prefix="tmpvol_",
    raw_image_dir_prefix="Volume_",
    combined_archive=False,
    multi_source=False,
    collapse_splits=False,
    fingerprints=False,
    prefetch_next=False,
)


def space_for(download_volumes: bool) -> ItemSpace:
    return VOLUMES if download_volumes else CHAPTERS


def space_of(kind: str) -> ItemSpace:
    if kind == VOLUME:
        return VOLUMES
    if kind == CHAPTER:
        return CHAPTERS
    raise ValueError(f"unknown item kind: {kind!r}")


def parse_tmp_dir_name(name: str) -> Optional[Tuple[str, str]]:
    """``"tmpvol_<hid>"`` -> ``(hid, "volume")``, ``"tmp_<hid>"`` -> ``(hid, "chapter")``.

    The longer prefix is checked first. ``tmpvol_`` does not start with
    ``tmp_`` (there is no underscore after "tmp"), so a chapter hid that
    happens to begin with "vol_" (``tmp_vol_x``) still reads as a chapter dir.
    Older scanners that only know ``startsWith("tmp_")`` ignore volume dirs
    instead of misreading them.
    """
    if name.startswith(VOLUMES.tmp_prefix) and len(name) > len(VOLUMES.tmp_prefix):
        return name[len(VOLUMES.tmp_prefix):], VOLUME
    if name.startswith(CHAPTERS.tmp_prefix) and len(name) > len(CHAPTERS.tmp_prefix):
        return name[len(CHAPTERS.tmp_prefix):], CHAPTER
    return None


# A volume archive stem ENDS in "Vol.<NN>[~d]", preceded by start-of-name or a
# space. Anchoring at the end is what rejects Komikku's chapter form
# "Vol.01 Ch.005", where Vol is only the chapter's volume membership.
# JS twin: UI-source/electron/series-ledger.js VOLUME_FILE_RE.
VOLUME_FILE_RE = re.compile(r"(?:^|\s)Vol\.(\d+(?:~\d+)?)$", re.IGNORECASE)
RAW_VOLUME_DIR_RE = re.compile(r"^Volume_([0-9]+(?:[.~][0-9]+)?)$", re.IGNORECASE)


def _normalize_number(token: str) -> Optional[str]:
    text = token.replace("~", ".")
    try:
        f = float(text)
    except ValueError:
        return None
    # :g matches --list-chapters' label form (grep _chap_label_str), so the
    # ledger and the update-check diff compare equal strings.
    return f"{f:g}"


def parse_volume_number_from_name(filename: str, base: Optional[str] = None) -> Optional[str]:
    """Volume label from a per-volume archive file name, or None.

    *base* (the series' sanitized base name), when known, must be exactly the
    text before " Vol.". This rules out a combined archive whose series title
    itself ends in "Vol.3". Callers without it accept any prefix. The
    ledger is the primary record; this is the file-based fallback.
    """
    # Strip only a KNOWN archive extension. A generic "\.\w+$" strip would eat
    # the ".01" of an extensionless "Title Vol.01".
    stem = re.sub(r"\.(?:cbz|pdf|epub|zip)$", "", filename, flags=re.IGNORECASE)
    m = VOLUME_FILE_RE.search(stem)
    if not m:
        return None
    if base is not None:
        prefix = stem[: m.start()].strip()
        if prefix != base.strip():
            return None
    return _normalize_number(m.group(1))
