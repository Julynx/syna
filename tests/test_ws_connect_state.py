"""The WebSocket connect path must settle the client's turn state.

The persisted event log ends on the last persistent event, which is
often a "Thinking..." status notice: busy/ready are pushed
non-persistent by design, so a reloaded page would otherwise show the
stale pill forever. After history_done the server sends one live
busy/ready event reflecting the worker's actual turn state.
"""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite
from fastapi.testclient import TestClient

from syna import server as server_module
from syna.server import SessionRecord


def _make_ws_record():
    """A SessionRecord shell with just the connect path's needs."""
    record = SessionRecord.__new__(SessionRecord)
    record.event_log = []
    record.event_log_lock = threading.Lock()
    record.subscribers = []
    record.connected = False
    record.container = None
    record.container_starting = False
    record.busy = threading.Event()
    record.touch = lambda: None
    return record


def test_connect_sends_ready_after_history_when_idle(monkeypatch):
    monkeypatch.setattr(server_module, "AUTH_TOKEN", "secret123")
    record = _make_ws_record()
    monkeypatch.setattr(server_module, "get_or_create_session", lambda cid: record)
    client = TestClient(server_module.app)

    record.push({"type": "status", "text": "Thinking..."})
    with client.websocket_connect("/ws/wstest?token=secret123") as ws:
        assert ws.receive_json() == {"type": "status", "text": "Thinking..."}
        assert ws.receive_json() == {"type": "history_done"}
        assert ws.receive_json() == {"type": "ready"}
    print("PASS an idle reconnect clears the stale Thinking pill")


def test_connect_sends_busy_when_a_turn_is_running(monkeypatch):
    monkeypatch.setattr(server_module, "AUTH_TOKEN", "secret123")
    record = _make_ws_record()
    monkeypatch.setattr(server_module, "get_or_create_session", lambda cid: record)
    client = TestClient(server_module.app)

    record.push({"type": "status", "text": "Thinking..."})
    record.busy.set()
    try:
        with client.websocket_connect("/ws/wstest?token=secret123") as ws:
            assert ws.receive_json() == {"type": "status", "text": "Thinking..."}
            assert ws.receive_json() == {"type": "history_done"}
            assert ws.receive_json() == {"type": "busy"}
    finally:
        record.busy.clear()
    print("PASS a mid-turn reconnect keeps the working pill")


def main():
    run_suite(
        [
            test_connect_sends_ready_after_history_when_idle,
            test_connect_sends_busy_when_a_turn_is_running,
        ]
    )


if __name__ == "__main__":
    main()
