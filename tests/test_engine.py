"""Tests for pintxos.engine.PollEngine.

Fake poll_fns are injected; no network or DB access. threading.Event is used
for synchronisation between the test thread and the worker instead of
time.sleep loops. Every test that starts an engine stops it in a finally
block, and the `engine` fixture asserts no thread leak on teardown.
"""

from __future__ import annotations

import json
import threading

import pytest

from pintxos.engine import FeedState, InvalidTransition, NullReporter, PollEngine

WAIT_TIMEOUT = 2.0


@pytest.fixture
def make_engine():
    """Factory fixture: yields a function to build engines, stops them all at teardown."""
    created: list[PollEngine] = []

    def _make(poll_fn):
        eng = PollEngine(poll_fn)
        created.append(eng)
        return eng

    yield _make

    for eng in created:
        eng.stop(timeout=WAIT_TIMEOUT)
        assert eng.is_running is False


def test_null_reporter_is_a_noop():
    r = NullReporter()
    r.fetching(1)
    r.summarizing(1, 1, 2)
    r.finished(1, 1, 0)
    r.failed(1, "x")
    r.api_key_missing(1)


def test_enqueue_twice_second_returns_false(make_engine):
    def poll_fn(feed_id, reporter):
        reporter.finished(feed_id, 0, 0)

    eng = make_engine(poll_fn)
    # Engine NOT started: queue stays observable.
    assert eng.enqueue(1) is True
    assert eng.enqueue(1) is False
    snap = eng.snapshot()
    assert snap["queue"] == [1]


def test_enqueue_all_fifo_order(make_engine):
    order: list[int] = []
    all_done = threading.Event()
    lock = threading.Lock()

    def poll_fn(feed_id, reporter):
        with lock:
            order.append(feed_id)
        reporter.finished(feed_id, 0, 0)
        if len(order) == 3:
            all_done.set()

    eng = make_engine(poll_fn)
    eng.start()
    assert eng.enqueue_all([1, 2, 3]) == 3
    assert all_done.wait(timeout=WAIT_TIMEOUT)
    assert order == [1, 2, 3]


def test_reporter_sequence_fetching_summarizing_finished(make_engine):
    done_event = threading.Event()

    def poll_fn(feed_id, reporter):
        reporter.fetching(feed_id)
        reporter.summarizing(feed_id, 1, 3)
        reporter.summarizing(feed_id, 2, 3)
        reporter.finished(feed_id, 2, 1)
        done_event.set()

    eng = make_engine(poll_fn)
    eng.start()
    eng.enqueue(1)
    assert done_event.wait(timeout=WAIT_TIMEOUT)
    # Give the worker a moment to clear `current` after finished() runs.
    for _ in range(200):
        snap = eng.snapshot()
        if snap["current"] is None:
            break
        threading.Event().wait(timeout=0.01)
    snap = eng.snapshot()
    feed = snap["feeds"][1]
    assert feed["state"] == "idle"
    assert feed["last_result"]["inserted"] == 2
    assert feed["last_result"]["skipped"] == 1
    assert feed["last_result"]["duration_ms"] >= 0


def test_failed_then_reenqueue_moves_error_to_queued(make_engine):
    def poll_fn(feed_id, reporter):
        reporter.fetching(feed_id)
        reporter.failed(feed_id, "boom")

    eng = make_engine(poll_fn)
    # Unstarted engine: drive the transition manually and check state,
    # then verify enqueue works after the worker (if any) is done.
    eng.enqueue(1)
    eng.fetching(1)
    eng.failed(1, "boom")
    snap = eng.snapshot()
    assert snap["feeds"][1]["state"] == "error"
    assert snap["feeds"][1]["last_result"]["error"] == "boom"

    assert eng.enqueue(1) is True
    snap = eng.snapshot()
    assert snap["feeds"][1]["state"] == "queued"


def test_api_key_missing_clears_queue_and_sets_paused_reason(make_engine):
    release = threading.Event()
    entered = threading.Event()

    def poll_fn(feed_id, reporter):
        if feed_id == 1:
            reporter.fetching(feed_id)
            entered.set()
            release.wait(timeout=WAIT_TIMEOUT)
            reporter.api_key_missing(feed_id)
        else:
            reporter.finished(feed_id, 0, 0)

    eng = make_engine(poll_fn)
    eng.start()
    eng.enqueue_all([1, 2, 3])
    assert entered.wait(timeout=WAIT_TIMEOUT)
    # At this point feed 1 is fetching, 2 and 3 are queued.
    release.set()

    # Wait until the queue drains / paused_reason is set.
    for _ in range(300):
        snap = eng.snapshot()
        if snap["paused_reason"] is not None:
            break
        threading.Event().wait(timeout=0.01)

    snap = eng.snapshot()
    assert snap["feeds"][1]["state"] == "error"
    assert snap["feeds"][2]["state"] == "idle"
    assert snap["feeds"][3]["state"] == "idle"
    assert snap["queue"] == []
    assert snap["paused_reason"] == "ANTHROPIC_API_KEY not set"

    done2 = threading.Event()

    def poll_fn2_wrapper(feed_id, reporter):
        reporter.finished(feed_id, 0, 0)
        done2.set()

    eng._poll_fn = poll_fn2_wrapper
    assert eng.enqueue(2) is True
    assert done2.wait(timeout=WAIT_TIMEOUT)
    for _ in range(200):
        snap = eng.snapshot()
        if snap["paused_reason"] is None:
            break
        threading.Event().wait(timeout=0.01)
    snap = eng.snapshot()
    assert snap["paused_reason"] is None


def test_poll_fn_raises_runtime_error_and_worker_continues(make_engine):
    processed: list[int] = []
    done_event = threading.Event()

    def poll_fn(feed_id, reporter):
        if feed_id == 1:
            reporter.fetching(feed_id)
            raise RuntimeError("boom")
        processed.append(feed_id)
        reporter.finished(feed_id, 0, 0)
        done_event.set()

    eng = make_engine(poll_fn)
    eng.start()
    eng.enqueue(1)
    eng.enqueue(2)
    assert done_event.wait(timeout=WAIT_TIMEOUT)

    for _ in range(300):
        snap = eng.snapshot()
        if snap["feeds"].get(1, {}).get("state") == "error":
            break
        threading.Event().wait(timeout=0.01)

    snap = eng.snapshot()
    assert snap["feeds"][1]["state"] == "error"
    assert "RuntimeError" in snap["feeds"][1]["last_result"]["error"]
    assert processed == [2]


def test_summarizing_from_idle_raises_invalid_transition(make_engine):
    eng = make_engine(lambda feed_id, reporter: None)
    with pytest.raises(InvalidTransition):
        eng.summarizing(1, 1, 2)


def test_snapshot_round_trips_through_json(make_engine):
    def poll_fn(feed_id, reporter):
        reporter.fetching(feed_id)
        reporter.summarizing(feed_id, 1, 2)
        reporter.finished(feed_id, 1, 0)

    eng = make_engine(poll_fn)
    eng.enqueue(1)
    eng.fetching(1)
    eng.summarizing(1, 1, 2)
    eng.finished(1, 1, 0)
    snap = eng.snapshot()
    encoded = json.dumps(snap)
    decoded = json.loads(encoded)
    assert decoded["feeds"]["1"]["state"] == "idle"


def test_stop_returns_within_timeout_and_is_running_false(make_engine):
    eng = make_engine(lambda feed_id, reporter: reporter.finished(feed_id, 0, 0))
    eng.start()
    assert eng.is_running is True
    eng.stop(timeout=WAIT_TIMEOUT)
    assert eng.is_running is False


def test_forget_removes_queued_feed(make_engine):
    eng = make_engine(lambda feed_id, reporter: reporter.finished(feed_id, 0, 0))
    eng.enqueue(1)
    eng.enqueue(2)
    eng.forget(1)
    snap = eng.snapshot()
    assert 1 not in snap["queue"]
    assert 1 not in snap["feeds"]
    assert snap["queue"] == [2]


def test_poll_fn_returns_without_reporting_finished_auto_finishes(make_engine):
    done_event = threading.Event()

    def poll_fn(feed_id, reporter):
        reporter.fetching(feed_id)
        done_event.set()
        # returns without calling finished/failed

    eng = make_engine(poll_fn)
    eng.start()
    eng.enqueue(1)
    assert done_event.wait(timeout=WAIT_TIMEOUT)

    for _ in range(300):
        snap = eng.snapshot()
        if snap["feeds"].get(1, {}).get("state") == "idle":
            break
        threading.Event().wait(timeout=0.01)

    snap = eng.snapshot()
    assert snap["feeds"][1]["state"] == "idle"
    assert snap["feeds"][1]["last_result"]["inserted"] == 0
    assert snap["feeds"][1]["last_result"]["skipped"] == 0
