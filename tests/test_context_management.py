"""Tests for token estimation, tool-output elision, and pod resource limits."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna.channel import is_channel_message, unwrap_channel, wrap_channel
from syna.context import (
    elide_tool_outputs,
    estimate_history_tokens,
    estimate_tokens,
)
from syna.state import build_pod_host_config, register_container, unregister_container
from syna.tools import TOOL_REGISTRY


class FakePod:
    """Records exec and copy_to calls, standing in for a bollard Container."""

    def exec(self, argv, **kwargs):
        return ""

    def copy_to(self, host_path: str, pod_dir: str):
        Path(host_path).write_text("persisted", encoding="utf-8")


def _tool_results_message(call: int, tool: str, output: str, saved_to: str | None) -> dict:
    attributes = f'call="{call}" tool="{tool}" status="ok"'
    if saved_to:
        attributes += f' saved_to="{saved_to}"'
    body = f"<tool_results>\n<result {attributes}>\n{output}\n</result>\n</tool_results>"
    return {"role": "user", "content": wrap_channel("TOOL_RESULTS", body)}


def test_estimate_tokens_bounds():
    assert estimate_tokens("") >= 1
    assert estimate_tokens("x" * 400) == 100
    assert estimate_tokens("x" * 401) == 101


def test_estimate_history_tokens_counts_every_message():
    messages = [
        {"role": "system", "content": "x" * 400},
        {"role": "user", "content": "y" * 40},
    ]
    total = estimate_history_tokens(messages)
    assert total == 100 + 10 + 2 * 4


def test_elide_replaces_old_saved_outputs_with_pointers():
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "task"},
        _tool_results_message(1, "web_search", "long result body", "/tool_outputs/0001_web_search.txt"),
    ]
    elided = elide_tool_outputs(messages, keep_recent=0)
    assert elided == 1
    _kind, body = unwrap_channel(messages[2]["content"])
    assert "[output elided; full text in /tool_outputs/0001_web_search.txt]" in body
    assert "long result body" not in body
    assert '<result call="1" tool="web_search" status="ok"' in body
    assert is_channel_message(messages[2]["content"], "TOOL_RESULTS")


def test_elide_keeps_results_without_saved_to():
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "task"},
        _tool_results_message(1, "execute_command", "precious output", None),
    ]
    elided = elide_tool_outputs(messages, keep_recent=0)
    assert elided == 0
    _, body = unwrap_channel(messages[2]["content"])
    assert "precious output" in body


def test_elide_keeps_recent_messages_untouched():
    recent = _tool_results_message(2, "web_search", "fresh output", "/tool_outputs/0002_web_search.txt")
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "task"},
        _tool_results_message(1, "web_search", "old output", "/tool_outputs/0001_web_search.txt"),
        {"role": "assistant", "content": "working"},
        recent,
    ]
    elided = elide_tool_outputs(messages, keep_recent=2)
    assert elided == 1
    _, recent_body = unwrap_channel(recent["content"])
    assert "fresh output" in recent_body
    _, old_body = unwrap_channel(messages[2]["content"])
    assert "old output" not in old_body


def test_elide_is_idempotent():
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "task"},
        _tool_results_message(1, "web_search", "body", "/tool_outputs/0001_web_search.txt"),
    ]
    assert elide_tool_outputs(messages, keep_recent=0) == 1
    assert elide_tool_outputs(messages, keep_recent=0) == 0
    _, body = unwrap_channel(messages[2]["content"])
    assert body.count("output elided") == 1


def test_pod_host_config_assembles_limits():
    host_config = build_pod_host_config(memory_mb=1024, pids_limit=64)
    assert host_config == {
        "Init": True,
        "Memory": 1024 * 1024 * 1024,
        "PidsLimit": 64,
    }


def test_pod_host_config_allows_unset_limits():
    host_config = build_pod_host_config(memory_mb=0, pids_limit=0)
    assert host_config == {"Init": True}


def test_session_elides_old_outputs_under_token_budget():
    TOOL_REGISTRY["smoke_tool"] = lambda label="unlabeled": f"ran: {label}"
    pod = FakePod()
    register_container(pod)
    try:
        from syna.session import AgentSession

        session = AgentSession(session_id="context-test")
        session.dump_path = Path(tempfile.mkdtemp(prefix="syna-context-")) / "dump.json"
        session.request_delay = 0
        session.context_budget_tokens = 8500
        session.elide_keep_recent_messages = 0
        session.client = FakeChat([json.dumps({"smoke_tool": {"label": "x" * 12000}}),
                                   json.dumps({"respond": {"text": "done"}})])
        session.run_turn("probe")
    finally:
        unregister_container()
        TOOL_REGISTRY.pop("smoke_tool", None)

    assert len(session.client.requests) == 2, "summarization must not run"
    assert session.client.replies == []

    tool_messages = [
        m for m in session.messages if is_channel_message(m["content"], "TOOL_RESULTS")
    ]
    assert len(tool_messages) == 1
    _, body = unwrap_channel(tool_messages[0]["content"])
    assert "[output elided; full text in /tool_outputs/" in body
    assert "ran: " not in body


class FakeMessage:
    def __init__(self, content):
        self.content = content
        self.refusal = None


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)
        self.finish_reason = "stop"


class FakeResponse:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]


class FakeChat:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.chat = self

    def send(self, **request):
        self.requests.append(request)
        return FakeResponse(self.replies.pop(0))


def main():
    test_estimate_tokens_bounds()
    test_estimate_history_tokens_counts_every_message()
    test_elide_replaces_old_saved_outputs_with_pointers()
    test_elide_keeps_results_without_saved_to()
    test_elide_keeps_recent_messages_untouched()
    test_elide_is_idempotent()
    test_pod_host_config_assembles_limits()
    test_pod_host_config_allows_unset_limits()
    test_session_elides_old_outputs_under_token_budget()
    print("ALL CONTEXT AND POD LIMIT TESTS PASSED")


if __name__ == "__main__":
    main()
