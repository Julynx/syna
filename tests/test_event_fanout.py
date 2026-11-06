"""Tests for the server's event fan-out.

Every connected WebSocket surface gets every session event: a reloaded
tab or a second window must never silently steal another surface's
events (the original shared-queue design split them between
connections, leaving rows running forever on one of them). The
registration contract mirrors the endpoint: a snapshot of the history
plus a subscription, taken together under the lock push() holds, so no
event is either missed or delivered twice.
"""

import queue
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna.server import SessionRecord


def _make_record() -> SessionRecord:
    """A SessionRecord shell with just the state push() touches."""
    record = SessionRecord.__new__(SessionRecord)
    record.event_log = []
    record.event_log_lock = threading.Lock()
    record.subscribers = []
    return record


def _register(record: SessionRecord):
    """Register a subscriber exactly as the WebSocket endpoint does."""
    subscriber = queue.Queue()
    with record.event_log_lock:
        history = list(record.event_log)
        record.subscribers.append(subscriber)
    return subscriber, history


def test_push_reaches_every_subscriber():
    """Two open surfaces both receive every event, in order."""
    record = _make_record()
    first, _ = _register(record)
    second, _ = _register(record)

    record.push({"type": "user_message", "content": "a"})
    record.push({"type": "status", "text": "b"})

    for subscriber in (first, second):
        assert subscriber.get_nowait() == {"type": "user_message", "content": "a"}
        assert subscriber.get_nowait() == {"type": "status", "text": "b"}
        assert subscriber.empty()
    assert len(record.event_log) == 2
    print("PASS every event reaches every connected surface in order")


def test_non_persistent_push_skips_the_log():
    """Ephemeral events are delivered but never replayed on reconnect."""
    record = _make_record()
    subscriber, _ = _register(record)

    record.push({"type": "status", "text": "ephemeral"}, persistent=False)

    assert subscriber.get_nowait() == {"type": "status", "text": "ephemeral"}
    assert record.event_log == []
    print("PASS non-persistent events are delivered without entering the log")


def test_registration_is_gap_free_and_duplicate_free():
    """An event pushed around a registration lands on one side only."""
    record = _make_record()
    first, _ = _register(record)
    record.push({"type": "user_message", "content": "before"})

    second, history = _register(record)
    record.push({"type": "user_message", "content": "after"})

    assert history == [{"type": "user_message", "content": "before"}]
    assert second.get_nowait() == {"type": "user_message", "content": "after"}
    assert second.empty(), "the history must not be re-delivered to the pump"
    assert first.get_nowait() == {"type": "user_message", "content": "before"}
    assert first.get_nowait() == {"type": "user_message", "content": "after"}
    print("PASS registration is atomic against push: no gap, no duplication")


def main():
    """Run every fan-out test and report a summary."""
    run_suite(
        [
            test_push_reaches_every_subscriber,
            test_non_persistent_push_skips_the_log,
            test_registration_is_gap_free_and_duplicate_free,
        ]
    )


if __name__ == "__main__":
    main()
