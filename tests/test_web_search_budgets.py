"""Tests for the web search's self-timed budgets.

The search is never detached: it blocks until it completes, fails, or
runs out of one of its own budgets. Starting the stealth browser from
cold is startup governed by the launch budget; the engine chain is work
governed by the search budget, enforced cooperatively — each
navigation's timeout is capped by what remains, so expiry stops the
chain within that navigation.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import MonkeyPatch, run_suite

from syna.capabilities import web_search
from syna.capabilities.browser import SharedBrowser


class _FakePage:
    """Records goto calls; answers evaluate with no results."""

    def __init__(self):
        self.goto_calls = []

    def goto(self, url, **kwargs):
        self.goto_calls.append({"url": url, **kwargs})

        async def _noop():
            return None

        return _noop()

    def evaluate(self, js):
        async def _noop():
            return []

        return _noop()


class _FakeBrowser:
    """Just enough browser for _search_engine's plumbing."""

    def __init__(self):
        self.page = _FakePage()

    def run(self, coro):
        coro.close()
        return []


class _CountingBrowser:
    """Counts instantiations to assert a single shared browser."""

    instances = 0

    def __init__(self, **kwargs):
        _CountingBrowser.instances += 1
        self.page = _FakePage()


def _fresh_shared_browser(monkeypatch: MonkeyPatch, factory) -> SharedBrowser:
    """Install an isolated SharedBrowser in web_search and return it."""
    shared = SharedBrowser(
        factory, thread_name="test-search-browser", log_label="Test browser"
    )
    monkeypatch.setattr(web_search, "_search_browser", shared)
    return shared


def test_search_budget_expires_mid_chain(monkeypatch: MonkeyPatch):
    """A chain slower than its budget stops with a clear timeout error."""

    def slow_engine(browser, query, engine, deadline):
        time.sleep(0.15)
        return []

    monkeypatch.setattr(web_search, "_get_browser", lambda: object())
    monkeypatch.setattr(web_search, "load_web_search_timeout_s", lambda: 0.1)
    monkeypatch.setattr(web_search, "_search_engine", slow_engine)
    monkeypatch.setattr(web_search, "_page_shows_challenge", lambda browser: False)
    try:
        web_search._browser_search("budget test")
    except ValueError as exc:
        assert "timed out" in str(exc), str(exc)
    else:
        raise AssertionError("the exhausted budget must raise")


def test_launch_budget_expires_while_the_constructor_runs(monkeypatch: MonkeyPatch):
    """A caller outlives the budget on a slow constructor; the helper lands.

    The constructor runs in a helper thread, so an expired budget
    abandons the wait, not the work: the helper keeps starting the
    browser and the next call finds it warm.
    """

    class _SlowBrowser:
        def __init__(self):
            time.sleep(0.3)
            self.page = _FakePage()

    shared = _fresh_shared_browser(monkeypatch, _SlowBrowser)
    monkeypatch.setattr(web_search, "load_browser_launch_timeout_s", lambda: 0.1)
    try:
        web_search._get_browser()
    except ValueError as exc:
        assert "did not start" in str(exc), str(exc)
    else:
        raise AssertionError("the exhausted launch budget must raise")
    deadline = time.monotonic() + 2.0
    while shared._instance is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert shared._instance is not None, (
        "the abandoned helper must finish and publish the browser"
    )
    assert web_search._get_browser() is shared._instance


def test_constructor_failure_propagates_and_the_next_call_retries(
    monkeypatch: MonkeyPatch,
):
    """A failed constructor is raised to the waiter; a retry starts anew."""

    class _FlakyBrowser:
        instances = 0

        def __init__(self):
            _FlakyBrowser.instances += 1
            if _FlakyBrowser.instances == 1:
                raise RuntimeError("boom")
            self.page = _FakePage()

    _FlakyBrowser.instances = 0
    _fresh_shared_browser(monkeypatch, _FlakyBrowser)
    monkeypatch.setattr(web_search, "load_browser_launch_timeout_s", lambda: 2.0)
    try:
        web_search._get_browser()
    except RuntimeError as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("the constructor's failure must propagate")
    browser = web_search._get_browser()
    assert isinstance(browser, _FlakyBrowser)
    assert _FlakyBrowser.instances == 2, "the retry must start a fresh launch"


def test_browser_is_created_exactly_once_under_a_race(monkeypatch: MonkeyPatch):
    """Concurrent first searches share one browser instead of racing."""
    _CountingBrowser.instances = 0
    shared = _fresh_shared_browser(monkeypatch, _CountingBrowser)
    monkeypatch.setattr(web_search, "load_browser_launch_timeout_s", lambda: 5.0)
    barrier = threading.Barrier(8)
    browsers = []
    errors = []

    def worker():
        barrier.wait()
        try:
            browsers.append(web_search._get_browser())
        except Exception as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, name=f"search-{i}", daemon=True)
        for i in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    assert len(browsers) == 8
    assert all(browser is browsers[0] for browser in browsers)
    assert shared._instance is browsers[0]
    assert _CountingBrowser.instances == 1, (
        "the creation lock must serialize the cold start"
    )


def test_interruptible_sleep_ends_early_when_cancelled(monkeypatch: MonkeyPatch):
    from syna.state import set_cancel_event

    cancel_event = threading.Event()
    cancel_event.set()
    set_cancel_event(cancel_event)
    try:
        started = time.monotonic()
        web_search._interruptible_sleep(5.0)
        elapsed = time.monotonic() - started
    finally:
        set_cancel_event(None)
    assert elapsed < 1.0, f"the sleep must end early on cancel (took {elapsed:.2f}s)"


def test_goto_timeout_is_deterministic_against_the_budget(monkeypatch: MonkeyPatch):
    """The goto timeout is the remaining budget, capped at its ceiling.

    A frozen clock makes the three cases exact: a healthy budget keeps
    the full 30 s ceiling, a small remaining budget caps the navigation,
    and an expired budget degrades to the minimum instead of hanging.
    """

    class _FrozenTime:
        def __init__(self, now):
            self._now = now

        def monotonic(self):
            return self._now

        def sleep(self, seconds):
            pass

    browser = _FakeBrowser()
    engine = web_search._ENGINES[0]
    now = 1000.0
    monkeypatch.setattr(web_search, "time", _FrozenTime(now))
    web_search._search_engine(browser, "q", engine, now + 300)
    assert browser.page.goto_calls[0]["timeout"] == (web_search._GOTO_TIMEOUT_S * 1000)
    web_search._search_engine(browser, "q", engine, now + 2.0)
    assert browser.page.goto_calls[1]["timeout"] == 2000
    web_search._search_engine(browser, "q", engine, now - 1.0)
    assert browser.page.goto_calls[2]["timeout"] == 1


def test_search_deadline_covers_queueing():
    """The harness deadline outlasts queueing plus the searcher's own work.

    A queued search spends its deadline waiting for the pacing lock
    before its own budgets start, so the deadline must embed the lock's
    wait cap — otherwise concurrent sessions could be stopped by the
    bug guard while their own enforcement never failed.
    """
    deadline = web_search.search_deadline_s()
    legitimate = (
        web_search._search_lock_wait_s()
        + web_search.load_web_search_timeout_s()
        + web_search.WEB_SEARCH_DEADLINE_SLACK_S
    )
    assert deadline >= legitimate, (deadline, legitimate)


def main():
    """Run every budget test and report a summary."""
    run_suite(
        [
            test_search_budget_expires_mid_chain,
            test_launch_budget_expires_while_the_constructor_runs,
            test_constructor_failure_propagates_and_the_next_call_retries,
            test_browser_is_created_exactly_once_under_a_race,
            test_interruptible_sleep_ends_early_when_cancelled,
            test_goto_timeout_is_deterministic_against_the_budget,
            test_search_deadline_covers_queueing,
        ]
    )


if __name__ == "__main__":
    main()
