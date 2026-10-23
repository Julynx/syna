"""Unit tests for the ask_user tool, the interaction bridge, and timeouts."""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import run_suite

from syna import interaction, parse, tools
from syna.tool_schemas import get_tool_definitions

QUESTION = "Which database should the project use?"
CHOICES = ["sqlite", "postgres"]


def _stub_call(call_id, name, **arguments):
    import json

    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _parse(calls, **kwargs):
    events = []
    outcome = parse.parse_and_execute_tools(
        calls,
        on_tool_event=lambda *status, **extra: events.append((status, extra)),
        echo_errors=False,
        **kwargs,
    )
    return outcome, events


def test_ask_user_schema_is_registered():
    definitions = {d["function"]["name"]: d for d in get_tool_definitions()}
    schema = definitions["ask_user"]["function"]
    assert set(schema["parameters"]["required"]) == {"question_text", "choices"}
    assert schema["parameters"]["properties"]["choices"]["type"] == "array"
    assert schema["parameters"]["properties"]["question_text"]["type"] == "string"
    assert "pauses" in schema["description"]


def test_ask_user_returns_the_responder_answer():
    interaction.register_responder(lambda question, choices: "postgres")

    try:
        result = tools.ask_user(QUESTION, CHOICES)
    finally:
        interaction.unregister_responder()

    assert result == "User's answer: postgres"


def test_ask_user_without_responder_is_a_clean_error():
    result = tools.ask_user(QUESTION, CHOICES)

    assert isinstance(result, dict)
    assert "not delivered" in result["error"]


def test_ask_user_rejects_malformed_arguments():
    results = [
        tools.ask_user("", CHOICES),
        tools.ask_user(QUESTION, "sqlite"),
        tools.ask_user(QUESTION, [1, 2]),
        tools.ask_user(QUESTION, ["", "sqlite"]),
    ]

    assert all(isinstance(result, dict) and "error" in result for result in results)


def test_ask_user_raises_when_cancelled():
    def cancel(question, choices):
        raise interaction.InteractionCancelled("stopped")

    interaction.register_responder(cancel)
    try:
        raised = False
        try:
            tools.ask_user(QUESTION, CHOICES)
        except interaction.InteractionCancelled:
            raised = True
        assert raised
    finally:
        interaction.unregister_responder()


def test_responder_reaches_the_tool_worker_thread():
    seen = {}

    def stub_ask_user(question_text, choices):
        seen["responder"] = interaction.get_responder()
        return "User's answer: sqlite"

    original = parse.TOOL_REGISTRY.get("ask_user")
    parse.TOOL_REGISTRY["ask_user"] = stub_ask_user
    interaction.register_responder(lambda question, choices: "unused")
    try:
        calls = [_stub_call("call_1", "ask_user", question_text=QUESTION, choices=[])]
        outcome, _ = _parse(calls)
    finally:
        interaction.unregister_responder()
        if original is not None:
            parse.TOOL_REGISTRY["ask_user"] = original
        else:
            parse.TOOL_REGISTRY.pop("ask_user", None)

    assert outcome.outputs[0]["status"] == "ok"
    assert seen["responder"] is not None


def test_ask_user_is_not_detached_at_the_tool_timeout():
    def slow_ask_user(question_text, choices):
        time.sleep(0.4)
        return "User's answer: sqlite"

    original = parse.TOOL_REGISTRY.get("ask_user")
    parse.TOOL_REGISTRY["ask_user"] = slow_ask_user
    interaction.register_responder(lambda question, choices: "unused")
    try:
        calls = [_stub_call("call_1", "ask_user", question_text=QUESTION, choices=[])]
        outcome, _ = _parse(calls, tool_timeout_s=0.05)
    finally:
        interaction.unregister_responder()
        if original is not None:
            parse.TOOL_REGISTRY["ask_user"] = original
        else:
            parse.TOOL_REGISTRY.pop("ask_user", None)

    assert outcome.outputs[0]["status"] == "ok"
    assert "sqlite" in outcome.outputs[0]["output"]


def test_cancelled_question_fails_the_batch_and_skips_the_rest():
    def cancel(question, choices):
        raise interaction.InteractionCancelled("stopped")

    def never_reached(question_text, choices):
        raise AssertionError("this call must be skipped")

    original_ask = parse.TOOL_REGISTRY.get("ask_user")
    original_think = parse.TOOL_REGISTRY.get("think")
    parse.TOOL_REGISTRY["ask_user"] = cancel
    try:
        calls = [
            _stub_call("call_1", "ask_user", question_text=QUESTION, choices=[]),
            _stub_call("call_2", "think", thought="never"),
        ]
        outcome, _events = _parse(calls)
    finally:
        if original_ask is not None:
            parse.TOOL_REGISTRY["ask_user"] = original_ask
        else:
            parse.TOOL_REGISTRY.pop("ask_user", None)
        if original_think is not None:
            parse.TOOL_REGISTRY["think"] = original_think

    assert outcome.outputs[0]["status"] == "error"
    assert outcome.outputs[1]["status"] == "skipped"


def test_pending_question_resolution():
    pending = interaction.PendingQuestion()

    assert not pending.active
    answers = pending.open()
    assert pending.active

    assert pending.resolve("postgres")
    assert not pending.active
    assert answers.get() == "postgres"

    assert not pending.resolve("late")


def test_pending_question_release_unblocks_waiter():
    pending = interaction.PendingQuestion()
    delivered = []

    def waiter():
        answers = pending.open()
        delivered.append(answers.get())

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    time.sleep(0.1)
    pending.resolve(None)
    thread.join(timeout=2)

    assert delivered == [None]


def test_cli_responder_number_picks_a_choice():
    from syna.ask import _terminal_responder

    class StubSession:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    session = StubSession()
    responder = _terminal_responder(session)
    inputs = iter(["2"])

    import builtins

    original_input = builtins.input
    builtins.input = lambda prompt="": next(inputs)
    try:
        chosen = responder(QUESTION, CHOICES)
    finally:
        builtins.input = original_input

    assert chosen == "postgres"
    assert not session.cancelled


def main():
    """Run every ask_user and interaction test and report a summary."""
    run_suite(
        [
            test_ask_user_schema_is_registered,
            test_ask_user_returns_the_responder_answer,
            test_ask_user_without_responder_is_a_clean_error,
            test_ask_user_rejects_malformed_arguments,
            test_ask_user_raises_when_cancelled,
            test_responder_reaches_the_tool_worker_thread,
            test_ask_user_is_not_detached_at_the_tool_timeout,
            test_cancelled_question_fails_the_batch_and_skips_the_rest,
            test_pending_question_resolution,
            test_pending_question_release_unblocks_waiter,
            test_cli_responder_number_picks_a_choice,
        ]
    )


if __name__ == "__main__":
    main()
