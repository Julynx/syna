"""Tests for queued messages and "Send now" injection at the turn's next opening.

Covers the server inbox (deque + condition: FIFO, per-id removal, waiting),
the websocket message translation (client-generated ids), and the session
injection checkpoint: a "Send now" message must be appended to history after
the running tool batch's results and before the next model request, without
interrupting the turn.
"""

import sys
import tempfile
import threading
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna.conversation.session import AgentSession
from syna.capabilities.tools import TOOL_REGISTRY
from syna.capabilities.processes import ProcessRecord, registry_for
from syna.server import SessionRecord, _inbox_item_from_message
from _runner import run_suite
from test_tool_feedback import FakeChat, FakeMessage, _make_tool_call


def _make_record():
    """A SessionRecord shell without the constructor's container/worker."""
    record = SessionRecord.__new__(SessionRecord)
    record.inbox = deque()
    record.inbox_ready = threading.Condition()
    return record


def is_tool_results(message: dict) -> bool:
    return message.get("role") == "tool" and "injected marker" in str(
        message.get("content", "")
    )


def _make_session():
    session = AgentSession(session_id="queued-test")
    session.dump_path = Path(tempfile.mkdtemp(prefix="syna-test-")) / "dump.json"
    session.request_delay = 0
    return session


def _make_record():
    """A SessionRecord shell without the constructor's container/worker."""
    record = SessionRecord.__new__(SessionRecord)
    record.inbox = deque()
    record.inbox_ready = threading.Condition()
    return record


REPLY_INJECT_TOOL = _make_tool_call("inject_tool", {})
REPLY_FINAL = FakeMessage(content="task done")


def test_inbox_item_carries_client_id():
    item = _inbox_item_from_message({"content": "hello", "id": "abc"})
    assert item is not None
    assert item["id"] == "abc"
    assert item["content"] == "hello"
    assert item["kind"] == "message"

    generated = _inbox_item_from_message({"content": "hello"})
    assert generated is not None
    assert generated["id"]

    assert _inbox_item_from_message({"content": "   "}) is None
    print("PASS inbox items carry the client-generated message id")


def test_inbox_fifo_and_per_id_removal():
    record = _make_record()
    record.enqueue_message({"id": "a", "content": "first"})
    record.enqueue_message({"id": "b", "content": "second"})
    record.enqueue_message({"id": "c", "content": "third"})

    taken = record.take_message("b")
    assert taken is not None and taken["id"] == "b"
    assert record.take_message("missing") is None

    assert record._next_message(timeout=0.0)["id"] == "a"
    assert record._next_message(timeout=0.0)["id"] == "c"
    assert record._next_message(timeout=0.01) is None
    print("PASS inbox keeps FIFO order while allowing per-id removal")


def test_inbox_wait_and_wake():
    record = _make_record()
    result = []

    def waiter():
        result.append(record._next_message(timeout=2.0))

    thread = threading.Thread(target=waiter)
    thread.start()
    record.enqueue_message({"id": "wake", "content": "hi"})
    thread.join(timeout=2.0)
    assert result and result[0] is not None and result[0]["id"] == "wake"
    print("PASS the worker wakes as soon as a message is enqueued")


def test_take_injections_drains_in_order():
    session = _make_session()
    session.queue_injection({"id": "1", "content": "one"})
    session.queue_injection({"id": "2", "content": "two"})
    items = session.take_injections()
    assert [item["id"] for item in items] == ["1", "2"]
    assert session.take_injections() == []
    print("PASS injection queue drains oldest first and empties")


def test_queue_injection_is_thread_safe():
    session = _make_session()

    def writer(base):
        for offset in range(20):
            session.queue_injection({"id": f"{base}-{offset}", "content": "x"})

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(session.take_injections()) == 80
    print("PASS concurrent send_now injections never lose a message")


def _run_injecting_turn():
    """One turn where the running tool queues a "Send now" injection."""
    session = _make_session()

    def injecting_tool() -> str:
        """Smoke tool that queues a send_now injection mid-turn."""
        session.queue_injection({"id": "q1", "content": "queued while working"})
        return "injected marker"

    TOOL_REGISTRY["inject_tool"] = injecting_tool
    events = []
    session.on_event = events.append
    session.client = FakeChat([REPLY_INJECT_TOOL, REPLY_FINAL])
    try:
        session.run_turn("start the work")
    finally:
        TOOL_REGISTRY.pop("inject_tool", None)
    return session, events


def test_injection_lands_after_tool_results_before_next_request():
    session, events = _run_injecting_turn()
    roles = [message["role"] for message in session.messages]
    assert roles == [
        "system",
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
    ]
    assert session.messages[4]["content"] == "queued while working"
    assert session.messages[5]["content"] == "task done"
    print("PASS injected message sits between the tool result and the reply")


def test_injected_message_reaches_the_model_in_the_next_request():
    session, _ = _run_injecting_turn()
    second_request = session.client.requests[1]
    request_messages = second_request["messages"]
    injected_index = next(
        index
        for index, message in enumerate(request_messages)
        if message.get("content") == "queued while working"
    )
    tool_index = next(
        index
        for index, message in enumerate(request_messages)
        if is_tool_results(message)
    )
    assert tool_index < injected_index
    assert injected_index == len(request_messages) - 2
    print("PASS the model receives the injected message with the tool results")


def test_injection_emits_user_message_event_with_id():
    _session, events = _run_injecting_turn()
    user_events = [event for event in events if event["type"] == "user_message"]
    assert len(user_events) == 1
    assert user_events[0]["id"] == "q1"
    assert user_events[0]["content"] == "queued while working"
    print("PASS the client is told when the queued pill becomes a bubble")


def test_requeue_injections_returns_undelivered_messages():
    record = _make_record()
    record.session = type(
        "FakeSession", (), {"take_injections": lambda self: [{"id": "z"}]}
    )()
    record._requeue_injections()
    assert [item["id"] for item in record.inbox] == ["z"]
    print("PASS undelivered injections fall back to the inbox as new turns")


def test_background_notice_queueing_and_wait_for_drop():
    record = _make_record()
    record.container = object()
    pushed = []
    record.push = lambda event, **kwargs: pushed.append(event)
    registry = registry_for(record.container)
    proc = ProcessRecord(id="p1", command="x", dir="/d")
    registry.processes["p1"] = proc

    record._on_background_completion(
        {"kind": "process", "id": "p1", "summary": "s", "model_notice": "n"}
    )
    assert len(record.inbox) == 1
    assert record.inbox[0]["bg_id"] == "p1"
    assert record.inbox[0]["bg_kind"] == "process"
    assert len(pushed) == 1 and pushed[0]["type"] == "background_done"
    assert not record._notice_is_stale({"bg_kind": "process", "bg_id": "p1"})

    proc.outcome_collected = True
    assert record._notice_is_stale({"bg_kind": "process", "bg_id": "p1"})
    assert not record._notice_is_stale({"bg_id": None})
    record._on_background_completion(
        {"kind": "process", "id": "p1", "summary": "s", "model_notice": "n"}
    )
    assert len(record.inbox) == 1, "the collected outcome is not enqueued twice"
    assert len(pushed) == 2, "the UI still learns about the completion"
    print("PASS outcome collection suppresses the duplicate wake notice")


def _notice_item(item_id: str, text: str) -> dict:
    from syna.protocol.channel import harness_notice, wrap_channel

    return {
        "kind": "message",
        "id": item_id,
        "bg_kind": "process",
        "bg_id": item_id,
        "content": wrap_channel("HARNESS_NOTICE", harness_notice(text)),
    }


def test_notice_batching_merges_head_notices_into_one_message():
    """Queued notices drain in one turn; a user message stops the sweep."""
    from syna.protocol.channel import unwrap_channel

    record = _make_record()
    record.container = object()
    record.inbox.extend(
        [
            _notice_item("n1", "first notice"),
            _notice_item("n2", "second notice"),
            {"kind": "message", "id": "u1", "content": "hello"},
            _notice_item("n3", "third notice"),
        ]
    )
    first = record.inbox.popleft()
    merged = record._merge_notice_batch(first)
    assert merged is not None
    kind, body = unwrap_channel(merged["content"])
    assert kind == "HARNESS_NOTICE"
    assert "first notice" in body and "second notice" in body
    assert "third notice" not in body, "a non-notice message stops the sweep"
    assert [item["id"] for item in record.inbox] == ["u1", "n3"]
    print("PASS queued notices merge into one wake message")


def test_notice_batching_never_touches_a_user_message():
    """A user message passes through unwrapped; notices behind it stay put."""
    user_msg = {
        "kind": "message",
        "id": "u1",
        "content": "Please summarize the report.",
        "was_queued": True,
    }
    record = _make_record()
    record.container = object()
    record.inbox.append(
        {
            "kind": "message",
            "id": "n1",
            "bg_kind": "process",
            "bg_id": "n1",
            "content": "notice body",
        }
    )
    untouched = record._merge_notice_batch(dict(user_msg))
    assert untouched["content"] == user_msg["content"], "content rewritten"
    assert [item["id"] for item in record.inbox] == ["n1"], "notice swallowed"
    alone = record._merge_notice_batch(dict(user_msg))
    assert alone["content"] == user_msg["content"]
    print("PASS a user message is never rewritten by notice batching")


def test_notice_batching_drops_collected_outcomes():
    from syna.protocol.channel import unwrap_channel

    record = _make_record()
    record.container = object()
    registry = registry_for(record.container)
    proc = ProcessRecord(id="p1", command="x", dir="/d")
    registry.processes["p1"] = proc
    proc.outcome_collected = True
    record.inbox.extend(
        [
            _notice_item("p1", "already collected"),
            _notice_item("n2", "fresh notice"),
        ]
    )
    first = record.inbox.popleft()
    merged = record._merge_notice_batch(first)
    kind, body = unwrap_channel(merged["content"])
    assert "already collected" not in body
    assert "fresh notice" in body
    print("PASS a collected outcome is dropped from the batch")


class _ParentSession:
    """Stands in for a live subagent session taking injections."""

    def __init__(self):
        self.injections = []

    def queue_injection(self, item):
        self.injections.append(item)


def _routing_record():
    record = _make_record()
    record.container = object()
    record.push = lambda event, **kwargs: None
    return record


def test_completion_notice_is_injected_into_a_live_parent(monkeypatch):
    from syna.capabilities import processes
    from syna.capabilities.subagents import SubagentRecord

    monkeypatch.setattr(processes, "_registries", {})
    record = _routing_record()
    registry = registry_for(record.container)
    parent = SubagentRecord(id="par", goal="g", level=2)
    parent.session = _ParentSession()
    registry.subagents["par"] = parent
    record._on_background_completion(
        {
            "kind": "process",
            "id": "child",
            "parent_id": "par",
            "summary": "s",
            "model_notice": "Process 'child' finished.",
        }
    )
    assert len(parent.session.injections) == 1
    assert parent.session.injections[0]["bg_id"] == "child"
    assert parent.session.injections[0]["notice_text"] == "Process 'child' finished."
    assert not record.inbox, "the parent's notice must not wake the top level"
    assert parent.events == [], "the viewer entry comes only at apply time"
    print("PASS a nested completion notice wakes only its parent")


def test_completion_notice_is_dropped_when_the_parent_is_gone(monkeypatch):
    from syna.capabilities import processes
    from syna.capabilities.subagents import SubagentRecord

    monkeypatch.setattr(processes, "_registries", {})
    record = _routing_record()
    registry = registry_for(record.container)
    parent = SubagentRecord(id="par", goal="g", level=2)
    parent.state = "finished"
    parent.end_reason = "response"
    parent.session = _ParentSession()
    registry.subagents["par"] = parent
    record._on_background_completion(
        {
            "kind": "process",
            "id": "child",
            "parent_id": "par",
            "summary": "s",
            "model_notice": "Process 'child' finished.",
        }
    )
    assert parent.session.injections == []
    assert not record.inbox, "a parented notice never falls back to the top level"
    record._on_background_completion(
        {
            "kind": "process",
            "id": "child2",
            "parent_id": "unknown-parent",
            "summary": "s",
            "model_notice": "Process 'child2' finished.",
        }
    )
    assert not record.inbox, "an unknown parent drops the notice too"
    print("PASS a settled or unknown parent drops the notice")


def test_injection_filter_drops_collected_outcomes_at_apply_time():
    session = _make_session()
    collected = {"flag": False}
    session.injection_filter = lambda item: not collected["flag"]
    session.queue_injection({"id": "n1", "content": "notice text"})
    session.apply_injections()
    assert "notice text" in str(session.messages[-1])
    session.queue_injection({"id": "n2", "bg_id": "p1", "content": "stale notice"})
    collected["flag"] = True
    assert session.apply_injections() == 0
    assert "stale notice" not in str(session.messages[-1])
    print("PASS the injection filter drops collected outcomes at apply time")


def test_applied_notice_emits_the_viewer_event_once():
    """An applied background notice emits injected_notice; drops emit none."""
    from syna.protocol.channel import harness_notice, wrap_channel

    session = _make_session()
    events = []
    session.on_event = lambda event: events.append(event)
    body = wrap_channel("HARNESS_NOTICE", harness_notice("Process 'p1' finished."))
    session.queue_injection(
        {
            "id": "n1",
            "bg_kind": "process",
            "bg_id": "p1",
            "notice_text": "Process 'p1' finished.",
            "content": body,
        }
    )
    assert session.apply_injections() == 1
    notices = [e for e in events if e.get("type") == "injected_notice"]
    user_msgs = [e for e in events if e.get("type") == "user_message"]
    assert len(notices) == 1 and notices[0]["bg_id"] == "p1"
    assert notices[0]["content"] == "Process 'p1' finished."
    assert len(user_msgs) == 1
    session.queue_injection({"id": "n2", "bg_id": "p2", "content": body})
    session.injection_filter = lambda item: False
    assert session.apply_injections() == 0
    assert len([e for e in events if e.get("type") == "injected_notice"]) == 1, (
        "a dropped notice emits no viewer event"
    )
    print("PASS an applied notice emits the viewer event; a dropped one does not")


def main():
    run_suite(
        [
            test_inbox_item_carries_client_id,
            test_inbox_fifo_and_per_id_removal,
            test_inbox_wait_and_wake,
            test_take_injections_drains_in_order,
            test_queue_injection_is_thread_safe,
            test_injection_lands_after_tool_results_before_next_request,
            test_injected_message_reaches_the_model_in_the_next_request,
            test_injection_emits_user_message_event_with_id,
            test_requeue_injections_returns_undelivered_messages,
            test_background_notice_queueing_and_wait_for_drop,
            test_notice_batching_merges_head_notices_into_one_message,
            test_notice_batching_never_touches_a_user_message,
            test_notice_batching_drops_collected_outcomes,
            test_completion_notice_is_injected_into_a_live_parent,
            test_completion_notice_is_dropped_when_the_parent_is_gone,
            test_injection_filter_drops_collected_outcomes_at_apply_time,
            test_applied_notice_emits_the_viewer_event_once,
        ]
    )


if __name__ == "__main__":
    main()
