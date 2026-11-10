"""Tests for endpoint-family cache routing and the session cache summary.

Sticky routing keys are chosen per endpoint family (session_id on
OpenRouter, prompt_cache_key on OpenAI, nothing on strict custom
endpoints), and the session aggregates per-response usage into one
end-of-session cache-hit summary.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna.config import cache_control_fields, sticky_routing_fields


def test_sticky_routing_fields_by_endpoint_family(monkeypatch):
    import syna.config as config_module

    monkeypatch.setattr(config_module, "get_env", dict)
    assert sticky_routing_fields("sess-1") == {"session_id": "sess-1"}, (
        "OpenRouter (the default endpoint) pins routing with session_id"
    )
    monkeypatch.setattr(
        config_module,
        "get_env",
        lambda: {"BASE_URL": "https://api.openai.com/v1"},
    )
    assert sticky_routing_fields("sess-1") == {"prompt_cache_key": "sess-1"}, (
        "OpenAI pins routing with its own prompt_cache_key field"
    )
    monkeypatch.setattr(
        config_module,
        "get_env",
        lambda: {"BASE_URL": "http://localhost:11434/v1"},
    )
    assert sticky_routing_fields("sess-1") == {}, (
        "strict custom endpoints must not receive unknown top-level fields;"
        " they rely on automatic prefix caching"
    )
    monkeypatch.setattr(config_module, "get_env", dict)
    assert sticky_routing_fields(None) == {}, "no session id, no routing fields"


def test_cache_control_fields_target_anthropic_on_openrouter(monkeypatch):
    import syna.config as config_module

    monkeypatch.setattr(config_module, "get_env", dict)
    monkeypatch.setattr(config_module, "load_config", dict)
    assert cache_control_fields("~anthropic/claude-sonnet-5.5") == {
        "cache_control": {"type": "ephemeral"}
    }, "the default model id's ~ prefix is stripped before the provider check"
    assert cache_control_fields("anthropic/claude-opus-5.5") == {
        "cache_control": {"type": "ephemeral"}
    }, "plain anthropic model ids get caching too"

    assert cache_control_fields("~z-ai/glm-flash-latest") == {}, (
        "providers that cache automatically must not receive the field"
    )
    assert cache_control_fields(None) == {} and cache_control_fields("") == {}, (
        "no model, no cache field"
    )
    monkeypatch.setattr(
        config_module,
        "get_env",
        lambda: {"BASE_URL": "https://api.openai.com/v1"},
    )
    assert cache_control_fields("anthropic/claude-opus-5.5") == {}, (
        "a non-OpenRouter endpoint must not receive OpenRouter-only fields"
    )


def test_cache_control_fields_honor_ttl_config(monkeypatch):
    import syna.config as config_module

    monkeypatch.setattr(config_module, "get_env", dict)
    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {"anthropic_cache_ttl": "1h"},
    )
    assert cache_control_fields("anthropic/claude-opus-5.5") == {
        "cache_control": {"type": "ephemeral", "ttl": "1h"}
    }, "the one-hour TTL avoids re-writing the prefix during long tool calls"
    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {"anthropic_cache_ttl": "5m"},
    )
    assert cache_control_fields("anthropic/claude-opus-5.5") == {
        "cache_control": {"type": "ephemeral"}
    }, "the default TTL matches Anthropic's own default"


class FakeUsageDetails:
    def __init__(self, cached_tokens):
        self.cached_tokens = cached_tokens


class FakeUsage:
    def __init__(self, prompt_tokens, cached_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = 5
        self.prompt_tokens_details = FakeUsageDetails(cached_tokens)
        self.completion_tokens_details = None


class FakeMessage:
    def __init__(self):
        self.content = "done"
        self.tool_calls = None
        self.refusal = None


class FakeChoice:
    def __init__(self, message):
        self.message = message
        self.finish_reason = "stop"


class FakeResponse:
    def __init__(self):
        self.choices = [FakeChoice(FakeMessage())]
        self.usage = FakeUsage(1000, 800)


class FakeChat:
    def __init__(self):
        self.requests = []
        self.chat = self

    def send(self, **request):
        self.requests.append(request)
        return FakeResponse()


def test_ask_question_applies_routing_fields_and_reports_usage(monkeypatch):
    import syna.config as config_module
    from syna.ask_utils import ask_question

    usage_samples = []
    client = FakeChat()
    monkeypatch.setattr(config_module, "get_env", dict)
    monkeypatch.setattr(config_module, "load_config", dict)

    reply = ask_question(
        client,
        "test-model",
        [{"role": "user", "content": "hi"}],
        delay=0,
        session_id="sess-1",
        usage_callback=lambda prompt, cached: usage_samples.append((prompt, cached)),
    )

    assert reply["content"] == "done"
    assert client.requests[0].get("session_id") == "sess-1", (
        "the OpenRouter default sends the session_id routing key"
    )
    assert "cache_control" not in client.requests[0], (
        "non-anthropic models get no cache opt-in"
    )
    assert usage_samples == [(1000, 800)], "usage flows to the callback"

    monkeypatch.setattr(
        config_module,
        "get_env",
        lambda: {"BASE_URL": "http://localhost:11434/v1"},
    )
    client = FakeChat()
    ask_question(
        client,
        "test-model",
        [{"role": "user", "content": "hi"}],
        delay=0,
        session_id="sess-1",
    )
    request = client.requests[0]
    assert "session_id" not in request and "prompt_cache_key" not in request, (
        "strict custom endpoints get a byte-clean request body"
    )

    monkeypatch.setattr(config_module, "get_env", dict)
    client = FakeChat()
    ask_question(
        client,
        "~anthropic/claude-sonnet-5.5",
        [{"role": "user", "content": "hi"}],
        delay=0,
        session_id="sess-1",
    )
    assert client.requests[0].get("cache_control") == {"type": "ephemeral"}, (
        "Claude conversations must opt in to prompt caching"
    )


class FakeLogger:
    def __init__(self):
        self.lines = []

    def info(self, message, *args):
        self.lines.append(message % args if args else message)

    def warning(self, message, *args):
        pass


def test_session_logs_cache_summary_on_close(monkeypatch):
    import syna.conversation.session as session_module
    from syna.conversation.session import AgentSession

    session = AgentSession(session_id="cache-summary-test")
    session._record_usage(1000, 800)
    session._record_usage(1000, 0)
    assert session._usage_totals == {
        "requests": 2,
        "prompt_tokens": 2000,
        "cached_tokens": 800,
    }, "usage accumulates across requests"

    fake_logger = FakeLogger()
    monkeypatch.setattr(session_module, "get_logger", lambda: fake_logger)
    session.close()
    summary_lines = [
        line for line in fake_logger.lines if "Session cache summary" in line
    ]
    assert len(summary_lines) == 1, "exactly one summary line on close"
    assert "hit_ratio=40%" in summary_lines[0], (
        "the ratio is cached tokens over prompt tokens"
    )

    quiet = AgentSession(session_id="cache-summary-quiet")
    fake_logger = FakeLogger()
    monkeypatch.setattr(session_module, "get_logger", lambda: fake_logger)
    quiet.close()
    assert not fake_logger.lines, "no requests, no summary line"


def main():
    from _runner import run_suite

    run_suite(
        [
            test_sticky_routing_fields_by_endpoint_family,
            test_cache_control_fields_target_anthropic_on_openrouter,
            test_cache_control_fields_honor_ttl_config,
            test_ask_question_applies_routing_fields_and_reports_usage,
            test_session_logs_cache_summary_on_close,
        ]
    )


if __name__ == "__main__":
    main()
