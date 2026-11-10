"""Tests for the CLI's background-work commands.

Covers /jobs listing and per-record inspection, /kill dispatch to the
right registry kind (running off the prompt thread, reporting the
outcome), the no-pod and unknown-id paths, and the terminal's
background_done event line.
"""

import builtins
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna.capabilities import processes, subagents
from syna.conversation import ask, terminal as terminal_module
from syna.conversation.terminal import TerminalChat, TerminalSession


class StubSession:
    """AgentSession stand-in with the calls the command paths touch."""

    def __init__(self):
        self.calls = []

    def clear_cancel(self):
        self.calls.append("clear")

    def cancel(self, reason="user interrupt"):
        self.calls.append(("cancel", reason))

    def run_turn(self, text):
        self.calls.append(("turn", text))

    def close(self):
        self.calls.append("close")


class FakeRecord:
    """The fields the listing and summary helpers read."""

    def __init__(self, id, state="running", started=1.0, command="", goal=""):
        self.id = id
        self.state = state
        self.started = started
        self.command = command
        self.goal = goal
        self.exit_code = 0 if state != "running" else None
        self.killed_by = None
        self.end_reason = None if state == "running" else "finished"
        self.stopped_by = None
        self.dir = "/pod/dir"


def _make_registry(process_records=(), subagent_records=()):
    registry = processes.PodRegistry()
    for record in process_records:
        registry.processes[record.id] = record
    for record in subagent_records:
        registry.subagents[record.id] = record
    return registry


def _make_chat(script, registry=None, container=object(), monkeypatch=None):
    """A TerminalChat whose conversation sees a fixed pod and registry."""
    said = []
    conversation = TerminalSession(StubSession(), notify=said.append)
    conversation.container_provider = lambda: container
    chat = TerminalChat(
        terminal=conversation,
        conversation_factory=lambda: conversation,
        say=said.append,
        read_line=_ScriptedInput(script),
    )
    if registry is not None and monkeypatch is not None:
        monkeypatch.setattr(
            terminal_module.processes, "registry_for", lambda c: registry
        )
    return chat, said


class _ScriptedInput:
    """A prompt stand-in: lines, interrupt sentinels, and sync hooks."""

    def __init__(self, script):
        self.script = list(script)

    def __call__(self, prompt=""):
        item = self.script.pop(0)
        if item is KeyboardInterrupt:
            raise KeyboardInterrupt
        if callable(item):
            item()
            return ""
        return item


def test_jobs_lists_processes_and_subagents(monkeypatch):
    registry = _make_registry(
        [
            FakeRecord("p-01", command="sleep 30"),
            FakeRecord("p-02", state="done", command="ls"),
        ],
        [FakeRecord("sa-01", goal="check the logs")],
    )
    chat, said = _make_chat(
        ["/jobs", KeyboardInterrupt], registry=registry, monkeypatch=monkeypatch
    )
    chat.run()
    joined = "\n".join(said)
    assert "process  p-01" in joined and "sleep 30" in joined
    assert "process  p-02" in joined and "exited with code 0" in joined
    assert "subagent sa-01" in joined and "check the logs" in joined
    print("PASS /jobs lists every record with its state and subject")


def test_jobs_with_no_work_says_so(monkeypatch):
    chat, said = _make_chat(
        ["/jobs", KeyboardInterrupt], registry=_make_registry(), monkeypatch=monkeypatch
    )
    chat.run()
    assert any("No background work" in line for line in said)
    print("PASS an empty registry reports no background work")


def test_jobs_without_a_pod_reports_it(monkeypatch):
    chat, said = _make_chat(["/jobs", KeyboardInterrupt], monkeypatch=monkeypatch)
    chat.terminal.container_provider = lambda: None
    chat.run()
    assert any("pod is not running" in line for line in said)
    print("PASS /jobs without a pod reports the pod is not running")


def test_jobs_id_shows_a_process_report(monkeypatch):
    registry = _make_registry([FakeRecord("p-01", command="sleep 30")])
    seen = {}
    monkeypatch.setattr(
        terminal_module.processes,
        "check_process_state",
        lambda container, pod_dir: (
            seen.update(container=container, pod_dir=pod_dir)
            or ("running", "hello", "", None)
        ),
    )
    monkeypatch.setattr(
        terminal_module.processes,
        "format_process_report",
        lambda record, state, stdout, stderr: f"report {record.id} {state} {stdout}",
    )
    chat, said = _make_chat(
        ["/jobs p-01", KeyboardInterrupt], registry=registry, monkeypatch=monkeypatch
    )
    chat.run()
    assert any("report p-01 running hello" in line for line in said)
    assert seen["pod_dir"] == "/pod/dir"
    print("PASS /jobs <id> shows a process's state and output tail")


def test_jobs_id_shows_a_subagent_report(monkeypatch):
    registry = _make_registry(subagent_records=[FakeRecord("sa-01", goal="goal text")])
    seen = {}
    monkeypatch.setattr(
        terminal_module.subagents,
        "check_subagent",
        lambda subagent_id, registry=None, container=None: (
            seen.update(id=subagent_id) or f"subagent report {subagent_id}"
        ),
    )
    chat, said = _make_chat(
        ["/jobs sa-01", KeyboardInterrupt], registry=registry, monkeypatch=monkeypatch
    )
    chat.run()
    assert any("subagent report sa-01" in line for line in said)
    print("PASS /jobs <id> shows a subagent's report")


def test_jobs_unknown_id_reports_the_registry_error(monkeypatch):
    chat, said = _make_chat(
        ["/jobs nope", KeyboardInterrupt],
        registry=_make_registry(),
        monkeypatch=monkeypatch,
    )
    chat.run()
    assert any("nope" in line and "!" in line for line in said)
    print("PASS an unknown id reports the registry's error")


def test_kill_dispatches_to_the_process_kind(monkeypatch):
    registry = _make_registry([FakeRecord("p-01", command="sleep 30")])
    seen = {}
    monkeypatch.setattr(
        terminal_module.processes,
        "kill_process",
        lambda process_id, registry=None, container=None, by="model": (
            seen.update(id=process_id, by=by) or "Process 'p-01': stopped."
        ),
    )
    chat, said = _make_chat(
        ["/kill p-01", KeyboardInterrupt], registry=registry, monkeypatch=monkeypatch
    )
    chat.run()
    assert _wait(lambda: seen.get("id") == "p-01")
    assert seen["by"] == "user"
    assert any("Stopped 'p-01'" in line for line in said)
    print("PASS /kill stops a process as a user kill")


def test_kill_dispatches_to_the_subagent_kind(monkeypatch):
    registry = _make_registry(subagent_records=[FakeRecord("sa-01", goal="g")])
    seen = {}
    monkeypatch.setattr(
        terminal_module.subagents,
        "kill_subagent",
        lambda subagent_id, registry=None, container=None, by="model": (
            seen.update(id=subagent_id, by=by) or "Subagent 'sa-01': stopped."
        ),
    )
    chat, said = _make_chat(
        ["/kill sa-01", KeyboardInterrupt], registry=registry, monkeypatch=monkeypatch
    )
    chat.run()
    assert _wait(lambda: seen.get("id") == "sa-01")
    assert seen["by"] == "user"
    assert any("Stopped 'sa-01'" in line for line in said)
    print("PASS /kill stops a subagent as a user kill")


def test_kill_keeps_the_prompt_live_while_killing(monkeypatch):
    registry = _make_registry([FakeRecord("p-01")])
    release = threading.Event()
    seen = {}
    state = {}

    def slow_kill(process_id, registry=None, container=None, by="model"):
        release.wait(timeout=5)
        seen["done"] = True
        return "Process 'p-01': stopped."

    monkeypatch.setattr(
        terminal_module.processes, "kill_process", lambda *a, **k: slow_kill(*a, **k)
    )
    chat, said = _make_chat(
        [
            "/kill p-01",
            lambda: state.update(prompt_free=not seen.get("done", False)),
            KeyboardInterrupt,
        ],
        registry=registry,
        monkeypatch=monkeypatch,
    )
    chat.run()
    release.set()
    assert _wait(lambda: seen.get("done") is True)
    assert state["prompt_free"] is True
    assert any("Stopped 'p-01'" in line for line in said)
    print("PASS a slow kill runs off the prompt thread and reports late")


def test_kill_without_an_id_shows_usage(monkeypatch):
    chat, said = _make_chat(
        ["/kill", KeyboardInterrupt],
        registry=_make_registry(),
        monkeypatch=monkeypatch,
    )
    chat.run()
    assert any("Usage: /kill <id>" in line for line in said)
    print("PASS /kill without an id shows its usage")


def test_kill_on_a_finished_record_reports_its_outcome(monkeypatch):
    registry = _make_registry([FakeRecord("p-01", state="done", command="ls")])
    monkeypatch.setattr(
        terminal_module.processes,
        "kill_process",
        lambda process_id, registry=None, container=None, by="model": (
            "Process 'p-01': exited with code 0."
        ),
    )
    chat, said = _make_chat(
        ["/kill p-01", KeyboardInterrupt], registry=registry, monkeypatch=monkeypatch
    )
    chat.run()
    assert any("Stopped 'p-01'" in line for line in said)
    assert not any(line.startswith("  !") for line in said)
    print("PASS killing a finished record reports its outcome, not an error")


def test_background_done_renders_a_summary_line():
    printed = []
    original_print = builtins.print
    builtins.print = lambda *args, **kwargs: printed.append(
        " ".join(str(a) for a in args)
    )
    try:
        ask._print_event(
            {
                "type": "background_done",
                "id": "p-01",
                "kind": "process",
                "summary": "exited with code 0",
            }
        )
    finally:
        builtins.print = original_print
    assert any(
        "Background process 'p-01': exited with code 0" in line for line in printed
    )
    print("PASS a finished background record prints its outcome line")


def _wait(predicate, timeout_s: float = 15.0) -> bool:
    """Poll a predicate until it holds or the timeout passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


if __name__ == "__main__":
    run_suite(
        [
            test_jobs_lists_processes_and_subagents,
            test_jobs_with_no_work_says_so,
            test_jobs_without_a_pod_reports_it,
            test_jobs_id_shows_a_process_report,
            test_jobs_id_shows_a_subagent_report,
            test_jobs_unknown_id_reports_the_registry_error,
            test_kill_dispatches_to_the_process_kind,
            test_kill_dispatches_to_the_subagent_kind,
            test_kill_keeps_the_prompt_live_while_killing,
            test_kill_without_an_id_shows_usage,
            test_kill_on_a_finished_record_reports_its_outcome,
            test_background_done_renders_a_summary_line,
        ]
    )
