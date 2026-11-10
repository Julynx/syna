"""Tests for the terminal front-end's worker and inbox.

Covers the TerminalSession worker's lifecycle (busy transitions,
cancel ownership, FIFO queueing of messages typed mid-turn, pending
question flow through a PendingQuestion, and clean shutdown) and the
TerminalChat loop's routing (lifecycle commands, question answering,
new chat, and delegated commands).
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna import interaction
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


class StubSession:
    """AgentSession stand-in: records calls and gates turns on an event.

    A turn waits on `turn_release` after announcing itself on
    `turn_started`, so tests control exactly how long it runs. The
    optional `cancel_hook` runs inside cancel(), standing in for the
    cooperative unwind a real cancellation triggers.
    """

    def __init__(self):
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

    def run_turn(self, text):
        self.turn_release.clear()
        self.calls.append(("turn", text))
        self.turn_started.set()
        self.turn_release.wait(timeout=10)

    def close(self):
        self.calls.append("close")


def _conversation(session=None):
    """A TerminalSession over a stub session with captured output."""
    printed = []
    return TerminalSession(session or StubSession(), notify=printed.append), printed


# --- worker -----------------------------------------------------------------


def test_submit_runs_the_turn_and_clears_cancel_in_the_worker():
    session = StubSession()
    conversation, _ = _conversation(session)
    try:
        conversation.submit("hello")
        assert _wait(lambda: ("turn", "hello") in session.calls)
        assert session.calls[0] == "clear"
    finally:
        session.turn_release.set()
        conversation.close()
    print("PASS the worker clears cancel and runs the submitted turn")


def test_messages_typed_while_busy_queue_fifo():
    session = StubSession()
    conversation, _ = _conversation(session)
    try:
        conversation.submit("first")
        assert _wait(session.turn_started.is_set)
        assert conversation.submit("second") is True
        session.turn_release.set()
        assert _wait(lambda: ("turn", "second") in session.calls)
        session.turn_release.set()
        turns = [call for call in session.calls if call[0] == "turn"]
        assert turns == [("turn", "first"), ("turn", "second")]
    finally:
        session.turn_release.set()
        conversation.close()
    print("PASS messages typed mid-turn queue FIFO for the next turn")


def test_idle_submit_reports_not_busy():
    session = StubSession()
    conversation, _ = _conversation(session)
    try:
        assert conversation.submit("hello") is False
        assert _wait(session.turn_started.is_set)
    finally:
        session.turn_release.set()
        conversation.close()
    print("PASS an idle conversation reports the message as not queued")


def test_stop_cancels_the_running_turn_and_unwinds_it():
    session = StubSession()
    session.cancel_hook = lambda reason: session.turn_release.set()
    conversation, _ = _conversation(session)
    try:
        conversation.submit("work")
        assert _wait(session.turn_started.is_set)
        conversation.stop()
        assert _wait(lambda: not conversation.busy.is_set())
        assert ("cancel", "user interrupt") in session.calls
    finally:
        session.turn_release.set()
        conversation.close()
    print("PASS /stop cancels the running turn")


def test_question_flows_from_worker_to_prompt_and_back():
    session = StubSession()
    printed = []

    def run_turn(text):
        answer = interaction.ask("Pick one", ["a", "b"])
        session.calls.append(("answer", answer))

    session.run_turn = run_turn
    conversation, printed = _conversation(session)
    try:
        conversation.submit("work")
        assert _wait(lambda: conversation.pending_question.active)
        conversation.pending_question.resolve("2")
        assert _wait(lambda: ("answer", "b") in session.calls)
        assert any("Pick one" in line for line in printed)
        assert any("1. a" in line for line in printed)
    finally:
        session.turn_release.set()
        conversation.close()
    print("PASS ask_user questions are answered through the pending question")


def test_stop_releases_a_pending_question_as_cancelled():
    session = StubSession()

    def run_turn(text):
        interaction.ask("Pick one", ["a", "b"])

    session.run_turn = run_turn
    conversation, printed = _conversation(session)
    try:
        conversation.submit("work")
        assert _wait(lambda: conversation.pending_question.active)
        conversation.stop()
        assert _wait(lambda: not conversation.busy.is_set())
        assert any("Task stopped" in line for line in printed)
    finally:
        session.turn_release.set()
        conversation.close()
    print("PASS a stop releases the pending question and stops the turn")


def test_worker_survives_a_failing_turn():
    session = StubSession()

    def run_turn(text):
        session.calls.append(("turn", text))
        if text == "bad":
            raise RuntimeError("boom")

    session.run_turn = run_turn
    printed = []
    conversation, printed = _conversation(session)
    try:
        conversation.submit("bad")
        assert _wait(lambda: any("failed unexpectedly" in line for line in printed))
        conversation.submit("good")
        assert _wait(lambda: ("turn", "good") in session.calls)
        assert _wait(lambda: not conversation.busy.is_set())
    finally:
        session.turn_release.set()
        conversation.close()
    print("PASS a failing turn is reported and the worker keeps going")


def test_close_stops_a_running_turn_and_joins_the_worker():
    session = StubSession()
    session.cancel_hook = lambda reason: session.turn_release.set()
    conversation, _ = _conversation(session)
    conversation.submit("work")
    assert _wait(session.turn_started.is_set)
    started = time.monotonic()
    conversation.close()
    assert time.monotonic() - started < 9.0
    assert ("cancel", "user interrupt") in session.calls
    print("PASS close cancels a running turn and joins the worker")


def test_stale_cancel_is_cleared_before_the_next_turn():
    session = StubSession()
    conversation, _ = _conversation(session)
    try:
        session.cancel("user interrupt")
        conversation.submit("hello")
        assert _wait(lambda: ("turn", "hello") in session.calls)
        clear_index = session.calls.index("clear")
        turn_index = session.calls.index(("turn", "hello"))
        assert clear_index < turn_index
    finally:
        session.turn_release.set()
        conversation.close()
    print("PASS a stale cancel never cancels the next turn")


# --- chat loop --------------------------------------------------------------


class ScriptedInput:
    """A prompt stand-in: lines, interrupt sentinels, and sync hooks.

    A string is returned as the typed line. KeyboardInterrupt and
    EOFError (the classes) are raised. A callable runs as a
    synchronization hook and yields an empty line, which the loop
    ignores.
    """

    def __init__(self, script):
        self.script = list(script)

    def __call__(self, prompt=""):
        item = self.script.pop(0)
        if item is KeyboardInterrupt:
            raise KeyboardInterrupt
        if item is EOFError:
            raise EOFError
        if callable(item):
            item()
            return ""
        return item


def _make_chat(script, session=None, factory_calls=None):
    """A TerminalChat over scripted input with captured output."""
    said = []
    session = session or StubSession()
    conversation, _ = _conversation(session)
    if factory_calls is not None:
        factory_calls.append(conversation)

    def factory():
        replacement, _ = _conversation(StubSession())
        factory_calls.append(replacement)
        return replacement

    chat = TerminalChat(
        terminal=conversation,
        conversation_factory=factory,
        say=said.append,
        read_line=ScriptedInput(script),
    )
    return chat, session, said


def test_lifecycle_command_stops_a_running_turn():
    state = {}
    session = StubSession()
    session.cancel_hook = lambda reason: session.turn_release.set()
    chat, session, said = _make_chat(
        [
            "work",
            lambda: _wait(lambda: state["chat"].terminal.busy.is_set()),
            "/stop",
            lambda: _wait(lambda: not state["chat"].terminal.busy.is_set()),
            KeyboardInterrupt,
        ],
        session=session,
    )
    state["chat"] = chat
    chat.run()
    assert ("cancel", "user interrupt") in session.calls
    assert any("Stopping..." in line for line in said)
    print("PASS /stop stops the running turn")


def test_stop_when_idle_says_nothing_is_running():
    chat, _, said = _make_chat(["/stop", KeyboardInterrupt])
    chat.run()
    assert any("Nothing is running" in line for line in said)
    print("PASS /stop at an idle prompt reports that nothing runs")


def test_interrupt_during_a_question_releases_it():
    session = StubSession()

    def run_turn(text):
        try:
            interaction.ask("Pick one", ["a", "b"])
        except interaction.InteractionCancelled:
            session.calls.append("cancelled")

    session.run_turn = run_turn
    state = {}
    chat, session, said = _make_chat(
        [
            "work",
            lambda: _wait(lambda: state["chat"].terminal.pending_question.active),
            KeyboardInterrupt,
            lambda: _wait(lambda: not state["chat"].terminal.busy.is_set()),
            KeyboardInterrupt,
        ],
        session=session,
    )
    state["chat"] = chat
    chat.run()
    assert any("Stopping..." in line for line in said)
    print("PASS Ctrl+C during a question releases it and stops the turn")


def test_question_is_answered_by_the_next_prompt_line():
    session = StubSession()

    def run_turn(text):
        answer = interaction.ask("Pick one", ["a", "b"])
        session.calls.append(("answer", answer))

    session.run_turn = run_turn
    state = {}
    chat, session, said = _make_chat(
        [
            "work",
            lambda: _wait(lambda: state["chat"].terminal.pending_question.active),
            "2",
            lambda: _wait(lambda: not state["chat"].terminal.busy.is_set()),
            KeyboardInterrupt,
        ],
        session=session,
    )
    state["chat"] = chat
    chat.run()
    assert ("answer", "b") in session.calls
    print("PASS the answer typed at the prompt reaches the question")


def test_newchat_swaps_the_conversation_and_resets_the_pod(monkeypatch):
    calls = []
    chat, _, said = _make_chat(["/newchat", KeyboardInterrupt], factory_calls=calls)
    monkeypatch.setattr(
        terminal_module, "reset_shared_container", lambda: calls.append("reset")
    )
    chat.run()
    assert len(calls) >= 3
    assert calls[1] == "reset"
    assert any("Started a fresh chat" in line for line in said)
    assert chat.terminal is calls[-1]
    print("PASS /newchat tears the old conversation down and starts fresh")


def test_eof_waits_for_the_running_turn_before_exiting():
    state = {}
    session = StubSession()
    chat, session, said = _make_chat(
        [
            "work",
            lambda: _wait(lambda: state["chat"].terminal.busy.is_set()),
            lambda: threading.Timer(0.3, session.turn_release.set).start(),
            EOFError,
        ],
        session=session,
    )
    state["chat"] = chat
    chat.run()
    assert any("Input closed" in line for line in said)
    assert any("Goodbye" in line for line in said)
    print("PASS closed stdin lets the running turn finish before exiting")


def test_unknown_command_reports_an_error():
    chat, _, said = _make_chat(["/nope", KeyboardInterrupt])
    chat.run()
    assert any("Unregistered command" in line for line in said)
    print("PASS an unknown command reports the registry's error")


def test_idle_interrupt_exits_with_a_farewell():
    chat, session, said = _make_chat([KeyboardInterrupt])
    chat.run()
    assert any("Goodbye" in line for line in said)
    assert "close" in session.calls
    print("PASS Ctrl+C at an idle prompt exits cleanly")


def test_queued_submission_prints_the_queue_notice():
    session = StubSession()
    session.cancel_hook = lambda reason: session.turn_release.set()
    state = {}
    chat, session, said = _make_chat(
        [
            "first",
            lambda: _wait(lambda: state["chat"].terminal.busy.is_set()),
            "second",
            lambda: _wait(lambda: any("Queued" in line for line in said)),
            lambda: threading.Timer(0.2, session.turn_release.set).start(),
            lambda: _wait(lambda: ("turn", "second") in session.calls),
            lambda: session.turn_release.set(),
            lambda: _wait(lambda: not state["chat"].terminal.busy.is_set()),
            KeyboardInterrupt,
        ],
        session=session,
    )
    state["chat"] = chat
    chat.run()
    assert any("Queued" in line for line in said)
    print("PASS a message typed mid-turn announces its queueing")


if __name__ == "__main__":
    run_suite(
        [
            test_submit_runs_the_turn_and_clears_cancel_in_the_worker,
            test_messages_typed_while_busy_queue_fifo,
            test_idle_submit_reports_not_busy,
            test_stop_cancels_the_running_turn_and_unwinds_it,
            test_question_flows_from_worker_to_prompt_and_back,
            test_stop_releases_a_pending_question_as_cancelled,
            test_worker_survives_a_failing_turn,
            test_close_stops_a_running_turn_and_joins_the_worker,
            test_stale_cancel_is_cleared_before_the_next_turn,
            test_lifecycle_command_stops_a_running_turn,
            test_stop_when_idle_says_nothing_is_running,
            test_interrupt_during_a_question_releases_it,
            test_question_is_answered_by_the_next_prompt_line,
            test_newchat_swaps_the_conversation_and_resets_the_pod,
            test_eof_waits_for_the_running_turn_before_exiting,
            test_unknown_command_reports_an_error,
            test_idle_interrupt_exits_with_a_farewell,
            test_queued_submission_prints_the_queue_notice,
        ]
    )
