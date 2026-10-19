"""Tool call parsing and execution for agent responses.

The parser is a strict pre-parse gate: a reply's tool markup is fully
scanned before anything executes. A tool block is recognized by the opener
pattern ```tool — a run of three or more backticks, the 'tool' tag, then
whitespace or the brace of an inline payload — at ANY position in the
reply, so a call glued to the end of a sentence or written inline on one
line is parsed and dispatched like any other. Line-start fences with any
other info open inert prose regions whose content can never execute.

Structurally broken markup (a block whose JSON decodes but that was never
closed) aborts the whole reply with a structural error. Every other
mistake — empty or unparsable contents, bad shapes, unregistered tools,
signature violations — becomes a descriptive error entry that never
dispatches, and scanning continues with the rest of the reply.
"""

import json
import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable

from .config import get_logger, get_project_root, load_config
from .state import get_registered_container, register_container, unregister_container
from .tools import TOOL_REGISTRY

DEFAULT_TOOL_CALL_TIMEOUT_S = 60

TOOL_OPEN = "```tool"
TOOL_INFO = "tool"
WHITESPACE = " \t\n\r"
FENCE_LINE_RE = re.compile(r"^ {0,3}(`{3,})[ \t]*(.*)$", re.MULTILINE)
BLOCK_CONTENTS_EXCERPT_CHARS = 120


@dataclass(frozen=True)
class ParseOutcome:
    """Result of parsing and executing one reply's tool calls.

    'structural_error' is set only when a ```tool block's JSON decodes but
    the block was never closed: nothing from that reply executes and the
    message names the problem (line number plus the exact parsed call) for
    the corrective feedback loop. 'outputs' carries one entry per parsed
    call in reply order; entries for calls that never reached dispatch
    carry "error": True.
    """

    structural_error: str | None
    outputs: list[dict]


@dataclass(frozen=True)
class _ParsedBlock:
    """One scanned ```tool block, before validation and dispatch.

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

    Runs of one or two backticks (inline code in surrounding text) can
    never close a block and are skipped. Returns the run's start position,
    or None when no such run exists before the end of the text.
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


def _find_tool_opener(text: str, cursor: int) -> int | None:
    """Find the next ```tool opener at/after 'cursor', at any position.

    An opener is a backtick run followed by the 'tool' tag and a boundary:
    whitespace, the brace of an inline JSON payload, or the end of the
    reply. Candidates without a boundary (```tooly, ```tool:, ```tool"...")
    are inert text, not calls, and are skipped.
    """
    while True:
        at = text.find(TOOL_OPEN, cursor)
        if at == -1:
            return None
        after = at + len(TOOL_OPEN)
        if after >= len(text) or text[after] in WHITESPACE or text[after] == "{":
            return at
        cursor = at + 1


def _is_tool_info(info: str) -> bool:
    """Return True when a fence line's info string opens a tool block."""
    return info.startswith(TOOL_INFO) and (
        len(info) == len(TOOL_INFO) or info[len(TOOL_INFO)] in WHITESPACE + "{"
    )


def _find_prose_opener(text: str, cursor: int) -> tuple[int, int] | None:
    """Find the next line-start fence with a non-tool info at/after 'cursor'.

    These open inert prose regions: tool markup inside them can never
    execute. Tool-info fences are skipped here because they belong to the
    tool scan.
    """
    pos = cursor
    while True:
        fence = FENCE_LINE_RE.search(text, pos)
        if fence is None:
            return None
        if not _is_tool_info(fence.group(2)):
            return fence.span(1)
        pos = _after_line(text, fence.start(1))


def _skip_prose_block(text: str, after_opener: int) -> int:
    """Skip an ordinary prose block, returning the resume position.

    The block ends at the next line-start bare fence. Balance is not
    enforced: an unclosed prose block silently swallows the rest of the
    reply, so stray fences can never turn into tool calls.
    """
    pos = _after_line(text, after_opener)
    while True:
        fence = FENCE_LINE_RE.search(text, pos)
        if fence is None:
            return len(text)
        if fence.group(2).strip() == "":
            return _after_line(text, fence.start(1))
        pos = _after_line(text, fence.start(1))


def _resume_after_closer(text: str, boundary: int, logger) -> int:
    """Consume a block's closing backtick run, logging a glued second call.

    Only the run itself is consumed so that further content on the same
    line (including another tool opener) is re-scanned normally. When the
    text right after the run is another 'tool' tag, that opener's backticks
    were consumed as this block's closer and its call is lost — logged to
    the dev log, since a markdown parser drops it the same way.
    """
    run_end = boundary
    while run_end < len(text) and text[run_end] == "`":
        run_end += 1
    if text.startswith(TOOL_INFO, run_end):
        after = run_end + len(TOOL_INFO)
        if after >= len(text) or text[after] in WHITESPACE or text[after] == "{":
            logger.warning(
                "A tool block's closing fence on line %d was followed by"
                " another tool opener; that second call was dropped"
                " (markdown-parser semantics).",
                _line_no(text, boundary),
            )
    return run_end


def _scan_tool_block(text: str, decoder, content_start: int, line_no: int, logger):
    """Scan one tool block's content, starting after its 'tool' info token.

    The JSON decoder is the string-aware tokenizer: backticks inside JSON
    strings are consumed as data, and the first backtick run after the
    decoded value closes the block, whatever its position or the rest of
    its line carries. A block whose JSON decodes but that has no closing
    run was never closed — a structural error carrying the exact parsed
    call. A block whose content does not decode is a content-level error
    entry, never a structural error: the scan resumes inside the block so
    later calls in the same reply still parse.

    Returns (payload, error, resume, structural_error).
    """
    try:
        payload, json_end = decoder.raw_decode(text, content_start)
    except json.JSONDecodeError as exc:
        peek = _find_closing_run(text, content_start)
        if peek is not None:
            empty = peek == content_start
        else:
            empty = content_start >= len(text)
        if empty:
            error = (
                "Empty tool block. A tool block must contain one JSON object"
                ' of the form: {"tool_name": {"argument": "value"}}.'
            )
        else:
            excerpt = text[content_start : content_start + BLOCK_CONTENTS_EXCERPT_CHARS]
            error = (
                f"The contents of a tool block could not be parsed as JSON:"
                f" {exc}. Block contents: {excerpt!r}"
            )
        if peek is None:
            return None, error, len(text), None
        if _find_tool_opener(text, peek) == peek:
            return None, error, content_start, None
        return None, error, _resume_after_closer(text, peek, logger), None
    boundary = _find_closing_run(text, json_end)
    if boundary is None:
        return (
            None,
            None,
            None,
            (
                f"A ```tool block starting on line {line_no} was never closed;"
                f" it contained {_describe_block_content(payload)}"
            ),
        )
    resume = _resume_after_closer(text, boundary, logger)
    return payload, None, resume, None


def _scan_tool_blocks(text: str):
    """Scan a reply into parsed ```tool blocks plus an optional structural error.

    The scan walks the reply once; whichever candidate appears first wins:
    a ```tool opener (any position) starts a tool block, a line-start fence
    with any other info opens an inert prose region. Mid-line backtick runs
    without the tool tag are inert content.

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
        tool_at = _find_tool_opener(text, pos)
        prose = _find_prose_opener(text, pos)
        if tool_at is None and prose is None:
            return blocks, None
        if prose is not None and (tool_at is None or prose[0] < tool_at):
            pos = _skip_prose_block(text, prose[1])
            continue
        content_start = _skip_whitespace(text, tool_at + len(TOOL_OPEN))
        payload, error, resume, structural = _scan_tool_block(
            text, decoder, content_start, _line_no(text, tool_at), logger
        )
        if structural is not None:
            return blocks, structural
        blocks.append(_ParsedBlock(_line_no(text, tool_at), payload, error))
        pos = resume


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


@lru_cache(maxsize=1)
def _tool_signatures() -> dict[str, dict]:
    """Return the parsed signature metadata of every tool, keyed by name."""
    from .inspect_module import get_function_metadata

    tools_path = get_project_root() / "src" / "syna" / "tools.py"
    return {entry["name"]: entry for entry in get_function_metadata(tools_path)}


def _render_signature(tool_name: str, entry: dict) -> str:
    """Render a tool's signature as a single informative line."""
    parameters = ", ".join(
        f"{argument['name']}: {argument['type']}"
        + (f" = {argument['default']}" if argument["has_default"] else "")
        for argument in entry["arguments"]
    )
    return f"{tool_name}({parameters})"


def _json_type_name(value) -> str:
    """Name a JSON value's type for argument type errors."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def _simple_type_match(value, annotation: str) -> bool | None:
    """Match a JSON value against one simple type annotation.

    Returns True/False for annotations the parser understands (str, int,
    float, bool, list, dict and their parametrized forms), or None when the
    annotation is too complex to check (Any, unknown names) so the argument
    passes through untouched.
    """
    if annotation in ("Any", "...", ""):
        return None
    if annotation == "str":
        return isinstance(value, str)
    if annotation == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if annotation == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if annotation == "bool":
        return isinstance(value, bool)
    if annotation == "list" or annotation.startswith("list["):
        return isinstance(value, list)
    if annotation == "dict" or annotation.startswith("dict["):
        return isinstance(value, dict)
    return None


def _argument_type_error(tool_name, arg_name, value, annotation, signature):
    """Return a type-mismatch error for one argument, or None when it is fine.

    Annotations may be unions ('list[str] | None'); null is accepted when
    None is one of the alternatives. When no alternative can be checked the
    argument is allowed, so complex annotations never produce false errors.
    """
    alternatives = [part.strip() for part in annotation.split("|")]
    if value is None:
        if "None" in alternatives:
            return None
        return (
            f"Argument '{arg_name}' of tool '{tool_name}' should be of type"
            f" {annotation}; got null. Signature: {signature}"
        )
    alternatives = [part for part in alternatives if part != "None"]
    results = [_simple_type_match(value, part) for part in alternatives]
    if any(result is True for result in results):
        return None
    if results and all(result is False for result in results):
        return (
            f"Argument '{arg_name}' of tool '{tool_name}' should be of type"
            f" {annotation}; got {_json_type_name(value)}."
            f" Signature: {signature}"
        )
    return None


def _signature_error(tool_name, tool_arguments) -> str | None:
    """Return a descriptive error when a call violates the tool's signature.

    Checks required arguments, unexpected arguments and simple type
    compatibility against the signature metadata extracted from the tools
    module source. Tools without metadata (e.g. test doubles) pass through
    unvalidated.
    """
    entry = _tool_signatures().get(tool_name)
    if entry is None:
        return None
    signature = _render_signature(tool_name, entry)
    declared = {argument["name"]: argument for argument in entry["arguments"]}
    missing = [
        argument["name"]
        for argument in entry["arguments"]
        if not argument["has_default"] and argument["name"] not in tool_arguments
    ]
    if missing:
        return (
            f"Missing required argument(s)"
            f" {', '.join(repr(name) for name in missing)}"
            f" for tool '{tool_name}'. Signature: {signature}"
        )
    unexpected = [name for name in tool_arguments if name not in declared]
    if unexpected:
        return (
            f"Unexpected argument(s) {', '.join(repr(name) for name in unexpected)}"
            f" for tool '{tool_name}'. Signature: {signature}"
        )
    for arg_name, value in tool_arguments.items():
        error = _argument_type_error(
            tool_name, arg_name, value, declared[arg_name]["type"], signature
        )
        if error is not None:
            return error
    return None


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
    block whose JSON decodes but that was never closed aborts the whole
    reply, and ParseOutcome.structural_error carries the reason while no
    call runs. When the structure is sound, every block is processed in
    reply order: content that is empty or not valid JSON, that fails the
    single-key {tool_name: {arguments}} shape, that names an unregistered
    tool, or that violates the tool's signature becomes an output entry
    with "error": True and never dispatches; well-formed calls run through
    the timeout machinery. Returns a ParseOutcome.
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
                "Tool block contains an empty JSON object; a tool call must"
                " have exactly one top-level key: the tool name (e.g."
                ' {"tool_name": {"argument": "value"}}).'
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
                f"Attempted to call a tool that is not registered:"
                f" '{tool_name}'. Registered tools:"
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

        argument_error = _signature_error(tool_name, tool_arguments)
        if argument_error is not None:
            logger.warning(argument_error)
            if echo_errors:
                print(f"  ! ({argument_error[:80]})")
            if on_tool_event is not None:
                on_tool_event("started", tool_name, signature)
                on_tool_event("error", tool_name, signature, output=argument_error)
                on_tool_event("finished", tool_name, signature, argument_error)
            outputs.append(
                {
                    "name": tool_name,
                    "signature": signature,
                    "output": argument_error,
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
