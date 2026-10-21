"""Concurrency tests for the web search rate-limit pacing."""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite
from syna import web_search

WORK_S = 0.05
DELAY_S = 0.3


class _FakeDDGS:
    """Stand-in for DDGS that records the interval each request occupied."""

    def __init__(self, intervals):
        self._intervals = intervals

    def text(self, query, max_results=10):
        start = time.monotonic()
        time.sleep(WORK_S)
        self._intervals.append((start, time.monotonic()))
        return [
            {"title": f"t{i}", "href": f"https://example.com/{i}", "body": "b"}
            for i in range(max_results)
        ]


def _run_concurrent_searches(thread_count):
    """Run searches from several threads and return their occupied intervals."""
    intervals = []
    original_ddgs = web_search.DDGS
    web_search.DDGS = lambda: _FakeDDGS(intervals)
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
        web_search.DDGS = original_ddgs
    intervals.sort()
    return intervals


def test_concurrent_searches_do_not_overlap():
    intervals = _run_concurrent_searches(4)
    assert len(intervals) == 4
    for (_, end_a), (start_b, _) in zip(intervals, intervals[1:]):
        assert start_b >= end_a + DELAY_S - 0.01


def test_search_spacing_across_batches():
    """Two searches in separate, non-overlapping calls still keep the gap."""
    intervals = _run_concurrent_searches(1)
    time.sleep(DELAY_S / 2)
    intervals.extend(_run_concurrent_searches(1))
    (_, end_first), (start_second, _) = intervals
    assert start_second >= end_first + DELAY_S - 0.01


def main():
    """Run every pacing test and report a summary."""
    run_suite(
        [
            test_concurrent_searches_do_not_overlap,
            test_search_spacing_across_batches,
        ]
    )


if __name__ == "__main__":
    main()
