"""Exhaustive tool-block parsing cases.

Run with: uv run python tests/test_tool_parsing.py

A data-driven matrix of meaningfully different replies. Every case
documents what the model sees: a dispatched output, a descriptive error
entry, a structural error, or deliberately inert text (no call attempt at
all). Universal invariants hold for every case: parsing never crashes, a
reply containing tool markup never fails silently, and every error message
is long enough to be actionable.
"""

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna.parse import parse_and_execute_tools
from syna.tools import TOOL_REGISTRY


def _smoke_tool(label: str = "unlabeled") -> str:
    """Minimal stand-in tool: echoes its argument back as the output."""
    return f"ran: {label}"


def _smoke_fail() -> str:
    """Stand-in tool that always raises, like a real tool crashing."""
    raise RuntimeError("boom")


def _slow_tool() -> str:
    """Stand-in tool that runs past any test timeout."""
    time.sleep(2)
    return "done slowly"


# Each case: name, reply, expect (per-output (kind, detail) in reply order,
# kinds: "output" exact, "contains" substring, "error" error entry holding
# detail), optional "structural" substring, optional "timeout" override,
# optional "dev_log" to assert the glued-second-call warning fired.
CASES = [
    # --- Dispatches -------------------------------------------------------
    {
        "name": "canonical block dispatches",
        "reply": '```tool\n{"smoke_tool": {}}\n```',
        "expect": [("output", "ran: unlabeled")],
    },
    {
        "name": "two prose-separated blocks both dispatch",
        "reply": (
            "Here is call one.\n"
            '```tool\n{"smoke_tool": {"label": "first"}}\n```\n'
            "Middle narration.\n"
            '```tool\n{"smoke_tool": {"label": "second"}}\n```\n'
            "Done."
        ),
        "expect": [("output", "ran: first"), ("output", "ran: second")],
    },
    {
        "name": "inline one-line block dispatches without shaming",
        "reply": 'Running ```tool {"smoke_tool": {"label": "inline"}}```',
        "expect": [("output", "ran: inline")],
    },
    {
        "name": "tool call right after the tag without a space dispatches",
        "reply": '```tool{"smoke_tool": {}}```',
        "expect": [("output", "ran: unlabeled")],
    },
    {
        "name": "block glued after prose dispatches",
        "reply": 'Working on it.```tool\n{"smoke_tool": {"label": "glued"}}\n```',
        "expect": [("output", "ran: glued")],
    },
    {
        "name": "two calls on one line both dispatch",
        "reply": (
            '```tool {"smoke_tool": {"label": "a"}}```'
            ' and ```tool {"smoke_tool": {"label": "b"}}```'
        ),
        "expect": [("output", "ran: a"), ("output", "ran: b")],
    },
    {
        "name": "trailing rubbish inside the block is discarded",
        "reply": '```tool\n{"smoke_tool": {"label": "x"}}\nsome remark\n```',
        "expect": [("output", "ran: x")],
    },
    {
        "name": "pretty-printed multiline JSON dispatches",
        "reply": '```tool\n{\n  "smoke_tool": {\n    "label": "pretty"\n  }\n}\n```',
        "expect": [("output", "ran: pretty")],
    },
    {
        "name": "backtick run inside a JSON string never closes the block",
        "reply": '```tool\n{"smoke_tool": {"label": "text with ``` inside"}}\n```',
        "expect": [("output", "ran: text with ``` inside")],
    },
    {
        "name": "a full fake block inside a JSON string is data",
        "reply": (
            "```tool\n"
            '{"smoke_tool": {"label": "```tool\\nnot a real block\\n```"}}\n'
            "```"
        ),
        "expect": [("output", "ran: ```tool\nnot a real block\n```")],
    },
    {
        "name": "a lang fence closes an open tool block",
        "reply": '```tool\n{"smoke_tool": {"label": "x"}}\n```python\nprint(1)\n```\n',
        "expect": [("output", "ran: x")],
    },
    {
        "name": "CRLF block dispatches",
        "reply": '```tool\r\n{"smoke_tool": {}}\r\n```\r\n',
        "expect": [("output", "ran: unlabeled")],
    },
    {
        "name": "indented opener dispatches",
        "reply": '    ```tool\n{"smoke_tool": {}}\n```',
        "expect": [("output", "ran: unlabeled")],
    },
    {
        "name": "blank lines around the JSON dispatch",
        "reply": '```tool\n\n\n{"smoke_tool": {}}\n\n\n```',
        "expect": [("output", "ran: unlabeled")],
    },
    {
        "name": "unicode payload survives the round trip",
        "reply": '```tool\n{"smoke_tool": {"label": "héllo — 世界"}}\n```',
        "expect": [("output", "ran: héllo — 世界")],
    },
    {
        "name": "unicode-escaped backticks in strings are data",
        "reply": '```tool\n{"smoke_tool": {"label": "\\u0060\\u0060\\u0060"}}\n```',
        "expect": [("output", "ran: ```")],
    },
    {
        "name": "respond dispatches with its text as output",
        "reply": '```tool\n{"respond": {"text": "All done!"}}\n```',
        "expect": [("output", "All done!")],
    },
    {
        "name": "nested JSON payload dispatches",
        "reply": '```tool\n{"smoke_tool": {"label": {"inner": "value"}}}\n```',
        "expect": [("output", "ran: {'inner': 'value'}")],
    },
    {
        "name": "duplicate JSON keys collapse to the last value",
        "reply": (
            '```tool\n{"smoke_tool": {"label": "first"},'
            ' "smoke_tool": {"label": "second"}}\n```'
        ),
        "expect": [("output", "ran: second")],
    },
    {
        "name": "mixed reply: valid call runs, broken block errors (decision A)",
        "reply": (
            '```tool\n{"smoke_tool": {"label": "first"}}\n```\n'
            '```tool\n{"smoke_tool": "broken"\n```'
        ),
        "expect": [
            ("output", "ran: first"),
            ("error", "could not be parsed as JSON"),
        ],
    },
    {
        "name": "broken block then valid call: the scan continues",
        "reply": (
            '```tool\n{"smoke_tool": "broken"\n```\n```tool\n{"smoke_tool": {}}\n```'
        ),
        "expect": [
            ("error", "could not be parsed as JSON"),
            ("output", "ran: unlabeled"),
        ],
    },
    {
        "name": "prose mention errors but a later real call dispatches",
        "reply": (
            "I will use ```tool blocks later. Real call:\n"
            '```tool\n{"smoke_tool": {}}\n```'
        ),
        "expect": [
            ("error", "could not be parsed as JSON"),
            ("output", "ran: unlabeled"),
        ],
    },
    {
        "name": "two opening tags: the nested one errors, the call dispatches",
        "reply": '```tool\n```tool\n{"smoke_tool": {}}\n```',
        "expect": [
            ("error", "Empty tool block."),
            ("output", "ran: unlabeled"),
        ],
    },
    {
        "name": "empty block between two valid calls: run, error, run",
        "reply": (
            '```tool {"smoke_tool": {"label": "a"}}``` '
            "```tool ``` "
            '```tool {"smoke_tool": {"label": "c"}}```'
        ),
        "expect": [
            ("output", "ran: a"),
            ("error", "Empty tool block."),
            ("output", "ran: c"),
        ],
    },
    {
        "name": "tool raising an exception is a visible execution error",
        "reply": '```tool\n{"smoke_fail": {}}\n```',
        "expect": [("contains", "Tool execution error: boom")],
    },
    {
        "name": "tool exceeding the timeout is detached with a notice",
        "reply": '```tool\n{"slow_tool": {}}\n```',
        "expect": [("contains", "timed out after 0.3 seconds")],
        "timeout": 0.3,
    },
    # --- Error entries ----------------------------------------------------
    {
        "name": "empty block",
        "reply": "```tool\n```\n",
        "expect": [("error", "Empty tool block.")],
    },
    {
        "name": "whitespace-only block",
        "reply": "```tool\n   \n```\n",
        "expect": [("error", "Empty tool block.")],
    },
    {
        "name": "opening tag with nothing after it (end of reply)",
        "reply": "Step 1:\n```tool\n",
        "expect": [("error", "Empty tool block.")],
    },
    {
        "name": "invalid JSON reports the exception and the contents",
        "reply": "```tool\n{broken\n```",
        "expect": [("error", "could not be parsed as JSON")],
    },
    {
        "name": "valid JSON with rubbish before it is rejected with the excerpt",
        "reply": '```tool oops {"smoke_tool": {}} ```',
        "expect": [("error", "Block contents: 'oops")],
    },
    {
        "name": "top-level string is not a tool call",
        "reply": '```tool\n"smoke_tool"\n```',
        "expect": [("error", "got a string")],
    },
    {
        "name": "top-level array is not a tool call",
        "reply": '```tool\n["smoke_tool"]\n```',
        "expect": [("error", "got an array")],
    },
    {
        "name": "top-level number is not a tool call",
        "reply": "```tool\n42\n```",
        "expect": [("error", "got a number")],
    },
    {
        "name": "top-level null is not a tool call",
        "reply": "```tool\nnull\n```",
        "expect": [("error", "got null")],
    },
    {
        "name": "empty object has no tool name",
        "reply": "```tool\n{}\n```",
        "expect": [("error", "empty JSON object")],
    },
    {
        "name": "multiple top-level keys are rejected",
        "reply": '```tool\n{"smoke_tool": {}, "respond": {"text": "hi"}}\n```',
        "expect": [("error", "got 2: 'smoke_tool', 'respond'")],
    },
    {
        "name": "string arguments are not an arguments object",
        "reply": '```tool\n{"smoke_tool": "ls -la"}\n```',
        "expect": [("error", "must be a JSON object")],
    },
    {
        "name": "null arguments are not an arguments object",
        "reply": '```tool\n{"smoke_tool": null}\n```',
        "expect": [("error", "got null")],
    },
    {
        "name": "array arguments are not an arguments object",
        "reply": '```tool\n{"smoke_tool": ["ls"]}\n```',
        "expect": [("error", "got an array")],
    },
    {
        "name": "unregistered tool names the mistake and lists the registry",
        "reply": '```tool\n{"tool_name": {"argument": "value"}}\n```',
        "expect": [
            ("error", "Attempted to call a tool that is not registered: 'tool_name'")
        ],
    },
    {
        "name": "missing required argument",
        "reply": '```tool\n{"web_search": {}}\n```',
        "expect": [
            ("error", "Missing required argument(s) 'query' for tool 'web_search'")
        ],
    },
    {
        "name": "unexpected extra argument",
        "reply": '```tool\n{"web_search": {"query": "x", "foo": 1}}\n```',
        "expect": [("error", "Unexpected argument(s) 'foo' for tool 'web_search'")],
    },
    {
        "name": "argument of the wrong type",
        "reply": '```tool\n{"web_search": {"query": 123}}\n```',
        "expect": [("error", "should be of type str; got integer")],
    },
    {
        "name": "boolean where a string is required",
        "reply": '```tool\n{"respond": {"text": true}}\n```',
        "expect": [("error", "should be of type str; got boolean")],
    },
    # --- Backtick run lengths --------------------------------------------
    {
        "name": "four-backtick opener dispatches",
        "reply": '````tool\n{"smoke_tool": {}}\n```',
        "expect": [("output", "ran: unlabeled")],
    },
    {
        "name": "five-backtick opener dispatches",
        "reply": '`````tool\n{"smoke_tool": {}}\n```',
        "expect": [("output", "ran: unlabeled")],
    },
    {
        "name": "four-backtick closer dispatches",
        "reply": '```tool\n{"smoke_tool": {}}\n````',
        "expect": [("output", "ran: unlabeled")],
    },
    {
        "name": "two-backtick opener is inert text",
        "reply": '``tool {"smoke_tool": {}}```',
        "expect": [],
    },
    {
        "name": "two-backtick closer never closes: structural error",
        "reply": '```tool\n{"smoke_tool": {}}\n``',
        "structural": "never closed",
        "expect": [],
    },
    {
        "name": "stray two-backtick run before the closer is tolerated",
        "reply": '```tool\n{"smoke_tool": {}}\n``\n```',
        "expect": [("output", "ran: unlabeled")],
    },
    {
        "name": "tag immediately closed with no space is inert",
        "reply": "```tool```",
        "expect": [],
    },
    {
        "name": "inline empty block with a space is an error entry",
        "reply": "```tool ```",
        "expect": [("error", "Empty tool block.")],
    },
    # --- Tag spelling and spacing ----------------------------------------
    {
        "name": "space between the backticks and tool is not an opener",
        "reply": '``` tool {"smoke_tool": {}}```',
        "expect": [],
    },
    {
        "name": "capitalized Tool is not an opener (case-sensitive)",
        "reply": '```Tool {"smoke_tool": {}}```',
        "expect": [],
    },
    {
        "name": "trailing spaces after the tag are tolerated",
        "reply": '```tool   \n{"smoke_tool": {}}\n```',
        "expect": [("output", "ran: unlabeled")],
    },
    {
        "name": "tool tag inside single-backtick inline code is inert",
        "reply": "`use ```tool` blocks",
        "expect": [],
    },
    {
        "name": "residual: a complete call inside inline code still dispatches",
        "reply": '`text ```tool {"smoke_tool": {}} ``` more`',
        "expect": [("output", "ran: unlabeled")],
    },
    # --- Special characters, encodings and line endings -------------------
    {
        "name": "BOM before the opener is tolerated",
        "reply": '﻿```tool\n{"smoke_tool": {}}\n```',
        "expect": [("output", "ran: unlabeled")],
    },
    {
        "name": "BOM inside the block breaks JSON decoding with an error",
        "reply": '```tool\n﻿{"smoke_tool": {}}\n```',
        "expect": [("error", "could not be parsed as JSON")],
    },
    {
        "name": "CR-only line endings dispatch",
        "reply": '```tool\r{"smoke_tool": {}}\r```',
        "expect": [("output", "ran: unlabeled")],
    },
    {
        "name": "mixed LF and CRLF endings dispatch",
        "reply": '```tool\r\n{"smoke_tool": {"label": "a"}}\n```\r\n',
        "expect": [("output", "ran: a")],
    },
    {
        "name": "multi-script payload survives the round trip",
        "reply": '```tool\n{"smoke_tool": {"label": "café ñ 中文 مرحبا"}}\n```',
        "expect": [("output", "ran: café ñ 中文 مرحبا")],
    },
    {
        "name": "emoji payload dispatches",
        "reply": '```tool\n{"smoke_tool": {"label": "🚀 launch"}}\n```',
        "expect": [("output", "ran: 🚀 launch")],
    },
    {
        "name": "emoji via surrogate escapes decodes correctly",
        "reply": '```tool\n{"smoke_tool": {"label": "\\ud83d\\ude00"}}\n```',
        "expect": [("output", "ran: 😀")],
    },
    {
        "name": "emoji tool name is an unregistered-tool error",
        "reply": '```tool\n{"🚀": {}}\n```',
        "expect": [("error", "Attempted to call a tool that is not registered: '🚀'")],
    },
    {
        "name": "NBSP after the tag breaks the boundary and is inert",
        "reply": '```tool {"smoke_tool": {}}```',
        "expect": [],
    },
    {
        "name": "NBSP inside the JSON is invalid JSON",
        "reply": '```tool\n{"smoke_tool": {"label": "x"}}\n```',
        "expect": [("error", "could not be parsed as JSON")],
    },
    {
        "name": "escaped newline and backticks inside a string are data",
        "reply": '```tool\n{"smoke_tool": {"label": "a\\n```\\nb"}}\n```',
        "expect": [("output", "ran: a\n```\nb")],
    },
    {
        "name": "escaped quotes inside a string are data",
        "reply": '```tool\n{"smoke_tool": {"label": "say \\"hi\\""}}\n```',
        "expect": [("output", 'ran: say "hi"')],
    },
    {
        "name": "escaped newline inside a string is data",
        "reply": '```tool\n{"smoke_tool": {"label": "line1\\nline2"}}\n```',
        "expect": [("output", "ran: line1\nline2")],
    },
    # --- JSON dialect mistakes -------------------------------------------
    {
        "name": "single-quoted JSON is invalid",
        "reply": "```tool\n{'smoke_tool': {}}\n```",
        "expect": [("error", "could not be parsed as JSON")],
    },
    {
        "name": "trailing comma is invalid JSON",
        "reply": '```tool\n{"smoke_tool": {},}\n```',
        "expect": [("error", "could not be parsed as JSON")],
    },
    {
        "name": "Python-style NaN decodes but is not an arguments object",
        "reply": '```tool\n{"smoke_tool": NaN}\n```',
        "expect": [("error", "got a number")],
    },
    # --- Two JSON payloads inside one block -------------------------------
    {
        "name": "second JSON object in one block is trailing rubbish",
        "reply": (
            '```tool\n{"smoke_tool": {"label": "a"}}\n'
            '{"smoke_tool": {"label": "b"}}\n```'
        ),
        "expect": [("output", "ran: a")],
    },
    # --- Signature type matrix (real tools, all pre-dispatch errors) ------
    {
        "name": "string where an int is required",
        "reply": '```tool\n{"read_file": {"file_path": "x", "start": "1"}}\n```',
        "expect": [("error", "should be of type int; got string")],
    },
    {
        "name": "boolean where an int is required",
        "reply": '```tool\n{"read_file": {"file_path": "x", "start": true}}\n```',
        "expect": [("error", "should be of type int; got boolean")],
    },
    {
        "name": "float where an int is required",
        "reply": '```tool\n{"read_file": {"file_path": "x", "start": 1.5}}\n```',
        "expect": [("error", "should be of type int; got number")],
    },
    {
        "name": "array where a dict is required",
        "reply": '```tool\n{"web_interact": {"command": "goto", "args": []}}\n```',
        "expect": [("error", "should be of type dict[str, Any]; got array")],
    },
    {
        "name": "object where a list is required",
        "reply": '```tool\n{"edit_file": {"file_path": "x", "operations": {"a": 1}}}\n```',
        "expect": [("error", "should be of type list[dict]; got object")],
    },
    {
        "name": "null for a required non-optional argument",
        "reply": '```tool\n{"web_search": {"query": null}}\n```',
        "expect": [("error", "should be of type str; got null")],
    },
    {
        "name": "respond with no text at all",
        "reply": '```tool\n{"respond": {}}\n```',
        "expect": [("error", "Missing required argument(s) 'text' for tool 'respond'")],
    },
    # --- Combinators ------------------------------------------------------
    {
        "name": "five-block reply with mixed outcomes surfaces every entry",
        "reply": (
            '```tool\n{"smoke_tool": "broken"\n```\n'
            '```tool\n{"unknown_tool": {}}\n```\n'
            '```tool\n{"smoke_tool": {"label": "ok1"}}\n```\n'
            '```tool\n{"web_search": {}}\n```\n'
            '```tool\n{"smoke_tool": {"label": "ok2"}}\n```'
        ),
        "expect": [
            ("error", "could not be parsed as JSON"),
            ("error", "Attempted to call a tool that is not registered"),
            ("output", "ran: ok1"),
            ("error", "Missing required argument(s) 'query'"),
            ("output", "ran: ok2"),
        ],
    },
    {
        "name": "valid call followed by a dangling opener surfaces both",
        "reply": '```tool\n{"smoke_tool": {}}\n```\n```tool',
        "expect": [
            ("output", "ran: unlabeled"),
            ("error", "Empty tool block."),
        ],
    },
    {
        "name": "three inline empty spans yield three error entries",
        "reply": "```tool ``` ```tool ``` ```tool ```",
        "expect": [
            ("error", "Empty tool block."),
            ("error", "Empty tool block."),
            ("error", "Empty tool block."),
        ],
    },
    {
        "name": "structural error names even unregistered calls",
        "reply": '```tool\n{"totally_fake": {"x": 1}}',
        "structural": "totally_fake(x=1)",
        "expect": [],
    },
    {
        "name": "very long rubbish is excerpted, not dumped",
        "reply": "```tool " + "x" * 500 + " ```",
        "expect": [("error", "could not be parsed as JSON")],
    },
    {
        "name": "empty reply is inert",
        "reply": "",
        "expect": [],
    },
    # --- Discovery edge cases and cross-cutting behavior -------------------
    {
        "name": "tool tag on the line after a fence is not an opener",
        "reply": '```\ntool\n{"smoke_tool": {}}\n```',
        "expect": [],
    },
    {
        "name": "a lang fence cannot close a prose region",
        "reply": "```text\nfoo\n```text\nmore",
        "expect": [],
    },
    {
        "name": "residual: doubled closer after a quoted example hides the next call",
        "reply": (
            "Example:\n"
            "```text\n"
            '```tool\n{"tool_name": {"argument": "value"}}\n'
            "```\n"
            "```\n"
            '```tool\n{"smoke_tool": {"label": "after"}}\n'
            "```"
        ),
        "expect": [],
    },
    {
        "name": "blockquote-prefixed call dispatches (any position)",
        "reply": '> ```tool {"smoke_tool": {"label": "quoted"}}```',
        "expect": [("output", "ran: quoted")],
    },
    {
        "name": "wrong-case tool name is unregistered (case-sensitive registry)",
        "reply": '```tool\n{"Smoke_Tool": {}}\n```',
        "expect": [
            ("error", "Attempted to call a tool that is not registered: 'Smoke_Tool'")
        ],
    },
    {
        "name": "unicode-escaped tool name decodes before the registry lookup",
        "reply": '```tool\n{"\\u0073moke_tool": {}}\n```',
        "expect": [("output", "ran: unlabeled")],
    },
    {
        "name": "a tool literally named 'unknown' is just unregistered",
        "reply": '```tool\n{"unknown": {}}\n```',
        "expect": [
            ("error", "Attempted to call a tool that is not registered: 'unknown'")
        ],
    },
    {
        "name": "respond is not special to the parser: both calls dispatch",
        "reply": (
            '```tool\n{"smoke_tool": {"label": "x"}}\n```\n'
            '```tool\n{"respond": {"text": "bye"}}\n```'
        ),
        "expect": [("output", "ran: x"), ("output", "bye")],
    },
    {
        "name": "a six-backtick closer run is consumed wholly",
        "reply": '```tool {"smoke_tool": {"label": "a"}}`````` rest',
        "expect": [("output", "ran: a")],
    },
    {
        "name": "extra arguments on a metadata-less tool fail at execution",
        "reply": '```tool\n{"smoke_tool": {"label": "x", "foo": 1}}\n```',
        "expect": [("contains", "Tool execution error")],
    },
    {
        "name": "JSON truncated mid-object at end of reply",
        "reply": "```tool{",
        "expect": [("error", "could not be parsed as JSON")],
    },
    {
        "name": "top-level boolean is not a tool call",
        "reply": "```tool\ntrue\n```",
        "expect": [("error", "got a boolean")],
    },
    {
        "name": "structural error reports the line of a later block",
        "reply": (
            '```tool\n{"smoke_tool": {}}\n```\ntext\n\n```tool\n{"smoke_tool": {}}'
        ),
        "structural": "line 6",
        "expect": [],
    },
    {
        "name": "structural error describes a non-object payload",
        "reply": '```tool\n"just a string"',
        "structural": "content of shape a string",
        "expect": [],
    },
    # --- Structural errors (nothing in the reply executes) ----------------
    {
        "name": "the incident: valid JSON, no closing fence",
        "reply": '```tool\n{"smoke_tool": {"label": "from the incident dump"}}',
        "structural": "never closed",
        "expect": [],
    },
    {
        "name": "second block unclosed: even the valid first call does not run",
        "reply": (
            '```tool\n{"smoke_tool": {"label": "first"}}\n```\n'
            '```tool\n{"smoke_tool": {"label": "second"}}'
        ),
        "structural": "never closed",
        "expect": [],
    },
    # --- Inert text (no call attempt; session-level no-calls warning) -----
    {
        "name": "no tool blocks at all",
        "reply": "Just chatting about the weather, no tool calls here.",
        "expect": [],
    },
    {
        "name": "tool call inside a ```text fenced block is inert",
        "reply": (
            "Example of the format:\n"
            "```text\n"
            '```tool\n{"tool_name": {"argument": "value"}}\n'
            "```\n"
            "```\n"
        ),
        "expect": [],
    },
    {
        "name": "```tooly is a prose fence, not a tool opener",
        "reply": "```tooly\nsome text\n```\n",
        "expect": [],
    },
    {
        "name": "```tool followed by a colon has no boundary and is inert",
        "reply": "Mention ```tool: like this in prose",
        "expect": [],
    },
    {
        "name": "emoji-decorated mention without a boundary is inert",
        "reply": "Trying ```tool🎯 now",
        "expect": [],
    },
    {
        "name": "residual: a stray bare fence hides the following tool block",
        "reply": 'Some text.\n```\n```tool\n{"smoke_tool": {}}\n```\n',
        "expect": [],
    },
    # --- Accepted residual: markdown-parser drop with a dev-log entry -----
    {
        "name": "glued second opener: first call runs, second dropped and logged",
        "reply": (
            '```tool {"smoke_tool": {"label": "a"}}```'
            'tool {"smoke_tool": {"label": "b"}}```'
        ),
        "expect": [("output", "ran: a")],
        "dev_log": True,
    },
]


def _capture_dev_log():
    handler = _LogCapture()
    logging.getLogger("syna").addHandler(handler)
    return handler


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _run_case(case):
    capture = _capture_dev_log() if case.get("dev_log") else None
    try:
        outcome = parse_and_execute_tools(
            case["reply"],
            on_tool_event=lambda *a, **k: None,
            echo_errors=False,
            tool_timeout_s=case.get("timeout"),
        )
    finally:
        if capture is not None:
            logging.getLogger("syna").removeHandler(capture)
    return outcome, capture


def _check_case(case):
    outcome, capture = _run_case(case)
    label = case["name"]

    if case.get("structural"):
        assert outcome.structural_error is not None, label
        assert case["structural"] in outcome.structural_error, (label, outcome)
        assert outcome.outputs == [], (label, outcome.outputs)
        assert len(outcome.structural_error) >= 20, label
    else:
        assert outcome.structural_error is None, (label, outcome.structural_error)

    expected = case["expect"]
    assert len(outcome.outputs) == len(expected), (label, outcome.outputs)
    for entry, (kind, detail) in zip(outcome.outputs, expected):
        if kind == "output":
            assert not entry.get("error"), (label, entry)
            assert entry["output"] == detail, (label, entry)
        elif kind == "contains":
            assert not entry.get("error"), (label, entry)
            assert detail in entry["output"], (label, entry)
        else:
            assert entry.get("error") is True, (label, entry)
            assert detail in entry["output"], (label, entry)
            assert len(entry["output"]) >= 20, (label, entry)
        assert entry["name"] and entry["signature"], (label, entry)

    if case.get("dev_log"):
        assert any("another tool opener" in message for message in capture.messages), (
            label,
            capture.messages,
        )

    print(f"PASS {label}")
    for entry in outcome.outputs:
        marker = "error" if entry.get("error") else "result"
        print(f"     model sees [{marker}] {entry['name']}: {entry['output'][:96]}")
    if outcome.structural_error:
        print(f"     model sees [structural]: {outcome.structural_error[:96]}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    TOOL_REGISTRY["smoke_tool"] = _smoke_tool
    TOOL_REGISTRY["smoke_fail"] = _smoke_fail
    TOOL_REGISTRY["slow_tool"] = _slow_tool
    try:
        for case in CASES:
            _check_case(case)
    finally:
        TOOL_REGISTRY.pop("smoke_tool", None)
        TOOL_REGISTRY.pop("smoke_fail", None)
        TOOL_REGISTRY.pop("slow_tool", None)
    print(f"ALL {len(CASES)} TOOL PARSING CASES PASSED")


if __name__ == "__main__":
    main()
