"""Tests for the Allegro speed mode.

The mode is a prompt suffix applied at turn start: build_prompt appends
it only when asked, the session rewrites its system message only on an
actual change (a no-op keeps the provider prompt cache untouched), and
the web server captures the client's flag at enqueue time so a turn
always runs with the mode the composer showed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna.conversation.session import (
    ALLEGRO_PROMPT_SUFFIX,
    AgentSession,
    build_prompt,
)
from syna.server import _inbox_item_from_message


def test_build_prompt_appends_suffix_only_when_asked():
    base = build_prompt()
    assert "Allegro" not in base, "the default system prompt must not mention the mode"
    allegro = build_prompt(allegro=True)
    assert allegro == base + ALLEGRO_PROMPT_SUFFIX, (
        "the base prompt must stay byte-identical; the mode is a pure suffix"
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


def test_set_allegro_rewrites_system_message_only_on_change():
    session = AgentSession(session_id="allegro-test")
    base_content = session.messages[0]["content"]
    assert session.allegro is False, "sessions start in normal mode"

    session.set_allegro(True)
    assert session.allegro is True
    assert session.messages[0]["content"] == base_content + ALLEGRO_PROMPT_SUFFIX

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
    item = _inbox_item_from_message({"content": "  do it  ", "allegro": True})
    assert item == {
        "kind": "message",
        "content": "do it",
        "allegro": True,
    }, "the flag is captured with the message, not read later"

    assert _inbox_item_from_message({"content": "plain"}) == {
        "kind": "message",
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
            test_suffix_keeps_tool_docs_out_of_the_prompt,
            test_set_allegro_rewrites_system_message_only_on_change,
            test_inbox_item_captures_allegro_at_enqueue_time,
        ]
    )


if __name__ == "__main__":
    main()
