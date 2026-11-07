"""Coverage for aio_android.build_argv — the Android CLI-argument builder.

This is a behavioural port of UI-source/electron/downloader.js:buildCliArgs, and
these tests are the reason the port lives in Python rather than Kotlin: the
guards are load-bearing (aio-dl.py HARD-ERRORS on several flag combinations) and
here they can be checked offline, with no device and no rebuild.

Every "blocked"/"incompatible" case below corresponds to a real Python-side
hard error or a silently-wrong output. If one of these starts failing, do NOT
relax the test — check downloader.js first, since the two must agree.
"""

from __future__ import annotations

import contextlib
import io
import json

import pytest

import aio_android
from aio_android import (
    UnsupportedUrlError,
    _BOOL_FLAGS,
    _VALUED_FLAGS,
    build_argv,
    build_argv_json,
)


def _pairs(argv):
    """{flag: value} for valued flags, so assertions don't depend on ordering."""
    out = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--") and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            out[argv[i]] = argv[i + 1]
            i += 2
        else:
            i += 1
    return out


# --------------------------------------------------------------------------
# Valued flags
# --------------------------------------------------------------------------

def test_empty_settings_produce_no_args():
    assert build_argv({}) == []


def test_valued_flags_are_emitted():
    argv = build_argv({"format": "cbz", "language": "en", "jobs": 3})
    assert _pairs(argv) == {"--format": "cbz", "--language": "en", "--jobs": "3"}


@pytest.mark.parametrize("empty", [None, ""])
def test_empty_values_are_skipped(empty):
    assert build_argv({"format": empty}) == []


def test_zero_is_emitted_not_treated_as_empty():
    # The trap: a truthiness filter would drop 0, and 0 is meaningful for
    # several knobs (e.g. --net-min-gap 0).
    assert _pairs(build_argv({"netMinGap": 0})) == {"--net-min-gap": "0"}


def test_default_valued_flags_are_skipped():
    # Both are the Python-side defaults; emitting them is pure noise.
    assert build_argv({"chapters": "all", "mtl": "avoid"}) == []
    assert _pairs(build_argv({"chapters": "1-5", "mtl": "exclude"})) == {
        "--chapters": "1-5",
        "--mtl": "exclude",
    }


def test_url_is_appended_last_as_positional():
    argv = build_argv({"format": "cbz", "url": "https://example.com/x"})
    assert argv[-1] == "https://example.com/x"


def test_blank_url_is_not_appended():
    assert build_argv({"url": "   "}) == []


# --------------------------------------------------------------------------
# Boolean flags
# --------------------------------------------------------------------------

def test_bool_flag_requires_exactly_true():
    assert build_argv({"verbose": True}) == ["--verbose"]
    for falsey in (False, None, 0, "", "true", 1):
        assert build_argv({"verbose": falsey}) == [], f"{falsey!r} must not enable the flag"


def test_prompt_urls_is_never_emitted():
    # It makes aio-dl.py read from stdin, which does not exist under Chaquopy.
    assert build_argv({"promptUrls": True}) == []


# --------------------------------------------------------------------------
# --webtoon-recompress compatibility
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", ["pdf", "none"])
def test_webtoon_recompress_stripped_on_incompatible_format(fmt):
    argv = build_argv({"format": fmt, "webtoonRecompress": True})
    assert "--webtoon-recompress" not in argv


@pytest.mark.parametrize("fmt", ["cbz", "epub"])
def test_webtoon_recompress_kept_on_archive_formats(fmt):
    assert "--webtoon-recompress" in build_argv({"format": fmt, "webtoonRecompress": True})


def test_komikku_rescues_webtoon_recompress_on_pdf():
    # --komikku coerces format->cbz BEFORE aio-dl.py's compatibility check.
    argv = build_argv({"format": "pdf", "komikku": True, "webtoonRecompress": True})
    assert "--webtoon-recompress" in argv


def test_webtoon_knobs_only_when_non_default_and_enabled():
    at_defaults = _pairs(build_argv({"format": "cbz", "webtoonRecompress": True,
                                     "webtoonRecompressQuality": 85, "webtoonRecompressMethod": 4}))
    assert "--webtoon-recompress-quality" not in at_defaults
    assert "--webtoon-recompress-method" not in at_defaults
    got = _pairs(build_argv({"format": "cbz", "webtoonRecompress": True,
                             "webtoonRecompressQuality": 70, "webtoonRecompressMethod": 6}))
    assert got["--webtoon-recompress-quality"] == "70"
    assert got["--webtoon-recompress-method"] == "6"
    # Knobs must not leak out when the master toggle is off.
    assert build_argv({"format": "cbz", "webtoonRecompressQuality": 70}) == ["--format", "cbz"]


# --------------------------------------------------------------------------
# --modernize compatibility (all seven blocking conditions)
# --------------------------------------------------------------------------

def test_modernize_allowed_on_plain_cbz():
    assert "--modernize" in build_argv({"format": "cbz", "modernize": True})


@pytest.mark.parametrize("extra", [
    {"format": "epub"},                    # stricter than webtoon: cbz only
    {"format": "cbz", "quality": 90},      # <100 disables the byte fast-path
    {"format": "cbz", "scaling": 50},
    {"format": "cbz", "cbzPreserveOriginals": False},
    {"format": "cbz", "noProcessing": True},
    {"format": "cbz", "width": 800},
    {"format": "cbz", "aspectRatio": "16:9"},
])
def test_modernize_stripped_when_fast_path_unsatisfiable(extra):
    argv = build_argv({"modernize": True, **extra})
    assert "--modernize" not in argv
    assert not [a for a in argv if a.startswith("--modernize")], "no knob may survive either"


def test_modernize_komikku_overrides_format_but_not_other_blockers():
    assert "--modernize" in build_argv({"format": "epub", "komikku": True, "modernize": True})
    assert "--modernize" not in build_argv(
        {"format": "epub", "komikku": True, "modernize": True, "noProcessing": True}
    )


def test_modernize_quality_100_and_scaling_100_do_not_block():
    argv = build_argv({"format": "cbz", "modernize": True, "quality": 100, "scaling": 100})
    assert "--modernize" in argv


def test_modernize_reversible_forces_the_jxl_distance0_pair():
    # A PAIR because `auto` + distance 0 is NOT reversible — auto still routes
    # color pages to the always-lossy AVIF branch.
    got = _pairs(build_argv({
        "format": "cbz", "modernize": True, "modernizeReversible": True,
        "modernizeFormat": "avif", "modernizeDistance": 3, "modernizeAvifSpeed": 2,
    }))
    assert got["--modernize-format"] == "jxl"
    assert got["--modernize-distance"] == "0"
    # The stored routing knobs must be ignored, not merged.
    assert "--modernize-avif-speed" not in got


def test_modernize_knobs_skip_defaults_but_emit_zero_speed():
    base = {"format": "cbz", "modernize": True}
    at_defaults = _pairs(build_argv({**base, "modernizeFormat": "auto", "modernizeDistance": 1.0,
                                     "modernizeQuality": 90, "modernizeAvifSpeed": 6,
                                     "modernizeMinSaving": 0.92, "modernizeEffort": 7}))
    assert not [k for k in at_defaults if k.startswith("--modernize-")], at_defaults
    # speed 0 is a valid non-default (slowest/smallest) — a truthiness test would drop it.
    assert _pairs(build_argv({**base, "modernizeAvifSpeed": 0}))["--modernize-avif-speed"] == "0"


def test_modernize_min_saving_and_effort_apply_under_reversible_too():
    got = _pairs(build_argv({
        "format": "cbz", "modernize": True, "modernizeReversible": True,
        "modernizeMinSaving": 0.5, "modernizeEffort": 9,
    }))
    assert got["--modernize-min-saving"] == "0.5"
    assert got["--modernize-effort"] == "9"


# --------------------------------------------------------------------------
# Negative-default and absent-means-on flags
# --------------------------------------------------------------------------

def test_cbz_preserve_originals_only_negates_on_explicit_false():
    assert "--no-cbz-preserve-originals" in build_argv({"cbzPreserveOriginals": False})
    for v in (True, None, "no-such-field"):
        assert "--no-cbz-preserve-originals" not in build_argv({"cbzPreserveOriginals": v})


def test_multi_source_lazy_is_absent_means_on():
    assert "--multi-source-lazy" in build_argv({"multiSource": True})
    assert "--multi-source-lazy" in build_argv({"multiSource": True, "multiSourceLazy": True})
    # Only an explicit false suppresses it.
    assert "--multi-source-lazy" not in build_argv(
        {"multiSource": True, "multiSourceLazy": False}
    )
    # Never emitted without the multi-source opt-in.
    assert "--multi-source-lazy" not in build_argv({"multiSourceLazy": True})


def test_collapse_splits_is_opt_in():
    assert build_argv({"collapseSplits": True}) == ["--collapse-splits"]
    assert build_argv({"collapseSplits": False}) == []


def test_disabled_sites_joined_with_commas():
    assert _pairs(build_argv({"disabledSites": ["comix", "asura"]})) == {
        "--disable-sites": "comix,asura"
    }
    assert build_argv({"disabledSites": []}) == []
    assert build_argv({"disabledSites": None}) == []


# --------------------------------------------------------------------------
# JNI wrapper
# --------------------------------------------------------------------------

def test_build_argv_json_round_trip():
    out = build_argv_json(json.dumps({"format": "cbz", "url": "https://e/x"}))
    assert json.loads(out) == ["--format", "cbz", "https://e/x"]


@pytest.mark.parametrize("bad,exc", [
    ("not json", ValueError),
    ("[1,2]", TypeError),
    ("42", TypeError),
])
def test_build_argv_json_rejects_bad_payloads(bad, exc):
    with pytest.raises(exc):
        build_argv_json(bad)


# --------------------------------------------------------------------------
# Flags added in Wave 2 (android/PARITY.md §3.4 "no table entry at all")
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key,flag", [
    ("noSidecarAssets", "--no-sidecar-assets"),
    ("noGroupFallback", "--no-group-fallback"),
    ("downloadVolumes", "--download-volumes"),
])
def test_new_bool_flags_are_emitted_and_require_exactly_true(key, flag):
    assert build_argv({key: True}) == [flag]
    for falsey in (False, None, 0, "", "true", 1):
        assert build_argv({key: falsey}) == []


def test_anilist_enrichment_is_reachable_from_the_settings_dict():
    """The FLAG half of "AniList enrichment is off and cannot be turned on".
    The env half is configure(metadata_source=…) — see
    tests/test_android_repair.py."""
    got = _pairs(build_argv({
        "metadataSource": "anilist", "metadataTagMinRank": 80,
    }))
    assert got == {"--metadata-source": "anilist", "--metadata-tag-min-rank": "80"}
    assert build_argv({"metadataRefresh": True}) == ["--metadata-refresh"]


def test_epub_dir_is_emitted_verbatim():
    """It moves the EPUB artifact only — the series metadata stays under
    --output-dir, which is why aio-dl.py keys final-file coverage per format."""
    assert _pairs(build_argv({"epubDir": "/storage/emulated/0/Books"})) == {
        "--epub-dir": "/storage/emulated/0/Books"
    }


@pytest.mark.parametrize("key,flag,default,other", [
    ("chapterDeadlineSeconds", "--chapter-deadline-seconds", 90, 180),
    ("chapterHostPoisonThreshold", "--chapter-host-poison-threshold", 5, 8),
    ("inlineChapterRetries", "--inline-chapter-retries", 2, 4),
    ("inlineChapterBackoff", "--inline-chapter-backoff", 30, 15),
])
def test_watchdog_knobs_skip_their_python_default(key, flag, default, other):
    """Each of these argparse defaults is itself read from an env var
    (AIO_CHAPTER_DEADLINE and friends), so emitting the default would silently
    BEAT an env override. Omitting it lets the override stand."""
    assert build_argv({key: default}) == []
    # A string carrying the same number is still the default — the reference's
    # bare !== would emit it.
    assert build_argv({key: str(default)}) == []
    assert _pairs(build_argv({key: other})) == {flag: str(other)}


def test_watchdog_zero_is_a_real_value_not_a_default():
    """0 disables the deadline and the poison threshold outright, and 0 retries
    means "abort on the first failed chapter". A truthiness filter drops all
    three."""
    got = _pairs(build_argv({
        "chapterDeadlineSeconds": 0,
        "chapterHostPoisonThreshold": 0,
        "inlineChapterRetries": 0,
    }))
    assert got == {
        "--chapter-deadline-seconds": "0",
        "--chapter-host-poison-threshold": "0",
        "--inline-chapter-retries": "0",
    }


def test_non_numeric_value_for_a_numeric_knob_is_dropped():
    """argparse would hard-error on it (type=float) and kill the run, so the
    safe direction is to omit and let Python apply its own default."""
    assert build_argv({"chapterDeadlineSeconds": "soon"}) == []


def test_every_table_flag_is_a_real_aio_dl_option():
    """A key whose flag does not exist is not a typo that throws — it is a run
    that dies at argparse. Checked against --help's own text rather than a
    hand-kept list."""
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    help_text = subprocess.run(
        [sys.executable, str(repo / "aio-dl.py"), "--help"],
        capture_output=True, text=True, cwd=str(repo), timeout=300,
    ).stdout
    for flag in list(_VALUED_FLAGS.values()) + list(_BOOL_FLAGS.values()):
        assert flag in help_text, f"{flag} is not an aio-dl.py option"


# --------------------------------------------------------------------------
# --group / --exclude-group: nargs="+" and the swallowed positional
# --------------------------------------------------------------------------

def test_group_emits_one_attached_flag_per_name():
    assert build_argv({"group": "Team A, Team B"}) == ["--group=Team A", "--group=Team B"]
    assert build_argv({"excludeGroup": "Bad TL"}) == ["--exclude-group=Bad TL"]


def test_group_accepts_a_list_as_well_as_a_comma_string():
    """The form field is free text today; a chip picker would hand over a list."""
    assert build_argv({"group": ["A", "B"]}) == ["--group=A", "--group=B"]


def test_group_drops_blank_names():
    # "A, " is what a trailing comma in the text field produces.
    assert build_argv({"group": "A, ,  B,"}) == ["--group=A", "--group=B"]
    assert build_argv({"group": " , "}) == []


def test_group_name_starting_with_a_dash_survives():
    """The attached form is what makes this parseable at all — detached, it
    would read as an unknown option."""
    assert build_argv({"group": "-Odd"}) == ["--group=-Odd"]


def _refresh_probe(extra_argv, library_dir):
    """Run --refresh-library-metadata against an EMPTY library and return its
    stdout.

    A real-parser probe with no network in it: that mode returns right after a
    directory scan, and its optional positional lands in the SAME `comic_url`
    dest a download URL does — so it answers "did an option swallow the
    positional" without downloading anything.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        aio_android._run_engine(
            ["--refresh-library-metadata", "-o", str(library_dir), *extra_argv]
        )
    return buffer.getvalue()


def test_group_flag_does_not_swallow_the_positional(tmp_path):
    """THE REGRESSION THIS SHAPE EXISTS TO FIX, against the real argparse.

    `--group` is declared nargs="+", so the detached `--group A <positional>`
    form makes argparse consume the positional as a second group name. On a
    download that is fatal: aio-dl.py exits with "You must provide at least one
    URL". It is reachable from an ordinary form — a user who sets a preferred
    group and leaves every other knob at its default emits exactly that.
    """
    library = tmp_path / "manga"
    library.mkdir()

    fixed = _refresh_probe(build_argv({"group": "Team A"}) + ["MyFilter"], library)
    assert "matching ['myfilter']" in fixed, fixed

    # The pre-fix emission, to prove the probe can actually see the failure.
    broken = _refresh_probe(["--group", "Team A", "MyFilter"], library)
    assert "matching" not in broken, broken


def test_exclude_group_flag_does_not_swallow_the_positional(tmp_path):
    library = tmp_path / "manga"
    library.mkdir()
    out = _refresh_probe(build_argv({"excludeGroup": "Bad TL"}) + ["MyFilter"], library)
    assert "matching ['myfilter']" in out, out


# --------------------------------------------------------------------------
# D7 — a pasted comix URL cannot start a download here
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://comix.to/title/abc-some-slug",
    "https://www.comix.to/title/abc",
    "http://comix.to/",
    "https://COMIX.TO/title/abc",
    "https://cdn.comix.to/title/abc",
    "https://comix.to:443/title/abc",
])
def test_comix_urls_are_refused(url):
    """--disable-sites cannot do this: it explicitly exempts a directly
    downloaded URL, so a pasted link would reach an engine that has no
    Patchright to drive."""
    with pytest.raises(UnsupportedUrlError) as excinfo:
        build_argv({"url": url})
    assert excinfo.value.site == "comix"


@pytest.mark.parametrize("url", [
    "https://mangadex.org/title/abc",
    "https://weebcentral.com/series/abc",
    # Adjacent hostnames that merely CONTAIN the name must still work — the
    # check is host-scoped, not a substring match.
    "https://comix.to.example.com/x",
    "https://notcomix.to/x",
    "https://example.com/comix.to/x",
])
def test_other_urls_still_pass(url):
    assert build_argv({"url": url})[-1] == url


def test_a_settings_blob_with_no_url_is_never_refused():
    """The library update path builds argv without a positional in some flows;
    refusing an empty URL would break every one of them."""
    assert build_argv({"format": "cbz"}) == ["--format", "cbz"]
    assert build_argv({"url": "", "format": "cbz"}) == ["--format", "cbz"]


def test_build_argv_json_returns_a_renderable_object_for_a_refusal():
    """Kotlin discriminates on the first character: `[` runs, `{` is shown. A
    raise would cross JNI as a stringified traceback, which is a crash rather
    than something a Compose screen can render."""
    raw = build_argv_json(json.dumps({"url": "https://comix.to/title/abc"}))
    assert raw.startswith("{")
    payload = json.loads(raw)
    assert payload["error"] == "unsupported_site"
    assert payload["site"] == "comix"
    assert payload["url"] == "https://comix.to/title/abc"
    # A finished sentence, so the UI renders it rather than composing wording.
    assert payload["message"].endswith(".")
    assert len(payload["message"]) > 40


def test_build_argv_json_success_is_still_a_bare_array():
    raw = build_argv_json(json.dumps({"format": "cbz", "url": "https://e/x"}))
    assert raw.startswith("[")
    assert json.loads(raw) == ["--format", "cbz", "https://e/x"]


def test_realistic_komikku_download_matches_expected_command_line():
    """End-to-end shape check against a plausible real settings blob."""
    argv = build_argv({
        "format": "cbz", "language": "en", "chapters": "all", "mtl": "avoid",
        "komikku": True, "multiSource": True, "imageWorkers": 8,
        "cbzPreserveOriginals": True, "disabledSites": ["comix"],
        "url": "https://mangadex.org/title/abc",
    })
    assert argv == [
        "--format", "cbz",
        "--language", "en",
        "--image-workers", "8",
        "--multi-source",
        "--komikku",
        "--multi-source-lazy",
        "--disable-sites", "comix",
        "https://mangadex.org/title/abc",
    ]
