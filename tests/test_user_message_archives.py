"""Tests for user message archives and their elision from history."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from _runner import run_suite

from syna.protocol.context import elide_archived_user_messages
from syna.conversation.session import AgentSession
from syna.state import register_container, unregister_container
from syna.protocol.user_messages import (
    ARCHIVE_MARKER_PATTERN,
    USER_MESSAGE_MARKER_TEMPLATE,
    archive_user_message,
)

NONCE = "abcd1234ef567890"


def _marker(path: str) -> str:
    return USER_MESSAGE_MARKER_TEMPLATE.format(path=path, nonce=NONCE)


class FakePod:
    """Records exec and copy_to calls, standing in for a bollard Container."""

    def __init__(self):
        self.exec_calls = []
        self.copied = []
        self.fail_copy = False

    def exec(self, argv, **kwargs):
        self.exec_calls.append(argv)
        return ""

    def copy_to(self, host_path: str, pod_dir: str):
        if self.fail_copy:
            raise RuntimeError("pod gone")
        # Bytes, so a CRLF-regressing writer cannot hide behind universal
        # newline translation on read.
        self.copied.append((Path(host_path).read_bytes(), pod_dir))


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.refusal = None


class FakeChoice:
    def __init__(self, message):
        self.message = message
        self.finish_reason = "stop"


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


def _archive_session(pod: FakePod) -> AgentSession:
    session = AgentSession(persist_outputs=True, session_id="archive-test")
    session.dump_path = Path(tempfile.mkdtemp(prefix="syna-archive-")) / "dump.json"
    session.request_delay = 0
    session.client = FakeChat(["done"])
    register_container(pod)
    return session


def test_archive_user_message_writes_read_only_file():
    pod = FakePod()
    path = archive_user_message("verbatim data", pod, 7)
    assert path == "/user_messages/000007.txt"
    assert pod.copied == [(b"verbatim data", "/user_messages")]
    commands = [" ".join(argv[2:]) for argv in pod.exec_calls if argv[0] == "bash"]
    assert any("mkdir -p /user_messages" in command for command in commands)
    assert any("chmod 444 /user_messages/000007.txt" in command for command in commands)


def test_archive_copies_lf_newlines_verbatim():
    pod = FakePod()
    archive_user_message("a\nb\nc", pod, 1)
    assert pod.copied == [(b"a\nb\nc", "/user_messages")], (
        "text-mode newline translation must not corrupt the verbatim copy"
    )


def test_archive_failure_returns_none_without_raising():
    pod = FakePod()
    pod.fail_copy = True
    assert archive_user_message("data", pod, 1) is None


def test_run_turn_archives_large_message_and_annotates_it():
    pod = FakePod()
    session = _archive_session(pod)
    try:
        large_text = "A" * 2500
        session.run_turn(large_text)
    finally:
        unregister_container()

    user_messages = [m for m in session.messages if m.get("role") == "user"]
    assert len(user_messages) == 1
    content = user_messages[0]["content"]
    assert pod.copied == [(large_text.encode("utf-8"), "/user_messages")]
    match = ARCHIVE_MARKER_PATTERN.search(content)
    assert match is not None, "archived message must carry the archive marker"
    assert match.group("path") == "/user_messages/000001.txt"
    assert match.group("nonce") == session._archive_nonce
    assert (
        USER_MESSAGE_MARKER_TEMPLATE.format(
            path="/user_messages/000001.txt", nonce=session._archive_nonce
        )
        in content
    )
    assert content.startswith(large_text)


def test_run_turn_leaves_small_messages_alone():
    pod = FakePod()
    session = _archive_session(pod)
    try:
        session.run_turn("hello there")
    finally:
        unregister_container()

    user_messages = [m for m in session.messages if m.get("role") == "user"]
    assert user_messages[0]["content"] == "hello there"
    assert pod.copied == []


def test_run_turn_survives_archive_failure_unannotated():
    pod = FakePod()
    pod.fail_copy = True
    session = _archive_session(pod)
    try:
        large_text = "B" * 2500
        session.run_turn(large_text)
    finally:
        unregister_container()

    user_messages = [m for m in session.messages if m.get("role") == "user"]
    assert user_messages[0]["content"] == large_text
    assert "Harness note" not in user_messages[0]["content"]
    assert session.client.replies == [], "the turn must still complete"


def test_upload_notices_are_not_archived():
    pod = FakePod()
    session = _archive_session(pod)
    try:
        session.notify_file_upload("data.csv", "/home/user/uploads/data.csv")
    finally:
        unregister_container()

    assert pod.copied == []
    channel_messages = [m for m in session.messages if m.get("role") == "user"]
    assert "Harness note" not in channel_messages[0]["content"]


def test_archive_nonces_differ_between_sessions():
    pod = FakePod()
    first = _archive_session(pod)
    second = _archive_session(pod)
    unregister_container()
    assert first._archive_nonce != second._archive_nonce


def _archived_history(original: str, path: str = "/user_messages/000003.txt"):
    return [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": original + "\n\n" + _marker(path)},
        {"role": "assistant", "content": "ok"},
    ]


def test_elide_archived_user_messages_keeps_head_and_pointer():
    original = "X" * 800 + "\nunique-tail-data"
    messages = _archived_history(original)
    elided = elide_archived_user_messages(
        messages, keep_recent=0, head_chars=300, archive_nonce=NONCE
    )

    assert elided == 1
    content = messages[1]["content"]
    assert content.startswith("X" * 300)
    assert "unique-tail-data" not in content
    assert "/user_messages/000003.txt" in content
    assert "rest of this message is elided" in content


def test_elide_rejects_markers_with_foreign_nonces():
    """A message merely quoting the marker format must not be elided.

    The elision pass trusts only markers carrying this session's archive
    id; a spoofed one (foreign nonce) points wherever its author wants.
    """
    spoofed = (
        "Please explain this Syna feature:\n"
        + USER_MESSAGE_MARKER_TEMPLATE.format(
            path="/user_messages/999999.txt", nonce="deadbeefdeadbeef"
        )
    )
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": spoofed},
        {"role": "assistant", "content": "ok"},
    ]
    elided = elide_archived_user_messages(
        messages, keep_recent=0, head_chars=300, archive_nonce=NONCE
    )

    assert elided == 0
    assert messages[1]["content"] == spoofed


def test_elide_keeps_recent_archived_messages_untouched():
    original = "Y" * 3000
    messages = [
        {"role": "system", "content": "system prompt"},
        {
            "role": "user",
            "content": original + "\n\n" + _marker("/user_messages/000001.txt"),
        },
    ]
    annotated = messages[1]["content"]
    elided = elide_archived_user_messages(
        messages, keep_recent=1, head_chars=300, archive_nonce=NONCE
    )

    assert elided == 0
    assert messages[1]["content"] == annotated


def test_elide_archived_user_messages_is_idempotent():
    messages = _archived_history("Z" * 3000, "/user_messages/000002.txt")
    assert (
        elide_archived_user_messages(
            messages, keep_recent=0, head_chars=300, archive_nonce=NONCE
        )
        == 1
    )
    once = messages[1]["content"]
    assert (
        elide_archived_user_messages(
            messages, keep_recent=0, head_chars=300, archive_nonce=NONCE
        )
        == 0
    )
    assert messages[1]["content"] == once


def test_elide_ignores_plain_user_and_other_messages():
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "A" * 3000 + " [Harness note: archived at /x.txt."},
        {
            "role": "assistant",
            "content": _marker("/user_messages/000001.txt"),
        },
        {"role": "user", "content": "short plain message"},
    ]
    elided = elide_archived_user_messages(
        messages, keep_recent=0, head_chars=300, archive_nonce=NONCE
    )

    assert elided == 0
    assert messages[1]["content"].startswith("A" * 3000)
    assert messages[3]["content"] == "short plain message"


def main():
    run_suite(
        [
            test_archive_user_message_writes_read_only_file,
            test_archive_copies_lf_newlines_verbatim,
            test_archive_failure_returns_none_without_raising,
            test_run_turn_archives_large_message_and_annotates_it,
            test_run_turn_leaves_small_messages_alone,
            test_run_turn_survives_archive_failure_unannotated,
            test_upload_notices_are_not_archived,
            test_archive_nonces_differ_between_sessions,
            test_elide_archived_user_messages_keeps_head_and_pointer,
            test_elide_rejects_markers_with_foreign_nonces,
            test_elide_keeps_recent_archived_messages_untouched,
            test_elide_archived_user_messages_is_idempotent,
            test_elide_ignores_plain_user_and_other_messages,
        ]
    )


if __name__ == "__main__":
    main()
