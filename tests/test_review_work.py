"""Tests for the review_work adversarial verification tool."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from _runner import run_suite

from syna import session as session_module
from syna import verification
from syna.config import read_asset_text
from syna.parse import parse_and_execute_tools
from syna.tool_schemas import get_tool_definitions
from syna.tools import TOOL_REGISTRY

VERIFIER_PROMPT_TEXT = read_asset_text("verifier_prompt.md")


class FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.refusal = None


class FakeChoice:
    def __init__(self, message):
        self.message = message
        self.finish_reason = (
            "tool_calls" if getattr(message, "tool_calls", None) else "stop"
        )


class FakeResponse:
    def __init__(self, message):
        self.choices = [FakeChoice(message)]


class FakeChat:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.chat = self

    def send(self, **request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, FakeMessage):
            return FakeResponse(reply)
        return FakeResponse(FakeMessage(content=reply))


def _make_tool_call_reply(name, args, call_id="call_1"):
    return FakeMessage(
        content=None,
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    )


def _capture_review_request(monkeypatch, reply="findings: none"):
    """Replace ask_question and create_model_client; return the capture dict."""
    calls = {}

    def fake_ask_question(
        client,
        model,
        messages,
        delay=2,
        effort=None,
        dump_path=None,
        response_format=None,
        accept_reply=None,
        session_id=None,
        tools=None,
        tool_choice=None,
    ):
        calls["model"] = model
        calls["messages"] = messages
        calls["effort"] = effort
        calls["tools"] = tools
        calls["session_id"] = session_id
        return {"content": reply}

    monkeypatch.setattr(verification, "ask_question", fake_ask_question)
    monkeypatch.setattr(session_module, "create_model_client", FakeClient)
    return calls


def _with_provider(provider, func):
    """Run func with a conversation-log provider installed, then clean up."""
    verification.set_conversation_log_provider(provider)
    try:
        return func()
    finally:
        verification.pop_conversation_log_provider()


def test_review_work_returns_verifier_report(monkeypatch):
    calls = _capture_review_request(monkeypatch)
    monkeypatch.setattr(
        verification,
        "get_env",
        lambda: {
            "VERIFIER_MODEL": "reviewer/x",
            "VERIFIER_MODEL_EFFORT": "low",
        },
    )
    conversation = [
        {"role": "system", "content": "agent system prompt"},
        {"role": "user", "content": "do the task"},
    ]
    result = _with_provider(
        lambda: conversation, lambda: TOOL_REGISTRY["review_work"](summary="did stuff")
    )

    assert result == "findings: none"
    assert calls["model"] == "reviewer/x"
    assert calls["effort"] == "low"
    assert calls["tools"] is None, "the reviewer must not receive tool schemas"
    assert calls["session_id"] is None, "the reviewer session is isolated"
    system_message, user_message = calls["messages"]
    assert system_message["role"] == "system"
    assert system_message["content"] == VERIFIER_PROMPT_TEXT
    body = user_message["content"]
    assert body.startswith("```text\n"), "the log must sit in a code block"
    log_chars = len(json.dumps(conversation, ensure_ascii=False))
    assert "Last " + str(log_chars) + " chars" in body
    assert "chars of agent execution log" in body
    assert '"do the task"' in body, "the serialized history must be included"
    assert "excluding deliverables to save bandwidth" in body
    assert "did stuff" in body


def test_review_log_tail_is_truncated_to_configured_chars(monkeypatch):
    calls = _capture_review_request(monkeypatch)
    monkeypatch.setattr(verification, "get_env", lambda: {"MODEL": "agent/main"})
    monkeypatch.setattr(verification, "load_verify_log_tail_chars", lambda: 100)
    conversation = [
        {"role": "user", "content": "x" * 10000},
        {"role": "assistant", "content": "y" * 10000},
    ]
    _with_provider(
        lambda: conversation, lambda: TOOL_REGISTRY["review_work"](summary="s")
    )

    expected_tail = json.dumps(conversation, ensure_ascii=False)[-100:]
    body = calls["messages"][1]["content"]
    assert "Last 100 chars of agent execution log" in body
    assert expected_tail in body, "the log must be the serialized history's tail"


def test_review_work_defaults_to_agent_model_and_high_effort(monkeypatch):
    calls = _capture_review_request(monkeypatch)
    monkeypatch.setattr(verification, "get_env", lambda: {"MODEL": "agent/main"})
    _with_provider(
        lambda: [{"role": "user", "content": "task"}],
        lambda: TOOL_REGISTRY["review_work"](summary="s"),
    )
    assert calls["model"] == "agent/main"
    assert calls["effort"] == "high"


def test_review_work_requires_a_conversation_provider(monkeypatch):
    _capture_review_request(monkeypatch)
    verification.pop_conversation_log_provider()
    result = TOOL_REGISTRY["review_work"](summary="s")
    assert isinstance(result, dict) and "error" in result


def test_review_work_without_model_reports_error(monkeypatch):
    _capture_review_request(monkeypatch)
    monkeypatch.setattr(verification, "get_env", lambda: {"MODEL": ""})
    result = _with_provider(
        lambda: [{"role": "user", "content": "task"}],
        lambda: TOOL_REGISTRY["review_work"](summary="s"),
    )
    assert isinstance(result, dict) and "MODEL" in result["error"]


def test_review_work_rejects_invalid_effort(monkeypatch):
    _capture_review_request(monkeypatch)
    monkeypatch.setattr(
        verification,
        "get_env",
        lambda: {"MODEL": "m", "VERIFIER_MODEL_EFFORT": "banana"},
    )
    result = _with_provider(
        lambda: [{"role": "user", "content": "task"}],
        lambda: TOOL_REGISTRY["review_work"](summary="s"),
    )
    assert isinstance(result, dict) and "VERIFIER_MODEL_EFFORT" in result["error"]


def test_review_work_survives_reviewer_failure(monkeypatch):
    _capture_review_request(monkeypatch)

    def broken_ask_question(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(verification, "ask_question", broken_ask_question)
    result = _with_provider(
        lambda: [{"role": "user", "content": "task"}],
        lambda: TOOL_REGISTRY["review_work"](summary="s"),
    )
    assert isinstance(result, dict) and "error" in result


def test_review_work_rejects_empty_summary():
    result = TOOL_REGISTRY["review_work"](summary="   ")
    assert isinstance(result, dict) and "error" in result


def test_review_work_is_registered_with_a_valid_schema():
    assert TOOL_REGISTRY["review_work"].__name__ == "review_work"
    schema = next(
        entry
        for entry in get_tool_definitions()
        if entry["function"]["name"] == "review_work"
    )
    parameters = schema["function"]["parameters"]
    assert parameters["required"] == ["summary"]
    assert parameters["properties"]["summary"]["type"] == "string"


def test_parse_gives_review_work_no_timeout(monkeypatch):
    captured = {}

    def fake_execute(tool_name, tool_arguments, timeout_s, output_path=None):
        captured["timeout_s"] = timeout_s
        return "ok", False, 0

    from syna import parse as parse_module

    monkeypatch.setattr(parse_module, "execute_tool_with_timeout", fake_execute)
    parse_and_execute_tools(
        [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "review_work", "arguments": '{"summary": "s"}'},
            }
        ]
    )
    assert captured["timeout_s"] is None


def test_reviewer_log_filters_only_the_pending_review_call(monkeypatch):
    from syna.session import AgentSession

    session = AgentSession(session_id="review-log-test")
    session.messages.append({"role": "user", "content": "task"})
    session.messages.append(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "review_work",
                        "arguments": '{"summary": "s"}',
                    },
                },
                {
                    "id": "c2",
                    "type": "function",
                    "function": {
                        "name": "think",
                        "arguments": '{"thought": "key reasoning"}',
                    },
                },
            ],
        }
    )
    log = session._reviewer_log()
    assert len(log) == 3, "the pending review_work call must be filtered out"
    sibling = log[-1]["tool_calls"]
    assert len(sibling) == 1, "siblings in the same batch must survive"
    assert sibling[0]["function"]["name"] == "think"
    assert "key reasoning" in sibling[0]["function"]["arguments"]

    session.messages.append(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c3",
                    "type": "function",
                    "function": {"name": "execute_command", "arguments": "{}"},
                }
            ],
        }
    )
    log = session._reviewer_log()
    assert len(log) == 4, "other pending calls must stay in the snapshot"
    assert log[-1]["tool_calls"][0]["function"]["name"] == "execute_command"

    session.messages[-1] = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c4",
                "type": "function",
                "function": {"name": "review_work", "arguments": '{"summary": "s"}'},
            }
        ],
    }
    log = session._reviewer_log()
    assert len(log) == 3, "a bare review call drops its empty message too"
    names = [call["function"]["name"] for call in log[-1]["tool_calls"]]
    assert names == ["review_work", "think"], (
        "only the trailing message is filtered; a completed review call"
        " earlier in history is part of the methodology and stays"
    )


def test_run_turn_wires_the_reviewer_end_to_end(monkeypatch):
    """Full run_turn: only the tool call and its result reach history."""
    from syna.session import AgentSession

    reviewed = {}

    def fake_ask_question(
        client,
        model,
        messages,
        delay=2,
        effort=None,
        dump_path=None,
        response_format=None,
        accept_reply=None,
        session_id=None,
        tools=None,
        tool_choice=None,
    ):
        reviewed["messages"] = messages
        return {"content": "review findings: none"}

    monkeypatch.setattr(verification, "ask_question", fake_ask_question)
    monkeypatch.setattr(session_module, "create_model_client", FakeClient)
    monkeypatch.setattr(verification, "get_env", lambda: {"MODEL": "agent/main"})

    session = AgentSession(session_id="review-e2e")
    session.dump_path = Path(tempfile.mkdtemp(prefix="syna-review-")) / "dump.json"
    session.request_delay = 0
    session.client = FakeChat(
        [
            _make_tool_call_reply("review_work", {"summary": "task done well"}),
            "Task complete: done well.",
        ]
    )
    session.run_turn("do the task")

    new_messages = session.messages[2:]
    assert [m.get("role") for m in new_messages] == [
        "assistant",
        "tool",
        "assistant",
    ], "history gains only the call, its result, and the final answer"
    assert "review findings: none" in new_messages[1]["content"]
    assert verification.current_conversation_log_provider() is None, (
        "the provider must be popped after the batch"
    )
    calls_with_review = [
        call
        for message in reviewed["messages"]
        for call in (message.get("tool_calls") or [])
    ]
    assert not calls_with_review, "the reviewer must not see the pending call"
    assert reviewed["messages"][0]["role"] == "system"


def main():
    run_suite(
        [
            test_review_work_returns_verifier_report,
            test_review_log_tail_is_truncated_to_configured_chars,
            test_review_work_defaults_to_agent_model_and_high_effort,
            test_review_work_requires_a_conversation_provider,
            test_review_work_without_model_reports_error,
            test_review_work_rejects_invalid_effort,
            test_review_work_survives_reviewer_failure,
            test_review_work_rejects_empty_summary,
            test_review_work_is_registered_with_a_valid_schema,
            test_parse_gives_review_work_no_timeout,
            test_reviewer_log_filters_only_the_pending_review_call,
            test_run_turn_wires_the_reviewer_end_to_end,
        ]
    )


if __name__ == "__main__":
    main()
