"""Tests for output cleaning, bounded rendering, and pod-side output persistence."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna.output import (
    NO_OUTPUT_TEXT,
    bounded_output,
    collapse_progress_lines,
    head_and_tail,
    strip_ansi_escapes,
)
from syna.parse import parse_and_execute_tools
from syna.state import register_container, unregister_container
from syna.tools import TOOL_REGISTRY


class FakePod:
    """Records exec and copy_to calls, standing in for a bollard Container."""

    def __init__(self):
        self.exec_calls: list[list[str]] = []
        self.copied: list[tuple[str, str]] = []

    def exec(self, argv, **kwargs):
        self.exec_calls.append(argv)
        return ""

    def copy_to(self, host_path: str, pod_dir: str):
        content = None
        host_file = Path(host_path)
        if host_file.exists():
            content = host_file.read_text(encoding="utf-8")
        self.copied.append((host_path, pod_dir, content))


def test_strip_ansi_escapes():
    text = "\x1b[32mgreen\x1b[0m plain \x1b[2Kerased"
    assert strip_ansi_escapes(text) == "green plain erased"


def test_collapse_progress_lines():
    text = "downloading 10%\rdownloading 50%\rdownloading 100%\ndone"
    assert collapse_progress_lines(text) == "downloading 100%\ndone"


def test_head_and_tail_short_text_untouched():
    text = "short"
    assert head_and_tail(text, 100, "/tool_outputs/a.txt") == text


def test_head_and_tail_keeps_both_ends():
    text = "A" * 500 + "MIDDLE" + "B" * 500
    cut = head_and_tail(text, 200, "/tool_outputs/a.txt")
    assert cut.startswith("A" * 100)
    assert cut.endswith("B" * 100)
    assert "MIDDLE" not in cut
    assert "806 characters omitted in /tool_outputs/a.txt" in cut


def test_bounded_output_empty_becomes_placeholder():
    assert bounded_output("", 100) == NO_OUTPUT_TEXT
    assert bounded_output("\n  \r\n", 100) == NO_OUTPUT_TEXT


def test_bounded_output_cleans_before_counting():
    text = "\x1b[31m" + "x" * 50 + "\x1b[0m"
    result = bounded_output(text, 30, None)
    assert "\x1b" not in result
    assert "characters omitted" in result


def _parse_with_pod(tool_calls, pod: FakePod):
    register_container(pod)
    try:
        return parse_and_execute_tools(tool_calls, echo_errors=False)
    finally:
        unregister_container()


def test_persistence_saves_host_tool_output_to_pod():
    TOOL_REGISTRY["smoke_tool"] = lambda label="unlabeled": f"ran: {label}"
    pod = FakePod()
    try:
        call = [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "smoke_tool",
                    "arguments": json.dumps({"label": "persist"}),
                },
            }
        ]
        outcome = _parse_with_pod(call, pod)
    finally:
        TOOL_REGISTRY.pop("smoke_tool", None)

    assert outcome.structural_error is None
    saved_to = outcome.outputs[0]["saved_to"]
    assert saved_to is not None
    assert saved_to.startswith("/tool_outputs/")
    assert saved_to.endswith("_smoke_tool.txt")

    assert pod.exec_calls == [["bash", "-c", "mkdir -p /tool_outputs"]]
    assert len(pod.copied) == 1
    host_path, pod_dir, content = pod.copied[0]
    assert pod_dir == "/tool_outputs"
    assert Path(host_path).name == saved_to.rsplit("/", 1)[1]
    assert content == "ran: persist"


def test_persistence_skipped_for_pod_native_tools():
    pod = FakePod()

    def fake_execute(command: str = "true") -> str:
        from syna.state import set_last_exit_code

        set_last_exit_code(0)
        return "simulated view"

    original_exec = TOOL_REGISTRY["execute_command"]
    TOOL_REGISTRY["execute_command"] = fake_execute
    try:
        call = [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "execute_command",
                    "arguments": json.dumps({"command": "true"}),
                },
            }
        ]
        outcome = _parse_with_pod(call, pod)
    finally:
        TOOL_REGISTRY["execute_command"] = original_exec

    assert outcome.structural_error is None
    saved_to = outcome.outputs[0]["saved_to"]
    assert saved_to is not None
    assert saved_to.endswith("_execute_command.txt")
    assert pod.copied == []
    assert "simulated view" in outcome.outputs[0]["output"]


def test_persistence_failure_keeps_output_and_drops_saved_to():
    class BrokenPod(FakePod):
        def exec(self, argv, **kwargs):
            raise RuntimeError("pod offline")

    TOOL_REGISTRY["smoke_tool"] = lambda label="unlabeled": f"ran: {label}"
    try:
        call = [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "smoke_tool",
                    "arguments": json.dumps({"label": "persist"}),
                },
            }
        ]
        outcome = _parse_with_pod(call, BrokenPod())
    finally:
        TOOL_REGISTRY.pop("smoke_tool", None)

    assert outcome.structural_error is None
    assert outcome.outputs[0]["output"] == "ran: persist"
    assert "saved_to" not in outcome.outputs[0]


def main():
    test_strip_ansi_escapes()
    test_collapse_progress_lines()
    test_head_and_tail_short_text_untouched()
    test_head_and_tail_keeps_both_ends()
    test_bounded_output_empty_becomes_placeholder()
    test_bounded_output_cleans_before_counting()
    test_persistence_saves_host_tool_output_to_pod()
    test_persistence_skipped_for_pod_native_tools()
    test_persistence_failure_keeps_output_and_drops_saved_to()
    print("ALL OUTPUT AND PERSISTENCE TESTS PASSED")


if __name__ == "__main__":
    main()
