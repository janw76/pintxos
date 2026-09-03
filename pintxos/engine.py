"""In-process poll engine: a queue + worker thread with an explicit state machine.

Replaces the raw-thread-per-request approach in pintxos.app/pintxos.poll (that
wiring happens in a later bead). This module has no dependency on the network
or the DB beyond pintxos.db.now() for timestamp formatting: poll_fn is
injected so tests never touch either.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from enum import StrEnum
from typing import Protocol

from pintxos.db import now

log = logging.getLogger("pintxos.engine")


class FeedState(StrEnum):
    idle = "idle"
    queued = "queued"
    fetching = "fetching"
    summarizing = "summarizing"
    error = "error"


class InvalidTransition(Exception):
    pass


# Explicit transition table: (from, to) pairs that are legal.
_TRANSITIONS: set[tuple[FeedState, FeedState]] = {
    (FeedState.idle, FeedState.queued),
    (FeedState.error, FeedState.queued),
    (FeedState.queued, FeedState.fetching),
    (FeedState.fetching, FeedState.summarizing),
    (FeedState.summarizing, FeedState.summarizing),
    (FeedState.fetching, FeedState.idle),
    (FeedState.summarizing, FeedState.idle),
    (FeedState.fetching, FeedState.error),
    (FeedState.summarizing, FeedState.error),
    (FeedState.queued, FeedState.idle),
}


class Reporter(Protocol):
    def fetching(self, feed_id: int) -> None: ...

    def summarizing(self, feed_id: int, done: int, total: int) -> None: ...

    def finished(self, feed_id: int, inserted: int, skipped: int) -> None: ...

    def failed(self, feed_id: int, message: str) -> None: ...

    def api_key_missing(self, feed_id: int) -> None: ...


class NullReporter:
    """A Reporter that does nothing; useful as a default/placeholder."""

    def fetching(self, feed_id: int) -> None:
        pass

    def summarizing(self, feed_id: int, done: int, total: int) -> None:
        pass

    def finished(self, feed_id: int, inserted: int, skipped: int) -> None:
        pass

    def failed(self, feed_id: int, message: str) -> None:
        pass

    def api_key_missing(self, feed_id: int) -> None:
        pass


class PollEngine:
    """Queue + single worker thread that runs poll_fn for each queued feed.

    One threading.Lock guards all internal bookkeeping. The engine itself
    implements Reporter: poll_fn is called with `self` so it can report
    progress, and every reporter method takes the lock, validates the state
    transition, and updates bookkeeping.

    ``forget(feed_id)``: drops the feed's state, progress and last_result and
    removes it from the queue. If the feed is currently running, poll_fn is
    NOT cancelled (there is no cooperative cancellation channel). The
    worker's later reporter calls for the forgotten id then see the default
    state "idle", so the next transition (e.g. idle -> summarizing) raises
    InvalidTransition inside poll_fn. The worker catches that like any other
    poll_fn exception, records nothing for the forgotten id (no failed()
    entry, no auto-finish), clears ``current`` and keeps processing the
    queue. Net effect: a forgotten feed never reappears in snapshot() and
    the worker survives.
    """

    def __init__(
        self,
        poll_fn: Callable[[int, Reporter], object],
        *,
        name: str = "pintxos-engine",
    ) -> None:
        self._poll_fn = poll_fn
        self._name = name

        self._lock = threading.Lock()
        self._queue: deque[int] = deque()
        self._states: dict[int, FeedState] = {}
        self._progress: dict[int, tuple[int, int]] = {}
        self._last_result: dict[int, dict] = {}
        self.current: int | None = None
        self.paused_reason: str | None = None
        self.last_run_started_at: str | None = None
        self.last_run_finished_at: str | None = None

        self._wake = threading.Event()
        self._stop = False
        self._thread: threading.Thread | None = None
        self._run_start_time: float | None = None

    # -- state helpers (caller must hold self._lock) -------------------

    def _state_of(self, feed_id: int) -> FeedState:
        return self._states.get(feed_id, FeedState.idle)

    def _transition(self, feed_id: int, to: FeedState) -> None:
        frm = self._state_of(feed_id)
        if (frm, to) not in _TRANSITIONS:
            raise InvalidTransition(f"{feed_id}: {frm} -> {to}")
        self._states[feed_id] = to
        log.info("feed %s: %s -> %s", feed_id, frm, to)

    # -- public API ------------------------------------------------------

    def enqueue(self, feed_id: int) -> bool:
        """Queue a feed for polling. False (no-op) if already queued/running."""
        with self._lock:
            state = self._state_of(feed_id)
            if state in (FeedState.queued, FeedState.fetching, FeedState.summarizing):
                return False
            to = FeedState.queued
            frm = state
            if (frm, to) not in _TRANSITIONS:
                raise InvalidTransition(f"{feed_id}: {frm} -> {to}")
            self._states[feed_id] = to
            log.info("feed %s: %s -> %s", feed_id, frm, to)
            self._queue.append(feed_id)
        self._wake.set()
        return True

    def enqueue_all(self, feed_ids: Iterable[int]) -> int:
        """Enqueue every id in feed_ids; returns the count actually queued."""
        count = 0
        for feed_id in feed_ids:
            if self.enqueue(feed_id):
                count += 1
        return count

    def start(self) -> None:
        """Start the worker thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the worker to stop and join it."""
        self._stop = True
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def forget(self, feed_id: int) -> None:
        """Drop all bookkeeping for feed_id.

        See the class docstring for how this interacts with a feed that is
        currently running.
        """
        with self._lock:
            self._states.pop(feed_id, None)
            self._progress.pop(feed_id, None)
            self._last_result.pop(feed_id, None)
            try:
                self._queue.remove(feed_id)
            except ValueError:
                pass
            # current is left as-is: the worker will finish the in-flight
            # poll_fn call and its reporter calls will find no state and
            # re-create it lazily (see class docstring).

    def snapshot(self) -> dict:
        with self._lock:
            feeds = {}
            all_ids = set(self._states) | set(self._progress) | set(self._last_result)
            for feed_id in all_ids:
                done_total = self._progress.get(feed_id)
                feeds[feed_id] = {
                    "state": str(self._state_of(feed_id)),
                    "progress": (
                        {"done": done_total[0], "total": done_total[1]}
                        if done_total is not None
                        else None
                    ),
                    "last_result": self._last_result.get(feed_id),
                }
            return {
                "running": self.current is not None or bool(self._queue),
                "current": self.current,
                "queue": list(self._queue),
                "paused_reason": self.paused_reason,
                "last_run_started_at": self.last_run_started_at,
                "last_run_finished_at": self.last_run_finished_at,
                "feeds": feeds,
            }

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- Reporter implementation ------------------------------------------

    def fetching(self, feed_id: int) -> None:
        with self._lock:
            frm = self._state_of(feed_id)
            to = FeedState.fetching
            if (frm, to) not in _TRANSITIONS:
                raise InvalidTransition(f"{feed_id}: {frm} -> {to}")
            self._states[feed_id] = to
            log.info("feed %s: %s -> %s", feed_id, frm, to)

    def summarizing(self, feed_id: int, done: int, total: int) -> None:
        with self._lock:
            frm = self._state_of(feed_id)
            to = FeedState.summarizing
            if (frm, to) not in _TRANSITIONS:
                raise InvalidTransition(f"{feed_id}: {frm} -> {to}")
            self._states[feed_id] = to
            self._progress[feed_id] = (done, total)
            log.info("feed %s: %s -> %s (%s/%s)", feed_id, frm, to, done, total)

    def finished(self, feed_id: int, inserted: int, skipped: int) -> None:
        with self._lock:
            frm = self._state_of(feed_id)
            to = FeedState.idle
            if (frm, to) not in _TRANSITIONS:
                raise InvalidTransition(f"{feed_id}: {frm} -> {to}")
            self._states[feed_id] = to
            duration_ms = self._elapsed_ms()
            self._last_result[feed_id] = {
                "finished_at": now(),
                "inserted": inserted,
                "skipped": skipped,
                "duration_ms": duration_ms,
            }
            self._progress.pop(feed_id, None)
            log.info("feed %s: %s -> %s (inserted=%s skipped=%s)", feed_id, frm, to, inserted, skipped)

    def failed(self, feed_id: int, message: str) -> None:
        with self._lock:
            frm = self._state_of(feed_id)
            to = FeedState.error
            if (frm, to) not in _TRANSITIONS:
                raise InvalidTransition(f"{feed_id}: {frm} -> {to}")
            self._states[feed_id] = to
            self._last_result[feed_id] = {"finished_at": now(), "error": message}
            self._progress.pop(feed_id, None)
            log.info("feed %s: %s -> %s (%s)", feed_id, frm, to, message)

    def api_key_missing(self, feed_id: int) -> None:
        message = "ANTHROPIC_API_KEY not set"
        with self._lock:
            frm = self._state_of(feed_id)
            to = FeedState.error
            if (frm, to) not in _TRANSITIONS:
                raise InvalidTransition(f"{feed_id}: {frm} -> {to}")
            self._states[feed_id] = to
            self._last_result[feed_id] = {"finished_at": now(), "error": message}
            self._progress.pop(feed_id, None)
            log.info("feed %s: %s -> %s (%s)", feed_id, frm, to, message)
            self.paused_reason = message
            # Clear the rest of the queue: queued -> idle for each.
            cleared = list(self._queue)
            self._queue.clear()
            for other_id in cleared:
                other_frm = self._state_of(other_id)
                other_to = FeedState.idle
                if (other_frm, other_to) in _TRANSITIONS:
                    self._states[other_id] = other_to
                    log.info("feed %s: %s -> %s (queue cleared)", other_id, other_frm, other_to)

    # -- worker ------------------------------------------------------------

    def _elapsed_ms(self) -> int:
        if self._run_start_time is None:
            return 0
        return max(0, int((time.monotonic() - self._run_start_time) * 1000))

    def _run(self) -> None:
        while True:
            self._wake.wait(timeout=None)
            self._wake.clear()
            if self._stop:
                return
            while True:
                with self._lock:
                    if self._stop:
                        return
                    if not self._queue:
                        break
                    feed_id = self._queue.popleft()
                    if self.current is None:
                        self.last_run_started_at = now()
                        self.paused_reason = None
                    self.current = feed_id
                self._run_start_time = time.monotonic()
                try:
                    self._poll_fn(feed_id, self)
                except Exception as e:  # noqa: BLE001 - must not kill the worker
                    log.exception("poll_fn blew up for feed %s", feed_id)
                    with self._lock:
                        state = self._state_of(feed_id)
                        can_fail = state in (FeedState.fetching, FeedState.summarizing)
                    if can_fail:
                        try:
                            self.failed(feed_id, repr(e))
                        except InvalidTransition:
                            pass
                # If poll_fn returned without reporting finished/failed,
                # treat it as a no-op success so the feed doesn't get stuck.
                with self._lock:
                    state = self._state_of(feed_id)
                    still_running = state in (FeedState.fetching, FeedState.summarizing)
                if still_running:
                    try:
                        self.finished(feed_id, 0, 0)
                    except InvalidTransition:
                        pass
                with self._lock:
                    self.current = None
                if self._stop:
                    return
            with self._lock:
                self.last_run_finished_at = now()
