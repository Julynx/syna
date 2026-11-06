"""Tests for the harness's terminal timeout semantics.

A worker thread exists to enforce a call's deadline, not to let the
conversation move on while work continues. At the deadline the harness
sets the call's cancel event and waits up to the stop grace: a worker
that exits within the grace delivers its real outcome (a ToolCancelled
exception becomes the deadline notice), and a worker that ignores the
stop request is detached with the hung-call notice.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import MonkeyPatch, run_suite

from syna.protocol import parse
from syna.state import ToolCancelled, get_cancel_event

SLOW_TOOL = "slow_stub_tool"


def _register_tool(monkeypatch: MonkeyPatch, fn):
    monkeypatch.setitem(parse.TOOL_REGISTRY, SLOW_TOOL, fn)


def test_call_finishing_before_deadline_returns_normally(monkeypatch):
    _register_tool(monkeypatch, lambda: "done")
    output, timed_out, exit_code = parse.execute_tool_with_timeout(SLOW_TOOL, {}, 5.0)
    assert output == "done"
    assert not timed_out
    assert exit_code is None


def test_error_from_the_tool_propagates(monkeypatch):
    def failing():
        raise ValueError("boom")

    _register_tool(monkeypatch, failing)
    try:
        parse.execute_tool_with_timeout(SLOW_TOOL, {}, 5.0)
    except ValueError as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("the tool's exception must propagate")


def test_cancel_event_reaches_the_worker(monkeypatch):
    seen = {}

    def observing():
        seen["event"] = get_cancel_event()
        return "done"

    _register_tool(monkeypatch, observing)
    parse.execute_tool_with_timeout(SLOW_TOOL, {}, 5.0)
    assert seen["event"] is not None, "the worker must see the cancel event"


def test_toolcancelled_becomes_the_deadline_notice(monkeypatch):
    def stopping():
        event = get_cancel_event()
        assert event is not None
        if not event.wait(2.0):
            raise AssertionError("the cancel event never fired")
        raise ToolCancelled("stopped")

    _register_tool(monkeypatch, stopping)
    output, timed_out, exit_code = parse.execute_tool_with_timeout(SLOW_TOOL, {}, 0.1)
    assert timed_out
    assert "stopped after 0.1 seconds" in output, output
    assert "nothing is running in the background" in output, output


def test_grace_completion_delivers_the_real_result(monkeypatch):
    """A call that finishes just past the deadline delivers its result.

    The deadline starts the stop; if the work completes anyway within
    the grace, the result is real and must not be discarded.
    """
    monkeypatch.setattr(parse, "load_max_cancel_wait_s", lambda: 5.0)
    started = threading.Event()

    def late_finisher():
        started.set()
        time.sleep(0.3)
        return "late but done"

    _register_tool(monkeypatch, late_finisher)
    output, timed_out, exit_code = parse.execute_tool_with_timeout(SLOW_TOOL, {}, 0.1)
    assert output == "late but done"
    assert not timed_out


def test_worker_ignoring_stop_is_detached_with_the_hung_notice(monkeypatch):
    monkeypatch.setattr(parse, "load_max_cancel_wait_s", lambda: 0.05)
    release = threading.Event()
    done = threading.Event()

    def hung():
        release.wait(5.0)
        done.set()
        return "lost result"

    _register_tool(monkeypatch, hung)
    output, timed_out, exit_code = parse.execute_tool_with_timeout(SLOW_TOOL, {}, 0.1)
    assert timed_out
    assert "hung" in output, output
    assert "may still be running" in output, output
    assert not done.is_set(), "the worker must still be running right after the detach"
    release.set()
    assert done.wait(2.0), "the detached worker eventually finishes"


def test_untimed_call_waits_for_completion(monkeypatch):
    _register_tool(monkeypatch, lambda: time.sleep(0.3) or "finally")
    output, timed_out, exit_code = parse.execute_tool_with_timeout(SLOW_TOOL, {}, None)
    assert output == "finally"
    assert not timed_out


def test_parse_reports_a_hung_call_as_timeout(monkeypatch):
    """The batch pipeline maps a hung call to a failed timeout row."""

    def hung():
        time.sleep(0.5)
        return "lost result"

    _register_tool(monkeypatch, hung)
    monkeypatch.setattr(parse, "load_max_cancel_wait_s", lambda: 0.05)
    outcome = parse.parse_and_execute_tools(
        [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": SLOW_TOOL, "arguments": "{}"},
            }
        ],
        echo_errors=False,
        tool_timeout_s=0.1,
    )
    row = outcome.outputs[0]
    assert row["status"] == "timeout"
    assert "hung" in row["output"], row["output"]


def test_parse_reports_a_stopped_call_as_timeout(monkeypatch):
    """A ToolCancelled call renders as the deadline notice, not an error."""

    def stopping():
        event = get_cancel_event()
        assert event is not None
        assert event.wait(2.0), "the cancel event never fired"
        raise ToolCancelled("stopped")

    _register_tool(monkeypatch, stopping)
    outcome = parse.parse_and_execute_tools(
        [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": SLOW_TOOL, "arguments": "{}"},
            }
        ],
        echo_errors=False,
        tool_timeout_s=0.1,
    )
    row = outcome.outputs[0]
    assert row["status"] == "timeout"
    assert "nothing is running in the background" in row["output"], row["output"]


def test_call_deadline_composition(monkeypatch: MonkeyPatch):
    """The per-tool deadline dispatch: regular, untimed, and self-timed."""
    from syna.capabilities import web_search

    assert parse.call_deadline_s("execute_command", 60.0) == 60.0
    assert parse.call_deadline_s("ask_user", 60.0) is None
    assert parse.call_deadline_s("review_work", 60.0) is None
    assert parse.call_deadline_s("web_search", 60.0) == web_search.search_deadline_s()
    monkeypatch.setattr(web_search, "load_web_search_timeout_s", lambda: 10.0)
    monkeypatch.setattr(web_search, "load_browser_launch_timeout_s", lambda: 20.0)
    monkeypatch.setattr(web_search, "load_max_cancel_wait_s", lambda: 1.0)
    assert web_search._search_lock_wait_s() == 91.0, (
        "launch budget + search budget + slack + stop grace"
    )
    assert web_search.search_deadline_s() == 161.0, (
        "the lock wait cap plus the searcher's own budget and slack"
    )


def main():
    """Run every cancellation test and report a summary."""
    run_suite(
        [
            test_call_finishing_before_deadline_returns_normally,
            test_error_from_the_tool_propagates,
            test_cancel_event_reaches_the_worker,
            test_toolcancelled_becomes_the_deadline_notice,
            test_grace_completion_delivers_the_real_result,
            test_worker_ignoring_stop_is_detached_with_the_hung_notice,
            test_untimed_call_waits_for_completion,
            test_call_deadline_composition,
            test_parse_reports_a_hung_call_as_timeout,
            test_parse_reports_a_stopped_call_as_timeout,
        ]
    )


if __name__ == "__main__":
    main()
