"""Coverage for aio_android's progress reporting: the ETA estimator and the
exit code a cancelled run returns.

The estimator is a behavioural port of
UI-source/electron/downloader.js:applyChapterEta. The interesting rule — and
the one worth a test — is that it SKIPS intervals opened by an already-processed
chapter, because on a resume those ticks cost ~0ms and would otherwise drag the
average to nearly nothing right before real downloading starts.

run_download is exercised against a stand-in for aio-dl.py: importing the real
module costs the whole 303-handler registry, and every behaviour under test here
(exit-code mapping, sink lifecycle, sys.argv hygiene) is about the wrapper, not
about downloading.
"""

from __future__ import annotations

import json
import sys

import pytest

import aio_android
from aio_android import CANCELLED_EXIT_CODE, _EtaEstimator, run_download


class _Clock:
    """Hand-cranked monotonic clock, in seconds."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _tick(estimator, chapter="1", resumed=False):
    """Push one chapter_start through and hand back the (mutated) event."""
    event = {"kind": "chapter_start", "chapter": chapter, "resumed": resumed}
    estimator.observe(event)
    return event


# --------------------------------------------------------------------------
# ETA — sampling
# --------------------------------------------------------------------------

def test_no_eta_before_the_two_sample_floor():
    clock = _Clock()
    eta = _EtaEstimator(clock)
    eta.observe({"kind": "chapters_selected", "total": 10})

    first = _tick(eta)
    assert "eta_ms" not in first          # no interval yet
    assert first["processed"] == 1 and first["total"] == 10

    clock.advance(10)
    assert "eta_ms" not in _tick(eta)     # one sample: still below the floor

    clock.advance(10)
    assert "eta_ms" in _tick(eta)         # two samples: now it reports


def test_eta_is_remaining_chapters_times_the_average():
    clock = _Clock()
    eta = _EtaEstimator(clock)
    eta.observe({"kind": "chapters_selected", "total": 10})

    _tick(eta)
    for _ in range(2):
        clock.advance(10)
        event = _tick(eta)

    # Three ticks at a steady 10s: the EMA is exactly 10_000ms, and 7 chapters
    # remain of 10.
    assert event["chapter_ms_ema"] == 10_000
    assert event["eta_samples"] == 2
    assert event["processed"] == 3
    assert event["eta_ms"] == 7 * 10_000


def test_ema_weights_recent_chapters_more():
    clock = _Clock()
    eta = _EtaEstimator(clock)
    eta.observe({"kind": "chapters_selected", "total": 100})

    _tick(eta)
    clock.advance(10)   # sample 1 -> ema = 10_000 (first sample is taken whole)
    _tick(eta)
    clock.advance(20)   # sample 2 -> 10_000 + 0.3 * (20_000 - 10_000) = 13_000
    event = _tick(eta)

    assert event["chapter_ms_ema"] == 13_000


def test_resumed_chapters_do_not_open_a_sampled_interval():
    """The whole point of the `resumed` flag.

    A resume replays already-downloaded chapters instantly. Sampling those
    intervals would report seconds-long ETAs for an hour of real work.
    """
    clock = _Clock()
    eta = _EtaEstimator(clock)
    eta.observe({"kind": "chapters_selected", "total": 10})

    # Four cached chapters, effectively instant.
    for _ in range(4):
        clock.advance(0.001)
        _tick(eta, resumed=True)

    # The first real chapter closes an interval OPENED by a cached one — not a
    # sample. Two real chapters after it produce exactly two samples.
    clock.advance(30)
    first_real = _tick(eta)
    assert "eta_ms" not in first_real, "interval opened by a cached tick was sampled"

    clock.advance(30)
    _tick(eta)
    clock.advance(30)
    event = _tick(eta)

    assert event["eta_samples"] == 2
    assert event["chapter_ms_ema"] == 30_000
    assert event["processed"] == 7
    assert event["eta_ms"] == 3 * 30_000


def test_eta_is_none_when_the_total_is_unknown():
    # Explicit None, not an absent key: consumers merge event fields onto held
    # state, so omitting it would leave a stale number on screen.
    clock = _Clock()
    eta = _EtaEstimator(clock)
    for _ in range(3):
        clock.advance(5)
        event = _tick(eta)
    assert event["eta_ms"] is None
    assert "total" not in event


def test_eta_never_goes_negative_past_the_expected_total():
    # Chapter counts can exceed the selection (split parts, retried chapters).
    clock = _Clock()
    eta = _EtaEstimator(clock)
    eta.observe({"kind": "chapters_selected", "total": 2})
    for _ in range(5):
        clock.advance(5)
        event = _tick(eta)
    assert event["eta_ms"] == 0


def test_unrelated_events_are_ignored_and_untouched():
    eta = _EtaEstimator(_Clock())
    event = {"kind": "chapter_saved", "chapter": "1", "path": "x.cbz"}
    eta.observe(event)
    assert event == {"kind": "chapter_saved", "chapter": "1", "path": "x.cbz"}


@pytest.mark.parametrize("total", [0, -1, "12", None])
def test_bogus_chapter_totals_are_ignored(total):
    eta = _EtaEstimator(_Clock())
    eta.observe({"kind": "chapters_selected", "total": total})
    assert "total" not in _tick(eta)


# --------------------------------------------------------------------------
# run_download — exit codes and sink lifecycle
# --------------------------------------------------------------------------

class _FakeAioDl:
    """Stand-in for the aio-dl module: just the surface run_download touches."""

    def __init__(self, behaviour=None, cancelled=False):
        self._behaviour = behaviour or (lambda: None)
        self._cancelled = cancelled
        self.sink = None
        self.argv_seen = None
        self.cleared = False

    def clear_run_cancel(self):
        self.cleared = True

    def _reset_host_concurrency_caps(self):
        pass

    def set_event_sink(self, sink):
        self.sink = sink

    def run_cancelled(self):
        return self._cancelled

    def main(self):
        self.argv_seen = list(sys.argv)
        self._behaviour()


@pytest.fixture
def fake_aio(monkeypatch):
    def install(behaviour=None, cancelled=False):
        module = _FakeAioDl(behaviour, cancelled)
        monkeypatch.setattr(aio_android, "_AIO_DL", module)
        return module

    return install


def _raise(exc):
    def go():
        raise exc
    return go


@pytest.mark.parametrize("behaviour,expected", [
    (None, 0),
    (_raise(SystemExit(None)), 0),
    (_raise(SystemExit(0)), 0),
    (_raise(SystemExit(1)), 1),
    (_raise(SystemExit(2)), 2),
    # sys.exit("message") — argparse and aio-dl.py's own bail-outs use this.
    (_raise(SystemExit("No chapters selected.")), 1),
    (_raise(KeyboardInterrupt()), 1),
])
def test_exit_code_mapping(fake_aio, behaviour, expected):
    fake_aio(behaviour)
    assert run_download(["--format", "cbz", "https://x/y"]) == expected


@pytest.mark.parametrize("behaviour", [
    None,
    _raise(SystemExit(0)),
    # The abort branch: cancelling mid-chapter makes aio-dl.py exit(1). That is
    # still a cancellation, and reporting it as a failure would send the user
    # hunting for a site problem that does not exist.
    _raise(SystemExit(1)),
])
def test_cancelled_run_reports_its_own_exit_code(fake_aio, behaviour):
    fake_aio(behaviour, cancelled=True)
    assert run_download(["https://x/y"]) == CANCELLED_EXIT_CODE


def test_uncancelled_run_is_never_reported_as_cancelled(fake_aio):
    fake_aio(_raise(SystemExit(1)), cancelled=False)
    assert run_download(["https://x/y"]) == 1


def test_run_clears_stale_cancellation_first(fake_aio):
    # The process is reused across downloads, unlike the desktop's
    # one-process-per-download model — so a previous cancel must not carry over.
    module = fake_aio()
    run_download(["https://x/y"])
    assert module.cleared is True


def test_argv_is_passed_with_a_program_name_and_then_restored(fake_aio):
    module = fake_aio()
    before = list(sys.argv)
    run_download(["--format", "cbz", "https://x/y"])
    assert module.argv_seen == ["aio-dl.py", "--format", "cbz", "https://x/y"]
    assert sys.argv == before


def test_argv_is_restored_even_when_main_explodes(fake_aio):
    fake_aio(_raise(RuntimeError("boom")))
    before = list(sys.argv)
    with pytest.raises(RuntimeError):
        run_download(["https://x/y"])
    assert sys.argv == before


def test_sink_receives_json_strings_and_is_uninstalled_afterwards(fake_aio):
    received = []
    module = fake_aio()
    module._behaviour = lambda: module.sink({"kind": "series", "title": "Anne Shirley"})
    run_download(["https://x/y"], sink=received.append)

    assert [json.loads(r) for r in received] == [{"kind": "series", "title": "Anne Shirley"}]
    # Left installed, the next run would emit into a dead consumer.
    assert module.sink is None


def test_sink_events_arrive_with_eta_fields_stamped(fake_aio):
    received = []
    module = fake_aio()

    def behaviour():
        module.sink({"kind": "chapters_selected", "total": 4})
        for n in range(1, 4):
            module.sink({"kind": "chapter_start", "chapter": str(n), "resumed": False})

    module._behaviour = behaviour
    run_download(["https://x/y"], sink=received.append)

    events = [json.loads(r) for r in received]
    assert events[1]["processed"] == 1 and events[1]["total"] == 4
    # Real elapsed time here is microseconds, so assert on the shape rather than
    # a duration: by the third chapter both samples exist and an ETA is present.
    assert "eta_ms" in events[3] and events[3]["eta_samples"] == 2


def test_a_broken_estimator_cannot_kill_the_run(fake_aio, monkeypatch):
    """The sink runs at aio-dl.py's emit sites, inside worker threads. Losing a
    progress tick is always better than losing the download."""
    class Exploding(_EtaEstimator):
        def observe(self, event):
            raise RuntimeError("estimator bug")

    monkeypatch.setattr(aio_android, "_EtaEstimator", Exploding)
    received = []
    module = fake_aio()
    module._behaviour = lambda: module.sink({"kind": "chapter_start", "chapter": "1"})

    assert run_download(["https://x/y"], sink=received.append) == 0
    assert json.loads(received[0])["kind"] == "chapter_start"
