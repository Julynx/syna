"""Tests for tool-level error truthfulness: browser failures, missing files."""

import json
import sys
import tempfile
from pathlib import Path

from bollard import DockerException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna import tools as tools_module
from syna.channel import is_channel_message, unwrap_channel
from syna.session import AgentSession
from syna.state import register_container, unregister_container
from syna.tools import TOOL_REGISTRY

MISSING_FILE_404 = (
    'Docker API Error (404): {"cause":"no such file or directory",'
    '"message":"no such file or directory","response":404}'
)
SERVER_ERROR_500 = 'Docker API Error (500): {"message":"server error"}'


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


REPLY_FINAL = FakeMessage(content="task done")


def _tool_call_reply(name: str, args: dict, call_id: str = "call_1") -> FakeMessage:
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


class FakePod:
    """Container stand-in that accepts persistence writes."""

    def exec(self, argv, **kwargs):
        return ""

    def copy_to(self, host_path: str, pod_dir: str):
        Path(host_path).write_text("persisted", encoding="utf-8")


class MissingPathPod(FakePod):
    """Container whose copy_from reports the pod path as absent."""

    def copy_from(self, source_path: str, destination_path: str):
        raise DockerException(MISSING_FILE_404)


class BrokenPod(FakePod):
    """Container whose copy_from fails with an unrelated server error."""

    def copy_from(self, source_path: str, destination_path: str):
        raise DockerException(SERVER_ERROR_500)


class MissingSharePathPod(FakePod):
    """Container whose existence probe reports every path as absent."""

    def exec(self, argv, **kwargs):
        return "missing"


def _make_session():
    session = AgentSession(session_id="tool-errors")
    session.dump_path = Path(tempfile.mkdtemp(prefix="syna-test-")) / "dump.json"
    session.request_delay = 0
    return session


def _tool_result_bodies(session) -> list[str]:
    results = [
        unwrap_channel(m["content"])[1]
        for m in session.messages
        if is_channel_message(m.get("content") or "", "TOOL_RESULTS")
    ]
    assert results, "no tool results message in history"
    return results


def test_web_interact_failure_marks_error_and_skips_siblings():
    def fake_goto(browser, url):
        return "ERROR: Page.goto: NS_ERROR_UNKNOWN_HOST"

    def fake_click(browser, ref):
        raise AssertionError("sibling call must be skipped after the failure")

    original = tools_module.BROWSER_COMMANDS
    tools_module.BROWSER_COMMANDS = {"goto": fake_goto, "click": fake_click}
    try:
        session = _make_session()
        reply = FakeMessage(
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "web_interact",
                        "arguments": json.dumps(
                            {"command": "goto", "args": {"url": "http://x.example"}}
                        ),
                    },
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {
                        "name": "web_interact",
                        "arguments": json.dumps(
                            {"command": "click", "args": {"ref": 1}}
                        ),
                    },
                },
            ]
        )
        session.client = FakeChat([reply, REPLY_FINAL])
        session.run_turn("probe")
    finally:
        tools_module.BROWSER_COMMANDS = original

    bodies = _tool_result_bodies(session)
    assert len(bodies) == 2
    assert 'status="error" exit_code="1"' in bodies[0]
    assert "NS_ERROR_UNKNOWN_HOST" in bodies[0]
    assert 'status="skipped"' in bodies[1]
    print("PASS failed browser command becomes an error row and stops the batch")


def test_web_interact_success_has_no_exit_code():
    def fake_snapshot(browser, viewport_only=None):
        return '[1] heading "Page"'

    original = tools_module.BROWSER_COMMANDS
    tools_module.BROWSER_COMMANDS = {"snapshot": fake_snapshot}
    try:
        session = _make_session()
        reply = _tool_call_reply("web_interact", {"command": "snapshot", "args": {}})
        session.client = FakeChat([reply, REPLY_FINAL])
        session.run_turn("probe")
    finally:
        tools_module.BROWSER_COMMANDS = original

    bodies = _tool_result_bodies(session)
    assert 'status="ok"' in bodies[0]
    assert "exit_code" not in bodies[0]
    print("PASS successful browser command stays a plain ok row")


def test_read_file_missing_path_clean_error():
    register_container(MissingPathPod())
    try:
        session = _make_session()
        reply = _tool_call_reply(
            "read_file",
            {"file_path": "/workspace/eval_notes.txt", "start": 1, "limit": 10},
        )
        session.client = FakeChat([reply, REPLY_FINAL])
        session.run_turn("probe")
    finally:
        unregister_container()

    bodies = _tool_result_bodies(session)
    assert 'status="error" exit_code="1"' in bodies[0]
    assert "No such file or directory: /workspace/eval_notes.txt" in bodies[0]
    assert "Docker API Error" not in bodies[0]
    print("PASS missing-file read reports a clean error, not a Docker 404")


def test_edit_file_missing_path_clean_error():
    register_container(MissingPathPod())
    try:
        session = _make_session()
        reply = _tool_call_reply(
            "edit_file",
            {
                "file_path": "/workspace/missing.py",
                "operations": [
                    {
                        "replace": {
                            "line_num_or_range": [1, 1],
                            "replace_with_lines": ["x"],
                        }
                    }
                ],
            },
        )
        session.client = FakeChat([reply, REPLY_FINAL])
        session.run_turn("probe")
    finally:
        unregister_container()

    bodies = _tool_result_bodies(session)
    assert 'status="error" exit_code="1"' in bodies[0]
    assert "No such file or directory: /workspace/missing.py" in bodies[0]
    assert "Docker API Error" not in bodies[0]
    print("PASS missing-file edit reports a clean error, not a Docker 404")


def test_read_file_other_docker_errors_propagate():
    register_container(BrokenPod())
    try:
        session = _make_session()
        reply = _tool_call_reply(
            "read_file",
            {"file_path": "/workspace/notes.txt", "start": 1, "limit": 10},
        )
        session.client = FakeChat([reply, REPLY_FINAL])
        session.run_turn("probe")
    finally:
        unregister_container()

    bodies = _tool_result_bodies(session)
    assert "Docker API Error (500)" in bodies[0]
    assert 'status="error"' in bodies[0]
    print("PASS unrelated Docker errors propagate unchanged")


def test_share_file_missing_path_reports_error():
    register_container(MissingSharePathPod())
    try:
        session = _make_session()
        reply = _tool_call_reply("share_file", {"file_path": "/workspace/gone.pdf"})
        session.client = FakeChat([reply, REPLY_FINAL])
        session.run_turn("probe")
    finally:
        unregister_container()

    bodies = _tool_result_bodies(session)
    assert 'status="error" exit_code="1"' in bodies[0]
    assert "Path not found inside the pod: /workspace/gone.pdf" in bodies[0]
    print("PASS missing-path share_file is an error row, not a successful dict")


def main():
    assert "read_file" in TOOL_REGISTRY and "edit_file" in TOOL_REGISTRY
    test_web_interact_failure_marks_error_and_skips_siblings()
    test_web_interact_success_has_no_exit_code()
    test_read_file_missing_path_clean_error()
    test_edit_file_missing_path_clean_error()
    test_read_file_other_docker_errors_propagate()
    test_share_file_missing_path_reports_error()
    print("ALL TOOL ERROR TESTS PASSED")


if __name__ == "__main__":
    main()
