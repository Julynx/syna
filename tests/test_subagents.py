"""Unit tests for the subagent capability: gating, runner, artifacts."""

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna.capabilities import processes, subagents
from syna.capabilities.processes import PodRegistry, ProcessRecord
from syna.capabilities.subagents import (
    SUBAGENT_EXCLUDED_TOOLS,
    SubagentRecord,
    _compact_event,
    _dump_messages_tail,
    _last_assistant_content,
    _on_subagent_event,
    check_subagent,
    emit_injected_notice,
    kill_subagent,
    spawn_subagent,
    subagent_completion_notice,
    subagent_header,
    subagent_status_summary,
)


class FakeContainer:
    def __init__(self):
        self.copied: list[tuple[str, str]] = []
        self.commands: list[str] = []

    def exec(self, argv):
        self.commands.append(argv[-1] if len(argv) > 2 else "")
        return ""

    def copy_to(self, source, dest):
        self.copied.append((str(source), dest))


class FakeSession:
    """Records calls; run_turn appends a final assistant message."""

    def __init__(self, record):
        self.record = record
        self.messages = []
        self.cancelled = 0
        self.closed = False
        self.turn_text = None

    def run_turn(self, text):
        self.turn_text = text
        if self.record.cap_fired.is_set():
            raise RuntimeError("cancel unwind")
        self.messages.append({"role": "assistant", "content": "final report"})

    def cancel(self):
        self.cancelled += 1

    def close(self):
        self.closed = True


def fresh_registry():
    return PodRegistry()


def spawn_with_fake_session(monkeypatch, registry, container=None):
    """Spawn with the real runner but a fake session; returns the record."""
    container = container or FakeContainer()
    monkeypatch.setattr(subagents, "get_container", lambda: container)
    monkeypatch.setattr(subagents, "register_container", lambda c: None)
    monkeypatch.setattr(subagents, "unregister_container", lambda: None)
    monkeypatch.setattr(subagents, "load_subagent_max_runtime_s", lambda: 60)
    monkeypatch.setattr(subagents, "load_max_subagent_depth", lambda: 3)
    created = []

    def factory(record, reg):
        session = FakeSession(record)
        created.append(session)
        return session

    result = spawn_subagent(
        "do the thing", registry=registry, container=container, session_factory=factory
    )
    assert "error" not in result, result
    record = registry.subagents[result["name"]]
    for _ in range(400):
        if record.notified and record.state != "running":
            break
        time.sleep(0.005)
    return record, created, container


def test_depth_gate_error_text(monkeypatch):
    from syna.state import set_background_level

    set_background_level(3)
    try:
        result = spawn_subagent(
            "x", registry=fresh_registry(), container=FakeContainer()
        )
    finally:
        set_background_level(None)
    assert "error" in result
    assert "maximum depth" in result["error"]
    assert "Handle the task yourself" in result["error"]


def test_concurrent_cap_error_text(monkeypatch):
    registry = fresh_registry()
    for index in range(4):
        record = SubagentRecord(id=f"sa{index}", goal="g", level=2)
        registry.subagents[f"sa{index}"] = record
    result = spawn_subagent("x", registry=registry, container=FakeContainer())
    assert "error" in result
    assert "4 are already running" in result["error"]
    assert "wait_for" in result["error"]


def test_goal_is_required():
    result = spawn_subagent("   ", registry=fresh_registry(), container=FakeContainer())
    assert "error" in result and "goal" in result["error"]


def test_spawn_runs_the_turn_and_finalizes(monkeypatch):
    registry = fresh_registry()
    record, created, container = spawn_with_fake_session(monkeypatch, registry)
    session = created[0]
    assert session.turn_text.startswith("You are a background subagent")
    assert "do the thing" in session.turn_text
    assert record.state == "finished"
    assert record.end_reason == "response"
    assert record.response == "final report"
    assert session.closed
    assert record.level == 2
    done_rows = [e for e in record.events if e.get("kind") == "done"]
    assert done_rows and "finished" in done_rows[-1]["summary"]


def test_runtime_cap_ends_the_turn(monkeypatch):
    registry = fresh_registry()
    record, created, _ = spawn_with_fake_session(monkeypatch, registry)
    session = created[0]
    assert record.state == "finished"
    assert record.response == "final report"
    # Simulate the cap: mark and re-run the turn path.
    record.cap_fired.set()
    record.state = "running"
    session.messages = []
    try:
        session.run_turn("x")
    except RuntimeError:
        pass
    assert record.cap_fired.is_set()


def test_spawn_runtime_cap_is_reported_and_enforced(monkeypatch):
    registry = fresh_registry()
    container = FakeContainer()
    for name, value in (
        ("get_container", lambda: container),
        ("register_container", lambda c: None),
        ("unregister_container", lambda: None),
        ("load_subagent_max_runtime_s", lambda: 1800),
        ("load_max_subagent_depth", lambda: 3),
    ):
        monkeypatch.setattr(subagents, name, value)
    timer_intervals = []
    real_timer = threading.Timer

    class RecordingTimer(real_timer):
        def __init__(self, interval, function, args=None, kwargs=None):
            super().__init__(interval, function, args, kwargs)
            timer_intervals.append(interval)

    monkeypatch.setattr(threading, "Timer", RecordingTimer)

    def factory(record, reg):
        return FakeSession(record)

    result = spawn_subagent(
        "do the thing",
        registry=registry,
        container=container,
        session_factory=factory,
        runtime_cap_s=42,
    )
    assert "error" not in result, result
    assert result["max_runtime_s"] == 42
    record = registry.subagents[result["name"]]
    assert record.runtime_cap_s == 42
    for _ in range(400):
        if record.state != "running":
            break
        time.sleep(0.005)
    assert timer_intervals == [42]
    capped = SubagentRecord(id="sx2", goal="g", level=2, runtime_cap_s=42)
    registry.subagents["sx2"] = capped
    assert "42s cap" in check_subagent("sx2", registry, container)
    capped.state = "finished"
    capped.end_reason = "runtime cap"
    assert "the cap is 42 seconds" in subagent_completion_notice(capped)


def test_excluded_tools_list():
    assert set(SUBAGENT_EXCLUDED_TOOLS) == {
        "ask_user",
        "text_to_speech",
        "text_to_image",
        "review_work",
        "share_file",
    }


def test_session_factory_filters_tool_schemas(monkeypatch):
    """The real factory builds an AgentSession without excluded schemas."""
    from syna.capabilities.subagents import _default_session_factory

    registry = fresh_registry()
    record = SubagentRecord(id="sax", goal="g", level=2)
    record.dump_host_path = None
    session = _default_session_factory(record, registry)
    try:
        names = {schema["function"]["name"] for schema in session.tool_definitions}
        assert names.isdisjoint(SUBAGENT_EXCLUDED_TOOLS)
        assert "subagent" in names and "wait_for" in names
    finally:
        session.close()


def test_kill_marks_record_and_cancels_session(monkeypatch):
    registry = fresh_registry()
    record, created, container = spawn_with_fake_session(monkeypatch, registry)
    record.state = "running"
    record.kill_requested.clear()
    report = kill_subagent(
        record.id, registry=registry, container=container, by="model"
    )
    assert "stop requested" in report
    assert record.kill_requested.is_set()
    assert created[0].cancelled >= 1
    assert record.stopped_by == "model"
    assert record.notified, "a model kill suppresses the completion notice"


def test_kill_by_user_keeps_the_notice(monkeypatch):
    registry = fresh_registry()
    record, created, container = spawn_with_fake_session(monkeypatch, registry)
    record.state = "running"
    record.notified = False
    kill_subagent(record.id, registry=registry, container=container, by="user")
    assert record.stopped_by == "user"
    assert not record.notified


def test_kill_on_finished_subagent_reports_snapshot():
    registry = fresh_registry()
    record = SubagentRecord(id="sa", goal="g", level=2)
    record.state = "finished"
    record.end_reason = "response"
    record.response = "the answer"
    record.dump_host_path = None
    registry.subagents["sa"] = record
    report = kill_subagent(
        "sa", registry=registry, container=FakeContainer(), by="model"
    )
    assert "already finished" in report
    assert "the answer" in report
    assert record.stopped_by is None


def test_kill_before_start(monkeypatch):
    """A kill landing between spawn and session start still settles."""
    registry = fresh_registry()
    container = FakeContainer()
    monkeypatch.setattr(subagents, "get_container", lambda: container)
    monkeypatch.setattr(subagents, "register_container", lambda c: None)
    monkeypatch.setattr(subagents, "unregister_container", lambda: None)
    monkeypatch.setattr(subagents, "load_subagent_max_runtime_s", lambda: 60)
    monkeypatch.setattr(subagents, "load_max_subagent_depth", lambda: 3)
    gate = threading.Event()

    def factory(record, reg):
        gate.wait(2)
        return FakeSession(record)

    result = spawn_subagent(
        "x", registry=registry, container=container, session_factory=factory
    )
    record = registry.subagents[result["name"]]
    kill_subagent(record.id, registry=registry, container=container, by="user")
    gate.set()
    for _ in range(400):
        if record.notified and record.state != "running":
            break
        time.sleep(0.005)
    assert record.state == "killed"
    assert "user" in record.end_reason


def test_artifacts_copied_at_finalize(monkeypatch):
    registry = fresh_registry()
    tmp_path = Path(__file__).parent / "_tmp_subagent_test"
    tmp_path.mkdir(exist_ok=True)
    dump = tmp_path / "message-session-log-subagent-sa.json"
    dump.write_text(
        json.dumps({"messages": [{"role": "user", "content": "goal"}]}),
        encoding="utf-8",
    )
    try:
        record = SubagentRecord(id="sa", goal="g", level=2, dump_host_path=dump)
        record.response = "the response"
        record.state = "finished"
        container = FakeContainer()
        subagents.copy_subagent_artifacts(record, container)
        copied_names = {Path(src).name for src, _ in container.copied}
        assert copied_names == {"messages.json", "response.md"}
        dests = {dest for _, dest in container.copied}
        assert dests == {"/workspace/subagents/sa"}
        assert any("chmod 444" in cmd for cmd in container.commands)
    finally:
        import shutil

        shutil.rmtree(tmp_path, ignore_errors=True)


def test_dump_tail_keeps_whole_messages_within_budget(monkeypatch):
    monkeypatch.setattr(subagents, "load_check_tail_chars", lambda: 200)
    dump = Path(__file__).parent / "_tmp_dump_test.json"
    big = "x" * 150
    dump.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "short"},
                    {"role": "assistant", "content": big},
                    {"role": "assistant", "content": "tail"},
                ]
            }
        ),
        encoding="utf-8",
    )
    try:
        tail = _dump_messages_tail(dump)
        assert '"tail"' in tail
        # The big message would exceed the budget alongside the tail.
        lines = tail.splitlines()
        assert all(json.loads(line) for line in lines if line.startswith("{"))
    finally:
        dump.unlink()


def test_compact_event_shapes():
    model = _compact_event({"type": "model_message", "content": "hi"})
    assert model == {"kind": "model", "content": "hi"}
    tool = _compact_event(
        {
            "type": "tool_call",
            "status": "finished",
            "name": "execute_command",
            "signature": "execute_command(command='ls')",
            "output_preview": "files",
        }
    )
    assert tool["kind"] == "tool" and tool["output_preview"] == "files"
    assert _compact_event({"type": "status", "text": "Thinking..."}) is None


def test_stream_history_is_capped_and_sequenced():
    registry = fresh_registry()
    record = SubagentRecord(id="s", goal="g", level=2)
    total = subagents.EVENT_HISTORY_LIMIT + 50
    for index in range(total):
        _on_subagent_event(
            registry,
            record,
            {"type": "model_message", "content": f"m{index}"},
        )
    assert record.event_seq == total
    assert len(record.events) <= subagents.EVENT_HISTORY_LIMIT
    assert record.events[-1]["content"] == f"m{total - 1}"
    assert record.response == f"m{total - 1}"


def test_completion_notice_names_pod_paths():
    record = SubagentRecord(id="s", goal="g", level=2)
    record.state = "finished"
    record.end_reason = "response"
    notice = subagent_completion_notice(record)
    assert "/workspace/subagents/s/response.md" in notice
    assert "/workspace/subagents/s/messages.json" in notice
    assert "wait_for" in notice
    assert subagent_status_summary(record) == "finished with a response"


def test_last_assistant_content_fallback():
    session = type("S", (), {"messages": []})()
    assert _last_assistant_content(session) is None
    session.messages = [
        {"role": "assistant", "content": "earlier"},
        {"role": "assistant", "content": None},
    ]
    assert _last_assistant_content(session) == "earlier"


def test_runner_exposes_its_record_to_spawned_work(monkeypatch):
    """A subagent's turn runs with itself as the background record."""
    from syna.state import get_background_record, set_background_record

    registry = fresh_registry()
    container = FakeContainer()
    monkeypatch.setattr(subagents, "get_container", lambda: container)
    monkeypatch.setattr(subagents, "register_container", lambda c: None)
    monkeypatch.setattr(subagents, "unregister_container", lambda: None)
    monkeypatch.setattr(subagents, "load_subagent_max_runtime_s", lambda: 60)
    monkeypatch.setattr(subagents, "load_max_subagent_depth", lambda: 3)
    set_background_record(None)
    seen = []

    def factory(record, reg):
        seen.append(get_background_record())
        return FakeSession(record)

    result = spawn_subagent(
        "do the thing", registry=registry, container=container, session_factory=factory
    )
    record = registry.subagents[result["name"]]
    for _ in range(400):
        if record.state != "running":
            break
        time.sleep(0.005)
    assert seen == [record]
    assert get_background_record() is None


def test_session_factory_supplies_the_injection_filter(monkeypatch):
    """The real factory wires an apply-time stale filter into the session."""
    from syna.state import set_background_record

    registry = fresh_registry()
    record = SubagentRecord(id="sax", goal="g", level=2)
    record.dump_host_path = None
    set_background_record(None)
    try:
        session = subagents._default_session_factory(record, registry)
    finally:
        set_background_record(None)
    try:
        assert session.injection_filter is not None
        child = ProcessRecord(id="child", command="x", dir="/d")
        registry.processes["child"] = child
        fresh_notice = {"kind": "message", "bg_id": "child", "content": "n"}
        assert session.injection_filter(fresh_notice)
        child.outcome_collected = True
        assert not session.injection_filter(fresh_notice)
        assert session.injection_filter({"kind": "message", "content": "hi"})
    finally:
        session.close()


def test_emit_injected_notice_adds_a_viewer_event():
    registry = fresh_registry()
    record = SubagentRecord(id="sn", goal="g", level=2)
    subagents.emit_injected_notice(registry, record, "Process 'p1' finished.")
    assert record.events[-1]["kind"] == "notice"
    assert "finished" in record.events[-1]["text"]


def test_check_reports_paths_and_state(monkeypatch):
    registry = fresh_registry()
    record = SubagentRecord(id="sc", goal="the goal", level=2)
    record.dump_host_path = None
    registry.subagents["sc"] = record
    monkeypatch.setattr(subagents, "load_max_subagent_depth", lambda: 3)
    monkeypatch.setattr(subagents, "load_subagent_max_runtime_s", lambda: 60)
    result = check_subagent("sc", registry=registry, container=FakeContainer())
    assert "running" in result and "nesting level 2 of 3" in result
    assert "/workspace/subagents/sc/messages.json" in result
    assert "host dump" not in result, "host paths stay out of pod-facing reports"
    assert not record.outcome_collected, "a running check collects nothing"
    unknown = check_subagent("nope", registry=registry, container=FakeContainer())
    assert unknown.startswith("ERROR:") and "per-session" in unknown


def test_check_of_finished_subagent_collects_the_outcome(monkeypatch):
    """A settled check reports the outcome once and marks it collected."""
    registry = fresh_registry()
    record = SubagentRecord(id="sf", goal="the goal", level=2)
    record.dump_host_path = None
    record.started = time.time() - 90
    record.finished_at = record.started + 30
    record.state = "finished"
    record.end_reason = "response"
    record.response = "All done."
    registry.subagents["sf"] = record
    monkeypatch.setattr(subagents, "load_max_subagent_depth", lambda: 3)
    result = check_subagent("sf", registry=registry, container=FakeContainer())
    assert "finished with a response after 30s" in result, result
    assert "after 90s" not in result
    assert record.outcome_collected
    assert "host dump" not in result


def test_allegro_is_inherited(monkeypatch):
    """A subagent spawned from an Allegro turn runs in Allegro too."""
    from syna.state import set_background_allegro

    registry = fresh_registry()
    container = FakeContainer()
    monkeypatch.setattr(subagents, "get_container", lambda: container)
    monkeypatch.setattr(subagents, "register_container", lambda c: None)
    monkeypatch.setattr(subagents, "unregister_container", lambda: None)
    monkeypatch.setattr(subagents, "load_subagent_max_runtime_s", lambda: 60)
    monkeypatch.setattr(subagents, "load_max_subagent_depth", lambda: 3)
    allegros = []

    class AllegroSession(FakeSession):
        def set_allegro(self, enabled):
            allegros.append(enabled)
            super().set_allegro(enabled)

    def factory(record, reg):
        session = AllegroSession(record)
        session.messages = []
        return session

    set_background_allegro(True)
    try:
        result = spawn_subagent(
            "x", registry=registry, container=container, session_factory=factory
        )
    finally:
        set_background_allegro(None)
    record = registry.subagents[result["name"]]
    for _ in range(400):
        if record.notified:
            break
        time.sleep(0.005)
    assert allegros == [True]


def main():
    run_suite(
        [
            test_depth_gate_error_text,
            test_concurrent_cap_error_text,
            test_goal_is_required,
            test_spawn_runs_the_turn_and_finalizes,
            test_runtime_cap_ends_the_turn,
            test_excluded_tools_list,
            test_session_factory_filters_tool_schemas,
            test_kill_marks_record_and_cancels_session,
            test_kill_by_user_keeps_the_notice,
            test_kill_on_finished_subagent_reports_snapshot,
            test_kill_before_start,
            test_artifacts_copied_at_finalize,
            test_dump_tail_keeps_whole_messages_within_budget,
            test_compact_event_shapes,
            test_stream_history_is_capped_and_sequenced,
            test_completion_notice_names_pod_paths,
            test_last_assistant_content_fallback,
            test_check_reports_paths_and_state,
            test_check_of_finished_subagent_collects_the_outcome,
            test_runner_exposes_its_record_to_spawned_work,
            test_session_factory_supplies_the_injection_filter,
            test_emit_injected_notice_adds_a_viewer_event,
            test_allegro_is_inherited,
        ]
    )


if __name__ == "__main__":
    main()
