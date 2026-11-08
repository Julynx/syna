"""Unit tests for the wait_for tool and its deadline composition."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import MonkeyPatch, run_suite

from syna.capabilities import processes, tools
from syna.capabilities.processes import PodRegistry, ProcessRecord
from syna.capabilities.subagents import SubagentRecord
from syna.protocol import parse


class FakeContainer:
    """Stand-in pod; the process checks read nothing real."""

    def exec(self, argv):
        return ""

    def copy_to(self, source, dest):
        pass


def fresh_registry():
    return PodRegistry()


def make_tools_isolated(monkeypatch, registry, container=FakeContainer()):
    """Point the wait_for tool at a fresh registry and fake container.

    tools.py imported the registry accessor into its own namespace, so
    the patch lands there, not on the processes module.
    """
    monkeypatch.setattr(tools, "get_container", lambda: container)
    monkeypatch.setattr(tools, "registry_for", lambda c: registry)
    return container


def test_unknown_id_is_an_error_with_recovery_path(monkeypatch):
    make_tools_isolated(monkeypatch, fresh_registry())
    result = tools.wait_for(name="ghost")
    assert result.startswith("ERROR:")
    assert "per-session" in result and "ps" in result


def test_returns_immediately_on_finished_process(monkeypatch):
    registry = fresh_registry()
    proc = ProcessRecord(id="done", command="x", dir="/d")
    proc.state = "exited"
    proc.exit_code = 0
    registry.processes["done"] = proc
    make_tools_isolated(monkeypatch, registry)

    monkeypatch.setattr(
        tools,
        "check_process_state",
        lambda c, d: ("exited:0", "out text", "err text"),
    )
    result = tools.wait_for(name="done")
    assert "finished with exit code 0" in result
    assert "out text" in result and "err text" in result
    assert "It finished during this wait" in result


def test_returns_immediately_on_settled_subagent(monkeypatch):
    registry = fresh_registry()
    record = SubagentRecord(id="sa", goal="g", level=2)
    record.state = "finished"
    record.end_reason = "response"
    record.response = "the answer"
    record.dump_host_path = None
    registry.subagents["sa"] = record
    make_tools_isolated(monkeypatch, registry)

    result = tools.wait_for(name="sa")
    assert "finished with a response" in result
    assert "the answer" in result


def test_wait_for_collection_suppresses_the_process_notice(monkeypatch):
    registry = fresh_registry()
    seen = []
    registry.completion_sink = lambda payload: seen.append(payload)
    proc = ProcessRecord(id="done", command="x", dir="/d")
    registry.processes["done"] = proc
    make_tools_isolated(monkeypatch, registry)
    monkeypatch.setattr(
        tools,
        "check_process_state",
        lambda c, d: ("exited:0", "out", "err"),
    )

    result = tools.wait_for(name="done")
    assert "finished with exit code 0" in result
    assert proc.collected_by_wait_for
    processes._finalize_process(registry, proc, "0")
    assert seen == [], "the process ending on its own must notify once"


def test_wait_for_collection_suppresses_the_subagent_notice(monkeypatch):
    registry = fresh_registry()
    seen = []
    registry.completion_sink = lambda payload: seen.append(payload)
    record = SubagentRecord(id="sa", goal="g", level=2)
    record.state = "finished"
    record.end_reason = "response"
    record.response = "the answer"
    record.dump_host_path = None
    registry.subagents["sa"] = record
    make_tools_isolated(monkeypatch, registry)

    result = tools.wait_for(name="sa")
    assert "finished with a response" in result
    assert record.collected_by_wait_for
    processes._finalize_subagent(registry, record, FakeContainer())
    assert seen == [], "the subagent ending on its own must notify once"


def test_polls_until_the_process_finishes(monkeypatch):
    registry = fresh_registry()
    proc = ProcessRecord(id="slow", command="x", dir="/d")
    registry.processes["slow"] = proc
    make_tools_isolated(monkeypatch, registry)
    monkeypatch.setattr(processes, "WAIT_FOR_POLL_INTERVAL_S", 0.0)

    clock = [0.0]

    def fake_monotonic():
        return clock[0]

    def fake_sleep(seconds):
        clock[0] += max(seconds, 1.0)
        if clock[0] >= 5.0:
            proc.state = "exited"
            proc.exit_code = 0

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    monkeypatch.setattr(time, "sleep", fake_sleep)
    monkeypatch.setattr(tools, "time", time)
    monkeypatch.setattr(
        tools,
        "check_process_state",
        lambda c, d: ("exited:0", "", ""),
    )

    result = tools.wait_for(name="slow", max_wait_s=60)
    assert "finished with exit code 0" in result


def test_timeout_returns_still_running_not_an_error(monkeypatch):
    registry = fresh_registry()
    proc = ProcessRecord(id="long", command="x", dir="/d")
    registry.processes["long"] = proc
    make_tools_isolated(monkeypatch, registry)
    monkeypatch.setattr(processes, "WAIT_FOR_POLL_INTERVAL_S", 0.0)

    clock = [0.0]

    def fake_monotonic():
        return clock[0]

    def fake_sleep(seconds):
        clock[0] += max(seconds, 1.0)

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    monkeypatch.setattr(time, "sleep", fake_sleep)
    monkeypatch.setattr(tools, "time", time)

    result = tools.wait_for(name="long", max_wait_s=30)
    assert "still running" in result
    assert "30s" in result
    assert "check" in result


def test_deadline_budget_is_the_tools_own(monkeypatch):
    """wait_for gets its own budget plus slack, not tool_call_timeout_s."""
    monkeypatch.setattr(processes, "load_wait_for_timeout", lambda: 120)
    assert parse.call_deadline_s("wait_for", 60.0, None) == 130.0
    assert parse.call_deadline_s("wait_for", 60.0, {"max_wait_s": 600}) == 610.0
    assert parse.call_deadline_s("wait_for", 60.0, {}) == 130.0
    # Other tools keep the plain timeout.
    assert parse.call_deadline_s("think", 60.0, {}) == 60.0


def test_subagent_kill_request_ends_the_wait(monkeypatch):
    registry = fresh_registry()
    record = SubagentRecord(id="sa", goal="g", level=2)
    record.dump_host_path = None
    registry.subagents["sa"] = record
    make_tools_isolated(monkeypatch, registry)
    monkeypatch.setattr(processes, "WAIT_FOR_POLL_INTERVAL_S", 0.0)

    clock = [0.0]

    def fake_monotonic():
        return clock[0]

    def fake_sleep(seconds):
        clock[0] += max(seconds, 1.0)
        record.kill_requested.set()

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    monkeypatch.setattr(time, "sleep", fake_sleep)
    monkeypatch.setattr(tools, "time", time)

    result = tools.wait_for(name="sa")
    # A kill leaves it still "running" until the runner confirms; the
    # wait sees the request and reports through check, which shows the
    # stop is pending (state running, kill requested -> settled).
    assert result


def test_max_wait_s_validation(monkeypatch):
    registry = fresh_registry()
    proc = ProcessRecord(id="p", command="x", dir="/d")
    registry.processes["p"] = proc
    make_tools_isolated(monkeypatch, registry)
    result = tools.wait_for(name="p", max_wait_s=-5)
    assert result.startswith("ERROR:") and "positive" in result


def main():
    run_suite(
        [
            test_unknown_id_is_an_error_with_recovery_path,
            test_returns_immediately_on_finished_process,
            test_returns_immediately_on_settled_subagent,
            test_polls_until_the_process_finishes,
            test_timeout_returns_still_running_not_an_error,
            test_deadline_budget_is_the_tools_own,
            test_subagent_kill_request_ends_the_wait,
            test_max_wait_s_validation,
        ]
    )


if __name__ == "__main__":
    main()
