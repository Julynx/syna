"""Tests for the server's web slash-command routing.

`_handle_web_command` is the authoritative gate for what the web UI's
client-side registry may send: a whitelisted command is enqueued as an
inbox item (so it runs strictly between turns), unknown names are
rejected, and submissions while a question is pending are refused.
Also pins the usage event's log behavior: exactly one replace-in-place
entry, so a reloaded page restores the chip while the replay cap never
floods.
"""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna.server import SessionRecord, _handle_web_command


def _make_record(enqueued):
    """A SessionRecord shell with just the command routing's needs."""
    record = SessionRecord.__new__(SessionRecord)
    record.event_log = []
    record.event_log_lock = threading.Lock()
    record.subscribers = []
    record.connected = False
    record.busy = threading.Event()
    record.touch = lambda: None
    record.enqueue_message = lambda item: enqueued.append(item)
    record.pending_question = SimpleNamespace(active=False)
    return record


def test_compact_command_is_enqueued_between_turns():
    enqueued = []
    record = _make_record(enqueued)
    _handle_web_command(record, {"type": "command", "name": "compact", "id": "c1"})
    assert enqueued == [{"kind": "command", "name": "compact", "id": "c1"}], enqueued


def test_unknown_command_is_rejected():
    enqueued = []
    record = _make_record(enqueued)
    _handle_web_command(record, {"type": "command", "name": "purge"})
    assert not enqueued, "an unknown command must never reach the inbox"
    assert record.event_log[0]["type"] == "error", record.event_log
    assert "/purge" in record.event_log[0]["text"], record.event_log


def test_command_is_rejected_while_a_question_is_pending():
    enqueued = []
    record = _make_record(enqueued)
    record.pending_question = SimpleNamespace(active=True)
    _handle_web_command(record, {"type": "command", "name": "compact"})
    assert not enqueued, "commands have no answer semantics for a question"
    assert "question" in record.event_log[0]["text"], record.event_log


def test_usage_events_keep_only_the_latest_in_the_log():
    """Usage is idempotent state, not history.

    Every response replaces the previous usage entry, so a reloaded
    page restores the chip with the session's latest numbers while the
    log neither floods the replay cap nor evicts real chat rows.
    """
    record = _make_record([])
    record._on_session_event({"type": "info", "text": "notice"})
    record._on_session_event(
        {
            "type": "usage",
            "context_tokens": 7000,
            "context_window": 1000000,
            "cost": 0.0003,
        }
    )
    record._on_session_event(
        {
            "type": "usage",
            "context_tokens": 9000,
            "context_window": 1000000,
            "cost": 0.0005,
        }
    )
    with record.event_log_lock:
        assert record.event_log == [
            {"type": "info", "text": "notice"},
            {
                "type": "usage",
                "context_tokens": 9000,
                "context_window": 1000000,
                "cost": 0.0005,
            },
        ], record.event_log


def main():
    run_suite(
        [
            test_compact_command_is_enqueued_between_turns,
            test_unknown_command_is_rejected,
            test_command_is_rejected_while_a_question_is_pending,
            test_usage_events_keep_only_the_latest_in_the_log,
        ]
    )


if __name__ == "__main__":
    main()
