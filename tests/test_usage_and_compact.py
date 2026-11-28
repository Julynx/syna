"""Tests for session usage accounting and the manual /compact command.

The usage event carries the last main-loop request's prompt tokens
(the conversation's context size) and the session's cumulative cost;
summarizer requests accumulate cost silently so the chip never flashes
a non-conversational number. `compact_now` is the manual trigger: it
ignores the enable_compaction switch and the automatic gates, elides
first, and announces when there is nothing to compact.
"""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna.capabilities.tools import TOOL_REGISTRY
from syna.protocol.channel import format_tool_result_content, is_channel_message
from syna.state import register_container, unregister_container


class FakePod:
    """Records nothing; stands in for a bollard Container."""

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
    def __init__(self, message, usage=None):
        self.choices = [FakeChoice(message)]
        self.usage = usage


class FakeChat:
    """Queued replies; optionally reports usage with every response."""

    def __init__(self, replies, prompt_tokens=None, cached_tokens=0, cost=None):
        self.replies = list(replies)
        self.requests = []
        self.chat = self
        self.prompt_tokens = prompt_tokens
        self.cached_tokens = cached_tokens
        self.cost = cost

    def send(self, **request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        message = (
            reply
            if isinstance(reply, FakeMessage)
            else FakeMessage(content=reply, tool_calls=None)
        )
        usage = None
        if self.prompt_tokens is not None:
            usage = SimpleNamespace(
                prompt_tokens=self.prompt_tokens,
                prompt_tokens_details=SimpleNamespace(cached_tokens=self.cached_tokens),
                cost=self.cost,
            )
        return FakeResponse(message, usage)


def _make_tool_call_reply(name, args, call_id="call_1"):
    return FakeMessage(
        content=None,
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": _json_args(args)},
            }
        ],
    )


def _json_args(args):
    import json

    return json.dumps(args)


def _make_session(events, **overrides):
    from syna.conversation.session import AgentSession

    session = AgentSession(
        session_id="usage-test",
        on_event=events.append,
    )
    session.dump_path = Path(tempfile.mkdtemp(prefix="syna-usage-")) / "dump.json"
    session.request_delay = 0
    session.context_budget_tokens = 10**9
    session.max_history_messages = 10**6
    session.elide_keep_recent_messages = 0
    session.elide_interval_messages = 10**6
    session.elide_every_requests = 10**6
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


def test_main_loop_emits_usage_events():
    """One usage event per main request, with the cumulative session cost."""
    TOOL_REGISTRY["smoke_tool"] = lambda label="unlabeled": f"ran: {label}"
    register_container(FakePod())
    try:
        events = []
        session = _make_session(events)
        session.client = FakeChat(
            [
                _make_tool_call_reply("smoke_tool", {"label": "x"}),
                FakeMessage(content="done", tool_calls=None),
            ],
            prompt_tokens=1200,
            cached_tokens=300,
            cost=0.01,
        )
        session.run_turn("probe")
    finally:
        unregister_container()
        TOOL_REGISTRY.pop("smoke_tool", None)

    usage_events = [event for event in events if event.get("type") == "usage"]
    assert len(usage_events) == 2, usage_events
    assert all(event["context_tokens"] == 1200 for event in usage_events)
    assert usage_events[0]["cost"] == 0.01
    assert usage_events[1]["cost"] == 0.02, "the cost must accumulate"
    totals = session._usage_totals
    assert totals["requests"] == 2
    assert totals["cost"] == 0.02


def test_summarizer_requests_accumulate_without_emitting():
    """The compaction request costs money but never reports a context size."""
    TOOL_REGISTRY["smoke_tool"] = lambda label="unlabeled": f"ran: {label}"
    register_container(FakePod())
    try:
        events = []
        session = _make_session(events)
        session.client = FakeChat(
            [
                _make_tool_call_reply(
                    "smoke_tool", {"label": "y" * 2000}, call_id="call_1"
                ),
                FakeMessage(content="done", tool_calls=None),
            ],
            prompt_tokens=1200,
            cached_tokens=0,
            cost=0.01,
        )
        session.run_turn("probe")
        events.clear()
        session.client = FakeChat(
            [FakeMessage(content="the summary", tool_calls=None)],
            prompt_tokens=500,
            cached_tokens=0,
            cost=0.25,
        )
        session.compact_now()
    finally:
        unregister_container()
        TOOL_REGISTRY.pop("smoke_tool", None)

    assert not [event for event in events if event.get("type") == "usage"], (
        "the summarizer's prompt size must not reach the usage chip"
    )
    assert session._usage_totals["cost"] == 0.27, "its cost still accumulates"
    assert any(event.get("type") == "compaction" for event in events)
    assert any(
        is_channel_message(message.get("content") or "", "COMPACTION")
        for message in session.messages
    ), "the summary must be spliced into history on the compaction channel"


def test_main_loop_pairs_the_estimate_with_real_usage():
    """The correction is derived from the request the estimate was taken on.

    `_pending_request_estimate` is captured right before the request and
    consumed by the response's usage callback, so the observed ratio is
    the estimator's error for exactly the billed message list.
    """
    TOOL_REGISTRY["smoke_tool"] = lambda label="unlabeled": f"ran: {label}"
    register_container(FakePod())
    try:
        events = []
        session = _make_session(events)
        session.client = FakeChat(
            [
                _make_tool_call_reply("smoke_tool", {"label": "x"}),
                FakeMessage(content="done", tool_calls=None),
            ],
            prompt_tokens=900,
            cached_tokens=0,
            cost=0.001,
        )
        session.run_turn("probe")
    finally:
        unregister_container()
        TOOL_REGISTRY.pop("smoke_tool", None)

    assert session._pending_request_estimate is None, "the pairing is consumed"
    assert session._estimate_correction < 1.0, (
        "a 900-token bill against a multi-thousand-token estimate must"
        " pull the correction down"
    )


def test_compaction_resets_the_estimate_correction():
    """The spliced summary is new composition; the old ratio no longer applies."""
    TOOL_REGISTRY["smoke_tool"] = lambda label="unlabeled": f"ran: {label}"
    register_container(FakePod())
    try:
        events = []
        session = _make_session(events)
        session._estimate_correction = 1.7
        session.client = FakeChat(
            [
                _make_tool_call_reply(
                    "smoke_tool", {"label": "y" * 2000}, call_id="call_1"
                ),
                FakeMessage(content="done", tool_calls=None),
            ]
        )
        session.run_turn("probe")
        session.client = FakeChat([FakeMessage(content="the summary", tool_calls=None)])
        session.compact_now()
    finally:
        unregister_container()
        TOOL_REGISTRY.pop("smoke_tool", None)

    assert session._estimate_correction == 1.0, (
        "the correction must reset when compaction rewrites history"
    )


def test_manual_compact_elides_with_the_corrected_budget():
    """compact_now uses the corrected token total, like the automatic path.

    The raw estimate sits under the budget while the correction puts it
    over, so the elision inside the manual command must see the budget
    as exceeded and run unconditionally.
    """
    TOOL_REGISTRY["smoke_tool"] = lambda label="unlabeled": f"ran: {label}"
    register_container(FakePod())
    try:
        events = []
        session = _make_session(events)
        session.context_budget_tokens = 5000
        session._estimate_correction = 1.5
        session.messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "x" * 16000},
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": format_tool_result_content(
                    status="ok",
                    output="o" * 2000,
                    saved_to="/tool_outputs/0001_smoke.txt",
                ),
            },
        ]
        session.client = FakeChat([FakeMessage(content="the summary", tool_calls=None)])
        session.compact_now()
    finally:
        unregister_container()
        TOOL_REGISTRY.pop("smoke_tool", None)

    tool_message = next(
        message
        for message in session.messages
        if is_channel_message(message.get("content") or "", "TOOL_RESULTS")
    )
    assert "output elided" in tool_message["content"], (
        "the corrected total is over budget, so the budget path must elide"
    )


def test_compact_now_without_compactable_announces_nothing():
    register_container(FakePod())
    try:
        events = []
        session = _make_session(events)
        session.client = FakeChat(
            [_make_tool_call_reply] * 0 + [],  # never consumed
            prompt_tokens=10,
            cached_tokens=0,
            cost=0.0,
        )
        session.compact_now()
    finally:
        unregister_container()

    assert not session.client.requests, "nothing compactable must not ask the model"
    info = [event for event in events if event.get("type") == "info"]
    assert info and info[0]["text"] == "Nothing to compact yet.", info


def test_compact_now_ignores_the_master_switch():
    """The manual command works with enable_compaction turned off."""
    TOOL_REGISTRY["smoke_tool"] = lambda label="unlabeled": f"ran: {label}"
    register_container(FakePod())
    try:
        events = []
        session = _make_session(events, compaction_enabled=False)
        session.client = FakeChat(
            [
                _make_tool_call_reply(
                    "smoke_tool", {"label": "y" * 2000}, call_id="call_1"
                ),
                FakeMessage(content="done", tool_calls=None),
            ]
        )
        session.run_turn("probe")
        session.client = FakeChat([FakeMessage(content="the summary", tool_calls=None)])
        session.compact_now()
    finally:
        unregister_container()
        TOOL_REGISTRY.pop("smoke_tool", None)

    assert any(event.get("type") == "compaction" for event in events), (
        "a manual compaction must run even with enable_compaction false"
    )


def test_usage_events_carry_the_models_context_window(monkeypatch):
    """The chip's denominator is the model's real window from the fetch."""
    import syna.conversation.session as session_module

    TOOL_REGISTRY["smoke_tool"] = lambda label="unlabeled": f"ran: {label}"
    register_container(FakePod())
    try:
        events = []
        session = _make_session(events)
        session.model = "test/model"
        session.client = FakeChat(
            [FakeMessage(content="done", tool_calls=None)],
            prompt_tokens=7000,
            cached_tokens=0,
            cost=0.0003,
        )
        monkeypatch.setattr(
            session_module,
            "peek_context_lengths",
            lambda: {"test/model": 1000000},
        )
        session.run_turn("probe")
    finally:
        unregister_container()
        TOOL_REGISTRY.pop("smoke_tool", None)

    usage_events = [event for event in events if event.get("type") == "usage"]
    assert len(usage_events) == 1, usage_events
    assert usage_events[0]["context_tokens"] == 7000
    assert usage_events[0]["context_window"] == 1000000
    assert usage_events[0]["cost"] == 0.0003


def test_usage_events_omit_an_unknown_window():
    """Without the models fetch there is no denominator to show."""
    TOOL_REGISTRY["smoke_tool"] = lambda label="unlabeled": f"ran: {label}"
    register_container(FakePod())
    try:
        events = []
        session = _make_session(events)
        session.client = FakeChat(
            [FakeMessage(content="done", tool_calls=None)],
            prompt_tokens=7000,
            cached_tokens=0,
            cost=0.0003,
        )
        session.run_turn("probe")
    finally:
        unregister_container()
        TOOL_REGISTRY.pop("smoke_tool", None)

    usage_events = [event for event in events if event.get("type") == "usage"]
    assert usage_events[0]["context_window"] is None


def main():
    run_suite(
        [
            test_main_loop_emits_usage_events,
            test_main_loop_pairs_the_estimate_with_real_usage,
            test_summarizer_requests_accumulate_without_emitting,
            test_compaction_resets_the_estimate_correction,
            test_manual_compact_elides_with_the_corrected_budget,
            test_compact_now_without_compactable_announces_nothing,
            test_compact_now_ignores_the_master_switch,
            test_usage_events_carry_the_models_context_window,
            test_usage_events_omit_an_unknown_window,
        ]
    )


if __name__ == "__main__":
    main()
