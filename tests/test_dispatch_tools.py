"""Tests for the process/subagent dispatch tools and max_runtime_s.

The dispatch tools (`process`, `subagent`) take an `action` plus an
`args` object. These tests cover the tool-body layer: action routing,
spawn argument pass-through (name, max_runtime_s), validation errors,
the model-facing tool schema, which must declare `action` as the
required entry point and describe max_runtime_s, and the dispatch
worker's propagation of the spawning subagent's record.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna.capabilities import tools
from syna.capabilities.subagents import SubagentRecord
from syna.protocol import parse
from syna.protocol.tool_schemas import get_tool_definitions
from syna.state import get_background_record, set_background_record


def test_dispatch_propagates_the_parent_record(monkeypatch):
    """A tool call dispatched inside a subagent's turn sees that record."""
    record = SubagentRecord(id="par", goal="g", level=2)
    seen = {}

    def probe():
        seen["record"] = get_background_record()
        return "probed"

    monkeypatch.setitem(tools.TOOL_REGISTRY, "background_probe", probe)
    set_background_record(record)
    try:
        outcome = parse.parse_and_execute_tools(
            [
                {
                    "id": "call_probe",
                    "type": "function",
                    "function": {"name": "background_probe", "arguments": "{}"},
                }
            ],
            on_tool_event=None,
            tool_timeout_s=5,
        )
    finally:
        set_background_record(None)
    assert seen["record"] is record, "the dispatch worker lost the parent"
    assert get_background_record() is None
    assert outcome.outputs and outcome.outputs[0]["output"] == "probed"


def test_dispatch_clears_the_parent_record_after_the_call():
    set_background_record(None)
    seen = {}

    def probe():
        seen["record"] = get_background_record()
        return "probed"

    tools.TOOL_REGISTRY["background_probe"] = probe
    try:
        parse.parse_and_execute_tools(
            [
                {
                    "id": "call_probe",
                    "type": "function",
                    "function": {"name": "background_probe", "arguments": "{}"},
                }
            ],
            on_tool_event=None,
            tool_timeout_s=5,
        )
    finally:
        tools.TOOL_REGISTRY.pop("background_probe", None)
    assert seen["record"] is None, "top-level dispatch has no parent"
    assert get_background_record() is None


def _captured(monkeypatch, target_name, return_value):
    """Replace a spawn function with a recorder returning return_value."""
    captured = {}

    def fake_spawn(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return return_value

    monkeypatch.setattr(tools, target_name, fake_spawn)
    return captured


def test_process_spawn_passes_name_and_runtime_cap(monkeypatch):
    captured = _captured(
        monkeypatch, "spawn_process", {"name": "ticker", "max_runtime_s": 5}
    )
    result = tools.process(
        "spawn",
        {"command": "sleep 1", "name": "ticker", "max_runtime_s": 5},
    )
    assert result == {"name": "ticker", "max_runtime_s": 5}
    assert captured["args"] == ("sleep 1",)
    assert captured["kwargs"] == {"name": "ticker", "runtime_cap_s": 5}
    print("PASS process spawn forwards name and max_runtime_s")


def test_process_spawn_without_cap_sends_none(monkeypatch):
    captured = _captured(monkeypatch, "spawn_process", {"name": "auto_0001"})
    tools.process("spawn", {"command": "sleep 1"})
    assert captured["kwargs"]["runtime_cap_s"] is None
    assert captured["kwargs"]["name"] is None
    print("PASS process spawn omits the cap when the model does")


def test_process_rejects_invalid_runtime_caps(monkeypatch):
    _captured(monkeypatch, "spawn_process", {})
    for value in ("5", 0, -3, 2.5, True, [5]):
        result = tools.process("spawn", {"command": "sleep 1", "max_runtime_s": value})
        assert result.startswith("ERROR: process spawn:"), (value, result)
        assert "positive integer" in result
    print("PASS process spawn rejects non-positive-integer caps")


def test_process_accepts_integral_float_cap(monkeypatch):
    captured = _captured(monkeypatch, "spawn_process", {"name": "p"})
    tools.process("spawn", {"command": "sleep 1", "max_runtime_s": 5.0})
    assert captured["kwargs"]["runtime_cap_s"] == 5
    print("PASS process spawn accepts an integral float cap")


def test_subagent_spawn_passes_name_and_runtime_cap(monkeypatch):
    captured = _captured(
        monkeypatch, "spawn_subagent", {"name": "worker", "max_runtime_s": 90}
    )
    result = tools.subagent(
        "spawn", {"goal": "do it", "name": "worker", "max_runtime_s": 90}
    )
    assert result["max_runtime_s"] == 90
    assert captured["kwargs"] == {"name": "worker", "runtime_cap_s": 90}
    print("PASS subagent spawn forwards name and max_runtime_s")


def test_subagent_rejects_invalid_runtime_caps(monkeypatch):
    _captured(monkeypatch, "spawn_subagent", {})
    result = tools.subagent("spawn", {"goal": "x", "max_runtime_s": "90"})
    assert result.startswith("ERROR: subagent spawn:")
    print("PASS subagent spawn rejects non-integer caps")


def test_unknown_action_errors_name_the_action():
    for tool_name, tool in (("process", tools.process), ("subagent", tools.subagent)):
        result = tool("pause", {})
        assert result == (
            f"ERROR: unknown {tool_name} action 'pause'. Available"
            " actions: check, kill, spawn"
        ), result
    print("PASS unknown actions are reported as actions")


def test_tool_schemas_declare_action_and_document_the_cap():
    schemas = {
        schema["function"]["name"]: schema["function"]
        for schema in get_tool_definitions()
    }
    for name in ("process", "subagent"):
        function = schemas[name]
        parameters = function["parameters"]
        assert "action" in parameters["properties"], name
        assert parameters["required"] == ["action", "args"], (
            name,
            parameters["required"],
        )
        assert "max_runtime_s" in function["description"], name
        action_description = parameters["properties"]["action"]["description"]
        assert "spawn" in action_description and "kill" in action_description, (
            name,
            action_description,
        )
    print("PASS tool schemas declare action and document max_runtime_s")


def main():
    run_suite(
        [
            test_process_spawn_passes_name_and_runtime_cap,
            test_process_spawn_without_cap_sends_none,
            test_process_rejects_invalid_runtime_caps,
            test_process_accepts_integral_float_cap,
            test_subagent_spawn_passes_name_and_runtime_cap,
            test_subagent_rejects_invalid_runtime_caps,
            test_unknown_action_errors_name_the_action,
            test_tool_schemas_declare_action_and_document_the_cap,
            test_dispatch_propagates_the_parent_record,
            test_dispatch_clears_the_parent_record_after_the_call,
        ]
    )


if __name__ == "__main__":
    main()
