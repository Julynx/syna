"""Tests for the browser tool's own budgets and cancellation.

The browser tool is never detached by the harness in normal operation:
each session command runs on the shared browser's loop under
`browser_command_timeout_s`, raced against the harness's stop event; a
command cancelled mid-flight discards the shared session
(reset-on-cancel); and the shared browser's cold start runs in a helper
thread bounded by the launch budget.
"""

import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import MonkeyPatch, run_suite

from syna.capabilities import tools
from syna.capabilities import browser as browser_module
from syna.capabilities.browser import (
    Browser,
    BrowserCommandTimeout,
    SharedBrowser,
    browser_deadline_s,
)
from syna.state import ToolCancelled, set_cancel_event


def _loop_browser(session) -> Browser:
    """A Browser whose loop runs, around a fake session (no Camoufox)."""
    browser = Browser.__new__(Browser)
    browser._loop = asyncio.new_event_loop()
    browser._thread = threading.Thread(target=browser._loop.run_forever, daemon=True)
    browser._thread.start()
    browser.last = ""
    browser._s = session
    return browser


class _FakeSession:
    """Session stand-in whose commands run a controllable coroutine."""

    def __init__(self, behavior):
        self._behavior = behavior

    async def call(self, name, args=None):
        return await self._behavior(name, args or {})


class _StubSharedBrowser:
    """SharedBrowser stand-in: always warm, records invalidations."""

    def __init__(self, instance):
        self._instance = instance
        self.invalidations = 0

    def get(self, launch_budget_s):
        return self._instance

    def invalidate(self):
        self.invalidations += 1
        return self._instance is not None


def test_command_delivers_its_result(monkeypatch: MonkeyPatch):
    """A command within its budget returns its view and updates `last`."""

    async def quick(name, args):
        return "URL: https://example.com"

    browser = _loop_browser(_FakeSession(quick))
    view = browser._tool("visit", url="https://example.com")
    assert view == "URL: https://example.com"
    assert browser.last == view


def test_command_budget_cancels_mid_flight(monkeypatch: MonkeyPatch):
    """A command slower than its budget is cancelled with a clear error."""
    monkeypatch.setattr(browser_module, "load_browser_command_timeout_s", lambda: 0.2)

    async def slow(name, args):
        await asyncio.sleep(2.0)
        return "never"

    browser = _loop_browser(_FakeSession(slow))
    started = time.monotonic()
    try:
        browser._tool("visit", url="https://example.com")
    except BrowserCommandTimeout as exc:
        message = str(exc)
    else:
        raise AssertionError("the exhausted command budget must raise")
    elapsed = time.monotonic() - started
    assert "timed out" in message, message
    assert "discarded" in message, message
    assert elapsed < 1.0, (
        f"the command must stop within its budget (took {elapsed:.2f}s)"
    )


def test_cancel_event_stops_the_command():
    """The harness's stop event cancels the in-flight command promptly."""

    async def slow(name, args):
        await asyncio.sleep(5.0)
        return "never"

    browser = _loop_browser(_FakeSession(slow))
    cancel_event = threading.Event()
    set_cancel_event(cancel_event)
    try:

        def stop_soon():
            time.sleep(0.3)
            cancel_event.set()

        threading.Thread(target=stop_soon, daemon=True).start()
        started = time.monotonic()
        try:
            browser._tool("visit", url="https://example.com")
        except ToolCancelled as exc:
            message = str(exc)
        else:
            raise AssertionError("the stop request must cancel the command")
        elapsed = time.monotonic() - started
    finally:
        set_cancel_event(None)
    assert "harness stopped" in message, message
    assert elapsed < 1.5, (
        f"the stop must be honored promptly, not at the deadline (took {elapsed:.2f}s)"
    )


def test_late_result_during_stop_delivery_counts():
    """A command finishing inside the acknowledgement window delivers.

    The stop arrives while the command is running; it catches the
    cancellation and lands its real result while the stop is being
    delivered: the result is real and the call must return it, not a
    cancellation notice.
    """

    async def stubborn(name, args):
        try:
            await asyncio.sleep(5.0)
            return "never"
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            return "late but real"

    browser = _loop_browser(_FakeSession(stubborn))
    cancel_event = threading.Event()
    set_cancel_event(cancel_event)
    try:

        def stop_soon():
            time.sleep(0.3)
            cancel_event.set()

        threading.Thread(target=stop_soon, daemon=True).start()
        started = time.monotonic()
        view = browser._tool("visit", url="https://example.com")
        elapsed = time.monotonic() - started
    finally:
        set_cancel_event(None)
    assert view == "late but real", view
    assert elapsed < 1.5, f"the result must arrive promptly (took {elapsed:.2f}s)"


def test_command_ignoring_cancellation_is_abandoned(monkeypatch: MonkeyPatch):
    """A command that swallows its cancellation is abandoned past the ack.

    The acknowledgement window is capped at half the harness's stop
    grace, so the abandonment (and the caller's error) always lands
    before the harness would detach and report the call hung.
    """
    monkeypatch.setattr(browser_module, "load_browser_command_timeout_s", lambda: 0.2)
    monkeypatch.setattr(browser_module, "_CANCEL_ACK_S", 0.2)
    release = threading.Event()
    abandoned_finished = threading.Event()

    async def unstoptable(name, args):
        while True:
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                continue
            if release.is_set():
                abandoned_finished.set()
                return "released"

    browser = _loop_browser(_FakeSession(unstoptable))
    started = time.monotonic()
    try:
        browser._tool("visit", url="https://example.com")
    except BrowserCommandTimeout as exc:
        message = str(exc)
    else:
        raise AssertionError("the abandoned command must raise the timeout")
    elapsed = time.monotonic() - started
    assert "timed out" in message, message
    assert elapsed < 1.5, (
        f"the abandonment must land within the stop grace (took {elapsed:.2f}s)"
    )
    release.set()
    assert abandoned_finished.wait(2.0), "the abandoned task must end for cleanup"
    browser._loop.call_soon_threadsafe(browser._loop.stop)


def test_ack_window_is_capped_by_the_harness_grace(monkeypatch: MonkeyPatch):
    """A tiny harness grace shrinks the acknowledgement window to match.

    The command swallows its cancellation, so the guard waits out the
    acknowledgement window before abandoning it. With the window's
    constant patched above the grace, only the min() cap keeps the
    abandonment inside the harness's stop grace.
    """
    monkeypatch.setattr(browser_module, "_CANCEL_ACK_S", 3.0)
    monkeypatch.setattr(browser_module, "load_max_cancel_wait_s", lambda: 1.0)
    release = threading.Event()
    abandoned_finished = threading.Event()

    async def unstoptable(name, args):
        while True:
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                continue
            if release.is_set():
                abandoned_finished.set()
                return "released"

    browser = _loop_browser(_FakeSession(unstoptable))
    cancel_event = threading.Event()
    set_cancel_event(cancel_event)
    try:

        def stop_soon():
            time.sleep(0.2)
            cancel_event.set()

        threading.Thread(target=stop_soon, daemon=True).start()
        started = time.monotonic()
        try:
            browser._tool("visit", url="https://example.com")
        except ToolCancelled:
            pass
        else:
            raise AssertionError("the stop must end the call")
        elapsed = time.monotonic() - started
    finally:
        set_cancel_event(None)
    assert elapsed < 1.0, (
        f"the abandonment must land inside the 1s grace (took {elapsed:.2f}s)"
    )
    release.set()
    assert abandoned_finished.wait(2.0), "the abandoned task must end for cleanup"
    browser._loop.call_soon_threadsafe(browser._loop.stop)


def test_screenshot_is_bounded_by_the_command_budget(monkeypatch: MonkeyPatch):
    """The screenshot capture runs under the same command budget."""
    monkeypatch.setattr(browser_module, "load_browser_command_timeout_s", lambda: 0.2)

    class _Session:
        async def screenshot_bytes(self):
            await asyncio.sleep(2.0)
            return b"png"

    browser = _loop_browser(_Session())
    try:
        browser.screenshot()
    except BrowserCommandTimeout as exc:
        assert "screenshot" in str(exc), str(exc)
    else:
        raise AssertionError("the screenshot must be bounded by the budget")


def test_timed_out_command_discards_the_session(monkeypatch: MonkeyPatch):
    """Reset-on-cancel: a mid-flight budget expiry discards the session."""

    class _BrokenBrowser:
        def _tool(self, name, **args):
            raise BrowserCommandTimeout("Browser command 'visit' timed out")

    stub = _StubSharedBrowser(_BrokenBrowser())
    monkeypatch.setattr(tools, "SHARED_BROWSER", stub)
    monkeypatch.setitem(tools.BROWSER_COMMANDS, "visit", Browser.visit)
    try:
        tools.browser("visit", {"url": "https://example.com"})
    except BrowserCommandTimeout:
        pass
    else:
        raise AssertionError("the timeout must propagate to the harness")
    assert stub.invalidations == 1, "the session must be discarded"


def test_stopped_command_discards_the_session(monkeypatch: MonkeyPatch):
    """Reset-on-cancel: a harness-stopped command discards the session."""

    class _StoppedBrowser:
        def _tool(self, name, **args):
            raise ToolCancelled("the harness stopped the browser command")

    stub = _StubSharedBrowser(_StoppedBrowser())
    monkeypatch.setattr(tools, "SHARED_BROWSER", stub)
    monkeypatch.setitem(tools.BROWSER_COMMANDS, "visit", Browser.visit)
    try:
        tools.browser("visit", {"url": "https://example.com"})
    except ToolCancelled:
        pass
    else:
        raise AssertionError("the stop must propagate to the harness")
    assert stub.invalidations == 1, "the session must be discarded"


def test_normal_command_keeps_the_session(monkeypatch: MonkeyPatch):
    """A command that completes cleanly never discards the session."""

    class _FakeBrowser:
        def _tool(self, name, **args):
            return "URL: https://example.com"

    stub = _StubSharedBrowser(_FakeBrowser())
    monkeypatch.setattr(tools, "SHARED_BROWSER", stub)
    monkeypatch.setitem(tools.BROWSER_COMMANDS, "visit", Browser.visit)
    view = tools.browser("visit", {"url": "https://example.com"})
    assert view.startswith("URL:")
    assert stub.invalidations == 0


def test_launch_budget_abandons_the_wait_not_the_start(monkeypatch: MonkeyPatch):
    """The shared browser's cold start is off the callers' path."""

    class _SlowBrowser:
        def __init__(self):
            time.sleep(0.3)

    shared = SharedBrowser(
        _SlowBrowser, thread_name="test-tool-browser", log_label="Test browser"
    )
    try:
        shared.get(0.1)
    except ValueError as exc:
        assert "did not start" in str(exc), str(exc)
    else:
        raise AssertionError("the exhausted launch budget must raise")
    deadline = time.monotonic() + 2.0
    while shared._instance is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert shared.get(5.0) is shared._instance


def test_invalidate_tears_down_without_blocking():
    """invalidate drops the instance and closes it off the callers' path."""

    class _ClosableBrowser:
        def __init__(self):
            self.closed = threading.Event()

        def close(self):
            time.sleep(0.3)
            self.closed.set()

    shared = SharedBrowser(
        _ClosableBrowser, thread_name="test-tool-browser", log_label="Test browser"
    )
    instance = shared.get(5.0)
    started = time.monotonic()
    assert shared.invalidate() is True
    assert time.monotonic() - started < 0.1, "invalidate must not block on the teardown"
    assert shared.invalidate() is False, "nothing left to invalidate"
    assert instance.closed.wait(2.0), "the helper must close the dropped instance"
    assert shared.get(5.0) is not instance, "the next get starts a clean browser"


def test_harness_deadline_covers_launch_and_command():
    """The browser deadline is the bug guard: launch + command + slack."""
    from syna.protocol import parse

    expected = (
        browser_module.load_browser_launch_timeout_s()
        + browser_module.load_browser_command_timeout_s()
        + browser_module.BROWSER_DEADLINE_SLACK_S
    )
    assert browser_deadline_s() == expected
    assert parse.call_deadline_s("browser", 60.0) == expected


def main():
    """Run every browser budget test and report a summary."""
    run_suite(
        [
            test_command_delivers_its_result,
            test_command_budget_cancels_mid_flight,
            test_cancel_event_stops_the_command,
            test_late_result_during_stop_delivery_counts,
            test_command_ignoring_cancellation_is_abandoned,
            test_ack_window_is_capped_by_the_harness_grace,
            test_screenshot_is_bounded_by_the_command_budget,
            test_timed_out_command_discards_the_session,
            test_stopped_command_discards_the_session,
            test_normal_command_keeps_the_session,
            test_launch_budget_abandons_the_wait_not_the_start,
            test_invalidate_tears_down_without_blocking,
            test_harness_deadline_covers_launch_and_command,
        ]
    )


if __name__ == "__main__":
    main()
