"""Tests for tool-level error truthfulness: browser failures, missing files."""

import json
import sys
from pathlib import Path

from bollard import DockerException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna import tools as tools_module
from syna.channel import is_channel_message, unwrap_channel
from syna.session import AgentSession
from syna.state import register_container, unregister_container
from syna.tools import TOOL_REGISTRY

REPLY_RESPOND = json.dumps({"respond": {"text": "task done"}})

MISSING_FILE_404 = (
    'Docker API Error (404): {"cause":"no such file or directory",'
    '"message":"no such file or directory","response":404}'
)
SERVER_ERROR_500 = 'Docker API Error (500): {"message":"server error"}'


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


def _make_session():
    session = AgentSession(session_id="tool-errors")
    session.dump_path = Path(__file__).parent / "tmp-dump-tool-errors.json"
    session.request_delay = 0
    return session


def _first_results_body(session):
    results = [
        m for m in session.messages
        if is_channel_message(m["content"], "TOOL_RESULTS")
    ]
    assert results, "no tool results message in history"
    return unwrap_channel(results[0]["content"])[1]


def test_web_interact_failure_marks_error_and_skips_siblings():
    def fake_goto(browser, url):
        return "ERROR: Page.goto: NS_ERROR_UNKNOWN_HOST"

    def fake_click(browser, ref):
        raise AssertionError("sibling call must be skipped after the failure")

    original = tools_module.BROWSER_COMMANDS
    tools_module.BROWSER_COMMANDS = {"goto": fake_goto, "click": fake_click}
    try:
        session = _make_session()
        reply = json.dumps(
            [
                {"web_interact": {"command": "goto", "args": {"url": "http://x.example"}}},
                {"web_interact": {"command": "click", "args": {"ref": 1}}},
            ]
        )
        session.client = FakeChat([reply, REPLY_RESPOND])
        session.run_turn("probe")
    finally:
        tools_module.BROWSER_COMMANDS = original

    body = _first_results_body(session)
    assert 'tool="web_interact" status="error" exit_code="1"' in body
    assert 'status="skipped"' in body
    assert "NS_ERROR_UNKNOWN_HOST" in body
    print("PASS failed browser command becomes an error row and stops the batch")


def test_web_interact_success_has_no_exit_code():
    def fake_snapshot(browser, viewport_only=None):
        return "[1] heading \"Page\""

    original = tools_module.BROWSER_COMMANDS
    tools_module.BROWSER_COMMANDS = {"snapshot": fake_snapshot}
    try:
        session = _make_session()
        reply = json.dumps({"web_interact": {"command": "snapshot", "args": {}}})
        session.client = FakeChat([reply, REPLY_RESPOND])
        session.run_turn("probe")
    finally:
        tools_module.BROWSER_COMMANDS = original

    body = _first_results_body(session)
    assert 'tool="web_interact" status="ok"' in body
    assert "exit_code" not in body
    print("PASS successful browser command stays a plain ok row")


def test_read_file_missing_path_clean_error():
    register_container(MissingPathPod())
    try:
        session = _make_session()
        reply = json.dumps(
            {"read_file": {"file_path": "/workspace/eval_notes.txt", "start": 1, "limit": 10}}
        )
        session.client = FakeChat([reply, REPLY_RESPOND])
        session.run_turn("probe")
    finally:
        unregister_container()

    body = _first_results_body(session)
    assert 'tool="read_file" status="error" exit_code="1"' in body
    assert "No such file or directory: /workspace/eval_notes.txt" in body
    assert "Docker API Error" not in body
    print("PASS missing-file read reports a clean error, not a Docker 404")


def test_edit_file_missing_path_clean_error():
    register_container(MissingPathPod())
    try:
        session = _make_session()
        reply = json.dumps(
            {
                "edit_file": {
                    "file_path": "/workspace/missing.py",
                    "operations": [
                        {"replace": {"line_num_or_range": [1, 1], "replace_with_lines": ["x"]}}
                    ],
                }
            }
        )
        session.client = FakeChat([reply, REPLY_RESPOND])
        session.run_turn("probe")
    finally:
        unregister_container()

    body = _first_results_body(session)
    assert 'tool="edit_file" status="error" exit_code="1"' in body
    assert "No such file or directory: /workspace/missing.py" in body
    assert "Docker API Error" not in body
    print("PASS missing-file edit reports a clean error, not a Docker 404")


def test_read_file_other_docker_errors_propagate():
    register_container(BrokenPod())
    try:
        session = _make_session()
        reply = json.dumps(
            {"read_file": {"file_path": "/workspace/notes.txt", "start": 1, "limit": 10}}
        )
        session.client = FakeChat([reply, REPLY_RESPOND])
        session.run_turn("probe")
    finally:
        unregister_container()

    body = _first_results_body(session)
    assert "Docker API Error (500)" in body
    assert 'status="error"' in body
    print("PASS unrelated Docker errors propagate unchanged")


def main():
    assert "read_file" in TOOL_REGISTRY and "edit_file" in TOOL_REGISTRY
    test_web_interact_failure_marks_error_and_skips_siblings()
    test_web_interact_success_has_no_exit_code()
    test_read_file_missing_path_clean_error()
    test_edit_file_missing_path_clean_error()
    test_read_file_other_docker_errors_propagate()
    print("ALL TOOL ERROR TESTS PASSED")


if __name__ == "__main__":
    main()
