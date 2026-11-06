"""Concurrency tests for the web search rate-limit pacing."""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import MonkeyPatch, run_suite

from syna.capabilities import web_search

WORK_S = 0.05
DELAY_S = 0.3


class _RecordingSearch:
    """Stand-in for _search that records the interval each request occupied."""

    def __init__(self, intervals):
        self._intervals = intervals

    def __call__(self, query):
        start = time.monotonic()
        time.sleep(WORK_S)
        self._intervals.append((start, time.monotonic()))
        return "results"


def _run_concurrent_searches(thread_count):
    """Run searches from several threads and return their occupied intervals."""
    intervals = []
    original_search = web_search._search
    web_search._search = _RecordingSearch(intervals)
    try:
        barrier = threading.Barrier(thread_count)

        def worker():
            barrier.wait()
            web_search.search("rate limit test", delay_s=DELAY_S)

        threads = [
            threading.Thread(target=worker, name=f"search-{i}", daemon=True)
            for i in range(thread_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        web_search._search = original_search
    intervals.sort()
    return intervals


def test_concurrent_searches_do_not_overlap():
    intervals = _run_concurrent_searches(4)
    assert len(intervals) == 4
    for (_, end_a), (start_b, _) in zip(intervals, intervals[1:], strict=False):
        assert start_b >= end_a + DELAY_S - 0.01


def test_search_spacing_across_batches():
    """Two searches in separate, non-overlapping calls still keep the gap."""
    intervals = _run_concurrent_searches(1)
    time.sleep(DELAY_S / 2)
    intervals.extend(_run_concurrent_searches(1))
    (_, end_first), (start_second, _) = intervals
    assert start_second >= end_first + DELAY_S - 0.01


def test_pacing_delay_is_randomized():
    """The pacing sleep spans [delay_s, 1.5*delay_s), never a fixed value."""
    durations = []
    original_search = web_search._search

    class _ShimTime:
        """Delegate to the real time module, recording sleep durations."""

        def sleep(self, seconds):
            durations.append(seconds)

        def __getattr__(self, name):
            return getattr(time, name)

    original_time = web_search.time
    web_search._search = lambda query: "results"
    web_search.time = _ShimTime()
    try:
        for _ in range(20):
            web_search.search("rate limit test", delay_s=DELAY_S)
    finally:
        web_search.time = original_time
        web_search._search = original_search
    assert len(durations) == 20
    assert len(set(durations)) > 1, "waits are fixed; they must be randomized"
    for duration in durations:
        assert DELAY_S <= duration < DELAY_S * 1.5


def test_lock_wait_is_bounded(monkeypatch: MonkeyPatch):
    """A search that cannot get the lock fails with a clear error.

    The cap outlasts every legitimate budget of the running search, so
    exceeding it means the running search wedged past every budget and
    could not be stopped.
    """
    monkeypatch.setattr(web_search, "_search", lambda query: "results")
    monkeypatch.setattr(web_search, "_search_lock_wait_s", lambda: 0.1)
    if not web_search._search_lock.acquire(timeout=1):
        raise AssertionError("the search lock must be free for this test")
    try:
        try:
            web_search.search("blocked test", delay_s=DELAY_S)
        except ValueError as exc:
            assert "stuck" in str(exc), str(exc)
        else:
            raise AssertionError("expected ValueError while the lock is held")
    finally:
        web_search._search_lock.release()


def test_lock_wait_cap_covers_every_legitimate_hold():
    """The cap exceeds the running search's worst legitimate lock hold.

    A queued search must never fail one that would have succeeded: the
    hold spans pacing, the browser launch budget, and the search budget,
    plus the harness's stop grace for a wedged run being cancelled.
    """
    from syna.config import (
        load_browser_launch_timeout_s,
        load_max_cancel_wait_s,
        load_web_search_timeout_s,
    )

    wait_limit = web_search._search_lock_wait_s()
    legitimate_hold = (
        load_browser_launch_timeout_s()
        + load_web_search_timeout_s()
        + load_max_cancel_wait_s()
        + 5
    )
    assert wait_limit > legitimate_hold, (wait_limit, legitimate_hold)


def main():
    """Run every pacing test and report a summary."""
    run_suite(
        [
            test_concurrent_searches_do_not_overlap,
            test_search_spacing_across_batches,
            test_pacing_delay_is_randomized,
            test_lock_wait_is_bounded,
            test_lock_wait_cap_covers_every_legitimate_hold,
        ]
    )


if __name__ == "__main__":
    main()
