"""Tests for the Allegro speed mode.

The mode is a prompt suffix plus a lighter runtime profile applied at
turn start: build_prompt appends the rule under its own section and
reports the halved command timeout, the session scales effort, pause,
and step budget (never slower than the user's own choices), and the
thread-local timeout scale halves the enforcement timeouts for exactly
one turn on one thread. The web server captures the client's flag at
enqueue time so a turn always runs with the mode the composer showed.
"""

import math
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna.config import set_timeout_scale
from syna.conversation.session import (
    ALLEGRO_PROMPT_SUFFIX,
    AgentSession,
    allegro_thinking_effort,
    build_prompt,
)
from syna.server import _inbox_item_from_message


def test_build_prompt_appends_suffix_only_when_asked():
    import re

    base = build_prompt()
    assert "Allegro" not in base, "the default system prompt must not mention the mode"
    allegro = build_prompt(allegro=True)
    number = re.search(r"Commands time out after (\d+) seconds", base).group(1)
    adjusted_base = base.replace(
        f"Commands time out after {number} seconds",
        f"Commands time out after {round(int(number) * 0.5)} seconds",
    )
    assert allegro == adjusted_base + ALLEGRO_PROMPT_SUFFIX, (
        "the base prompt must stay identical except the reported command"
        " timeout, which Allegro halves; the rule rides as a pure suffix"
    )
    assert allegro.endswith(
        "Speed doesn't override your other values or guidelines."
    ), "the suffix must close cleanly after the base prompt"


def test_suffix_keeps_tool_docs_out_of_the_prompt():
    prompt = build_prompt(allegro=True)
    assert "'Allegro' mode" in prompt, "the mode must name itself"
    assert "{tools}" not in prompt and "## Tools" not in prompt, (
        "the suffix must not resurrect the removed tool-doc injection"
    )


def test_suffix_gets_its_own_section():
    import re

    allegro = build_prompt(allegro=True)
    assert "\n\n## Allegro mode\n\n" in allegro, (
        "the mode must open its own h2 section, not dangle under the last base section"
    )
    headings = re.findall(r"^## (.+)$", allegro, re.MULTILINE)
    assert headings[-1] == "Allegro mode", (
        f"the mode section must close the prompt, got {headings[-1]!r}"
    )


def test_prompt_reports_the_halved_command_timeout(monkeypatch):
    import re

    import syna.conversation.session as session_module

    monkeypatch.setattr(session_module, "load_command_timeout_s", lambda: 51)
    base_number = re.search(
        r"Commands time out after (\d+) seconds", build_prompt()
    ).group(1)
    allegro_number = re.search(
        r"Commands time out after (\d+) seconds", build_prompt(allegro=True)
    ).group(1)
    assert base_number == "51", "the base prompt reports the configured timeout"
    assert allegro_number == "26", (
        "the Allegro prompt must report the halved timeout the"
        " enforcement layer actually uses, half-up like the settings note"
    )


def test_allegro_thinking_effort_guardrails(monkeypatch):
    import syna.conversation.session as session_module

    monkeypatch.setattr(session_module, "is_openrouter", lambda: True)
    assert allegro_thinking_effort("high") == "low", "high drops to low"
    assert allegro_thinking_effort("medium") == "low", "the default drops to low"
    assert allegro_thinking_effort("xhigh") == "low"
    assert allegro_thinking_effort("low") == "low"
    assert allegro_thinking_effort("minimal") == "minimal", (
        "a faster user setting must never be slowed down"
    )
    assert allegro_thinking_effort("none") == "none", (
        "a faster user setting must never be slowed down"
    )
    assert allegro_thinking_effort(None) is None

    monkeypatch.setattr(session_module, "is_openrouter", lambda: False)
    assert allegro_thinking_effort("high") == "high", (
        "custom endpoints keep the user's explicit choice; the reasoning"
        " object must not be injected where the user never opted into it"
    )
    assert allegro_thinking_effort(None) is None


def test_allegro_profile_scales_and_restores(monkeypatch):
    import syna.conversation.session as session_module

    monkeypatch.setattr(session_module, "is_openrouter", lambda: True)
    session = AgentSession(session_id="allegro-profile")
    base = (
        session.request_delay,
        session.max_steps_per_turn,
        session.step_warning_remaining,
        session.thinking_effort,
    )

    session.set_allegro(True)
    assert session.request_delay == base[0] / 2, "the per-request pause halves"
    assert session.max_steps_per_turn == base[1] // 2, "the step budget halves"
    assert session.step_warning_remaining == min(base[2], (base[1] // 2) // 2), (
        "the wrap-up warning follows the halved budget"
    )
    assert session.thinking_effort == allegro_thinking_effort(base[3])

    session.set_allegro(False)
    assert (
        session.request_delay,
        session.max_steps_per_turn,
        session.step_warning_remaining,
        session.thinking_effort,
    ) == base, "disabling must restore every profile value exactly"
    session.close()


def test_step_warning_clamps_for_small_budgets():
    session = AgentSession(session_id="allegro-clamp")
    session._base_max_steps_per_turn = 20
    session._base_step_warning_remaining = 20

    session.set_allegro(True)
    assert session.max_steps_per_turn == 10
    assert session.step_warning_remaining == 5, (
        "the wrap-up warning must not outlive most of the halved budget;"
        " unclamped it would fire before the first step"
    )

    session.set_allegro(False)
    assert session.step_warning_remaining == 20, "the user's value is restored"
    session.close()


def test_timeout_scale_halves_enforcement_and_does_not_leak():
    from syna.config import load_command_timeout_s, load_tool_call_timeout_s

    base_cmd = load_command_timeout_s()
    base_tool = load_tool_call_timeout_s()
    set_timeout_scale(0.5)
    try:
        assert load_command_timeout_s() == max(1, math.floor(base_cmd * 0.5 + 0.5)), (
            "the command timeout halves while the scale is active, half-up"
        )
        assert load_tool_call_timeout_s() == max(1, base_tool * 0.5), (
            "the tool-call detach timeout halves while the scale is active"
        )

        seen = {}
        reader = threading.Thread(
            target=lambda: seen.update(cmd=load_command_timeout_s())
        )
        reader.start()
        reader.join()
        assert seen["cmd"] == base_cmd, (
            "the scale is thread-local: another session's thread keeps"
            " its configured timeouts"
        )
    finally:
        set_timeout_scale(None)
    assert load_command_timeout_s() == base_cmd, "clearing restores the config"
    assert load_tool_call_timeout_s() == base_tool


def test_run_turn_applies_the_scale_for_the_turn(monkeypatch):
    import syna.conversation.session as session_module
    from syna.config import (
        ALLEGRO_TIMEOUT_SCALE,
        load_command_timeout_s,
        scale_seconds,
    )

    session = AgentSession(session_id="allegro-turn")
    base_cmd = load_command_timeout_s()
    observed = []

    def fake_ask(client, model, messages, **kwargs):
        observed.append(load_command_timeout_s())
        return {"content": "done", "tool_calls": None}

    monkeypatch.setattr(session_module, "ask_question", fake_ask)

    session.set_allegro(True)
    session.run_turn("hello")
    session.set_allegro(False)
    session.run_turn("hello again")

    assert observed == [scale_seconds(base_cmd, ALLEGRO_TIMEOUT_SCALE), base_cmd], (
        "the halved enforcement applies during the Allegro turn, and the"
        " configured timeout returns the moment the turn is over"
    )
    session.close()


def test_set_allegro_rewrites_system_message_only_on_change():
    session = AgentSession(session_id="allegro-test")
    base_content = session.messages[0]["content"]
    assert session.allegro is False, "sessions start in normal mode"

    session.set_allegro(True)
    enabled_content = session.messages[0]["content"]
    assert enabled_content != base_content, "enabling rewrites the system message"
    assert session.allegro is True

    system_message = session.messages[0]
    session.messages[0]["content"] = "tampered marker"
    session.set_allegro(True)
    assert session.messages[0] is system_message, (
        "an unchanged mode must be a no-op: no rewrite, no cache invalidation"
    )
    assert session.messages[0]["content"] == "tampered marker", (
        "an unchanged mode must be a no-op: no rewrite, no cache invalidation"
    )

    session.set_allegro(False)
    assert session.allegro is False
    assert session.messages[0]["content"] == base_content, (
        "disabling must restore the base prompt exactly"
    )
    session.close()


def test_inbox_item_captures_allegro_at_enqueue_time():
    item = _inbox_item_from_message(
        {"content": "  do it  ", "allegro": True, "id": "msg-1"}
    )
    assert item == {
        "kind": "message",
        "id": "msg-1",
        "content": "do it",
        "allegro": True,
    }, "the flag is captured with the message, not read later"

    plain = _inbox_item_from_message({"content": "plain"})
    assert plain == {
        "kind": "message",
        "id": plain["id"],
        "content": "plain",
        "allegro": False,
    }, "a missing flag means normal mode"

    assert _inbox_item_from_message({"content": "   "}) is None, (
        "blank content must not enqueue anything"
    )
    assert _inbox_item_from_message({}) is None


def main():
    from _runner import run_suite

    run_suite(
        [
            test_build_prompt_appends_suffix_only_when_asked,
            test_suffix_gets_its_own_section,
            test_suffix_keeps_tool_docs_out_of_the_prompt,
            test_prompt_reports_the_halved_command_timeout,
            test_allegro_thinking_effort_guardrails,
            test_allegro_profile_scales_and_restores,
            test_step_warning_clamps_for_small_budgets,
            test_timeout_scale_halves_enforcement_and_does_not_leak,
            test_run_turn_applies_the_scale_for_the_turn,
            test_set_allegro_rewrites_system_message_only_on_change,
            test_inbox_item_captures_allegro_at_enqueue_time,
        ]
    )


if __name__ == "__main__":
    main()
