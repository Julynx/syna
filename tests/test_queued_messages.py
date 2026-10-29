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
        ]
    )


if __name__ == "__main__":
    main()
