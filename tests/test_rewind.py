"""Tests for the rewind feature: workspace snapshots, points, and truncation.

Covers the pod-side git command composition and output parsing (against a
recording fake pod), the pod-init results registry that gates availability,
the session-side rewind points (creation, truncation, purge, injection
clearing), and the server-side rewind execution: files-first ordering, event
log truncation, and the history_rewind broadcast.
"""

import sys
import tempfile
import threading
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna import interaction, pod_init, server as server_module, state
from syna.capabilities import processes, rewind, subagents
from syna.conversation import session as session_module
from syna.conversation.session import AgentSession
from syna.protocol.channel import unwrap_channel
from syna.server import SessionRecord

SNAP_ENV_PREFIX = (
    "export "
    f"GIT_DIR={pod_init.REWIND_SNAP_DIR} GIT_WORK_TREE={pod_init.REWIND_SNAP_ROOT}"
)


class RecordingPod:
    """Exec recorder standing in for the pod; answers from a script."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.scripts = []

    def exec(self, argv):
        self.scripts.append(argv[-1])
        return self.outputs.pop(0)


def _make_session(events=None):
    session = AgentSession(
        session_id="rewind-test", on_event=events or (lambda e: None)
    )
    session.request_delay = 0
    return session


def _point(session, key, text="hello"):
    """Append a user message and record its rewind point; the message."""
    message = {"role": "user", "content": text}
    session.messages.append(message)
    session.add_rewind_point(key, text, {"tree": "a" * 40, "nested": {}}, 1234, message)
    return message


def _make_record(session):
    """A SessionRecord shell with the state the rewind paths touch."""
    record = SessionRecord.__new__(SessionRecord)
    record.client_id = "rewind-test"
    record.inbox = deque()
    record.inbox_ready = threading.Condition()
    record.event_log = []
    record.event_log_lock = threading.Lock()
    record.subscribers = []
    record.pending_rewind = None
    record.checkpoints_enabled = True
    record.container = None
    record.session = session
    record.pushed = []
    record.push = lambda event, persistent=True: record.pushed.append(event)
    return record


# --- pod-init step and availability ---------------------------------------


def test_rewind_step_is_registered_with_paths_and_script():
    steps = {step.name: step for step in pod_init.pod_init_steps()}
    assert "rewind" in steps and "doh" in steps
    step = steps["rewind"]
    assert step.enabled is True
    assert step.asset == "pod_rewind_setup.sh"
    assert step.env["SNAP_DIR"] == pod_init.REWIND_SNAP_DIR
    assert step.env["SNAP_ROOT"] == pod_init.REWIND_SNAP_ROOT
    script = pod_init.asset_path(step.asset).read_text(encoding="utf-8")
    assert "apt-get install -y -qq git" in script
    assert "git init -q --bare" in script
    assert "config core.bare false" in script
    assert pod_init.SUCCESS_MARKER in script
    print("PASS the rewind init step installs git and prepares the store")


def test_init_results_registry_round_trip():
    class FakeContainer:
        pass

    container = FakeContainer()
    pod_init.record_init_results(container, {"doh": True, "rewind": True})
    try:
        assert pod_init.init_results_for(container) == {"doh": True, "rewind": True}
        assert pod_init.init_results_for(object()) == {}
        assert rewind.available_for(container) is True
    finally:
        pod_init.forget_init_results(container)
    assert pod_init.init_results_for(container) == {}
    assert rewind.available_for(container) is False
    print("PASS availability is gated by the recorded init results")


def test_rewind_step_failure_disables_rewind():
    pod = RecordingPod(["apt-get failed hard"])
    results = pod_init.run_pod_init_steps(pod)
    assert results["rewind"] is False
    print("PASS a failed rewind step reports inactive and fails open")


# --- snapshot and rollback command composition ----------------------------


def test_snapshot_composes_status_add_and_write_tree():
    pod = RecordingPod([f"outer {'b' * 40}\n"])
    snap = rewind.snapshot(pod)
    assert snap == {"tree": "b" * 40, "nested": {}}
    script = pod.scripts[0]
    assert SNAP_ENV_PREFIX in script
    assert "git status --porcelain" in script
    assert "git add -A" in script
    assert "git write-tree" in script
    assert "find" in script and "env -u GIT_DIR" in script
    print("PASS the snapshot runs status, add -A, and write-tree")


def test_snapshot_reuses_the_last_tree_when_unchanged():
    pod = RecordingPod([f"outer {'b' * 40}\n", f"outer reused {'b' * 40}"])
    snap = rewind.snapshot(pod)
    assert rewind.snapshot(pod) == snap
    script = pod.scripts[1]
    assert f"SYNA_LAST_TREE={'b' * 40}" in script
    assert '"$(git status --porcelain)"' in script
    assert (
        "git add -A" not in script.split("SYNA_LAST_TREE", 1)[1].split("reused", 1)[0]
    )
    print("PASS an unchanged workspace reuses the previous tree")


def test_snapshot_captures_nested_repositories():
    nested_tree = "f" * 40
    pod = RecordingPod([f"outer {'b' * 40}\nnested /workspace/proj\t{nested_tree}\n"])
    snap = rewind.snapshot(pod)
    assert snap == {"tree": "b" * 40, "nested": {"/workspace/proj": nested_tree}}
    script = pod.scripts[0]
    assert f"GIT_INDEX_FILE={rewind.NESTED_INDEX_PATH}" in script
    assert 'git -C "$repo" add -A' in script
    assert 'git -C "$repo" write-tree' in script
    print("PASS nested repositories are snapshotted through a temp index")


def test_snapshot_reuses_nested_trees_when_all_clean():
    nested_tree = "f" * 40
    pod = RecordingPod(
        [
            f"outer {'b' * 40}\nnested /workspace/proj\t{nested_tree}\n",
            f"outer reused {'b' * 40}\nnested reused /workspace/proj\t{nested_tree}",
        ]
    )
    first = rewind.snapshot(pod)
    second = rewind.snapshot(pod)
    assert second == first
    script = pod.scripts[1]
    assert "/workspace/proj" in script and nested_tree in script
    assert "SYNA_LAST_NESTED" in script
    print("PASS a fully clean workspace reuses the nested trees too")


def test_snapshot_failure_and_garbage_yield_none():
    assert rewind.snapshot(RecordingPod([rewind.SNAP_ERROR_MARK])) is None
    assert rewind.snapshot(RecordingPod(["not a hash at all"])) is None
    assert rewind.snapshot(RecordingPod([f"outer {'z' * 39}\n"])) is None
    assert rewind.snapshot(RecordingPod(["outer a * 40\nnested /p bad\n"])) is None

    class DeadPod:
        def exec(self, argv):
            raise RuntimeError("pod gone")

    assert rewind.snapshot(DeadPod()) is None
    print("PASS snapshot failures and malformed output return None")


def test_rollback_composes_read_tree_checkout_and_clean_fd():
    pod = RecordingPod([rewind.ROLLBACK_OK_MARK])
    tree = "c" * 40
    assert rewind.rollback(pod, tree) is True
    script = pod.scripts[0]
    assert f"git read-tree {tree}" in script
    assert "git checkout-index -a -f" in script
    assert "git clean -fd" in script
    assert "-fdx" not in script
    assert SNAP_ENV_PREFIX in script
    print("PASS the rollback restores the tree and cleans with -fd")


def test_rollback_restores_nested_repositories_with_temp_index():
    pod = RecordingPod([rewind.ROLLBACK_OK_MARK])
    nested_tree = "f" * 40
    assert rewind.rollback(pod, "c" * 40, {"/workspace/proj": nested_tree}) is True
    script = pod.scripts[0]
    assert f"git -C /workspace/proj read-tree {nested_tree}" in script
    assert "git -C /workspace/proj checkout-index -a -f" in script
    assert "git -C /workspace/proj clean -fd" in script
    assert "if [ -d /workspace/proj ]" in script
    assert "rev-parse --git-dir" in script
    assert f"GIT_INDEX_FILE={rewind.NESTED_INDEX_PATH}" in script
    print("PASS nested repositories restore through a temp index, .git untouched")


def test_rollback_skips_a_nested_repo_that_vanished():
    pod = RecordingPod([rewind.ROLLBACK_OK_MARK])
    nested_tree = "f" * 40
    assert rewind.rollback(pod, "c" * 40, {"/workspace/gone": nested_tree}) is True
    script = pod.scripts[0]
    assert "if [ -d /workspace/gone ]" in script
    assert "rev-parse --git-dir" in script
    # The guarded commands sit inside the if, so a vanished repo is
    # skipped by the pod and the restore still confirms.
    assert script.index("if [ -d /workspace/gone ]") < script.index(
        "git -C /workspace/gone read-tree"
    )
    print("PASS a deleted nested repository no longer aborts the rollback")


def test_rollback_failure_reports_false():
    pod = RecordingPod([rewind.SNAP_ERROR_MARK.replace("SNAP", "ROLLBACK")])
    assert rewind.rollback(pod, "d" * 40) is False

    class DeadPod:
        def exec(self, argv):
            raise RuntimeError("pod gone")

    assert rewind.rollback(DeadPod(), "d" * 40) is False
    print("PASS a failed or dead-pod rollback reports False")


# --- session rewind points -------------------------------------------------


def test_run_turn_records_a_point_and_emits_state(monkeypatch):
    events = []
    session = _make_session(events.append)
    monkeypatch.setattr(
        session_module, "ask_question", lambda *args, **kwargs: {"content": "done"}
    )
    session.run_turn(
        "hello",
        rewind={"key": "msg-1", "snap": {"tree": "a" * 40, "nested": {}}, "pod": 77},
    )
    point = session.rewind_points["msg-1"]
    assert point["text"] == "hello"
    assert point["snap"] == {"tree": "a" * 40, "nested": {}}
    assert point["pod"] == 77
    assert point["message"] is session.messages[1]
    state_events = [event for event in events if event["type"] == "rewind_state"]
    assert len(state_events) == 1
    assert state_events[0]["added"] == ["msg-1"] and state_events[0]["removed"] == []
    print("PASS a turn with rewind info records the point and emits a delta")


def test_run_turn_without_rewind_records_nothing(monkeypatch):
    session = _make_session()
    monkeypatch.setattr(
        session_module, "ask_question", lambda *args, **kwargs: {"content": "done"}
    )
    session.run_turn("hello")
    assert session.rewind_points == {}
    print("PASS turns without rewind info record no point")


def test_rewind_truncates_history_and_returns_the_text():
    session = _make_session()
    _point(session, "msg-1")
    session.messages.append({"role": "assistant", "content": "reply"})
    _point(session, "msg-2", "second")
    session.messages.append({"role": "assistant", "content": "reply 2"})

    text = session.rewind("msg-2")

    assert text == "second"
    assert len(session.messages) == 3
    assert session.messages[-1] == {"role": "assistant", "content": "reply"}
    assert "msg-2" not in session.rewind_points
    assert "msg-1" in session.rewind_points
    print("PASS rewinding deletes the chosen message and everything after")


def test_rewind_purges_later_points_and_pending_state():
    session = _make_session()
    _point(session, "msg-1")
    _point(session, "msg-2", "second")
    failed = {"role": "assistant", "content": "broken"}
    session.messages.append(failed)
    session._pending_failed_messages.append(failed)
    session.queue_injection({"id": "inj", "content": "queued"})

    session.rewind("msg-2")

    assert "msg-2" not in session.rewind_points
    assert session._pending_failed_messages == []
    assert session.take_injections() == []
    print("PASS rewinding purges later points, failures, and injections")


def test_rewind_of_a_compacted_away_message_still_works():
    """Compaction summarized the message, but the UI still shows it.

    The rewind cuts the history back to its verbatim beginning (system
    prompt plus first real user message) - the summary covering the
    point cannot be split - and the point's tree snapshot still
    restores exactly.
    """
    session = _make_session()
    _point(session, "msg-1")
    _point(session, "msg-2", "second")
    del session.messages[2]
    session._mark_compacted_rewind_points()
    assert session.rewind_points["msg-2"]["compacted"] is True
    assert "msg-1" not in [
        name for name, p in session.rewind_points.items() if p.get("compacted")
    ]

    text = session.rewind("msg-2")

    assert text == "second"
    assert len(session.messages) == 2
    assert session.messages[-1] == {"role": "user", "content": "hello"}
    assert "msg-2" not in session.rewind_points
    assert "msg-1" in session.rewind_points
    print("PASS a compacted-away message rewinds to the verbatim beginning")


def test_compaction_marks_stale_points_without_a_delta():
    events = []
    session = _make_session(events.append)
    _point(session, "msg-1")
    _point(session, "msg-2", "second")
    del session.messages[2]

    session._mark_compacted_rewind_points()

    assert list(session.rewind_points) == ["msg-1", "msg-2"]
    assert session.rewind_points["msg-2"]["compacted"] is True
    assert session.rewind_points["msg-2"]["message"] is None
    deltas = [event for event in events if event["type"] == "rewind_state"]
    assert len(deltas) == 2
    print("PASS compaction marks points without hiding their buttons")


def test_rewind_notice_records_what_happened():
    session = _make_session()
    _point(session, "msg-1")

    session.record_rewind_notice(reverted=True)

    notice = session.messages[-1]
    assert notice["role"] == "user"
    kind, body = unwrap_channel(notice["content"])
    assert kind == "HARNESS_NOTICE"
    assert "reverted" in body and "/workspace" in body

    session.record_rewind_notice(reverted=False)
    kind, body = unwrap_channel(session.messages[-1]["content"])
    assert "NOT reverted" in body
    print("PASS the rewind notice tells the model what state it faces")


def test_rewinding_an_unknown_point_returns_none():
    session = _make_session()
    assert session.rewind("nope") is None
    print("PASS rewinding an unknown point changes nothing")


def test_rewind_delta_events_track_the_point_lifecycle():
    events = []
    session = _make_session(events.append)
    _point(session, "msg-1")
    _point(session, "msg-2", "second")

    session.rewind("msg-2")

    deltas = [event for event in events if event["type"] == "rewind_state"]
    assert deltas == [
        {"type": "rewind_state", "added": ["msg-1"], "removed": []},
        {"type": "rewind_state", "added": ["msg-2"], "removed": []},
        {"type": "rewind_state", "added": [], "removed": ["msg-2"]},
    ]
    print("PASS rewind_state events are small ordered deltas")


def test_compaction_marks_stale_points_and_emits_state():
    events = []
    session = _make_session(events.append)
    _point(session, "msg-1")
    _point(session, "msg-2", "second")
    del session.messages[1]

    session._mark_compacted_rewind_points()

    assert list(session.rewind_points) == ["msg-1", "msg-2"]
    assert session.rewind_points["msg-1"]["compacted"] is True
    assert session.rewind_points["msg-2"]["compacted"] is not True
    deltas = [event for event in events if event["type"] == "rewind_state"]
    assert len(deltas) == 2
    print("PASS points whose messages were compacted away stay rewirable")


# --- server-side rewind execution ------------------------------------------


def _seed_event_log(record, key, content="hello"):
    record.event_log.append({"type": "user_message", "content": content, "id": key})
    record.event_log.append({"type": "tool_call", "id": 1, "status": "finished"})


def test_execute_rewind_truncates_history_log_and_inbox():
    session = _make_session()
    _point(session, "msg-1")
    _point(session, "msg-2", "second")
    record = _make_record(session)
    record.event_log.append({"type": "user_message", "content": "hello", "id": "msg-1"})
    _seed_event_log(record, "msg-2")
    record.inbox.append({"kind": "message", "id": "q1", "content": "queued"})

    record._execute_rewind({"id": "msg-2", "mode": "messages"})

    assert record.pushed == [
        {
            "type": "history_rewind",
            "id": "msg-2",
            "content": "second",
        }
    ]
    assert list(record.inbox) == []
    assert len(record.event_log) == 1
    assert record.event_log[0]["id"] == "msg-1"
    assert len(session.messages) == 3
    kind, body = unwrap_channel(session.messages[-1]["content"])
    assert kind == "HARNESS_NOTICE" and "NOT reverted" in body
    print("PASS a messages-only rewind truncates, notifies, logs, and purges")


def test_execute_rewind_is_files_first(monkeypatch):
    session = _make_session()
    _point(session, "msg-1")
    _point(session, "msg-2", "second")
    record = _make_record(session)
    _seed_event_log(record, "msg-2")
    record.inbox.append({"kind": "message", "id": "q1", "content": "queued"})

    class FakeContainer:
        pass

    record.container = FakeContainer()
    monkeypatch.setattr(rewind, "rollback", lambda *args: False)

    record._execute_rewind({"id": "msg-2", "mode": "all"})

    assert [event["type"] for event in record.pushed] == ["error"]
    assert len(session.messages) == 3
    assert "msg-2" in session.rewind_points
    assert record.inbox != []
    print("PASS a failed file rollback leaves every message in place")


def test_execute_rewind_rejects_a_foreign_pod(monkeypatch):
    session = _make_session()
    _point(session, "msg-1", "hello")

    class FakeContainer:
        pass

    record = _make_record(session)
    record.container = FakeContainer()
    rolled = []
    monkeypatch.setattr(rewind, "rollback", lambda *args: rolled.append(1))

    record._execute_rewind({"id": "msg-1", "mode": "all"})

    assert rolled == []
    assert [event["type"] for event in record.pushed] == ["error"]
    assert len(session.messages) == 2
    print("PASS a snapshot from another pod is never restored")


def test_execute_rewind_of_a_missing_point_only_toasts():
    session = _make_session()
    record = _make_record(session)
    record.inbox.append({"kind": "message", "id": "q1", "content": "queued"})

    record._execute_rewind({"id": "ghost", "mode": "messages"})

    assert [event["type"] for event in record.pushed] == ["error"]
    assert record.inbox != []
    print("PASS rewinding a missing point keeps the queue and toasts")


def test_pending_rewind_runs_after_the_turn(monkeypatch):
    session = _make_session()
    _point(session, "msg-1")
    _point(session, "msg-2", "second")
    record = _make_record(session)
    _seed_event_log(record, "msg-2")
    record.pending_rewind = {"id": "msg-2", "mode": "messages"}

    record._run_pending_rewind()

    assert record.pending_rewind is None
    assert record.pushed and record.pushed[0]["type"] == "history_rewind"
    assert len(session.messages) == 3

    record._run_pending_rewind()
    assert len(record.pushed) == 1
    print("PASS the rewind requested during a busy turn runs once, after it")


def test_rewind_state_event_is_decorated_with_availability(monkeypatch):
    session = _make_session()
    record = _make_record(session)

    class FakeContainer:
        pass

    record.container = FakeContainer()
    monkeypatch.setattr(rewind, "available_for", lambda container: True)
    event = record._decorate_rewind_state(
        {"type": "rewind_state", "added": ["m"], "removed": []}
    )
    assert event["available"] is True

    monkeypatch.setattr(rewind, "available_for", lambda container: False)
    event = record._decorate_rewind_state(
        {"type": "rewind_state", "added": [], "removed": []}
    )
    assert event["available"] is False

    record.container = None
    event = record._decorate_rewind_state(
        {"type": "rewind_state", "added": [], "removed": []}
    )
    assert event["available"] is False

    record.container = FakeContainer()
    record.checkpoints_enabled = False
    event = record._decorate_rewind_state(
        {"type": "rewind_state", "added": [], "removed": []}
    )
    assert event["available"] is False
    print("PASS rewind_state carries the checkpoints setting and pod availability")


# --- round-2 fixes ----------------------------------------------------------


def test_route_rewind_parks_when_busy_and_queues_when_idle():
    session = _make_session()
    record = _make_record(session)
    record.pending_question = interaction.PendingQuestion()
    record.busy = threading.Event()

    server_module._route_rewind(record, "m1", "messages")
    assert record.pending_rewind is None
    assert list(record.inbox) == [{"kind": "rewind", "id": "m1", "mode": "messages"}]

    record.busy.set()
    server_module._route_rewind(record, "m2", "all")
    assert record.pending_rewind == {"id": "m2", "mode": "all"}
    assert len(record.inbox) == 1
    assert not record.pending_question.active
    print("PASS a busy rewind parks after cancelling; an idle one queues")


def test_second_click_overwrites_the_parked_rewind():
    session = _make_session()
    record = _make_record(session)
    record.pending_question = interaction.PendingQuestion()
    record.busy = threading.Event()
    record.busy.set()

    server_module._route_rewind(record, "m1", "messages")
    server_module._route_rewind(record, "m2", "all")

    assert record.pending_rewind == {"id": "m2", "mode": "all"}
    print("PASS the latest rewind wins while a turn is busy")


def test_execute_rewind_purge_keeps_queued_rewind_items():
    session = _make_session()
    _point(session, "msg-1")
    record = _make_record(session)
    _seed_event_log(record, "msg-1")
    queued_rewind = {"kind": "rewind", "id": "msg-1", "mode": "messages"}
    record.inbox.append({"kind": "message", "id": "q1", "content": "queued"})
    record.inbox.append(dict(queued_rewind))

    record._execute_rewind({"id": "msg-1", "mode": "messages"})

    assert list(record.inbox) == [queued_rewind]
    print("PASS the rewind purge never swallows another rewind request")


def test_cancelled_turn_still_records_its_point(monkeypatch):
    events = []
    session = _make_session(events.append)

    def fake_ask(*args, **kwargs):
        raise session_module.TurnCancelled()

    monkeypatch.setattr(session_module, "ask_question", fake_ask)
    session.cancel()
    session.run_turn(
        "hello", rewind={"key": "msg-1", "snap": {"tree": "a" * 40}, "pod": 7}
    )

    assert "msg-1" in session.rewind_points
    assert session.rewind_points["msg-1"]["text"] == "hello"
    print("PASS a cancelled turn still leaves its rewind point behind")


def _fake_registry(processes_dict, subagents_dict):
    class FakeRegistry:
        def __init__(self):
            self.lock = threading.Lock()
            self.processes = processes_dict
            self.subagents = subagents_dict

    return FakeRegistry()


def test_stop_background_for_rewind_kills_and_suppresses_notices(monkeypatch):
    from types import SimpleNamespace

    session = _make_session()
    record = _make_record(session)

    class FakeContainer:
        pass

    record.container = FakeContainer()
    proc = SimpleNamespace(id="p1", outcome_collected=False, state="running")
    sub = SimpleNamespace(
        id="s1",
        outcome_collected=False,
        state="running",
        kill_requested=threading.Event(),
    )
    registry = _fake_registry({"p1": proc}, {"s1": sub})
    monkeypatch.setattr(processes, "registry_for", lambda container: registry)
    killed = []

    def fake_kill_process(process_id, registry=None, container=None, by="model"):
        killed.append(("process", process_id, by, proc.outcome_collected))
        proc.state = "exited"

    def fake_kill_subagent(subagent_id, registry=None, container=None, by="model"):
        killed.append(("subagent", subagent_id, by, sub.outcome_collected))
        sub.state = "ended"

    monkeypatch.setattr(processes, "kill_process", fake_kill_process)
    monkeypatch.setattr(subagents, "kill_subagent", fake_kill_subagent)
    monkeypatch.setattr(processes, "process_status_summary", lambda r: "stopped")
    monkeypatch.setattr(
        subagents, "subagent_status_summary", lambda r: "stopped by the user"
    )

    stopped = record._stop_background_for_rewind()

    assert stopped == 2
    assert killed == [
        ("process", "p1", "user", True),
        ("subagent", "s1", "user", True),
    ]
    assert [event["type"] for event in record.pushed] == [
        "background_done",
        "background_done",
    ]
    print("PASS the rewind stops background work with notices suppressed")


def test_stop_background_for_rewind_fails_open(monkeypatch):
    from types import SimpleNamespace

    session = _make_session()
    record = _make_record(session)

    class FakeContainer:
        pass

    record.container = FakeContainer()
    proc = SimpleNamespace(id="p1", outcome_collected=False, state="running")
    registry = _fake_registry({"p1": proc}, {})

    def boom(*args, **kwargs):
        raise RuntimeError("engine wedged")

    monkeypatch.setattr(processes, "registry_for", lambda container: registry)
    monkeypatch.setattr(processes, "kill_process", boom)
    monkeypatch.setattr(server_module, "REWIND_SUBAGENT_SETTLE_S", 0)

    record._stop_background_for_rewind()

    assert record.pushed == []
    print("PASS a failing kill is logged and the rewind proceeds")


def test_create_container_seeds_the_pristine_tree(monkeypatch):
    tree = "e" * 40
    scripts = []

    class ScriptAwarePod:
        def exec(self, argv):
            script = argv[-1]
            scripts.append(script)
            if "SYNA_LAST_TREE" in script:
                return f"outer reused {tree}"
            if "write-tree" in script:
                return f"outer {tree}"
            return pod_init.SUCCESS_MARKER

    class FakeClient:
        def run_container(self, *args, **kwargs):
            return ScriptAwarePod()

    monkeypatch.setattr(state, "client", None)
    monkeypatch.setattr(state, "init_docker_client", lambda: None)
    monkeypatch.setattr(state, "client", FakeClient())
    monkeypatch.setattr(pod_init, "get_data_home", lambda: Path(tempfile.mkdtemp()))
    container = state.create_container()
    try:
        seed_scripts = [script for script in scripts if "write-tree" in script]
        assert len(seed_scripts) == 1
        seeded = rewind.snapshot(container)
        assert seeded == {"tree": tree, "nested": {}}
        assert "SYNA_LAST_TREE" in scripts[-1]
    finally:
        rewind.forget_pod(container)
        pod_init.forget_init_results(container)
    print("PASS pod creation seeds the pristine tree for the first message")


def test_clear_rewind_points_emits_the_emptied_state():
    events = []
    session = _make_session(events.append)
    _point(session, "msg-1")

    session.clear_rewind_points()

    assert session.rewind_points == {}
    assert events[-1] == {
        "type": "rewind_state",
        "added": [],
        "removed": ["msg-1"],
    }

    session.clear_rewind_points()
    assert len(events) == 2
    print("PASS a new pod clears every point and publishes the state")


def main():
    run_suite(
        [
            test_rewind_step_is_registered_with_paths_and_script,
            test_init_results_registry_round_trip,
            test_rewind_step_failure_disables_rewind,
            test_snapshot_composes_status_add_and_write_tree,
            test_snapshot_reuses_the_last_tree_when_unchanged,
            test_snapshot_captures_nested_repositories,
            test_snapshot_reuses_nested_trees_when_all_clean,
            test_snapshot_failure_and_garbage_yield_none,
            test_rollback_composes_read_tree_checkout_and_clean_fd,
            test_rollback_restores_nested_repositories_with_temp_index,
            test_rollback_skips_a_nested_repo_that_vanished,
            test_rollback_failure_reports_false,
            test_run_turn_records_a_point_and_emits_state,
            test_run_turn_without_rewind_records_nothing,
            test_rewind_truncates_history_and_returns_the_text,
            test_rewind_purges_later_points_and_pending_state,
            test_rewind_of_a_compacted_away_message_still_works,
            test_rewinding_an_unknown_point_returns_none,
            test_rewind_delta_events_track_the_point_lifecycle,
            test_compaction_marks_stale_points_without_a_delta,
            test_rewind_notice_records_what_happened,
            test_execute_rewind_truncates_history_log_and_inbox,
            test_execute_rewind_is_files_first,
            test_execute_rewind_rejects_a_foreign_pod,
            test_execute_rewind_of_a_missing_point_only_toasts,
            test_pending_rewind_runs_after_the_turn,
            test_rewind_state_event_is_decorated_with_availability,
            test_clear_rewind_points_emits_the_emptied_state,
            test_route_rewind_parks_when_busy_and_queues_when_idle,
            test_second_click_overwrites_the_parked_rewind,
            test_execute_rewind_purge_keeps_queued_rewind_items,
            test_cancelled_turn_still_records_its_point,
            test_stop_background_for_rewind_kills_and_suppresses_notices,
            test_stop_background_for_rewind_fails_open,
            test_create_container_seeds_the_pristine_tree,
        ]
    )


if __name__ == "__main__":
    main()
