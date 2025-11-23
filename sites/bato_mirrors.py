"""Alternative domains / mirrors for Bato."""

from __future__ import annotations

_BASE_MIRRORS = (
    "ato.to",
    "bato.ac",
    "bato.bz",
    "bato.cc",
    "bato.cx",
    "bato.day",
    "bato.id",
    "bato.ing",
    "bato.pw",
    "bato.red",
    "bato.run",
    "bato.sh",
    "bato.si",
    "bato.to",
    "bato.vc",
    "batocomic.com",
    "batocomic.net",
    "batocomic.org",
    "batoto.in",
    "batoto.tv",
    "batotoo.com",
    "batotwo.com",
    "batpub.com",
    "batread.com",
    "battwo.com",
    "comiko.net",
    "comiko.org",
    "dto.to",
    "fto.to",
    "hto.to",
    "jto.to",
    "kuku.to",
    "lto.to",
    "mangatoto.com",
    "mangatoto.net",
    "mangatoto.org",
    "mto.to",
    "nto.to",
    "okok.to",
    "readtoto.com",
    "readtoto.net",
    "readtoto.org",
    "ruru.to",
    "vba.to",
    "vto.to",
    "wba.to",
    "wto.to",
    "xbato.com",
    "xbato.net",
    "xbato.org",
    "xba.to",
    "xdxd.to",
    "xto.to",
    "yba.to",
    "yto.to",
    "zbato.com",
    "zbato.net",
    "zbato.org",
    "zba.to",
)


def _with_www(domains):
    """Return tuple with original + www-prefixed forms."""
    seen = set()
    ordered = []
    for domain in domains:
        if domain not in seen:
            ordered.append(domain)
            seen.add(domain)
        if not domain.startswith("www."):
            www_variant = f"www.{domain}"
            if www_variant not in seen:
                ordered.append(www_variant)
                seen.add(www_variant)
    return tuple(ordered)


BATO_MIRRORS = _with_www(_BASE_MIRRORS)

__all__ = ["BATO_MIRRORS"]
