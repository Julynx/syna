"""Tests for the model-aware context budget.

The budget a session starts with comes from the config (or the 250000
default). On OpenRouter a background fetch of the models list overrides
it with half the model's real context window once it lands, regardless
of the config value; pending, failed, and non-OpenRouter cases keep the
start value. All network access is faked: these tests must stay
offline.
"""

import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys_path = str(Path(__file__).resolve().parent.parent / "src")
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

import syna.conversation.session as session_module
from syna.conversation.session import (
    DEFAULT_CONTEXT_BUDGET_TOKENS,
    AgentSession,
    resolve_context_budget,
    resolved_budget_for,
)

RETRY_CONFIG = {
    "timeout_ms": 120000,
    "retry": {
        "strategy": "backoff",
        "initial_interval_ms": 1000,
        "max_interval_ms": 10000,
        "exponent": 1.5,
        "max_elapsed_time_ms": 30000,
        "retry_connection_errors": False,
    },
}


def _reset_fetch_state(monkeypatch, lengths=None):
    """Fresh fetch state for one test: nothing started, cache as given."""
    monkeypatch.setattr(session_module, "_context_lengths_cache", lengths)
    monkeypatch.setattr(session_module, "_context_fetch_started", False)


def test_config_value_is_the_start_budget(monkeypatch):
    _reset_fetch_state(monkeypatch)
    monkeypatch.setattr(
        session_module, "load_config", lambda: {"context_budget_tokens": 50_000}
    )
    monkeypatch.setattr(session_module, "ensure_context_fetch", lambda: None)
    assert resolve_context_budget("z-ai/glm-flash-latest") == 50_000, (
        "the config value is the budget a session starts with"
    )


def test_unset_config_defaults_to_250k(monkeypatch):
    _reset_fetch_state(monkeypatch)
    monkeypatch.setattr(session_module, "load_config", dict)
    monkeypatch.setattr(session_module, "ensure_context_fetch", lambda: None)
    assert resolve_context_budget(None) == DEFAULT_CONTEXT_BUDGET_TOKENS
    assert resolve_context_budget("z-ai/glm-flash-latest") == 250_000


def test_bogus_config_values_fall_back_to_default(monkeypatch):
    _reset_fetch_state(monkeypatch)
    monkeypatch.setattr(
        session_module, "load_config", lambda: {"context_budget_tokens": 0}
    )
    monkeypatch.setattr(session_module, "ensure_context_fetch", lambda: None)
    assert resolve_context_budget("z-ai/glm-flash-latest") == 250_000, (
        "a non-positive config value is no budget at all"
    )


def test_resolved_window_wins_once_the_fetch_lands(monkeypatch):
    _reset_fetch_state(
        monkeypatch,
        {"z-ai/glm-flash-latest": 1048576, "~z-ai/glm-flash-latest": 1048576},
    )
    assert resolved_budget_for("~z-ai/glm-flash-latest") == 524_288, (
        "tilde-prefixed ids are real OpenRouter ids and match verbatim"
    )
    assert resolved_budget_for("z-ai/glm-flash-latest") == 524_288, (
        "the stripped form still matches ids listed without the tilde"
    )


def test_pending_or_failed_fetch_keeps_the_start_value(monkeypatch):
    _reset_fetch_state(monkeypatch, None)
    assert resolved_budget_for("z-ai/glm-flash-latest") is None, (
        "a pending fetch must not resolve anything"
    )
    _reset_fetch_state(monkeypatch, {})
    assert resolved_budget_for("made-up/model") is None, (
        "a model missing from the list must not resolve anything"
    )

    monkeypatch.setattr(session_module, "load_config", lambda: dict(RETRY_CONFIG))
    monkeypatch.setattr(
        session_module,
        "get_env",
        lambda: {"OPENROUTER_API_KEY": "sk-or-test", "MODEL": "~z-ai/glm-flash-latest"},
    )
    session = AgentSession(session_id="budget-pending", context_budget_tokens=99_000)
    assert session.model == "~z-ai/glm-flash-latest", (
        "the test pins the model so the real .env cannot leak in"
    )
    assert session._current_budget() == 99_000, (
        "while nothing has resolved, the start value applies"
    )
    monkeypatch.setattr(
        session_module,
        "_context_lengths_cache",
        {"z-ai/glm-flash-latest": 1048576},
    )
    assert session._current_budget() == 524_288, (
        "once the fetch lands, half the real window wins regardless of the start value"
    )


def test_non_openrouter_endpoint_never_fetches(monkeypatch):
    _reset_fetch_state(monkeypatch)
    monkeypatch.setattr(session_module, "load_config", dict)
    monkeypatch.setattr(
        session_module, "load_base_url", lambda: "http://localhost:11434/v1"
    )
    resolve_context_budget("llama3")
    assert not session_module._context_fetch_started, (
        "endpoints that cannot use the models list must not fetch it"
    )
    assert resolve_context_budget("llama3") == DEFAULT_CONTEXT_BUDGET_TOKENS


class _FakeModelsItem:
    def __init__(self, model_id, context_length):
        self.id = model_id
        self.context_length = context_length


_FAKE_ITEMS = [
    _FakeModelsItem("good/model", 100_000),
    _FakeModelsItem("missing/model", None),
    _FakeModelsItem("string/model", "100000"),
    _FakeModelsItem("zero/model", 0),
    _FakeModelsItem("bool/model", True),
    _FakeModelsItem("~z-ai/glm-flash-latest", 1048576),
]


class _FakeModelsApi:
    def list(self):
        return SimpleNamespace(result=SimpleNamespace(data=_FAKE_ITEMS))


class _FakeOpenRouter:
    """Stands in for the SDK client; raises when told to."""

    fail = False

    def __init__(self, api_key=None, server_url=None, timeout_ms=None):
        if _FakeOpenRouter.fail:
            raise ConnectionError("no network")
        self.models = _FakeModelsApi()


def test_load_context_lengths_filters_bogus_entries(monkeypatch):
    monkeypatch.setattr(session_module, "OpenRouter", _FakeOpenRouter)
    monkeypatch.setattr(
        session_module, "get_env", lambda: {"OPENROUTER_API_KEY": "sk-or-test"}
    )
    lengths = session_module._load_context_lengths()
    assert lengths == {
        "good/model": 100_000,
        "~z-ai/glm-flash-latest": 1048576,
    }, "missing, non-integer, non-positive, and boolean lengths are dropped"


def test_load_context_lengths_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(session_module, "OpenRouter", _FakeOpenRouter)
    monkeypatch.setattr(
        session_module, "get_env", lambda: {"OPENROUTER_API_KEY": "sk-or-test"}
    )
    _FakeOpenRouter.fail = True
    try:
        assert session_module._load_context_lengths() == {}, (
            "a failed fetch caches an empty dict, not an exception"
        )
    finally:
        _FakeOpenRouter.fail = False


def test_load_context_lengths_without_key_is_empty(monkeypatch):
    monkeypatch.setattr(session_module, "get_env", dict)
    assert session_module._load_context_lengths() == {}, (
        "no key, no SDK client, no fetch"
    )


def test_ensure_context_fetch_runs_once(monkeypatch):
    _reset_fetch_state(monkeypatch)
    monkeypatch.setattr(
        session_module, "load_base_url", lambda: "https://openrouter.ai/api/v1"
    )
    calls = []
    started = threading.Event()

    def counting_load():
        calls.append(1)
        started.set()
        time.sleep(0.05)
        return {"z-ai/glm-flash-latest": 1048576}

    monkeypatch.setattr(session_module, "_load_context_lengths", counting_load)
    session_module.ensure_context_fetch()
    session_module.ensure_context_fetch()
    assert started.wait(timeout=3), "the background fetch must run"
    time.sleep(0.15)
    assert len(calls) == 1, "the list is fetched once per process"
    assert session_module.peek_context_lengths() == {
        "z-ai/glm-flash-latest": 1048576
    }, "the fetch result lands in the module cache"


def test_server_session_record_constructs_with_a_budget(monkeypatch):
    """Regression: SessionRecord.__init__ resolves a budget and stays runnable.

    The first wiring of this used get_env in server.py without importing
    it - a NameError on every web session's first connect, invisible to
    tests because they all built records via __new__ (skipping __init__).
    This test constructs the real object. Only the container start is
    neutralized; everything else runs for real, so an unimported name or
    a broken resolve call fails here.
    """
    import syna.server as server_module

    monkeypatch.setattr(session_module, "load_config", lambda: dict(RETRY_CONFIG))
    monkeypatch.setattr(
        server_module,
        "resolve_context_budget",
        lambda model: 4321,
    )
    monkeypatch.setattr(
        server_module.SessionRecord, "_start_container", lambda self: None
    )
    record = server_module.SessionRecord("budget-wiring-test")
    try:
        assert record.session.context_budget_tokens == 4321, (
            "the server passes the resolved budget into the session"
        )
    finally:
        record.teardown()
        record.worker.join(timeout=3)
    assert not record.worker.is_alive(), "the worker must stop on teardown"


def main():
    from _runner import run_suite

    run_suite(
        [
            test_config_value_is_the_start_budget,
            test_unset_config_defaults_to_250k,
            test_bogus_config_values_fall_back_to_default,
            test_resolved_window_wins_once_the_fetch_lands,
            test_pending_or_failed_fetch_keeps_the_start_value,
            test_non_openrouter_endpoint_never_fetches,
            test_load_context_lengths_filters_bogus_entries,
            test_load_context_lengths_failure_returns_empty,
            test_load_context_lengths_without_key_is_empty,
            test_ensure_context_fetch_runs_once,
            test_server_session_record_constructs_with_a_budget,
        ]
    )


if __name__ == "__main__":
    main()
