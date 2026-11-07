"""Coverage for aio_android's cross-site search API — the Python half of M6.

WHY OFFLINE: a real search fans out to ~40 sites and takes 40-100 seconds, so
none of the decisions worth testing (which flags get emitted, what happens when
a download holds the engine, how a payload is recovered from a log-polluted
buffer) are practical to check on a device. Each of these would otherwise cost
a rebuild-install cycle and a live network.

The argv tests are the load-bearing ones. Two flags here exist for
Android-specific reasons and would look like noise to someone tidying up:
comix is force-excluded because it drives its own Patchright session rather
than going through the WebView seam, and --enable-ml-rating must NEVER be
emitted: the flag no longer exists (the ML rating was removed 2026-10-21), so a
stale saved enableMlRating setting would otherwise fail every search.

Cross-file: aio_android.build_search_argv / search (the code),
UI-source/electron/searcher.js:buildSearchArgs (the reference being ported),
android/.../core/SearchRepository.kt (the caller).
"""

from __future__ import annotations

import json
import threading

import pytest

import aio_android


def _flag_value(argv, flag):
    """Value following `flag`, or None when the flag is absent."""
    return argv[argv.index(flag) + 1] if flag in argv else None


# --- argv construction -----------------------------------------------------

def test_minimal_search_argv():
    argv = aio_android.build_search_argv("frieren")
    assert argv[:3] == ["--search", "frieren", "--search-json"]


def test_comix_is_always_excluded():
    """comix never went through the browser seam (it drives Patchright
    directly), so on Android it can only burn fan-out budget failing."""
    argv = aio_android.build_search_argv("x")
    assert "comix" in _flag_value(argv, "--disable-sites").split(",")


def test_unavailable_search_sites_holds_only_search_capable_handlers():
    """The tuple feeds `--disable-sites`, which the orchestrator applies to the
    SEARCH fan-out. Naming a handler that never searches is inert — it reads
    like a guard while guarding nothing, which is what the old comment claiming
    kagane belonged here would have produced."""
    import sites

    capable = {h.name for h in sites.iter_search_capable_handlers()}
    for name in aio_android._UNAVAILABLE_SEARCH_SITES:
        assert name in capable, f"{name} is not search-capable; excluding it is a no-op"


def test_weebcentral_and_kagane_are_deliberately_absent():
    """Both were named in this tuple's own comment as belonging here, and
    neither does. Pinned so a future reader re-adding them has to argue with a
    test rather than with a comment.

    weebcentral rescues itself HERE: its ladder is `cloudscraper ->
    rescue_cf_html -> raise`, and rescue_cf_html's second tier is the embedder
    browser, i.e. this app's WebView bridge. impit is genuinely absent on
    Android, which only means the first tier no-ops.

    kagane is not search-capable on either platform — it never overrides
    BaseSiteHandler.search, so the fan-out filters it out before disable-sites
    is consulted. Its pywidevine dependency gates DOWNLOADS, a different list.
    """
    import inspect

    import sites
    from sites import weebcentral

    assert "weebcentral" not in aio_android._UNAVAILABLE_SEARCH_SITES
    assert "kagane" not in aio_android._UNAVAILABLE_SEARCH_SITES

    capable = {h.name for h in sites.iter_search_capable_handlers()}
    assert "weebcentral" in capable
    assert "kagane" not in capable
    # The seam the reasoning above rests on: if weebcentral stops going through
    # rescue_cf_html, the "it rescues itself here" claim needs re-checking and
    # this test is where that shows up.
    assert "rescue_cf_html" in inspect.getsource(weebcentral)


def test_comix_is_not_duplicated_when_the_user_already_disabled_it():
    argv = aio_android.build_search_argv("x", {"disabledSites": ["comix", "mangakatana"]})
    names = _flag_value(argv, "--disable-sites").split(",")
    assert names.count("comix") == 1
    assert "mangakatana" in names


def test_disabled_sites_accepts_a_comma_string_too():
    """The settings blob round-trips through JSON and Kotlin; a list is the
    normal shape but a comma string is what a hand-edited setting looks like."""
    argv = aio_android.build_search_argv("x", {"disabledSites": "Mangakatana, Asura "})
    names = _flag_value(argv, "--disable-sites").split(",")
    assert "mangakatana" in names and "asura" in names


def test_ml_rating_is_never_emitted():
    """aio-dl.py no longer accepts --enable-ml-rating (ML rating removed), so
    an old saved enableMlRating value must not reach the argv."""
    for settings in ({}, {"enableMlRating": True}, {"enableMlRating": "true"}):
        assert "--enable-ml-rating" not in aio_android.build_search_argv("x", settings)


def test_mobile_parallelism_is_lower_than_the_desktop_default():
    argv = aio_android.build_search_argv("x")
    assert int(_flag_value(argv, "--search-parallelism")) < aio_android._NET_DEFAULTS["searchParallelism"]


def test_explicit_parallelism_beats_the_mobile_baseline():
    argv = aio_android.build_search_argv("x", {"searchParallelism": 6})
    assert _flag_value(argv, "--search-parallelism") == "6"


@pytest.mark.parametrize("level,expected", [("low", "2"), ("balanced", "3"), ("high", "5")])
def test_resource_limit_overrides_parallelism(level, expected):
    """Resource Limits is a HARD override, so it wins even over an explicit
    user value — same semantics as the download knobs."""
    argv = aio_android.build_search_argv("x", {"searchParallelism": 6, "networkLimit": level})
    assert _flag_value(argv, "--search-parallelism") == expected


def test_unlimited_level_leaves_the_explicit_value_alone():
    argv = aio_android.build_search_argv("x", {"searchParallelism": 6, "networkLimit": "unlimited"})
    assert _flag_value(argv, "--search-parallelism") == "6"


def test_language_and_valued_flags():
    argv = aio_android.build_search_argv("x", {
        "language": "en", "searchTimeout": 90, "searchMinMatch": 0.5,
        "multiSourceQualityMin": 0.3,
    })
    assert _flag_value(argv, "--search-language") == "en"
    assert _flag_value(argv, "--search-timeout") == "90"
    assert _flag_value(argv, "--search-min-match") == "0.5"
    assert _flag_value(argv, "--multi-source-quality-min") == "0.3"


def test_blank_valued_flags_are_omitted_not_emitted_empty():
    """An empty string reaching argparse as a value is worse than an absent
    flag — it would parse as a literal empty argument."""
    argv = aio_android.build_search_argv("x", {"language": "  ", "searchTimeout": ""})
    assert "--search-language" not in argv
    assert "--search-timeout" not in argv


def test_boolean_toggles():
    argv = aio_android.build_search_argv("x", {
        "seededOnly": True, "multiSource": True, "collapseSplits": True,
    })
    for flag in ("--seeded-only", "--multi-source", "--collapse-splits"):
        assert flag in argv


def test_collapse_splits_requires_an_explicit_true():
    """Matches the desktop's `=== true`: absent/None/false all mean the safer
    OFF default, so older saved settings dicts stay off."""
    for value in (None, False, "true", 1):
        assert "--collapse-splits" not in aio_android.build_search_argv("x", {"collapseSplits": value})


def test_build_search_argv_json_round_trip():
    argv = json.loads(aio_android.build_search_argv_json("frieren", json.dumps({"language": "en"})))
    assert argv[:3] == ["--search", "frieren", "--search-json"]
    assert "--search-language" in argv


# --- search() behaviour ----------------------------------------------------

def test_empty_query_is_rejected_before_touching_the_engine(monkeypatch):
    called = []
    monkeypatch.setattr(aio_android, "_run_engine", lambda *a, **k: called.append(a))
    for query in ("", "   ", None):
        assert json.loads(aio_android.search(query))["error"] == "no_query"
    assert not called


def test_search_returns_engine_busy_while_a_download_holds_the_lock():
    """A tap during a download must fail fast, not block the UI behind a
    40-minute run. The lock is released from the same thread that took it."""
    acquired = threading.Event()
    release = threading.Event()

    def holder():
        with aio_android._ENGINE_LOCK:
            acquired.set()
            release.wait(5)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    acquired.wait(5)
    try:
        assert json.loads(aio_android.search("frieren"))["error"] == aio_android.ENGINE_BUSY
    finally:
        release.set()
        t.join(5)


def test_payload_is_recovered_from_a_log_polluted_buffer(monkeypatch):
    """The engine writes progress lines before the contract, and search
    pretty-prints with indent=2 so the payload spans many lines."""
    payload = {"query": "frieren", "candidates": [{"canonical_title": "Frieren"}]}

    def fake_engine(argv, sink=None):
        print("[*] Searching 40 sites...")
        print("[*] Probing image quality...")
        print(json.dumps(payload, indent=2))
        return 0

    monkeypatch.setattr(aio_android, "_run_engine", fake_engine)
    assert json.loads(aio_android.search("frieren")) == payload


def test_empty_candidate_list_is_a_result_not_an_error(monkeypatch):
    monkeypatch.setattr(
        aio_android, "_run_engine",
        lambda argv, sink=None: print(json.dumps({"query": "zzz", "candidates": []}, indent=2)),
    )
    out = json.loads(aio_android.search("zzz"))
    assert out["candidates"] == []
    assert "error" not in out


def test_engine_writing_nothing_is_reported_not_swallowed(monkeypatch):
    """Silence must not read as "no results" — that would hide a real
    failure behind an empty results page."""
    monkeypatch.setattr(aio_android, "_run_engine", lambda argv, sink=None: None)
    assert json.loads(aio_android.search("frieren"))["error"] == "no_search_payload"


def test_engine_exception_becomes_a_reportable_error(monkeypatch):
    def boom(argv, sink=None):
        raise RuntimeError("rapidfuzz is required for cross-site search.")

    monkeypatch.setattr(aio_android, "_run_engine", boom)
    out = json.loads(aio_android.search("frieren"))
    assert out["error"] == "search_failed"
    assert "rapidfuzz" in out["detail"]


def test_engine_sys_exit_still_yields_a_payload_when_one_was_written(monkeypatch):
    """aio_search_cli sys.exit()s on some paths; anything already written to
    stdout is still the contract."""
    def exiting(argv, sink=None):
        print(json.dumps({"candidates": []}, indent=2))
        raise SystemExit(1)

    monkeypatch.setattr(aio_android, "_run_engine", exiting)
    assert json.loads(aio_android.search("x"))["candidates"] == []


def test_search_releases_the_lock_after_an_engine_failure(monkeypatch):
    """A leaked lock would wedge every later download AND update check."""
    monkeypatch.setattr(aio_android, "_run_engine", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    aio_android.search("frieren")
    assert aio_android._ENGINE_LOCK.acquire(blocking=False)
    aio_android._ENGINE_LOCK.release()


def test_search_argv_reaches_the_engine_unmodified(monkeypatch):
    seen = {}

    def capture(argv, sink=None):
        seen["argv"] = list(argv)
        print(json.dumps({"candidates": []}, indent=2))

    monkeypatch.setattr(aio_android, "_run_engine", capture)
    aio_android.search("frieren", json.dumps({"networkLimit": "low"}))
    assert seen["argv"] == aio_android.build_search_argv("frieren", {"networkLimit": "low"})


def test_corrupt_settings_json_does_not_prevent_a_search(monkeypatch):
    """A bad settings blob should degrade to defaults, not deny the feature."""
    monkeypatch.setattr(
        aio_android, "_run_engine",
        lambda argv, sink=None: print(json.dumps({"candidates": []}, indent=2)),
    )
    assert "error" not in json.loads(aio_android.search("frieren", "not json"))


# --- the device parity check ----------------------------------------------

def test_fingerprint_is_stable_across_rapidfuzz_backends():
    """diagnostics() reports this so device-vs-desktop parity is a string
    comparison. It is only meaningful if the value cannot differ for a
    legitimate reason — no Unicode-skew character may appear in the corpus.
    See tests/test_fuzzy_match.py for what "skew" means here."""
    from sites import fuzzy_match

    for a, b in aio_android._FINGERPRINT_PAIRS:
        for s in (a, b):
            for ch in s:
                if ch in fuzzy_match._MATCH_SEPARATORS:
                    continue
                probe = "a" + ch + "b"
                assert fuzzy_match.normalize_for_match(probe) == probe, (
                    f"fingerprint corpus contains normalizable char {ch!r}"
                )


def test_fingerprint_exercises_both_normalized_characters():
    """A fingerprint that never meets an underscore or an NBSP could not
    detect a regressed normalizer."""
    corpus = "".join(a + b for a, b in aio_android._FINGERPRINT_PAIRS)
    assert "_" in corpus
    assert "\u00a0" in corpus


def test_diagnostics_reports_the_backend_and_fingerprint():
    out = json.loads(aio_android.diagnostics())
    assert out["rapidfuzz_backend"] in ("cpp", "python")
    assert out["capabilities"]["rapidfuzz"] is True
    assert len(out["rapidfuzz_fingerprint"].split()) == len(aio_android._FINGERPRINT_PAIRS)
