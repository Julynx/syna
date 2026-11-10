"""Tests for the CLI's rewind feature.

Covers the worker's rewind-point creation (checkpoint gating, snapshot
failure paths, the rewind dict passed to the session), the /rewind and
/undo commands (listing, selection, the mode prompt, the busy-parking
path), and the rewind execution itself: files-first ordering, the
recreated-pod guard, rollback failure leaving messages untouched, and
the inbox purge sparing queued rewind requests.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna.conversation import terminal as terminal_module
from syna.conversation.terminal import TerminalChat, TerminalSession


def _wait(predicate, timeout_s: float = 5.0) -> bool:
    """Poll a predicate until it holds or the timeout passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class RewindStubSession:
    """AgentSession stand-in recording rewind-relevant calls in order."""

    def __init__(self):
        self.rewind_points = {}
        self.calls = []
        self.turn_started = threading.Event()
        self.turn_release = threading.Event()
        self.cancel_hook = None

    def clear_cancel(self):
        self.calls.append("clear")

    def cancel(self, reason="user interrupt"):
        self.calls.append(("cancel", reason))
        if self.cancel_hook is not None:
            self.cancel_hook(reason)

    def run_turn(self, text, rewind=None):
        self.calls.append(("turn", text, rewind is not None))
        self.turn_release.clear()
        self.turn_started.set()
        self.turn_release.wait(timeout=5)

    def rewind(self, key):
        self.calls.append(("rewind", key))
        return self.rewind_points.get(key, {}).get("text", "the original")

    def record_rewind_notice(self, reverted):
        self.calls.append(("notice", reverted))

    def close(self):
        self.calls.append("close")


def _point(session, key, text="do the thing", pod=None, compacted=False):
    """Record one rewind point the way the session's machinery would."""
    entry = {
        "text": text,
        "snap": {"tree": "a" * 40, "nested": {}},
        "pod": pod if pod is not None else 1234,
        "message": None,
    }
    if compacted:
        entry["compacted"] = True
    session.rewind_points[key] = entry


def _conversation(session, checkpoints=True, container=object()):
    """A TerminalSession with captured output and a fixed container."""
    printed = []
    conversation = TerminalSession(
        session,
        notify=printed.append,
        container_provider=lambda: container,
        checkpoints_enabled=checkpoints,
    )
    return conversation, printed


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


def _make_chat(script, session, checkpoints=True, container=object(), monkeypatch=None):
    """A TerminalChat over scripted input with captured output.

    The pod is faked end to end: the worker's snapshot path sees the
    given container, so no test ever reaches the real engine.
    """
    said = []
    conversation, _ = _conversation(session, checkpoints, container)
    if monkeypatch is not None:
        monkeypatch.setattr(terminal_module, "get_container", lambda: container)
        monkeypatch.setattr(
            terminal_module.rewind,
            "snapshot",
            lambda c: {"tree": "a" * 40, "nested": {}},
        )
    chat = TerminalChat(
        terminal=conversation,
        conversation_factory=lambda: conversation,
        say=said.append,
        read_line=_ScriptedInput(script),
    )
    return chat, said


# --- rewind point creation --------------------------------------------------


def test_no_snapshot_when_checkpoints_are_off(monkeypatch):
    session = RewindStubSession()
    conversation, _ = _conversation(session, checkpoints=False)
    assert conversation._rewind_info_for() is None
    conversation.close()
    print("PASS checkpoints off means no snapshot and no rewind point")


def test_snapshot_when_checkpoints_are_on(monkeypatch):
    session = RewindStubSession()
    container = object()
    monkeypatch.setattr(terminal_module, "get_container", lambda: container)
    monkeypatch.setattr(
        terminal_module.rewind,
        "snapshot",
        lambda c: {"tree": "a" * 40, "nested": {}},
    )
    conversation, _ = _conversation(session, container=container)
    info = conversation._rewind_info_for()
    conversation.close()
    assert info is not None
    assert info["snap"] == {"tree": "a" * 40, "nested": {}}
    assert info["pod"] == id(container)
    assert len(info["key"]) == 32
    print("PASS checkpoints on snapshots the workspace before the turn")


def test_snapshot_failure_means_no_point(monkeypatch):
    session = RewindStubSession()
    monkeypatch.setattr(terminal_module, "get_container", lambda: object())
    monkeypatch.setattr(terminal_module.rewind, "snapshot", lambda c: None)
    conversation, _ = _conversation(session)
    try:
        assert conversation._rewind_info_for() is None
    finally:
        conversation.close()

    def boom():
        raise RuntimeError("no engine")

    monkeypatch.setattr(terminal_module, "get_container", boom)
    conversation, _ = _conversation(session)
    try:
        assert conversation._rewind_info_for() is None
    finally:
        conversation.close()
    print("PASS a failed pod or snapshot simply records no rewind point")


def test_the_turn_receives_the_rewind_dict(monkeypatch):
    session = RewindStubSession()
    container = object()
    monkeypatch.setattr(terminal_module, "get_container", lambda: container)
    monkeypatch.setattr(
        terminal_module.rewind,
        "snapshot",
        lambda c: {"tree": "a" * 40, "nested": {}},
    )
    conversation, _ = _conversation(session, container=container)
    try:
        conversation.submit("hello")
        assert _wait(lambda: ("turn", "hello", True) in session.calls)
    finally:
        session.turn_release.set()
        conversation.close()
    print("PASS the user-message turn carries its rewind point")


# --- commands ----------------------------------------------------------------


def test_rewind_lists_points_with_previews():
    session = RewindStubSession()
    _point(session, "k1", text="first message")
    _point(session, "k2", text="second message", compacted=True)
    chat, said = _make_chat(["/rewind", KeyboardInterrupt], session)
    chat.run()
    joined = "\n".join(said)
    assert "1. first message" in joined
    assert "2. second message (compacted)" in joined
    print("PASS /rewind lists every point oldest first with markers")


def test_rewind_number_selects_and_enqueues(monkeypatch):
    session = RewindStubSession()
    _point(session, "k1", text="first")
    _point(session, "k2", text="second")
    chat, said = _make_chat(
        ["/rewind 2", "1", KeyboardInterrupt], session, monkeypatch=monkeypatch
    )
    chat.run()
    assert ("rewind", "k2") in session.calls
    assert ("notice", False) in session.calls
    assert any("Rewinding" in line for line in said)
    print("PASS /rewind <n> runs a messages-only rewind for that point")


def test_undo_picks_the_newest_point(monkeypatch):
    session = RewindStubSession()
    _point(session, "k1", text="first")
    _point(session, "k2", text="second")
    chat, _ = _make_chat(
        ["/undo", "1", KeyboardInterrupt], session, monkeypatch=monkeypatch
    )
    chat.run()
    assert ("rewind", "k2") in session.calls
    assert ("notice", False) in session.calls
    print("PASS /undo targets the newest point with the chosen mode")


def test_cancel_choice_aborts_the_rewind():
    session = RewindStubSession()
    _point(session, "k1", text="first")
    chat, said = _make_chat(["/undo", "3", KeyboardInterrupt], session)
    chat.run()
    assert list(chat.terminal.inbox) == []
    assert any("Rewind cancelled" in line for line in said)
    print("PASS the Cancel choice aborts the rewind cleanly")


def test_no_points_message_explains_disabled_checkpoints():
    session = RewindStubSession()
    chat, said = _make_chat(["/undo", KeyboardInterrupt], session, checkpoints=False)
    chat.run()
    assert any("Checkpoints are disabled" in line for line in said)
    print("PASS /undo without checkpoints points at the setting")


def test_invalid_rewind_number_is_reported():
    session = RewindStubSession()
    _point(session, "k1", text="first")
    chat, said = _make_chat(["/rewind 9", KeyboardInterrupt], session)
    chat.run()
    assert any("No such rewind point" in line for line in said)
    print("PASS an out-of-range rewind number is reported")


# --- busy parking ------------------------------------------------------------


def test_busy_rewind_stops_and_parks_the_request(monkeypatch):
    session = RewindStubSession()
    session.cancel_hook = lambda reason: session.turn_release.set()
    _point(session, "k1", text="first")
    state = {}
    chat, said = _make_chat(
        [
            "work",
            lambda: _wait(lambda: state["chat"].terminal.busy.is_set()),
            "/undo",
            "1",
            lambda: _wait(
                lambda: ("rewind", "k1") in state["chat"].terminal.session.calls
            ),
            lambda: _wait(lambda: not state["chat"].terminal.busy.is_set()),
            KeyboardInterrupt,
        ],
        session,
        monkeypatch=monkeypatch,
    )
    state["chat"] = chat
    chat.run()
    assert ("cancel", "user interrupt") in session.calls
    assert ("rewind", "k1") in session.calls
    assert ("notice", False) in session.calls
    assert any("runs once the task has stopped" in line for line in said)
    print("PASS a rewind during a busy turn stops it and runs once stopped")


def test_parked_rewind_runs_before_the_next_queued_message(monkeypatch):
    session = RewindStubSession()
    session.cancel_hook = lambda reason: session.turn_release.set()
    _point(session, "k1", text="first")
    state = {}
    chat, said = _make_chat(
        [
            "work",
            lambda: _wait(lambda: state["chat"].terminal.busy.is_set()),
            "/undo",
            "1",
            lambda: _wait(lambda: ("rewind", "k1") in session.calls),
            "follow-up",
            lambda: _wait(
                lambda: any(
                    call[0] == "turn" and call[1] == "follow-up"
                    for call in session.calls
                )
            ),
            lambda: _wait(lambda: not state["chat"].terminal.busy.is_set()),
            KeyboardInterrupt,
        ],
        session,
        monkeypatch=monkeypatch,
    )
    state["chat"] = chat
    chat.run()
    order = [
        call[0] for call in session.calls if call[0] in ("rewind", "turn", "cancel")
    ]
    assert order[:4] == ["turn", "cancel", "rewind", "turn"]
    print("PASS a parked rewind executes before the next queued message")


# --- execution ---------------------------------------------------------------


def test_execute_rewind_messages_only():
    session = RewindStubSession()
    _point(session, "k1", text="do the thing")
    conversation, printed = _conversation(session)
    try:
        conversation._execute_rewind({"kind": "rewind", "id": "k1", "mode": "messages"})
    finally:
        conversation.close()
    assert ("rewind", "k1") in session.calls
    assert ("notice", False) in session.calls
    joined = "\n".join(printed)
    assert "Rewound to before: do the thing" in joined
    assert "> do the thing" in joined
    print("PASS a messages-only rewind truncates history and says so")


def test_execute_rewind_all_restores_files_first(monkeypatch):
    session = RewindStubSession()
    container = object()
    _point(session, "k1", text="do the thing", pod=id(container))
    order = []
    stopped_calls = []
    monkeypatch.setattr(
        terminal_module.processes,
        "stop_running_background_work",
        lambda c, on_stopped=None: stopped_calls.append(c) or 2,
    )
    monkeypatch.setattr(
        terminal_module.rewind,
        "rollback",
        lambda c, tree, nested=None: order.append("rollback") or True,
    )
    original_rewind = session.rewind

    def recording_rewind(key):
        order.append("rewind")
        return original_rewind(key)

    session.rewind = recording_rewind
    conversation, printed = _conversation(session, container=container)
    try:
        conversation._execute_rewind({"kind": "rewind", "id": "k1", "mode": "all"})
    finally:
        conversation.close()
    assert order == ["rollback", "rewind"]
    assert stopped_calls == [container]
    assert ("notice", True) in session.calls
    joined = "\n".join(printed)
    assert "workspace restored" in joined
    assert "2 background task(s) stopped" in joined
    print("PASS a workspace rewind stops work, restores files, then truncates")


def test_execute_rewind_all_rejects_a_recreated_pod():
    session = RewindStubSession()
    _point(session, "k1", text="do the thing", pod=9999)
    conversation, printed = _conversation(session, container=object())
    try:
        conversation._execute_rewind({"kind": "rewind", "id": "k1", "mode": "all"})
    finally:
        conversation.close()
    assert ("rewind", "k1") not in session.calls
    assert any("pod was recreated" in line for line in printed)
    print("PASS a rewind across a recreated pod keeps the messages")


def test_execute_rewind_rollback_failure_leaves_messages(monkeypatch):
    session = RewindStubSession()
    container = object()
    _point(session, "k1", text="do the thing", pod=id(container))
    monkeypatch.setattr(
        terminal_module.processes,
        "stop_running_background_work",
        lambda c, on_stopped=None: 1,
    )
    monkeypatch.setattr(
        terminal_module.rewind,
        "rollback",
        lambda c, tree, nested=None: False,
    )
    conversation, printed = _conversation(session, container=container)
    try:
        conversation._execute_rewind({"kind": "rewind", "id": "k1", "mode": "all"})
    finally:
        conversation.close()
    assert ("rewind", "k1") not in session.calls
    joined = "\n".join(printed)
    assert "Could not restore the workspace files" in joined
    assert "Background work was stopped" in joined
    print("PASS a failed rollback leaves every message in place")


def test_execute_rewind_of_a_gone_point_is_reported():
    session = RewindStubSession()
    conversation, printed = _conversation(session)
    try:
        conversation._execute_rewind(
            {"kind": "rewind", "id": "gone", "mode": "messages"}
        )
    finally:
        conversation.close()
    assert any("no longer be rewound" in line for line in printed)
    print("PASS rewinding a vanished point reports it honestly")


def test_purge_spares_queued_rewind_items():
    session = RewindStubSession()
    conversation, _ = _conversation(session)
    try:
        with conversation.inbox_ready:
            conversation.inbox.append({"kind": "message", "id": "m1", "content": "x"})
            conversation.inbox.append({"kind": "rewind", "id": "k1", "mode": "all"})
        conversation.purge_inbox(keep_kinds=("rewind",))
        with conversation.inbox_ready:
            remaining = list(conversation.inbox)
        assert [item["kind"] for item in remaining] == ["rewind"]
    finally:
        conversation.close()
    print("PASS the post-rewind purge spares queued rewind requests")


if __name__ == "__main__":
    run_suite(
        [
            test_no_snapshot_when_checkpoints_are_off,
            test_snapshot_when_checkpoints_are_on,
            test_snapshot_failure_means_no_point,
            test_the_turn_receives_the_rewind_dict,
            test_rewind_lists_points_with_previews,
            test_rewind_number_selects_and_enqueues,
            test_undo_picks_the_newest_point,
            test_cancel_choice_aborts_the_rewind,
            test_no_points_message_explains_disabled_checkpoints,
            test_invalid_rewind_number_is_reported,
            test_busy_rewind_stops_and_parks_the_request,
            test_parked_rewind_runs_before_the_next_queued_message,
            test_execute_rewind_messages_only,
            test_execute_rewind_all_restores_files_first,
            test_execute_rewind_all_rejects_a_recreated_pod,
            test_execute_rewind_rollback_failure_leaves_messages,
            test_execute_rewind_of_a_gone_point_is_reported,
            test_purge_spares_queued_rewind_items,
        ]
    )
