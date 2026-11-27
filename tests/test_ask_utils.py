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


def _usage_client(usage):
    """A fake client returning one response with the given usage object."""

    class FakeChat:
        def send(self, **request):
            message = SimpleNamespace(content="done", refusal=None, tool_calls=None)
            choice = SimpleNamespace(finish_reason="stop", message=message)
            return SimpleNamespace(choices=[choice], usage=usage)

    return SimpleNamespace(chat=FakeChat())


def _ask_with_usage(monkeypatch, client, usage_callback):
    monkeypatch.setattr(ask_utils, "load_config", lambda: {"max_output_tokens": 100})
    ask_utils.ask_question(
        client,
        "test/model",
        [{"role": "user", "content": "hi"}],
        delay=0,
        usage_callback=usage_callback,
    )


def test_usage_callback_receives_tokens_and_cost(monkeypatch):
    details = SimpleNamespace(cached_tokens=40)
    usage = SimpleNamespace(
        prompt_tokens=100, prompt_tokens_details=details, cost=0.0035
    )
    received = []
    _ask_with_usage(
        monkeypatch, _usage_client(usage), lambda *args: received.append(args)
    )
    assert received == [(100, 40, 0.0035)], received


def test_usage_callback_reports_none_for_unset_cost(monkeypatch):
    """An UNSET sentinel (not None) still reaches the callback as None."""
    sentinel = object()
    details = SimpleNamespace(cached_tokens=40)
    usage = SimpleNamespace(
        prompt_tokens=100, prompt_tokens_details=details, cost=sentinel
    )
    received = []
    _ask_with_usage(
        monkeypatch, _usage_client(usage), lambda *args: received.append(args)
    )
    assert received == [(100, 40, None)], received


def test_usage_callback_reports_none_without_cost_field(monkeypatch):
    usage = SimpleNamespace(
        prompt_tokens=100,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
    )
    received = []
    _ask_with_usage(
        monkeypatch, _usage_client(usage), lambda *args: received.append(args)
    )
    assert received == [(100, 0, None)], received


def main():
    """Run every ask_utils test and report a summary."""
    run_suite(
        [
            test_capped_marker_on_length_finish_reason,
            test_capped_marker_on_token_count_despite_tool_calls_reason,
            test_capped_marker_on_max_tokens_finish_reason,
            test_no_marker_under_the_cap,
            test_request_messages_strip_internal_keys,
            test_usage_callback_receives_tokens_and_cost,
            test_usage_callback_reports_none_for_unset_cost,
            test_usage_callback_reports_none_without_cost_field,
        ]
    )


if __name__ == "__main__":
    main()
