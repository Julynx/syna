"""Smoke tests for tool-block parsing and the session feedback loop.

Run with: uv run python tests/test_tool_feedback.py

Uses the exact reply shapes that caused the tool-outage incident, plus a
scripted fake model client to verify the session-level feedback without
network, Docker or a real model. The expected behavior follows the v4
grammar: a ```tool opener is recognized at ANY position (glued or inline),
backticks inside JSON strings never close, prose-fenced regions are inert,
and every malformed call gets a descriptive error entry instead of failing
silently. The one structural error — valid JSON that is never closed —
executes nothing.
"""

import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna.parse import _signature_error, parse_and_execute_tools
from syna.session import AgentSession
from syna.tools import TOOL_REGISTRY

REPLY_CLOSED = '```tool\n{"smoke_tool": {}}\n```'
REPLY_UNCLOSED = '```tool\n{"smoke_tool": {"label": "from the incident dump"}}'
REPLY_TWO_PROSE_SEPARATED = (
    "Here is call one.\n"
    '```tool\n{"smoke_tool": {"label": "first"}}\n```\n'
    "Middle narration.\n"
    '```tool\n{"smoke_tool": {"label": "second"}}\n```\n'
    "Done."
)
REPLY_GLUED_OPENERS = (
    '```tool\n{"smoke_tool": {"label": "first"}}\n'
    '```tool\n{"smoke_tool": {"label": "second"}}\n'
    "```"
)
REPLY_INLINE_ONE_LINE = (
    'Running the search now.```tool {"smoke_tool": {"label": "inline"}}```'
)
REPLY_ONE_LINE_TWO_CALLS = (
    '```tool {"smoke_tool": {"label": "a"}}```'
    ' and ```tool {"smoke_tool": {"label": "b"}}```'
)
REPLY_RUN_ONLY_CLOSER_TWO_CALLS = (
    '```tool {"smoke_tool": {"label": "a"}}```'
    ' trailing text ```tool {"smoke_tool": {"label": "b"}}```'
)
REPLY_MENTION_PLUS_REAL = (
    'I will use ```tool blocks later. Real call:\n```tool\n{"smoke_tool": {}}\n```'
)
REPLY_NESTED_OPENERS = '```tool\n```tool\n{"smoke_tool": {}}\n```'
REPLY_BROKEN_THEN_REAL = (
    '```tool\n{"smoke_tool": "broken"\n```\n```tool\n{"smoke_tool": {}}\n```'
)
REPLY_HEREDOC_PHANTOM = (
    '```tool\n{"smoke_tool": {"label": "```tool\\nnot a real block\\n```"}}\n```'
)
REPLY_LANG_CLOSER = (
    '```tool\n{"smoke_tool": {"label": "x"}}\n```python\nprint(1)\n```\n'
)
REPLY_TRAILING_PROSE = '```tool\n{"smoke_tool": {"label": "x"}}\nsome remark\n```\n'
REPLY_INLINE_CODE_PROSE = (
    '```tool\n{"smoke_tool": {"label": "x"}}\nuse `label` now\n```\n'
)
REPLY_BROKEN_JSON_CLOSED = '```tool\n{"smoke_tool": {"label": "broken"\n```\n'
REPLY_BROKEN_JSON_UNCLOSED = '```tool\n{"smoke_tool": {"label": "broken"'
REPLY_EMPTY_BLOCK = "```tool\n```\n"
REPLY_WHITESPACE_BLOCK = "```tool\n   \n```\n"
REPLY_EOF_OPENER = "Step 1:\n```tool\n"
REPLY_BARE_UNCLOSED = "Trying the tool now.```tool"
REPLY_QUOTED_EXAMPLE = (
    "Example of the format:\n"
    "```text\n"
    '```tool\n{"tool_name": {"argument": "value"}}\n'
    "```\n"
    "```\n"
)
REPLY_STRAY_MARKUP = (
    "Here is the format you asked about:\n\n"
    "```tool\n"
    '{"tool_name": {"argument": "value"}}\n'
    "```\n"
)
REPLY_NO_CALLS = "Just chatting about the weather, no tool calls here."
REPLY_RESPOND = '```tool\n{"respond": {"text": "task done"}}\n```'
REPLY_STRING_ARGS = '```tool\n{"smoke_tool": "ls -la"}\n```'
REPLY_NULL_ARGS = '```tool\n{"smoke_tool": null}\n```'
REPLY_LIST_ARGS = '```tool\n{"smoke_tool": ["ls"]}\n```'
REPLY_TOP_LEVEL_LIST = '```tool\n["smoke_tool"]\n```'
REPLY_TOP_LEVEL_STRING = '```tool\n"smoke_tool"\n```'
REPLY_TOP_LEVEL_NUMBER = "```tool\n42\n```"
REPLY_EMPTY_OBJECT = "```tool\n{}\n```"
REPLY_MULTI_KEY = '```tool\n{"smoke_tool": {}, "respond": {"text": "hi"}}\n```'
REPLY_FAILING_TOOL = '```tool\n{"smoke_fail": {}}\n```'
REPLY_CRLF = '```tool\r\n{"smoke_tool": {}}\r\n```\r\n'
REPLY_CRLF_UNCLOSED = '```tool\r\n{"smoke_tool": {}}\r\n'
REPLY_MISSING_ARG = '```tool\n{"web_search": {}}\n```'
REPLY_UNEXPECTED_ARG = '```tool\n{"web_search": {"query": "x", "foo": 1}}\n```'
REPLY_TYPE_MISMATCH = '```tool\n{"web_search": {"query": 123}}\n```'
REPLY_RESPOND_BAD_TYPE = '```tool\n{"respond": {"text": 42}}\n```'


def _smoke_tool(label: str = "unlabeled") -> str:
    """Minimal stand-in tool: echoes its argument back as the output."""
    return f"ran: {label}"


def _smoke_fail() -> str:
    """Stand-in tool that always raises, like a real tool crashing."""
    raise RuntimeError("boom")


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


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


def _parse(reply):
    events = []
    outcome = parse_and_execute_tools(
        reply, on_tool_event=lambda *a, **k: events.append((a, k)), echo_errors=False
    )
    return outcome, events


def _capture_dev_log():
    handler = _LogCapture()
    logger = logging.getLogger("syna")
    logger.addHandler(handler)
    return handler


def test_prose_separated_blocks_both_execute():
    outcome, _ = _parse(REPLY_TWO_PROSE_SEPARATED)
    assert outcome.structural_error is None
    assert [o["output"] for o in outcome.outputs] == ["ran: first", "ran: second"]
    print("PASS prose-separated well-formed blocks both execute")


def test_incident_unclosed_block_is_structural():
    outcome, events = _parse(REPLY_UNCLOSED)
    assert outcome.outputs == []
    assert "never closed" in outcome.structural_error
    assert "line 1" in outcome.structural_error
    assert "smoke_tool(label='from the incident dump')" in outcome.structural_error
    assert not any(a[0] == "started" for a, _ in events)
    print("PASS unclosed block (the incident) is a structural error, nothing runs")


def test_glued_openers_drop_second_call():
    capture = _capture_dev_log()
    try:
        outcome, _ = _parse(REPLY_GLUED_OPENERS)
    finally:
        logging.getLogger("syna").removeHandler(capture)
    assert outcome.structural_error is None
    assert [o["output"] for o in outcome.outputs] == ["ran: first"]
    assert any("another tool opener" in m for m in capture.messages)
    print("PASS glued second call is dropped like a markdown parser would, and logged")


def test_inline_block_on_one_line_dispatches():
    outcome, _ = _parse(REPLY_INLINE_ONE_LINE)
    assert outcome.structural_error is None
    assert [o["output"] for o in outcome.outputs] == ["ran: inline"]
    assert not any(o.get("error") for o in outcome.outputs)
    print("PASS inline one-line block dispatches without any shaming")


def test_one_line_two_calls_both_execute():
    outcome, _ = _parse(REPLY_ONE_LINE_TWO_CALLS)
    assert outcome.structural_error is None
    assert [o["output"] for o in outcome.outputs] == ["ran: a", "ran: b"]
    outcome, _ = _parse(REPLY_RUN_ONLY_CLOSER_TWO_CALLS)
    assert outcome.structural_error is None
    assert [o["output"] for o in outcome.outputs] == ["ran: a", "ran: b"]
    print("PASS the closer consumes only the run; two calls on one line both run")


def test_mention_and_real_call_coexist():
    outcome, _ = _parse(REPLY_MENTION_PLUS_REAL)
    assert outcome.structural_error is None
    assert len(outcome.outputs) == 2
    assert outcome.outputs[0]["error"] is True
    assert "could not be parsed as JSON" in outcome.outputs[0]["output"]
    assert outcome.outputs[1]["output"] == "ran: unlabeled"
    print("PASS a prose mention errors but a later real call still dispatches")


def test_nested_openers_recover_second_call():
    outcome, _ = _parse(REPLY_NESTED_OPENERS)
    assert outcome.structural_error is None
    assert len(outcome.outputs) == 2
    assert outcome.outputs[0]["error"] is True
    assert "Empty tool block." in outcome.outputs[0]["output"]
    assert outcome.outputs[1]["output"] == "ran: unlabeled"
    print("PASS a nested opener errors and the second call still dispatches")


def test_broken_json_then_real_call_both_surface():
    outcome, _ = _parse(REPLY_BROKEN_THEN_REAL)
    assert outcome.structural_error is None
    assert len(outcome.outputs) == 2
    assert outcome.outputs[0]["error"] is True
    assert "could not be parsed as JSON" in outcome.outputs[0]["output"]
    assert outcome.outputs[1]["output"] == "ran: unlabeled"
    print("PASS a broken block errors without swallowing the next real call")


def test_inline_tool_markup_inside_json_string():
    outcome, _ = _parse(REPLY_HEREDOC_PHANTOM)
    assert outcome.structural_error is None
    assert len(outcome.outputs) == 1
    assert outcome.outputs[0]["name"] == "smoke_tool"
    assert not outcome.outputs[0].get("error")
    print("PASS ```tool inside a JSON string is data; the call runs")


def test_lang_fence_closes_block():
    outcome, _ = _parse(REPLY_LANG_CLOSER)
    assert outcome.structural_error is None
    assert [o["output"] for o in outcome.outputs] == ["ran: x"]
    print("PASS a ```lang fence line closes an open tool block")


def test_trailing_prose_inside_block_tolerated():
    outcome, _ = _parse(REPLY_TRAILING_PROSE)
    assert outcome.structural_error is None
    assert [o["output"] for o in outcome.outputs] == ["ran: x"]
    outcome, _ = _parse(REPLY_INLINE_CODE_PROSE)
    assert outcome.structural_error is None
    assert [o["output"] for o in outcome.outputs] == ["ran: x"]
    print(
        "PASS trailing prose and single-backtick inline code inside a block are inert"
    )


def test_broken_json_with_closer_is_error_entry():
    outcome, _ = _parse(REPLY_BROKEN_JSON_CLOSED)
    assert outcome.structural_error is None
    assert len(outcome.outputs) == 1
    assert outcome.outputs[0]["error"] is True
    assert "could not be parsed as JSON" in outcome.outputs[0]["output"]
    assert "Block contents:" in outcome.outputs[0]["output"]
    print("PASS broken JSON with a closer is a never-dispatched error entry")


def test_broken_json_without_closer_is_error_entry():
    outcome, _ = _parse(REPLY_BROKEN_JSON_UNCLOSED)
    assert outcome.structural_error is None
    assert len(outcome.outputs) == 1
    assert outcome.outputs[0]["error"] is True
    assert "could not be parsed as JSON" in outcome.outputs[0]["output"]
    print("PASS broken JSON without a closer errors without nuking the reply")


def test_empty_block_is_error_entry():
    for reply in (REPLY_EMPTY_BLOCK, REPLY_WHITESPACE_BLOCK):
        outcome, _ = _parse(reply)
        assert outcome.structural_error is None
        assert len(outcome.outputs) == 1
        assert outcome.outputs[0]["error"] is True
        assert "Empty tool block." in outcome.outputs[0]["output"]
        assert '"tool_name"' in outcome.outputs[0]["output"]
    print("PASS empty tool blocks become error entries with format help")


def test_opener_at_end_of_reply_is_error_entry():
    outcome, _ = _parse(REPLY_EOF_OPENER)
    assert outcome.structural_error is None
    assert len(outcome.outputs) == 1
    assert outcome.outputs[0]["error"] is True
    assert "Empty tool block." in outcome.outputs[0]["output"]
    outcome, _ = _parse(REPLY_BARE_UNCLOSED)
    assert outcome.structural_error is None
    assert outcome.outputs[0]["error"] is True
    print("PASS a dangling ```tool opener becomes an error entry, not silence")


def test_quoted_tool_example_in_prose_block_is_inert():
    outcome, _ = _parse(REPLY_QUOTED_EXAMPLE)
    assert outcome.structural_error is None
    assert outcome.outputs == []
    print("PASS tool markup inside a prose-fenced block is inert")


def test_boundary_negatives_are_inert_text():
    outcome, _ = _parse("```tooly\nsome text\n```\n")
    assert outcome.structural_error is None
    assert outcome.outputs == []
    outcome, _ = _parse("Mention ```tool: like this in prose")
    assert outcome.structural_error is None
    assert outcome.outputs == []
    outcome, _ = _parse('A ```tool"quoted" mention')
    assert outcome.structural_error is None
    assert outcome.outputs == []
    print("PASS non-boundary ```tool lookalikes are silent text")


def test_midline_tool_markup_dispatches():
    outcome, _ = _parse('Trying the tool now.```tool\n{"smoke_tool": {}}\n```')
    assert outcome.structural_error is None
    assert [o["output"] for o in outcome.outputs] == ["ran: unlabeled"]
    print("PASS a tool block glued after prose dispatches")


def test_crlf_replies_parse():
    outcome, _ = _parse(REPLY_CRLF)
    assert outcome.structural_error is None
    assert [o["output"] for o in outcome.outputs] == ["ran: unlabeled"]
    outcome, _ = _parse(REPLY_CRLF_UNCLOSED)
    assert outcome.outputs == []
    assert "never closed" in outcome.structural_error
    print("PASS CRLF replies parse; a CRLF unclosed block is still structural")


def test_shape_errors_never_crash():
    malformed = {
        REPLY_STRING_ARGS: "got a string",
        REPLY_NULL_ARGS: "got null",
        REPLY_LIST_ARGS: "got an array",
        REPLY_TOP_LEVEL_LIST: "got an array",
        REPLY_TOP_LEVEL_STRING: "got a string",
        REPLY_TOP_LEVEL_NUMBER: "got a number",
        REPLY_EMPTY_OBJECT: "empty JSON object",
        REPLY_MULTI_KEY: "got 2: 'smoke_tool', 'respond'",
    }
    for reply, expected_shape in malformed.items():
        outcome, _ = _parse(reply)
        assert outcome.structural_error is None
        assert len(outcome.outputs) == 1
        assert outcome.outputs[0]["error"] is True
        assert expected_shape in outcome.outputs[0]["output"]
    print("PASS malformed-but-decodable calls become error entries, never crash")


def test_unknown_tool_is_error_entry():
    outcome, _ = _parse(
        'Example:\n```tool\n{"tool_name": {"argument": "value"}}\n```\n'
    )
    assert outcome.structural_error is None
    assert outcome.outputs[0]["error"] is True
    assert (
        "Attempted to call a tool that is not registered: 'tool_name'"
        in (outcome.outputs[0]["output"])
    )
    assert "Registered tools:" in outcome.outputs[0]["output"]
    print("PASS unregistered tools become named error entries")


def test_signature_violations_are_error_entries():
    outcome, _ = _parse(REPLY_MISSING_ARG)
    assert outcome.structural_error is None
    assert outcome.outputs[0]["error"] is True
    assert (
        "Missing required argument(s) 'query' for tool 'web_search'"
        in (outcome.outputs[0]["output"])
    )
    assert "Signature: web_search(query: str)" in outcome.outputs[0]["output"]

    outcome, _ = _parse(REPLY_UNEXPECTED_ARG)
    assert outcome.outputs[0]["error"] is True
    assert (
        "Unexpected argument(s) 'foo' for tool 'web_search'"
        in (outcome.outputs[0]["output"])
    )

    outcome, _ = _parse(REPLY_TYPE_MISMATCH)
    assert outcome.outputs[0]["error"] is True
    assert "should be of type str; got integer" in outcome.outputs[0]["output"]

    outcome, _ = _parse(REPLY_RESPOND_BAD_TYPE)
    assert outcome.outputs[0]["error"] is True
    assert "should be of type str; got integer" in outcome.outputs[0]["output"]
    print("PASS signature violations (missing/unexpected/mistyped) error pre-dispatch")


def test_signature_validation_accepts_optionals():
    assert (
        _signature_error("execute_command", {"command": "ls", "arguments": None})
        is None
    )
    assert _signature_error("read_file", {"file_path": "x"}) is None
    assert _signature_error("smoke_tool", {}) is None
    error = _signature_error("web_search", {"query": 5})
    assert error is not None and "got integer" in error
    print(
        "PASS optional args, null-for-Optional and metadata-less tools pass validation"
    )


def _make_session():
    session = AgentSession(session_id="smoke-test")
    session.dump_path = Path(tempfile.mkdtemp(prefix="syna-test-")) / "dump.json"
    session.request_delay = 0
    return session


def _correctives(session, marker):
    return [
        m["content"]
        for m in session.messages
        if m["role"] == "user" and marker in m["content"]
    ]


def test_session_structural_reply_kept_and_warned():
    session = _make_session()
    session.client = FakeChat([REPLY_UNCLOSED, REPLY_RESPOND])
    events = []
    session.on_event = events.append
    session.run_turn("probe")
    reply_idx = next(
        i
        for i, m in enumerate(session.messages)
        if m["role"] == "assistant" and "from the incident dump" in m["content"]
    )
    corrective = session.messages[reply_idx + 1]
    assert "never closed" in corrective["content"]
    assert "line 1" in corrective["content"]
    assert '{"tool_name": {"argument": "value"}}' in corrective["content"]
    assert "```tool ...```" in corrective["content"]
    assert "task done" in session.messages[-1]["content"]
    assert any(
        e["type"] == "status" and "Malformed tool block" in e["text"] for e in events
    )
    print("PASS structural failure keeps the reply and warns with reason + skeleton")


def test_session_no_calls_reply_kept_and_warned():
    session = _make_session()
    session.client = FakeChat([REPLY_NO_CALLS, REPLY_RESPOND])
    session.run_turn("probe")
    reply_idx = next(
        i
        for i, m in enumerate(session.messages)
        if m["role"] == "assistant" and "Just chatting" in m["content"]
    )
    corrective = session.messages[reply_idx + 1]
    assert "no executable tool calls" in corrective["content"]
    assert '{"tool_name": {"argument": "value"}}' in corrective["content"]
    assert "task done" in session.messages[-1]["content"]
    print("PASS no-call reply stays in history followed by the format warning")


def test_session_inline_block_not_shamed():
    session = _make_session()
    session.client = FakeChat([REPLY_INLINE_ONE_LINE, REPLY_RESPOND])
    session.run_turn("probe")
    assert "ran: inline" in str(session.messages)
    assert not _correctives(session, "[System]: WARNING")
    assert "task done" in session.messages[-1]["content"]
    print("PASS an inline one-line block runs cleanly with no corrective warning")


def test_session_unknown_tool_feedback():
    session = _make_session()
    session.client = FakeChat([REPLY_STRAY_MARKUP, REPLY_RESPOND])
    session.run_turn("probe")
    corrective = _correctives(session, "no executable tool call ran")
    assert len(corrective) == 1
    assert (
        "Attempted to call a tool that is not registered: 'tool_name'"
        in (corrective[0])
    )
    assert '{"tool_name": {"argument": "value"}}' in corrective[0]
    reply_idx = next(
        i
        for i, m in enumerate(session.messages)
        if m["role"] == "assistant" and "Here is the format" in m["content"]
    )
    assert "no executable tool call ran" in session.messages[reply_idx + 1]["content"]
    assert "task done" in session.messages[-1]["content"]
    print("PASS unregistered-tool calls get named feedback and keep the reply")


def test_session_malformed_args_recovery():
    session = _make_session()
    session.client = FakeChat([REPLY_STRING_ARGS, REPLY_CLOSED, REPLY_RESPOND])
    events = []
    session.on_event = events.append
    session.run_turn("probe")
    corrective = _correctives(session, "no executable tool call ran")
    assert len(corrective) == 1
    assert "must be a JSON object" in corrective[0] and "got a string" in corrective[0]
    assert "ran: unlabeled" in session.messages[-3]["content"]
    assert "task done" in session.messages[-1]["content"]
    assert not any("Task aborted" in e.get("text", "") for e in events)
    print("PASS string-args reply is corrected, not fatal; next reply recovers")


def test_session_corrective_path_and_abort():
    session = _make_session()
    session.client = FakeChat([REPLY_UNCLOSED] * 3)
    events = []
    session.on_event = events.append
    session.run_turn("probe")
    corrective = _correctives(session, "no executable tool call ran")
    assert len(corrective) == 2
    aborted = [
        m["content"] for m in session.messages if "task was aborted" in m["content"]
    ]
    assert len(aborted) == 1
    kept = [
        m
        for m in session.messages
        if m["role"] == "assistant" and "from the incident dump" in m["content"]
    ]
    assert len(kept) == 3
    assert "```tool ...```" in corrective[0]
    assert events[-1]["type"] == "error" and "Task aborted" in events[-1]["text"]
    assert "[Tool]: []" not in str(session.messages)
    print("PASS session corrective feedback, streak, and abort after 3")


def test_session_streak_resets_on_dispatch():
    session = _make_session()
    session.client = FakeChat(
        [REPLY_UNCLOSED, REPLY_CLOSED, REPLY_UNCLOSED, REPLY_RESPOND]
    )
    events = []
    session.on_event = events.append
    session.run_turn("probe")
    assert not any("Task aborted" in e.get("text", "") for e in events)
    assert "ran: unlabeled" in str(session.messages)
    assert "task done" in session.messages[-1]["content"]
    print("PASS a dispatching reply between failures resets the streak")


def test_session_tool_execution_failure_does_not_count():
    session = _make_session()
    session.client = FakeChat([REPLY_FAILING_TOOL, REPLY_RESPOND])
    events = []
    session.on_event = events.append
    session.run_turn("probe")
    assert not _correctives(session, "no executable tool call ran")
    assert "Tool execution error: boom" in str(session.messages)
    assert not any("Task aborted" in e.get("text", "") for e in events)
    assert "task done" in session.messages[-1]["content"]
    print("PASS tool execution failures flow through [Tool] and never count")


def _make_web_session():
    """A session configured like the web server: errors surface as rows."""
    session = _make_session()
    session.emit_tool_errors = True
    return session


def test_web_parse_failure_becomes_failed_tool_row():
    session = _make_web_session()
    session.client = FakeChat([REPLY_BROKEN_JSON_CLOSED, REPLY_RESPOND])
    events = []
    session.on_event = events.append
    session.run_turn("probe")
    started = next(
        e
        for e in events
        if e["type"] == "tool_call"
        and e.get("parse_error")
        and e["status"] == "started"
    )
    finished = next(
        e
        for e in events
        if e["type"] == "tool_call"
        and e.get("parse_error")
        and e["status"] == "finished"
    )
    assert started["name"] == "unknown"
    assert started["raw"].startswith('{"smoke_tool": {"label": "broken"')
    assert finished["id"] == started["id"]
    assert finished["failed"] is True
    assert "could not be parsed as JSON" in finished["output"]
    assert finished["output_preview"]
    assert not any(e["type"] == "error" for e in events)
    assert "task done" in session.messages[-1]["content"]
    print("PASS undecodable blocks render as failed tool rows, not error pills")


def test_web_signature_violation_becomes_failed_tool_row():
    session = _make_web_session()
    session.client = FakeChat([REPLY_MISSING_ARG, REPLY_RESPOND])
    events = []
    session.on_event = events.append
    session.run_turn("probe")
    finished = next(
        e
        for e in events
        if e["type"] == "tool_call" and e["status"] == "finished" and e.get("failed")
    )
    assert finished["name"] == "web_search"
    assert "Missing required argument(s) 'query'" in finished["output"]
    assert finished["raw"] == '{"web_search": {}}'
    assert not any(e["type"] == "error" for e in events)
    assert not any(e.get("parse_error") for e in events)
    assert "task done" in session.messages[-1]["content"]
    print("PASS signature violations surface as failed rows with the signature")


def test_web_events_carry_raw_json_and_arguments():
    session = _make_web_session()
    session.client = FakeChat([REPLY_CLOSED, REPLY_RESPOND])
    events = []
    session.on_event = events.append
    session.run_turn("probe")
    started = next(
        e
        for e in events
        if e["type"] == "tool_call"
        and e["status"] == "started"
        and not e.get("parse_error")
    )
    finished = next(
        e
        for e in events
        if e["type"] == "tool_call"
        and e["status"] == "finished"
        and not e.get("parse_error")
    )
    assert started["raw"] == '{"smoke_tool": {}}'
    assert started["arguments"] == {}
    assert finished["raw"] == started["raw"]
    assert "ran: unlabeled" in finished["output"]
    assert not finished.get("failed")
    print("PASS dispatch events carry the exact block JSON and parsed arguments")


def test_corrective_status_is_quiet():
    session = _make_web_session()
    session.client = FakeChat([REPLY_UNCLOSED, REPLY_RESPOND])
    events = []
    session.on_event = events.append
    session.run_turn("probe")
    corrective = [
        e
        for e in events
        if e["type"] == "status" and "Malformed tool block" in e["text"]
    ]
    assert len(corrective) == 1
    assert corrective[0]["quiet"] is True
    thinking = [
        e for e in events if e["type"] == "status" and e["text"] == "Thinking..."
    ]
    assert thinking
    assert all("quiet" not in e for e in thinking)
    print("PASS corrective statuses are quiet so the pill keeps saying Thinking...")


def main():
    TOOL_REGISTRY["smoke_tool"] = _smoke_tool
    TOOL_REGISTRY["smoke_fail"] = _smoke_fail
    try:
        test_prose_separated_blocks_both_execute()
        test_incident_unclosed_block_is_structural()
        test_glued_openers_drop_second_call()
        test_inline_block_on_one_line_dispatches()
        test_one_line_two_calls_both_execute()
        test_mention_and_real_call_coexist()
        test_nested_openers_recover_second_call()
        test_broken_json_then_real_call_both_surface()
        test_inline_tool_markup_inside_json_string()
        test_lang_fence_closes_block()
        test_trailing_prose_inside_block_tolerated()
        test_broken_json_with_closer_is_error_entry()
        test_broken_json_without_closer_is_error_entry()
        test_empty_block_is_error_entry()
        test_opener_at_end_of_reply_is_error_entry()
        test_quoted_tool_example_in_prose_block_is_inert()
        test_boundary_negatives_are_inert_text()
        test_midline_tool_markup_dispatches()
        test_crlf_replies_parse()
        test_shape_errors_never_crash()
        test_unknown_tool_is_error_entry()
        test_signature_violations_are_error_entries()
        test_signature_validation_accepts_optionals()
        test_session_structural_reply_kept_and_warned()
        test_session_no_calls_reply_kept_and_warned()
        test_session_inline_block_not_shamed()
        test_session_unknown_tool_feedback()
        test_session_malformed_args_recovery()
        test_session_corrective_path_and_abort()
        test_session_streak_resets_on_dispatch()
        test_session_tool_execution_failure_does_not_count()
        test_web_parse_failure_becomes_failed_tool_row()
        test_web_signature_violation_becomes_failed_tool_row()
        test_web_events_carry_raw_json_and_arguments()
        test_corrective_status_is_quiet()
    finally:
        TOOL_REGISTRY.pop("smoke_tool", None)
        TOOL_REGISTRY.pop("smoke_fail", None)
    print("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
