"""Tool call parsing and execution for agent responses under the JSON protocol.

Under the Syna v5 protocol, the model communicates exclusively via JSON. Every
reply must be either a single tool call object {"tool_name": {...args}} or a
non-empty array of such objects. Markdown fences are tolerated only as a single
outer wrapper. Free-form prose and simulated harness blocks are rejected before
dispatch.
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
CONTENT_EXCERPT_CHARS = 300
DETAIL_RAW_CAP_CHARS = 6000

WRAPPER_FENCE_PATTERN = re.compile(
    r"^```[A-Za-z0-9_-]*\s*\n?(.*?)\n?\s*```$", re.DOTALL
)


@dataclass(frozen=True)
class ParseOutcome:
    """Result of parsing and executing one reply's tool calls.

    'structural_error' is set when the reply fails protocol validation
    (invalid JSON, wrong shape, respond mixed with other calls, or invalid
    wrappers), in which case nothing is executed. 'outputs' carries one entry
    per processed call in reply order; entries for calls that never reached
    dispatch carry "error": True.
    """

    structural_error: str | None
    outputs: list[dict]


def _strip_leading_think_block(text: str) -> str:
    """Strip one leading <think>...</think> block if present."""
    stripped = text.strip()
    if stripped.startswith("<think>"):
        end_idx = stripped.find("</think>")
        if end_idx != -1:
            return stripped[end_idx + len("</think>") :].strip()
    return stripped


def _unwrap_single_code_fence(text: str) -> tuple[str, str | None]:
    """Strip exactly one markdown code fence wrapper if present.

    Returns a tuple of (inner_content, error_message).
    """
    if not text.startswith("```"):
        if text.endswith("```"):
            return "", "Malformed response ending with an unopened code fence."
        return text, None

    match = WRAPPER_FENCE_PATTERN.match(text)
    if not match:
        return "", "Unclosed or malformed markdown code fence wrapper."

    inner = match.group(1).strip()
    if "```" in inner:
        return (
            "",
            "Multiple code fence blocks are not permitted. Provide a single"
            " JSON payload (an object or an array of objects).",
        )
    return inner, None


def _describe_json_shape(value: Any) -> str:
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


def _json_type_name(value: Any) -> str:
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


def _simple_type_match(value: Any, annotation: str) -> bool | None:
    """Match a JSON value against one simple type annotation."""
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


def _argument_type_error(
    tool_name: str, arg_name: str, value: Any, annotation: str, signature: str
) -> str | None:
    """Return a type-mismatch error for one argument, or None when valid."""
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


def _signature_error(tool_name: str, tool_arguments: dict) -> str | None:
    """Return a descriptive error when a call violates the tool's signature."""
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


def build_signature(tool_name: str, tool_arguments: dict) -> str:
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


def build_detached_call_notice(
    tool_name: str, tool_arguments: dict, timeout_s: float
) -> str:
    """Compose the tool result reported to the model for a detached call."""
    return (
        f"Tool call timed out after {timeout_s} seconds:"
        f" {build_signature(tool_name, tool_arguments)}."
        " The call was not aborted: it is still running in the background and"
        " its result will be discarded. Continue working without waiting for"
        " it. If the call runs a process inside the container, you can locate"
        " and terminate it with another execute_command (e.g. ps / kill)."
    )


def execute_tool_with_timeout(
    tool_name: str, tool_arguments: dict, timeout_s: float
) -> tuple[Any, bool]:
    """Run a tool in a worker thread and return an (output, detached) tuple."""
    tool_fn = TOOL_REGISTRY[tool_name]
    container = get_registered_container()
    finished = threading.Event()
    outcome: dict[str, Any] = {}

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


def _validate_raw_payload(text: str) -> tuple[Any, str | None]:
    """Validate and decode raw model text into a JSON data structure.

    Returns (decoded_value, error_message).
    """
    clean_text = text.lstrip("\ufeff")
    clean_text = _strip_leading_think_block(clean_text)
    clean_text = clean_text.strip()
    if not clean_text:
        return (
            None,
            "Empty response. Your reply must be a valid JSON tool call or"
            " array of tool calls.",
        )

    inner_json, unwrap_error = _unwrap_single_code_fence(clean_text)
    if unwrap_error is not None:
        return None, unwrap_error

    try:
        data = json.loads(inner_json)
    except json.JSONDecodeError as exc:
        excerpt = clean_text[:CONTENT_EXCERPT_CHARS]
        return (
            None,
            f"The response could not be parsed as JSON: {exc}. Received content"
            f" excerpt: {excerpt!r}",
        )

    return data, None


def _extract_tool_calls(data: Any) -> tuple[list[tuple[str, dict, str]], str | None]:
    """Validate the JSON shape and extract a list of (tool_name, arguments, raw_json) tuples."""
    if not isinstance(data, (dict, list)):
        return (
            [],
            "A response must be a JSON object or array of objects; got"
            f" {_describe_json_shape(data)}.",
        )

    items: list[Any]
    if isinstance(data, list):
        if not data:
            return (
                [],
                "The response array is empty. Every reply must contain at least"
                " one tool call.",
            )
        items = data
    else:
        items = [data]

    calls: list[tuple[str, dict, str]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return (
                [],
                f"Item at index {index} in the tool call array must be a JSON"
                f" object; got {_describe_json_shape(item)}.",
            )
        if not item:
            return (
                [],
                f"Tool call object at index {index} is empty; each call must"
                " have exactly one key (the tool name).",
            )
        if len(item) > 1:
            keys = ", ".join(repr(key) for key in list(item)[:4])
            return (
                [],
                f"Tool call object at index {index} must have exactly one key"
                f" (the tool name); got {len(item)}: {keys}.",
            )
        tool_name = next(iter(item))
        tool_arguments = item[tool_name]
        if not isinstance(tool_arguments, dict):
            return (
                [],
                f"The arguments for tool '{tool_name}' at index {index} must"
                f" be a JSON object; got {_describe_json_shape(tool_arguments)}.",
            )
        raw_json = json.dumps(item)[:DETAIL_RAW_CAP_CHARS]
        calls.append((tool_name, tool_arguments, raw_json))

    return calls, None


def parse_and_execute_tools(
    text: str,
    on_tool_event: Callable | None = None,
    echo_errors: bool = True,
    tool_timeout_s: float | None = None,
) -> ParseOutcome:
    """Parse pure JSON tool invocations and execute them sequentially.

    Parameters:
        text: Raw response string from the model.
        on_tool_event: Optional callback receiving tool progress events.
        echo_errors: Whether to echo failure summaries to standard output.
        tool_timeout_s: Execution timeout per individual tool call in seconds.
    """
    if tool_timeout_s is None:
        tool_timeout_s = load_tool_call_timeout()
    logger = get_logger()

    data, validation_error = _validate_raw_payload(text)
    if validation_error is not None:
        logger.warning("Response failed validation: %s", validation_error)
        return ParseOutcome(validation_error, [])

    calls, shape_error = _extract_tool_calls(data)
    if shape_error is not None:
        logger.warning("Response shape invalid: %s", shape_error)
        return ParseOutcome(shape_error, [])

    has_respond = any(tool_name == "respond" for tool_name, _, _ in calls)
    if has_respond and len(calls) > 1:
        respond_error = (
            "The 'respond' tool cannot be combined with other tool calls in the"
            " same reply. 'respond' must be alone in its reply. Nothing was"
            " executed."
        )
        logger.warning(respond_error)
        return ParseOutcome(respond_error, [])

    outputs: list[dict] = []

    def _record_never_ran(
        entry_name: str, entry_signature: str, output: str, raw: str
    ):
        if echo_errors:
            print(f"  ! ({output[:80]})")
        if on_tool_event is not None:
            on_tool_event("error", entry_name, entry_signature, output=output, raw=raw)
        outputs.append(
            {
                "name": entry_name,
                "signature": entry_signature,
                "output": output,
                "error": True,
            }
        )

    for tool_name, tool_arguments, raw in calls:
        signature = build_signature(tool_name, tool_arguments)

        if tool_name not in TOOL_REGISTRY:
            output = (
                f"Attempted to call a tool that is not registered: '{tool_name}'."
                f" Registered tools: {', '.join(sorted(TOOL_REGISTRY))}"
            )
            logger.warning(output)
            if echo_errors:
                print(f"  ! ({output})")
            if on_tool_event is not None:
                on_tool_event(
                    "started",
                    tool_name,
                    signature,
                    raw=raw,
                    arguments=tool_arguments,
                )
                on_tool_event(
                    "error",
                    tool_name,
                    signature,
                    output=output,
                    raw=raw,
                    arguments=tool_arguments,
                )
                on_tool_event(
                    "finished",
                    tool_name,
                    signature,
                    output,
                    raw=raw,
                    arguments=tool_arguments,
                )
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
                on_tool_event(
                    "started",
                    tool_name,
                    signature,
                    raw=raw,
                    arguments=tool_arguments,
                )
                on_tool_event(
                    "error",
                    tool_name,
                    signature,
                    output=argument_error,
                    raw=raw,
                    arguments=tool_arguments,
                )
                on_tool_event(
                    "finished",
                    tool_name,
                    signature,
                    argument_error,
                    raw=raw,
                    arguments=tool_arguments,
                )
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
            on_tool_event(
                "started",
                tool_name,
                signature,
                raw=raw,
                arguments=tool_arguments,
            )

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
                on_tool_event(
                    "error",
                    tool_name,
                    signature,
                    output=output,
                    raw=raw,
                    arguments=tool_arguments,
                )

        if detached:
            if echo_errors:
                print(f"  ! (Tool call detached after {tool_timeout_s} seconds)")
            logger.warning(
                "Tool call detached after %s seconds: %s",
                tool_timeout_s,
                signature,
            )
            if on_tool_event is not None:
                on_tool_event(
                    "error",
                    tool_name,
                    signature,
                    output=output,
                    raw=raw,
                    arguments=tool_arguments,
                )

        if on_tool_event is not None:
            on_tool_event(
                "finished",
                tool_name,
                signature,
                output,
                raw=raw,
                arguments=tool_arguments,
            )

        outputs.append({"name": tool_name, "signature": signature, "output": output})

    return ParseOutcome(None, outputs)


parse_and_execute_reply = parse_and_execute_tools
