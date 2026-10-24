"""End-to-end simulated task: a scripted multi-turn conversation driving the
full session loop — batched calls, stop-on-failure, persistence, elision,
budget nudges, think notes — without a live model or container."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna.protocol.channel import is_channel_message, unwrap_channel
from syna.protocol.context import estimate_tokens
from syna.conversation.session import AgentSession, build_prompt
from syna.capabilities.tools import TOOL_REGISTRY


class FakePod:
    """Container stand-in: accepts output persistence writes."""

    def exec(self, argv, **kwargs):
        return ""

    def copy_to(self, host_path: str, pod_dir: str):
        Path(host_path).write_text("persisted", encoding="utf-8")


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


def _register_tools():
    def smoke_exec(command: str) -> str:
        """Simulated execute_command: returns a view with an exit code."""
        from syna.state import set_last_exit_code

        if "missing_thing" in command:
            set_last_exit_code(1)
            return "cat: missing_thing: No such file or directory"
        set_last_exit_code(0)
        return f"output of: {command}"

    def smoke_search(query: str) -> str:
        return f"Results for {query}:\n1. Result one\n2. Result two"

    TOOL_REGISTRY["smoke_exec"] = smoke_exec
    TOOL_REGISTRY["smoke_search"] = smoke_search


def _unregister_tools():
    TOOL_REGISTRY.pop("smoke_exec", None)
    TOOL_REGISTRY.pop("smoke_search", None)


def _tool_results_messages(session: AgentSession):
    return [
        m
        for m in session.messages
        if is_channel_message(m.get("content") or "", "TOOL_RESULTS")
    ]


def main():
    _register_tools()
    register_ok = False
    try:
        from syna.state import register_container, unregister_container

        register_container(FakePod())
        register_ok = True

        session = AgentSession(
            session_id="simulated-task",
            persist_outputs=True,
            emit_tool_errors=True,
        )
        session.dump_path = Path(tempfile.mkdtemp(prefix="syna-sim-")) / "dump.json"
        session.request_delay = 0
        # The budget must sit above the natural post-elision history size
        # (so the turn ends without compaction) but below its pre-elision
        # size (so elision actually runs). Scaling it off the prompt size
        # keeps both properties as the prompt grows.
        session.context_budget_tokens = estimate_tokens(build_prompt()) + 3500
        session.elide_keep_recent_messages = 0
        events = []
        session.on_event = events.append

        # Turn script:
        # 1. batch: big web_search (persisted), then a command that fails
        #    (exit 1) -> the third call must be skipped
        # 2. lone think note
        # 3. think + retry of just the failing step, succeeding now
        # 4. final text answer
        big_query = "q" * 9000
        replies = [
            FakeMessage(
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "smoke_search",
                            "arguments": json.dumps({"query": big_query}),
                        },
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "smoke_exec",
                            "arguments": json.dumps({"command": "cat missing_thing"}),
                        },
                    },
                    {
                        "id": "call_3",
                        "type": "function",
                        "function": {
                            "name": "smoke_exec",
                            "arguments": json.dumps({"command": "echo never runs"}),
                        },
                    },
                ]
            ),
            FakeMessage(
                tool_calls=[
                    {
                        "id": "call_4",
                        "type": "function",
                        "function": {
                            "name": "think",
                            "arguments": json.dumps(
                                {"thought": "The file is missing; use echo instead."}
                            ),
                        },
                    }
                ]
            ),
            FakeMessage(
                tool_calls=[
                    {
                        "id": "call_5",
                        "type": "function",
                        "function": {
                            "name": "think",
                            "arguments": json.dumps(
                                {
                                    "thought": "The cat failed because the file is missing; use echo instead."
                                }
                            ),
                        },
                    },
                    {
                        "id": "call_6",
                        "type": "function",
                        "function": {
                            "name": "smoke_exec",
                            "arguments": json.dumps({"command": "echo recovered"}),
                        },
                    },
                ]
            ),
            FakeMessage(
                content="Task done. Search results summarized; the missing file was reported."
            ),
        ]
        session.client = FakeChat(replies)
        session.run_turn("Search for the topic and read missing_thing.")

        # 1. Exactly four model requests were made (batch, lone think,
        #    think+retry, final text answer).
        assert len(session.client.requests) == 4, len(session.client.requests)

        # 2. In turn 1: 3 tool result messages were produced; the failing command stopped the batch
        #    and the third call was marked skipped.
        tool_msgs = _tool_results_messages(session)
        assert (
            len(tool_msgs) == 6
        )  # 3 from batch + 1 from lone think + 2 from think+retry

        first_body = unwrap_channel(tool_msgs[0]["content"])[1]
        second_body = unwrap_channel(tool_msgs[1]["content"])[1]
        third_body = unwrap_channel(tool_msgs[2]["content"])[1]

        assert 'status="ok"' in first_body
        assert 'status="error" exit_code="1"' in second_body
        assert 'status="skipped"' in third_body
        assert "never runs" not in third_body

        # 3. The lone think call produced a tool result with '(noted)'
        fourth_body = unwrap_channel(tool_msgs[3]["content"])[1]
        assert "(noted)" in fourth_body

        # 4. Old outputs with saved_to were elided
        elided_count = sum(
            "output elided" in (m.get("content") or "") for m in session.messages
        )
        assert elided_count >= 1, f"expected elided messages, got {elided_count}"

        # 5. The final text response reached the user as model_message event
        final_events = [e for e in events if e.get("type") == "model_message"]
        assert len(final_events) == 1
        assert "Task done" in final_events[0]["content"]

        print(
            "PASS end-to-end simulated task completed cleanly under native tool calling"
        )

    finally:
        _unregister_tools()
        if register_ok:
            unregister_container()


if __name__ == "__main__":
    main()
