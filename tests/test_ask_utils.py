"""Offline tests for ask_utils reply bookkeeping and the wire payload.

Covers the `_capped` marker (replies cut off by the output-token limit)
and the request-side message allowlist that keeps harness bookkeeping
off the wire.
"""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna import ask_utils


def _fake_client(finish_reason="stop", completion_tokens=10):
    captured = []

    class FakeChat:
        def send(self, **request):
            captured.append(request)
            message = SimpleNamespace(content="done", refusal=None, tool_calls=None)
            choice = SimpleNamespace(finish_reason=finish_reason, message=message)
            usage = SimpleNamespace(completion_tokens=completion_tokens)
            return SimpleNamespace(choices=[choice], usage=usage)

    return SimpleNamespace(chat=FakeChat()), captured


def _ask(monkeypatch, client):
    monkeypatch.setattr(ask_utils, "load_config", lambda: {"max_output_tokens": 100})
    with tempfile.TemporaryDirectory() as temp_dir:
        return ask_utils.ask_question(
            client,
            "test/model",
            [{"role": "user", "content": "hi"}],
            delay=0,
            dump_path=Path(temp_dir) / "dump.json",
        )


def test_capped_marker_on_length_finish_reason(monkeypatch):
    client, _captured = _fake_client(finish_reason="length", completion_tokens=10)
    reply = _ask(monkeypatch, client)
    assert reply.get("_capped") is True


def test_capped_marker_on_token_count_despite_tool_calls_reason(monkeypatch):
    client, _captured = _fake_client(finish_reason="tool_calls", completion_tokens=100)
    reply = _ask(monkeypatch, client)
    assert reply.get("_capped") is True, (
        "providers report 'tool_calls' for truncated batches, so the token"
        " count against max_tokens is the fallback signal"
    )


def test_capped_marker_on_max_tokens_finish_reason(monkeypatch):
    client, _captured = _fake_client(finish_reason="max_tokens", completion_tokens=10)
    reply = _ask(monkeypatch, client)
    assert reply.get("_capped") is True, (
        "Anthropic-native endpoints spell the truncation reason 'max_tokens'"
    )


def test_no_marker_under_the_cap(monkeypatch):
    client, _captured = _fake_client(finish_reason="stop", completion_tokens=99)
    reply = _ask(monkeypatch, client)
    assert "_capped" not in reply


def test_request_messages_strip_internal_keys():
    stripped = ask_utils._request_messages(
        [
            {"role": "assistant", "content": "x", "_capped": True},
            {
                "role": "user",
                "content": "hi",
                "image_tokens": 7,
                "image_path": "/workspace/a.png",
            },
        ]
    )
    assert all(set(message) <= {"role", "content"} for message in stripped)


def main():
    """Run every ask_utils test and report a summary."""
    run_suite(
        [
            test_capped_marker_on_length_finish_reason,
            test_capped_marker_on_token_count_despite_tool_calls_reason,
            test_capped_marker_on_max_tokens_finish_reason,
            test_no_marker_under_the_cap,
            test_request_messages_strip_internal_keys,
        ]
    )


if __name__ == "__main__":
    main()
