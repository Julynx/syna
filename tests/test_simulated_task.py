"""End-to-end simulated task: a scripted multi-turn conversation driving the
full session loop — batched calls, stop-on-failure, persistence, elision,
budget nudges, think+respond — without a live model or container."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna.channel import is_channel_message, unwrap_channel
from syna.session import AgentSession
from syna.tools import TOOL_REGISTRY


class FakePod:
    """Container stand-in: accepts output persistence writes."""

    def exec(self, argv, **kwargs):
        return ""

    def copy_to(self, host_path: str, pod_dir: str):
        Path(host_path).write_text("persisted", encoding="utf-8")


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
        m for m in session.messages
        if is_channel_message(m["content"], "TOOL_RESULTS")
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
        session.context_budget_tokens = 9000
        session.elide_keep_recent_messages = 0
        events = []
        session.on_event = events.append

        # Turn script:
        # 1. batch: big web_search (persisted), then a command that fails
        #    (exit 1) -> the third call must be skipped
        # 2. think + retry of just the failing step, succeeding now
        # 3. respond with the final report
        big_query = "q" * 9000
        replies = [
            json.dumps(
                [
                    {"smoke_search": {"query": big_query}},
                    {"smoke_exec": {"command": "cat missing_thing"}},
                    {"smoke_exec": {"command": "echo never runs"}},
                ]
            ),
            json.dumps(
                [
                    {"think": {"thought": "The cat failed because the file is missing; use echo instead."}},
                    {"smoke_exec": {"command": "echo recovered"}},
                ]
            ),
            json.dumps(
                {"respond": {"text": "Task done. Search results summarized; the missing file was reported."}}
            ),
        ]
        session.client = FakeChat(replies)
        session.run_turn("Search for the topic and read missing_thing.")

        # 1. Exactly three model requests were made.
        assert len(session.client.requests) == 3, len(session.client.requests)

        # 2. The first batch ran in order; the failing command stopped it and
        #    the third call was skipped.
        first_results = unwrap_channel(_tool_results_messages(session)[0]["content"])[1]
        assert 'call="1" tool="smoke_search" status="ok"' in first_results
        assert 'call="2" tool="smoke_exec" status="error" exit_code="1"' in first_results
        assert 'call="3" tool="smoke_exec" status="skipped"' in first_results
        assert "never runs" not in first_results

        # 3. Every executed call has a saved_to path in the pod.
        assert first_results.count("saved_to=") == 2

        # 4. The think call produced no result entry.
        second_results = unwrap_channel(_tool_results_messages(session)[1]["content"])[1]
        assert "think" not in second_results
        assert 'tool="smoke_exec" status="ok"' in second_results

        # 5. Old outputs left the recent window and were elided to their
        #    saved_to pointers — including the failing one — while all
        #    metadata (status, exit_code, saved_to) stays readable.
        elided_count = sum(
            "output elided" in m["content"] for m in session.messages
        )
        assert elided_count == 1, f"expected one elided message, got {elided_count}"
        assert first_results.count("[output elided; full text in /tool_outputs/") == 2
        assert 'status="error" exit_code="1"' in first_results
        assert "never runs" not in first_results

        # 6. The second batch's output was recent enough to stay intact.
        assert "output of: echo recovered" in second_results

        # 6. The respond message reached the user and the turn ended cleanly.
        model_messages = [e for e in events if e["type"] == "model_message"]
        assert len(model_messages) == 1
        assert "Task done" in model_messages[0]["content"]
        assert not any(e["type"] == "error" for e in events)

        # 7. History is append-only and coherent: system, task, and replies.
        assert session.messages[0]["role"] == "system"
        assert session.messages[1]["content"] == "Search for the topic and read missing_thing."
    finally:
        if register_ok:
            from syna.state import unregister_container

            unregister_container()
        _unregister_tools()

    print("SIMULATED TASK PASSED: full loop behaved per protocol")


if __name__ == "__main__":
    main()
