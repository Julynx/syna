"""Tool call parsing and execution for agent responses.

The parser is a strict pre-parse gate: a reply's tool markup is fully
scanned before anything executes. Structurally broken markup (a ```tool
block that was never closed) aborts the whole reply with a structural
error; sound markup is executed call by call, and calls whose content is
empty, not valid JSON, or badly shaped never dispatch and are reported
back to the model as error entries instead.
"""

import json
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .config import get_logger, load_config
from .state import get_registered_container, register_container, unregister_container
from .tools import TOOL_REGISTRY

DEFAULT_TOOL_CALL_TIMEOUT_S = 60

FENCE_LINE_RE = re.compile(r"^ {0,3}(`{3,})[ \t]*(.*)$", re.MULTILINE)
TOOL_INFO = "tool"
TOOL_INFO_BOUNDARIES = frozenset("{ \t")
WHITESPACE = " \t\n\r"


@dataclass(frozen=True)
class ParseOutcome:
    """Result of parsing and executing one reply's tool calls.

    'structural_error' is set when the reply's tool markup is broken beyond
    a single call's content (a ```tool block that was never closed): nothing
    is executed and the message names the problem for the corrective
    feedback loop. 'outputs' carries one entry per parsed call in reply
    order; entries for calls that never reached dispatch carry "error": True.
    """

    structural_error: str | None
    outputs: list[dict]


@dataclass(frozen=True)
class _ParsedBlock:
    """One scanned ```tool block, before shape validation and dispatch.

    'payload' is the decoded JSON value of a sound block; 'error' is the
    corrective message for a block whose content is empty or not valid
    JSON. Exactly one of the two is set.
    """

    line_no: int
    payload: Any = None
    error: str | None = None


def _line_no(text: str, pos: int) -> int:
    """Return the 1-based line number of character position 'pos'."""
    return text.count("\n", 0, pos) + 1


def _after_line(text: str, pos: int) -> int:
    """Return the position just past the newline that ends 'pos''s line."""
    at = text.find("\n", pos)
    return len(text) if at == -1 else at + 1


def _skip_whitespace(text: str, pos: int) -> int:
    """Return the first position at/after 'pos' holding a non-whitespace char."""
    while pos < len(text) and text[pos] in WHITESPACE:
        pos += 1
    return pos


def _find_closing_run(text: str, pos: int) -> int | None:
    """Find the first backtick run of three or more backticks at/after 'pos'.

    Runs of one or two backticks (inline code in trailing prose) can never
    close a block and are skipped. Returns the run's start position, or
    None when no such run exists before the end of the text.
    """
    while True:
        at = text.find("`", pos)
        if at == -1:
            return None
        run_end = at
        while run_end < len(text) and text[run_end] == "`":
            run_end += 1
        if run_end - at >= 3:
            return at
        pos = run_end


def _is_tool_info(info: str) -> bool:
    """Return True when a fence line's info string opens a tool block.

    The info must start with 'tool' followed by the end of the line,
    whitespace, or the brace of an inline JSON payload; any other info
    ('python', 'tooly', 'tool```', ...) opens an ordinary prose block.
    """
    return info.startswith(TOOL_INFO) and (
        len(info) == len(TOOL_INFO) or info[len(TOOL_INFO)] in TOOL_INFO_BOUNDARIES
    )


def _describe_json_shape(value) -> str:
    """Describe a decoded JSON value's shape for corrective feedback."""
    if isinstance(value, dict):
        keys = ", ".join(repr(key) for key in list(value)[:4])
        return f"an object with {len(value)} key(s): {keys}"
    if isinstance(value, list):
        return f"an array with {len(value)} item(s)"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, (int, float)):
        return "a number"
    if value is None:
        return "null"
    return type(value).__name__


def _describe_block_content(payload) -> str:
    """Best-effort naming of a decoded call for structural error messages."""
    if isinstance(payload, dict) and len(payload) == 1:
        name = next(iter(payload))
        arguments = payload[name]
        if isinstance(arguments, dict):
            return f"the call {build_signature(name, arguments)}"
        return (
            f"tool name '{name}' with {_describe_json_shape(arguments)}"
            " as its arguments"
        )
    return f"content of shape {_describe_json_shape(payload)}"


def _resume_after_closer(text: str, boundary: int, logger) -> int:
    """Consume a tool block's closing fence line and log any trailing text.

    The closing run may carry trailing text on its line (e.g. the 'tool' of
    a second block's opener glued to a missing closer); like a markdown
    parser, the text is discarded as part of the closing line, and logged
    so a silently dropped call is visible in the dev log.
    """
    run_end = boundary
    while run_end < len(text) and text[run_end] == "`":
        run_end += 1
    newline_at = text.find("\n", run_end)
    if newline_at == -1:
        remainder = text[run_end:]
        line_end = len(text)
    else:
        remainder = text[run_end:newline_at].rstrip("\r")
        line_end = newline_at + 1
    if remainder.strip():
        logger.warning(
            "Discarded text after a tool block's closing fence"
            " (line %d): %.100r",
            _line_no(text, boundary),
            remainder,
        )
    return line_end


def _scan_tool_block(text: str, decoder, content_start: int, line_no: int, logger):
    """Scan one tool block's content, starting after its 'tool' info token.

    The JSON decoder is the string-aware tokenizer: backticks inside JSON
    strings are consumed as data, and the first backtick run of three or
    more after the decoded value closes the block whatever its line carries
    (a bare fence, another opener's fence, or prose containing one). A block
    with no such run was never closed, which is a structural error carrying
    the exact parsed call.

    Returns (payload, error, resume, structural_error): the decoded payload
    or a content-level error message, the position to resume scanning at
    when the block is sound, and a structural error when it is not.
    """
    try:
        payload, json_end = decoder.raw_decode(text, content_start)
    except json.JSONDecodeError as exc:
        boundary = _find_closing_run(text, content_start)
        if boundary is None:
            if text[content_start:].strip():
                return None, None, None, (
                    f"A ```tool block starting on line {line_no} was never"
                    f" closed, and its content is not valid JSON: {exc}"
                )
            return None, None, None, (
                f"A ```tool block starting on line {line_no} was never closed"
                " and had no content"
            )
        if not text[content_start:boundary].strip():
            error = "The tool block was empty."
        else:
            error = f"A tool call could not be decoded as JSON: {exc}"
        resume = _resume_after_closer(text, boundary, logger)
        return None, error, resume, None
    boundary = _find_closing_run(text, json_end)
    if boundary is None:
        return None, None, None, (
            f"A ```tool block starting on line {line_no} was never closed;"
            f" it contained {_describe_block_content(payload)}"
        )
    resume = _resume_after_closer(text, boundary, logger)
    return payload, None, resume, None


def _skip_prose_block(text: str, after_opener: int) -> int:
    """Skip an ordinary prose block, returning the resume position.

    The block ends at the next line-start bare fence; tool markup inside
    stays inert. Balance is not enforced: an unclosed prose block silently
    swallows the rest of the reply, so stray fences can never turn into
    tool calls.
    """
    pos = _after_line(text, after_opener)
    while True:
        fence = FENCE_LINE_RE.search(text, pos)
        if fence is None:
            return len(text)
        if fence.group(2).strip() == "":
            return _after_line(text, fence.start(1))
        pos = _after_line(text, fence.start(1))


def _scan_tool_blocks(text: str):
    """Scan a reply into parsed ```tool blocks plus an optional structural error.

    Fence openers are recognized only at line starts (0-3 spaces of indent,
    a run of three or more backticks); an info string starting with 'tool'
    opens a tool block, every other fence opens an ordinary prose block.
    Mid-line backtick runs are inert content, so a ```tool mentioned inside
    a sentence or glued after prose never opens a block.

    Returns (blocks, structural_error). 'structural_error' is None when the
    reply's tool markup is structurally sound, in which case every block
    carries either a decoded payload or a content-level error message; the
    first structural violation wins and the blocks scanned so far are
    discarded (nothing from a broken reply executes).
    """
    logger = get_logger()
    decoder = json.JSONDecoder()
    blocks = []
    pos = 0
    while True:
        opener = FENCE_LINE_RE.search(text, pos)
        if opener is None:
            return blocks, None
        run_start, run_end = opener.span(1)
        info = opener.group(2).rstrip("\r")
        if _is_tool_info(info):
            content_start = _skip_whitespace(text, opener.start(2) + len(TOOL_INFO))
            payload, error, resume, structural = _scan_tool_block(
                text, decoder, content_start, _line_no(text, run_start), logger
            )
            if structural is not None:
                return blocks, structural
            blocks.append(_ParsedBlock(_line_no(text, run_start), payload, error))
            pos = resume
            continue
        pos = _skip_prose_block(text, run_end)


def build_signature(tool_name, tool_arguments):
    """Render a tool call as a single-line python-like signature."""
    return (
        f"{tool_name}("
        + ", ".join(
            f"{arg_name}={arg_value!r}"
            if isinstance(arg_value, str)
            else f"{arg_name}={arg_value}"
            for arg_name, arg_value in tool_arguments.items()
        )
        + ")"
    )


def load_tool_call_timeout() -> float:
    """Return the per-tool-call timeout in seconds from the configuration."""
    return load_config().get("tool_call_timeout_s", DEFAULT_TOOL_CALL_TIMEOUT_S)


def build_detached_call_notice(tool_name, tool_arguments, timeout_s):
    """Compose the tool result reported to the model for a detached call."""
    return (
        f"Tool call timed out after {timeout_s} seconds:"
        f" {build_signature(tool_name, tool_arguments)}."
        " The call was not aborted: it is still running in the background and"
        " its result will be discarded. Continue working without waiting for"
        " it. If the call runs a process inside the container, you can locate"
        " and terminate it with another execute_command (e.g. ps / kill)."
    )


def execute_tool_with_timeout(tool_name, tool_arguments, timeout_s):
    """Run a tool in a worker thread and return its output.

    The tool resolves the container registered in the calling thread. If it
    does not finish within 'timeout_s' seconds, the worker is detached and a
    notice is returned instead; the detached call keeps running and its
    eventual result is discarded. Tool exceptions propagate to the caller
    unchanged.

    Returns a (output, detached) tuple.
    """
    tool_fn = TOOL_REGISTRY[tool_name]
    container = get_registered_container()
    finished = threading.Event()
    outcome = {}

    def worker():
        if container is not None:
            register_container(container)
        try:
            outcome["output"] = tool_fn(**tool_arguments)
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            if container is not None:
                unregister_container()
            finished.set()

    worker_thread = threading.Thread(
        target=worker, name=f"syna-tool-{tool_name}", daemon=True
    )
    worker_thread.start()
    if finished.wait(timeout_s):
        if "error" in outcome:
            raise outcome["error"]
        return outcome["output"], False

    return build_detached_call_notice(tool_name, tool_arguments, timeout_s), True


def parse_and_execute_tools(
    text,
    on_tool_event: Callable | None = None,
    echo_errors: bool = True,
    tool_timeout_s: float | None = None,
) -> ParseOutcome:
    """Parse tool invocations from text and execute their corresponding functions.

    'on_tool_event' is called as on_tool_event(status, name, signature, output)
    with status "started" before each tool runs (it may raise to abort the
    remaining calls), with status "error" when a tool call cannot be decoded,
    fails, or is detached after exceeding the timeout, and with status
    "finished" once its output is available.

    'echo_errors' controls the legacy stdout rendering of failures, meant for
    the terminal CLI; frontends that report errors through 'on_tool_event'
    should pass False to keep the console clean.

    'tool_timeout_s' overrides the configured per-call timeout; a call that
    exceeds it is detached and the model is told it may keep working.

    The reply's tool markup is fully scanned before anything executes: a
    structurally broken block (one that was never closed) aborts the whole
    reply, and ParseOutcome.structural_error carries the reason while no
    call runs. When the structure is sound, every block is processed in
    reply order: content that is empty or not valid JSON, or that fails the
    single-key {tool_name: {arguments}} shape, becomes an output entry with
    "error": True and never dispatches; well-formed calls run through the
    timeout machinery. Returns a ParseOutcome.
    """
    outputs = []
    if tool_timeout_s is None:
        tool_timeout_s = load_tool_call_timeout()
    logger = get_logger()

    blocks, structural_error = _scan_tool_blocks(text)
    if structural_error is not None:
        logger.warning("Structurally invalid tool markup: %s", structural_error)
        return ParseOutcome(structural_error, [])

    def _record_never_ran(entry_name: str, entry_signature: str, output: str):
        """Record a call that never reached dispatch, with its error event."""
        if echo_errors:
            print(f"  ! ({output[:80]})")
        if on_tool_event is not None:
            on_tool_event("error", entry_name, entry_signature, output=output)
        outputs.append(
            {
                "name": entry_name,
                "signature": entry_signature,
                "output": output,
                "error": True,
            }
        )

    for block in blocks:
        if block.error is not None:
            _record_never_ran("unknown", build_signature("unknown", {}), block.error)
            continue

        tool = block.payload

        if not isinstance(tool, dict):
            output = (
                "A tool call must be a single JSON object whose key is the"
                f" tool name and whose value is the arguments object; got"
                f" {_describe_json_shape(tool)}."
            )
            _record_never_ran("unknown", build_signature("unknown", {}), output)
            continue

        if not tool:
            output = (
                "A tool call must have exactly one top-level key (the tool"
                " name); got an empty object."
            )
            _record_never_ran("unknown", build_signature("unknown", {}), output)
            continue

        if len(tool) > 1:
            keys = ", ".join(repr(key) for key in list(tool)[:4])
            output = (
                "A tool call must have exactly one top-level key (the tool"
                f" name); got {len(tool)}: {keys}."
            )
            _record_never_ran("unknown", build_signature("unknown", {}), output)
            continue

        tool_name = next(iter(tool))
        tool_arguments = tool[tool_name]

        if not isinstance(tool_arguments, dict):
            output = (
                f"The arguments of tool '{tool_name}' must be a JSON object"
                f" mapping argument names to values; got"
                f" {_describe_json_shape(tool_arguments)}."
            )
            _record_never_ran(tool_name, build_signature(tool_name, {}), output)
            continue

        signature = build_signature(tool_name, tool_arguments)

        if tool_name not in TOOL_REGISTRY:
            output = (
                f"Unknown tool '{tool_name}'. Available tools:"
                f" {', '.join(sorted(TOOL_REGISTRY))}"
            )
            logger.warning(output)
            if echo_errors:
                print(f"  ! ({output})")
            if on_tool_event is not None:
                on_tool_event("started", tool_name, signature)
                on_tool_event("error", tool_name, signature, output=output)
                on_tool_event("finished", tool_name, signature, output)
            outputs.append(
                {
                    "name": tool_name,
                    "signature": signature,
                    "output": output,
                    "error": True,
                }
            )
            continue

        if on_tool_event is not None:
            on_tool_event("started", tool_name, signature)

        try:
            output, detached = execute_tool_with_timeout(
                tool_name, tool_arguments, tool_timeout_s
            )
        except Exception as exc:
            if echo_errors:
                print(f"  ! (Tool execution failed: {str(exc)[:64]})")
            output = f"Tool execution error: {exc}"
            detached = False
            if on_tool_event is not None:
                on_tool_event("error", tool_name, signature, output=output)

        if detached:
            if echo_errors:
                print(f"  ! (Tool call detached after {tool_timeout_s} seconds)")
            logger.warning(
                "Tool call detached after %s seconds: %s", tool_timeout_s, signature
            )
            if on_tool_event is not None:
                on_tool_event("error", tool_name, signature, output=output)

        if on_tool_event is not None:
            on_tool_event("finished", tool_name, signature, output)

        outputs.append({"name": tool_name, "signature": signature, "output": output})

    return ParseOutcome(None, outputs)
