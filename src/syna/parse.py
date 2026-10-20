"""Tool call parsing and execution for agent responses under the JSON protocol.

Under the Syna v5 protocol the model communicates via JSON: every reply is a
single tool call object or a non-empty array of them, optionally inside one
code fence or after one <think> block. The parser decodes only the FIRST JSON
value; any text around it is discarded and reported back to the model as a
warning, so a fabricated tail can never execute. Validation happens entirely
before dispatch: on a structural error nothing runs.

Execution is sequential. The first failing call (bad signature, raised
exception, timeout, or non-zero command exit) stops the batch; the remaining
calls are reported as "skipped". Every result is labeled with its position,
tool name, status, and — when available — exit code and the pod path
(`/tool_outputs/`) holding the full output.
"""

import json
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .config import (
    get_logger,
    get_project_root,
    load_output_limit_chars,
    load_tool_call_timeout_s,
)
from .output import bounded_output
from .state import (
    get_container,
    get_registered_container,
    next_call_index,
    pop_last_exit_code,
    register_container,
    set_call_output_path,
    unregister_container,
)
from .tools import TOOL_REGISTRY

CONTENT_EXCERPT_CHARS = 300

TIMEOUT_EXIT_CODES = {124, 137}

TOOLS_PERSISTED_IN_POD = {"execute_command"}
SKIPPED_CALL_NOTICE = "Skipped: an earlier call in this reply failed."


class ReplyFormatError(ValueError):
    """Raised when a model reply cannot be turned into tool calls."""


@dataclass(frozen=True)
class ParseOutcome:
    """Result of parsing and executing one reply's tool calls.

    'structural_error' is set when the reply fails protocol validation
    (invalid JSON, wrong shape, or respond combined with non-think calls), in
    which case nothing is executed. 'outputs' carries one entry per processed
    call in reply order, including skipped calls; entries for calls that
    never reached dispatch carry "error": True. 'warnings' lists protocol
    tolerances (ignored surrounding text) that the harness surfaces to the
    model as harness notices. 'think_signatures' lists the signature of every
    think call in the reply; think calls produce no output row, so an empty
    'outputs' together with non-empty 'think_signatures' identifies a
    think-only reply.
    """

    structural_error: str | None
    outputs: list[dict]
    warnings: list[str]
    think_signatures: list[str] = field(default_factory=list)


def _strip_leading_think_block(text: str) -> str:
    """Strip one leading <think>...</think> block if present."""
    stripped = text.strip()
    if stripped.startswith("<think>"):
        end_idx = stripped.find("</think>")
        if end_idx != -1:
            return stripped[end_idx + len("</think>") :].strip()
    return stripped


def _strip_complete_code_fence(text: str) -> str:
    """Remove one surrounding Markdown code fence if both ends are present."""
    stripped = text.strip()
    if not (
        stripped.startswith("```") and stripped.endswith("```") and "\n" in stripped
    ):
        return stripped
    body = stripped.split("\n", 1)[1]
    return body[: body.rfind("```")].rstrip()


def _decode_reply(text: str) -> tuple[object, list[str]]:
    """Decode the first JSON value in a reply and report discarded text.

    Returns (value, warnings). Raises ReplyFormatError when no decodable JSON
    value exists; nothing is ever executed on error.
    """
    clean_text = text.lstrip("\ufeff")
    clean_text = _strip_leading_think_block(clean_text)
    clean_text = _strip_complete_code_fence(clean_text).strip()
    if not clean_text:
        raise ReplyFormatError(
            "Empty response. Your reply must be a valid JSON tool call or"
            " array of tool calls."
        )

    start_positions = [
        position
        for position in (clean_text.find("["), clean_text.find("{"))
        if position != -1
    ]
    if not start_positions:
        # No object or array; still decode bare primitives ("42", '"text"')
        # so shape validation can report them precisely.
        try:
            value, end = json.JSONDecoder(strict=False).raw_decode(clean_text)
        except json.JSONDecodeError as exc:
            excerpt = clean_text[:CONTENT_EXCERPT_CHARS]
            raise ReplyFormatError(
                "No JSON value found in the response. Reply with a JSON array"
                f" of tool calls only. Received content excerpt: {excerpt!r}"
            ) from exc
        warnings = []
        if clean_text[end:].strip():
            warnings.append("Text after the JSON value was discarded.")
        return value, warnings

    warnings: list[str] = []
    start = min(start_positions)
    if clean_text[:start].strip():
        warnings.append("Text before the JSON value was ignored.")
    try:
        value, end = json.JSONDecoder(strict=False).raw_decode(clean_text, start)
    except json.JSONDecodeError as exc:
        excerpt = clean_text[:CONTENT_EXCERPT_CHARS]
        raise ReplyFormatError(
            f"The response could not be parsed as JSON: {exc}. Nothing was"
            f" executed. Received content excerpt: {excerpt!r}"
        ) from exc
    if clean_text[end:].strip():
        warnings.append("Text after the JSON value was discarded.")
    return value, warnings


def _describe_json_shape(value: object) -> str:
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


def _json_type_name(value: object) -> str:
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


def _simple_type_match(value: object, annotation: str) -> bool | None:
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
    tool_name: str, arg_name: str, value: object, annotation: str, signature: str
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
    return load_tool_call_timeout_s()


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
    tool_name: str,
    tool_arguments: dict,
    timeout_s: float,
    output_path: str | None = None,
) -> tuple[object, bool, int | None]:
    """Run a tool in a worker thread and return (output, detached, exit_code).

    The worker inherits the calling thread's container and the call's output
    path (both are thread-locals), and reports back the exit code recorded by
    the tool, if any.
    """
    tool_fn = TOOL_REGISTRY[tool_name]
    container = get_registered_container()
    finished = threading.Event()
    outcome: dict = {}

    def worker():
        if container is not None:
            register_container(container)
        set_call_output_path(output_path)
        try:
            outcome["output"] = tool_fn(**tool_arguments)
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            outcome["exit_code"] = pop_last_exit_code()
            set_call_output_path(None)
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
        return outcome["output"], False, outcome.get("exit_code")

    return build_detached_call_notice(tool_name, tool_arguments, timeout_s), True, None


def _persist_tool_output(
    output_text: str, saved_to: str | None, persist_outputs: bool
) -> str | None:
    """Save a host-side tool output into the pod at its call output path.

    Uses the calling thread's container when one is bound (web sessions);
    with `persist_outputs` (CLI sessions) it falls back to the shared pod.
    The saved path is what the model sees as `saved_to`.
    """
    container = get_registered_container()
    if container is None and persist_outputs:
        container = get_container()
    if container is None or saved_to is None:
        return None
    try:
        container.exec(["bash", "-c", "mkdir -p /tool_outputs"])
        file_name = PurePosixPath(saved_to).name
        with tempfile.TemporaryDirectory(prefix="syna-persist-") as temp_dir:
            host_file = Path(temp_dir, file_name)
            host_file.write_text(output_text, encoding="utf-8", errors="replace")
            container.copy_to(str(host_file), "/tool_outputs")
        return saved_to
    except Exception:
        get_logger().exception("Failed to persist tool output to %s", saved_to)
        return None


def _extract_tool_calls(data: object) -> tuple[list[tuple[str, dict, str]], str | None]:
    """Validate the JSON shape and extract a list of (tool_name, arguments, raw_json) tuples."""
    if not isinstance(data, (dict, list)):
        return (
            [],
            "A response must be a JSON object or array of objects; got"
            f" {_describe_json_shape(data)}.",
        )

    items: list[object]
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
        raw_json = json.dumps(item)[:6000]
        calls.append((tool_name, tool_arguments, raw_json))

    return calls, None


def _enforce_respond_placement(calls: list[tuple[str, dict, str]]) -> str | None:
    """Reject replies where respond appears twice or beside non-think calls."""
    names = [tool_name for tool_name, _, _ in calls]
    if names.count("respond") > 1:
        return "'respond' may only appear once in a reply. Nothing was executed."
    if "respond" in names and set(names) - {"respond", "think"}:
        return "'respond' may only be combined with 'think'. Nothing was executed."
    return None


def parse_and_execute_tools(
    text: str,
    on_tool_event: Callable | None = None,
    echo_errors: bool = True,
    tool_timeout_s: float | None = None,
    persist_outputs: bool = False,
) -> ParseOutcome:
    """Parse JSON tool invocations and execute them sequentially.

    The first failing call stops the batch; later calls are recorded as
    skipped. Parameters:
        text: Raw response string from the model.
        on_tool_event: Optional callback receiving tool progress events.
        echo_errors: Whether to echo failure summaries to standard output.
        tool_timeout_s: Execution timeout per individual tool call in seconds.
        persist_outputs: Whether host-side tool outputs may be saved into a
            lazily created shared pod (CLI). Web sessions persist through the
            thread's bound container regardless.
    """
    if tool_timeout_s is None:
        tool_timeout_s = load_tool_call_timeout()
    logger = get_logger()
    output_limit = load_output_limit_chars()

    try:
        data, warnings = _decode_reply(text)
    except ReplyFormatError as exc:
        structural_error = str(exc)
        logger.warning("Response failed validation: %s", structural_error)
        return ParseOutcome(structural_error, [], [])

    calls, shape_error = _extract_tool_calls(data)
    if shape_error is not None:
        structural_error = " ".join([*warnings, shape_error]).strip()
        logger.warning("Response shape invalid: %s", shape_error)
        return ParseOutcome(structural_error, [], warnings)

    respond_error = _enforce_respond_placement(calls)
    if respond_error is not None:
        logger.warning(respond_error)
        return ParseOutcome(respond_error, [], warnings)

    outputs: list[dict] = []
    think_signatures: list[str] = []
    failure_occurred = False

    def emit(
        status, name, call_signature, output=None, raw="", arguments=None, **extra
    ):
        """Forward one tool progress event to the session's listener."""
        if on_tool_event is not None:
            on_tool_event(
                status,
                name,
                call_signature,
                output=output,
                raw=raw,
                arguments=arguments,
                **extra,
            )

    for position, (tool_name, tool_arguments, raw) in enumerate(calls, start=1):
        signature = build_signature(tool_name, tool_arguments)

        if failure_occurred:
            emit("started", tool_name, signature, raw=raw, arguments=tool_arguments)
            emit(
                "skipped",
                tool_name,
                signature,
                output=SKIPPED_CALL_NOTICE,
                raw=raw,
                arguments=tool_arguments,
            )
            outputs.append(
                {
                    "call": position,
                    "name": tool_name,
                    "signature": signature,
                    "output": SKIPPED_CALL_NOTICE,
                    "status": "skipped",
                    "error": True,
                }
            )
            continue

        if tool_name not in TOOL_REGISTRY:
            output = (
                f"Attempted to call a tool that is not registered: '{tool_name}'."
                f" Registered tools: {', '.join(sorted(TOOL_REGISTRY))}"
            )
            logger.warning(output)
            if echo_errors:
                print(f"  ! ({output})")
            emit("started", tool_name, signature, raw=raw, arguments=tool_arguments)
            emit(
                "error",
                tool_name,
                signature,
                output=output,
                raw=raw,
                arguments=tool_arguments,
            )
            emit(
                "finished",
                tool_name,
                signature,
                output,
                raw=raw,
                arguments=tool_arguments,
                failed=True,
            )
            outputs.append(
                {
                    "call": position,
                    "name": tool_name,
                    "signature": signature,
                    "output": output,
                    "status": "error",
                    "error": True,
                }
            )
            failure_occurred = True
            continue

        argument_error = _signature_error(tool_name, tool_arguments)
        if argument_error is not None:
            logger.warning(argument_error)
            if echo_errors:
                print(f"  ! ({argument_error[:80]})")
            emit("started", tool_name, signature, raw=raw, arguments=tool_arguments)
            emit(
                "error",
                tool_name,
                signature,
                output=argument_error,
                raw=raw,
                arguments=tool_arguments,
            )
            emit(
                "finished",
                tool_name,
                signature,
                argument_error,
                raw=raw,
                arguments=tool_arguments,
                failed=True,
            )
            outputs.append(
                {
                    "call": position,
                    "name": tool_name,
                    "signature": signature,
                    "output": argument_error,
                    "status": "error",
                    "error": True,
                }
            )
            failure_occurred = True
            continue

        emit("started", tool_name, signature, raw=raw, arguments=tool_arguments)

        call_output_path = f"/tool_outputs/{next_call_index():04d}_{tool_name}.txt"
        try:
            output, detached, exit_code = execute_tool_with_timeout(
                tool_name, tool_arguments, tool_timeout_s, call_output_path
            )
        except Exception as exc:
            if echo_errors:
                print(f"  ! (Tool execution failed: {str(exc)[:64]})")
            output = f"Tool execution error: {exc}"
            detached = False
            exit_code = None
            status = "error"
            failure_occurred = True
        else:
            if detached:
                status = "timeout"
                failure_occurred = True
            elif exit_code is not None and exit_code != 0:
                status = "timeout" if exit_code in TIMEOUT_EXIT_CODES else "error"
                failure_occurred = True
            else:
                status = "ok"

        if tool_name == "think":
            think_signatures.append(signature)
            emit(
                "finished",
                tool_name,
                signature,
                output,
                raw=raw,
                arguments=tool_arguments,
            )
            continue

        if tool_name in TOOLS_PERSISTED_IN_POD:
            saved_to = call_output_path if exit_code is not None else None
        else:
            saved_to = _persist_tool_output(
                str(output), call_output_path, persist_outputs
            )

        failed_run = status in ("error", "timeout")
        model_output = bounded_output(str(output), output_limit, saved_to)
        if failed_run and echo_errors:
            print(f"  ! ({status}: {str(output)[:80]})")

        if failed_run:
            emit(
                "error",
                tool_name,
                signature,
                output=model_output,
                raw=raw,
                arguments=tool_arguments,
            )
        emit(
            "finished",
            tool_name,
            signature,
            model_output,
            raw=raw,
            arguments=tool_arguments,
            failed=failed_run,
            **({"structured_output": output} if isinstance(output, dict) else {}),
        )
        outputs.append(
            {
                "call": position,
                "name": tool_name,
                "signature": signature,
                "output": model_output,
                "status": status,
                **({"exit_code": exit_code} if exit_code is not None else {}),
                **({"saved_to": saved_to} if saved_to else {}),
            }
        )

    return ParseOutcome(None, outputs, warnings, think_signatures)


parse_and_execute_reply = parse_and_execute_tools
