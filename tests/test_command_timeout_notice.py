"""Tests for the explicit timeout notice on execute_command results.

When the pod-side `timeout` wrapper kills a command (exit codes 124 and
137), the tool result must say exactly what happened and state the
maximum runtime of one execute_command call. The harness detach notice
must do the same for execute_command specifically, where it must be
clear that the command limit did not fire and the container call itself
stopped responding.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import MonkeyPatch, run_suite

import syna.capabilities.tools as tools
from syna.capabilities.tools import command_runtime_limit_s, execute_command
from syna.protocol.parse import build_detached_call_notice
from syna.state import pop_last_exit_code


class FakeContainer:
    """Answers the runner exec with nothing and the reader exec with a
    canned exit-code view, standing in for the pod."""

    def __init__(self, exit_code):
        self.exit_code = exit_code

    def exec(self, argv, **kwargs):
        script = argv[2] if len(argv) > 2 else ""
        if "SYNA_EXIT" in script:
            if self.exit_code is None:
                return "partial output"
            return f"partial output\n<<<SYNA_EXIT={self.exit_code}>>>"
        return ""


def _run_command(patch: MonkeyPatch, exit_code) -> str:
    patch.setattr(tools, "get_container", lambda: FakeContainer(exit_code))
    patch.setattr(
        tools,
        "get_call_output_path",
        lambda: "/tool_outputs/0001_execute_command.txt",
    )
    patch.setattr(tools, "load_command_timeout_s", lambda: 50)
    patch.setattr(tools, "load_tool_call_timeout_s", lambda: 60)
    try:
        return execute_command(command="sleep 100")
    finally:
        pop_last_exit_code()


def test_timeout_124_gets_explicit_notice(monkeypatch: MonkeyPatch):
    result = _run_command(monkeypatch, 124)
    assert result.startswith("partial output")
    assert "TIMEOUT: this execute_command call failed." in result
    assert "exit code 124" in result
    assert "50 seconds is the maximum runtime" in result
    assert "nohup" in result
    print("PASS a timeout-killed command states what happened and the limit")


def test_timeout_137_notice_hedges_the_cause(monkeypatch: MonkeyPatch):
    result = _run_command(monkeypatch, 137)
    assert "exit code 137" in result
    assert "50 seconds is the maximum runtime" in result
    print("PASS a SIGKILL timeout result hedges between kill and system kill")


def test_successful_command_has_no_notice(monkeypatch: MonkeyPatch):
    result = _run_command(monkeypatch, 0)
    assert result == "partial output"
    print("PASS successful commands carry no timeout notice")


def test_command_runtime_limit_is_bounded_by_detach_budget(
    monkeypatch: MonkeyPatch,
):
    monkeypatch.setattr(tools, "load_command_timeout_s", lambda: 200)
    monkeypatch.setattr(tools, "load_tool_call_timeout_s", lambda: 60)
    assert command_runtime_limit_s() == 55

    monkeypatch.setattr(tools, "load_command_timeout_s", lambda: 50)
    assert command_runtime_limit_s() == 50

    monkeypatch.setattr(tools, "load_command_timeout_s", lambda: 30)
    assert command_runtime_limit_s() == 30
    print("PASS the reported limit is the effective one, detach-bounded")


def test_detach_notice_states_execute_command_limit(monkeypatch: MonkeyPatch):
    monkeypatch.setattr(tools, "load_command_timeout_s", lambda: 200)
    monkeypatch.setattr(tools, "load_tool_call_timeout_s", lambda: 60)
    notice = build_detached_call_notice(
        "execute_command", {"command": "sleep 100"}, 60.0
    )
    assert "harness detach limit" in notice
    assert "55 seconds" in notice

    monkeypatch.setattr(tools, "load_command_timeout_s", lambda: 50)
    notice = build_detached_call_notice(
        "execute_command", {"command": "sleep 100"}, 60.0
    )
    assert "50 seconds" in notice

    other = build_detached_call_notice("web_search", {"query": "x"}, 60.0)
    assert "harness detach limit" not in other
    print("PASS the detach notice gives execute_command's own limit")


def main():
    run_suite(
        [
            test_timeout_124_gets_explicit_notice,
            test_timeout_137_notice_hedges_the_cause,
            test_successful_command_has_no_notice,
            test_command_runtime_limit_is_bounded_by_detach_budget,
            test_detach_notice_states_execute_command_limit,
        ]
    )


if __name__ == "__main__":
    main()
