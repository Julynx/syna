"""Tests for the CLI's stop semantics.

Covers the interrupt matrix of the terminal loop: Ctrl+C at the idle
prompt exits with a farewell, Ctrl+C during a turn requests a
cooperative cancellation and returns to the prompt, a pending ask_user
question cancelled by Ctrl+C routes through the same stop path, and
EOF ends the session.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna.conversation import ask
from syna.interaction import InteractionCancelled


class StubSession:
    """Records the session calls the terminal loop makes, in order."""

    def __init__(self):
        self.calls = []

    def clear_cancel(self):
        self.calls.append("clear")

    def cancel(self, reason="user interrupt"):
        self.calls.append(("cancel", reason))

    def run_turn(self, text):
        self.calls.append(("turn", text))
        if text == "boom":
            raise KeyboardInterrupt

    def close(self):
        self.calls.append("close")


class ScriptedInput:
    """A stdin stand-in feeding lines and raising the given interrupts."""

    def __init__(self, lines):
        self.lines = list(lines)

    def __call__(self, prompt=""):
        item = self.lines.pop(0)
        if item is KeyboardInterrupt:
            raise KeyboardInterrupt
        if item is EOFError:
            raise EOFError
        return item


def _patch_io(monkeypatch, lines):
    printed = []
    monkeypatch.setattr(ask, "input", ScriptedInput(lines))
    monkeypatch.setattr(
        ask, "print", lambda *args, **kwargs: printed.append(" ".join(map(str, args)))
    )
    return printed


def _patch_loop_seams(monkeypatch, session):
    monkeypatch.setattr(ask, "check_setup", lambda: None)
    monkeypatch.setattr(ask, "show_welcome", lambda: None)
    monkeypatch.setattr(ask, "_create_session", lambda: session)


def test_stop_turn_requests_cancellation(monkeypatch):
    session = StubSession()
    printed = _patch_io(monkeypatch, [])
    ask.stop_turn(session)
    assert session.calls == [("cancel", "user interrupt")]
    assert any("Stopping..." in line for line in printed)
    print("PASS stop_turn cancels cooperatively and says so")


def test_loop_exits_on_idle_interrupt_with_farewell(monkeypatch):
    session = StubSession()
    printed = _patch_io(monkeypatch, [KeyboardInterrupt])
    _patch_loop_seams(monkeypatch, session)
    ask.ask_loop()
    assert session.calls == ["close"]
    assert any("Goodbye" in line for line in printed)
    print("PASS idle Ctrl+C exits with a farewell instead of crashing")


def test_interrupt_during_turn_stops_and_continues(monkeypatch):
    session = StubSession()
    printed = _patch_io(monkeypatch, ["boom", "hello", KeyboardInterrupt])
    _patch_loop_seams(monkeypatch, session)
    ask.ask_loop()
    assert session.calls == [
        "clear",
        ("turn", "boom"),
        ("cancel", "user interrupt"),
        "clear",
        ("turn", "hello"),
        "close",
    ]
    assert any("Stopping..." in line for line in printed)
    print("PASS Ctrl+C during a turn stops it and returns to the prompt")


def test_eof_ends_the_session(monkeypatch):
    session = StubSession()
    printed = _patch_io(monkeypatch, [EOFError])
    _patch_loop_seams(monkeypatch, session)
    ask.ask_loop()
    assert session.calls == ["close"]
    print("PASS closed stdin ends the session cleanly")


def test_responder_interrupt_routes_through_stop_turn(monkeypatch):
    session = StubSession()
    printed = _patch_io(monkeypatch, [KeyboardInterrupt])
    responder = ask._terminal_responder(session)
    try:
        responder("Pick one", ["a", "b"])
        raise AssertionError("responder should have raised InteractionCancelled")
    except InteractionCancelled:
        pass
    assert session.calls == [("cancel", "user interrupt")]
    assert any("Stopping..." in line for line in printed)
    print("PASS a cancelled question stops the turn through the shared path")


if __name__ == "__main__":
    run_suite(
        [
            test_stop_turn_requests_cancellation,
            test_loop_exits_on_idle_interrupt_with_farewell,
            test_interrupt_during_turn_stops_and_continues,
            test_eof_ends_the_session,
            test_responder_interrupt_routes_through_stop_turn,
        ]
    )
