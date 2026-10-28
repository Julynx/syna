"""Tests for token estimation, tool-output elision, and pod resource limits."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna.protocol.channel import (
    format_tool_result_content,
    is_channel_message,
    unwrap_channel,
)
from syna.protocol.context import (
    elide_tool_outputs,
    estimate_history_tokens,
    estimate_tokens,
)
from syna.state import build_pod_host_config, register_container, unregister_container
from syna.capabilities.tools import TOOL_REGISTRY


class FakePod:
    """Records exec and copy_to calls, standing in for a bollard Container."""

    def exec(self, argv, **kwargs):
        return ""

    def copy_to(self, host_path: str, pod_dir: str):
        Path(host_path).write_text("persisted", encoding="utf-8")


def _tool_results_message(
    call: int, tool: str, output: str, saved_to: str | None
) -> dict:
    content = format_tool_result_content(status="ok", output=output, saved_to=saved_to)
    return {"role": "tool", "tool_call_id": f"call_{call}", "content": content}


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
        _tool_results_message(
            1, "web_search", "long result body", "/tool_outputs/0001_web_search.txt"
        ),
    ]
    elided = elide_tool_outputs(messages, keep_recent=0)
    assert elided == 1
    _kind, body = unwrap_channel(messages[2]["content"])
    assert "[output elided; full text in /tool_outputs/0001_web_search.txt]" in body
    assert "long result body" not in body
    assert '<result status="ok"' in body
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
    recent = _tool_results_message(
        2, "web_search", "fresh output", "/tool_outputs/0002_web_search.txt"
    )
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "task"},
        _tool_results_message(
            1, "web_search", "old output", "/tool_outputs/0001_web_search.txt"
        ),
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
        _tool_results_message(
            1, "web_search", "body", "/tool_outputs/0001_web_search.txt"
        ),
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


def test_create_container_self_heals_stale_endpoint():
    from _runner import MonkeyPatch

    from syna import state

    class FlakyClient:
        """Fails its first container run on a dead pipe, then works."""

        def __init__(self):
            self.runs = 0

        def run_container(self, image, **kwargs):
            self.runs += 1
            if self.runs == 1:
                raise FileNotFoundError(
                    2, "No such file or directory", "\\\\.\\pipe\\docker_engine"
                )
            return f"container-{self.runs}"

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    patch = MonkeyPatch()
    flaky = FlakyClient()
    fake_stack = type(
        "FakeStack",
        (),
        {"closed": False, "close": lambda self: setattr(self, "closed", True)},
    )()
    patch.setattr(state, "client", flaky)
    patch.setattr(state, "_exit_stack", fake_stack)
    patch.setattr(state, "_global_container", None)
    patch.setattr(state, "build_pod_host_config", lambda **k: {"Init": True})
    patch.setattr(state, "init_docker_client", lambda: setattr(state, "client", flaky))
    try:
        container = state.create_container()
        assert container == "container-2", "the retry after the reset must succeed"
        assert state.client is flaky
    finally:
        patch.undo()


def test_create_container_non_connection_error_propagates():
    from _runner import MonkeyPatch

    from syna import state

    class HostileClient:
        def run_container(self, image, **kwargs):
            raise RuntimeError(
                "Docker API Error (500): OCI runtime attempted to invoke a"
                " command that was not found"
            )

    patch = MonkeyPatch()
    patch.setattr(state, "client", HostileClient())
    init_calls = []
    patch.setattr(state, "init_docker_client", lambda: init_calls.append(1))
    try:
        try:
            state.create_container()
        except RuntimeError as exc:
            assert "500" in str(exc)
        else:
            raise AssertionError("a non-connection error must propagate")
        assert not init_calls, "no client reset for a non-connection error"
    finally:
        patch.undo()


def test_connection_error_classification():
    from syna import state

    assert state._is_connection_error(
        FileNotFoundError(2, "No such file or directory", "\\\\.\\pipe\\docker_engine")
    )
    assert state._is_connection_error(ConnectionError("refused"))
    assert state._is_connection_error(
        RuntimeError("No Docker or Podman API endpoint is reachable.")
    )
    assert not state._is_connection_error(RuntimeError("Docker API Error (500): boom"))
    assert not state._is_connection_error(ValueError("bad arguments"))


def _make_tool_call_reply(name, args, call_id="call_1"):
    return FakeMessage(
        content=None,
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args),
                },
            }
        ],
    )


def _make_text_reply(text):
    return FakeMessage(content=text, tool_calls=None)


def test_session_elides_old_outputs_under_token_budget():
    TOOL_REGISTRY["smoke_tool"] = lambda label="unlabeled": f"ran: {label}"
    pod = FakePod()
    register_container(pod)
    try:
        from syna.conversation.session import AgentSession

        session = AgentSession(session_id="context-test")
        session.dump_path = Path(tempfile.mkdtemp(prefix="syna-context-")) / "dump.json"
        session.request_delay = 0
        session.context_budget_tokens = 14000
        session.elide_keep_recent_messages = 0
        session.client = FakeChat(
            [
                _make_tool_call_reply("smoke_tool", {"label": "x" * 24000}),
                _make_text_reply("done"),
            ]
        )
        session.run_turn("probe")
    finally:
        unregister_container()
        TOOL_REGISTRY.pop("smoke_tool", None)

    assert len(session.client.requests) == 2, "summarization must not run"
    assert session.client.replies == []

    tool_messages = [
        m
        for m in session.messages
        if is_channel_message(m.get("content") or "", "TOOL_RESULTS")
    ]
    assert len(tool_messages) == 1
    _, body = unwrap_channel(tool_messages[0]["content"])
    assert "[output elided; full text in /tool_outputs/" in body
    assert "ran: " not in body


def test_session_elides_old_outputs_on_message_interval():
    TOOL_REGISTRY["smoke_tool"] = lambda label="unlabeled": f"ran: {label}"
    pod = FakePod()
    register_container(pod)
    try:
        from syna.conversation.session import AgentSession

        session = AgentSession(session_id="context-interval-test")
        session.dump_path = Path(tempfile.mkdtemp(prefix="syna-context-")) / "dump.json"
        session.request_delay = 0
        session.context_budget_tokens = 10**9
        session.max_history_messages = 10**6
        session.elide_keep_recent_messages = 1
        session.elide_interval_messages = 2
        session.elide_every_requests = 1
        session.client = FakeChat(
            [
                _make_tool_call_reply(
                    "smoke_tool", {"label": "first"}, call_id="call_1"
                ),
                _make_tool_call_reply(
                    "smoke_tool", {"label": "second"}, call_id="call_2"
                ),
                _make_text_reply("done"),
            ]
        )
        session.run_turn("probe")
    finally:
        unregister_container()
        TOOL_REGISTRY.pop("smoke_tool", None)

    assert not session.client.replies, "summarization must not run"

    tool_messages = [
        m
        for m in session.messages
        if is_channel_message(m.get("content") or "", "TOOL_RESULTS")
    ]
    assert len(tool_messages) == 2
    _, first_body = unwrap_channel(tool_messages[0]["content"])
    _, second_body = unwrap_channel(tool_messages[1]["content"])
    assert "[output elided; full text in /tool_outputs/" in first_body
    assert "ran: first" not in first_body
    assert "ran: second" in second_body, "recent output must stay intact"


def test_session_elision_is_batched_by_request_interval():
    TOOL_REGISTRY["smoke_tool"] = lambda label="unlabeled": f"ran: {label}"
    pod = FakePod()
    register_container(pod)
    try:
        from syna.conversation.session import AgentSession

        session = AgentSession(session_id="context-batch-test")
        session.dump_path = Path(tempfile.mkdtemp(prefix="syna-batch-")) / "dump.json"
        session.request_delay = 0
        session.context_budget_tokens = 10**9
        session.max_history_messages = 10**6
        session.elide_keep_recent_messages = 1
        session.elide_interval_messages = 2
        session.elide_every_requests = 10
        session.client = FakeChat(
            [
                _make_tool_call_reply(
                    "smoke_tool", {"label": "first"}, call_id="call_1"
                ),
                _make_tool_call_reply(
                    "smoke_tool", {"label": "second"}, call_id="call_2"
                ),
                _make_tool_call_reply(
                    "smoke_tool", {"label": "third"}, call_id="call_3"
                ),
                _make_text_reply("done"),
            ]
        )
        session.run_turn("probe")
    finally:
        unregister_container()
        TOOL_REGISTRY.pop("smoke_tool", None)

    third_prompt = session.client.requests[2]["messages"]
    assert any("ran: first" in str(m.get("content")) for m in third_prompt), (
        "outputs must stay full-text between batched elision passes"
    )
    assert not any(
        "output elided" in str(m.get("content")) for m in session.messages
    ), "no batched elision pass is due within the request budget"


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.refusal = None


class FakeChoice:
    def __init__(self, message):
        self.message = message
        self.finish_reason = (
            "stop" if not getattr(message, "tool_calls", None) else "tool_calls"
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


def main():
    test_estimate_tokens_bounds()
    test_estimate_history_tokens_counts_every_message()
    test_elide_replaces_old_saved_outputs_with_pointers()
    test_elide_keeps_results_without_saved_to()
    test_elide_keeps_recent_messages_untouched()
    test_elide_is_idempotent()
    test_pod_host_config_assembles_limits()
    test_pod_host_config_allows_unset_limits()
    test_create_container_self_heals_stale_endpoint()
    test_create_container_non_connection_error_propagates()
    test_connection_error_classification()
    test_session_elides_old_outputs_under_token_budget()
    test_session_elides_old_outputs_on_message_interval()
    test_session_elision_is_batched_by_request_interval()
    print("ALL CONTEXT AND POD LIMIT TESTS PASSED")


if __name__ == "__main__":
    main()
