"""Tool call parsing and execution for agent responses under native tool calling.

Under native OpenRouter tool calling, the model returns structured tool_calls.
Each tool call carries a tool_call_id, function name, and JSON-encoded arguments.
The parser decodes and validates arguments before executing.

Execution is sequential. The first failing call (argument error, raised
exception, timeout, or non-zero command exit) stops the batch; the remaining
calls are reported as "skipped". Every call receives a result labeled with its
position, tool_call_id, tool name, status, and, when available, exit code and
the pod path (/tool_outputs/) holding the full output.

Timeouts are terminal, never detours: each call runs in a worker thread only
so the harness can enforce a strict deadline. When the deadline passes the
harness asks the call to stop through its cancel event and waits for it to
exit; the model never sees a result while the call is still running, and a
call that ignores the stop request is detached with an explicit hung-call
notice.
"""

import json
import shlex
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..config import (
    get_logger,
    load_max_cancel_wait_s,
    load_output_limit_chars,
    load_tool_call_timeout_s,
)
from ..interaction import get_responder, register_responder, unregister_responder
from ..pod_io import pod_copy_to, pod_exec
from .output import bounded_output
from ..state import (
    ToolCancelled,
    get_background_allegro,
    get_background_level,
    get_background_record,
    get_container,
    get_registered_container,
    next_call_index,
    pop_last_exit_code,
    register_container,
    set_background_allegro,
    set_background_level,
    set_background_record,
    set_call_output_path,
    set_cancel_event,
    set_last_exit_code,
    unregister_container,
)
from ..capabilities.tools import (
    COMMAND_TIMEOUT_EXIT_CODES,
    TOOL_REGISTRY,
    command_runtime_limit_s,
)
from ..capabilities.browser import browser_deadline_s
from ..capabilities.processes import wait_for_deadline_s
from ..capabilities.web_search import search_deadline_s
from ..capabilities.verification import (
    REVIEW_TOOL_NAME,
    current_conversation_log_provider,
    pop_conversation_log_provider,
    set_conversation_log_provider,
)

TIMEOUT_EXIT_CODES = COMMAND_TIMEOUT_EXIT_CODES
TOOLS_PERSISTED_IN_POD = {"execute_command"}
SKIPPED_CALL_NOTICE = "Skipped: an earlier call in this reply failed."


@dataclass(frozen=True)
class ParseOutcome:
    """Result of parsing and executing one reply's tool calls.

    'structural_error' is reserved for protocol-level errors.
    'outputs' carries one entry per processed call in reply order, including
    skipped calls; entries for calls that failed or were skipped carry "error": True.
    'warnings' lists protocol notices surfaced to the model.
    'think_signatures' lists the signature of every think call in the batch.
    """

    structural_error: str | None
    outputs: list[dict]
    warnings: list[str]
    think_signatures: list[str] = field(default_factory=list)


def _tool_signatures() -> dict[str, dict]:
    """Return the parsed signature metadata of every tool, keyed by name."""
    from .inspect_module import get_function_metadata

    tools_path = _tools_module_path()
    return {entry["name"]: entry for entry in get_function_metadata(tools_path)}


def _tools_module_path():
    """Return the filesystem path of the tools module."""
    from ..capabilities import tools

    return tools.__file__


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


def call_deadline_s(
    tool_name: str, tool_timeout_s: float | None, tool_arguments: dict | None = None
) -> float | None:
    """Return the per-call deadline in seconds, or None when untimed.

    Regular tools are bounded by tool_call_timeout_s (subject to the
    calling thread's Allegro scale). ask_user waits for the user by
    design and review_work is bounded by the model client's own timeout
    and retry policy, so neither gets a harness deadline. web_search
    and browser enforce their own phase budgets (browser launch cap,
    search or command budget); their deadlines are each tool's own
    worst-case composition: a bug guard that only fires when the tool's
    internal enforcement has failed, never the routine limit. wait_for
    follows its own wait budget (from its arguments) plus slack, so a
    wait that expires returns a graceful "still running" result instead
    of being stopped by the harness.
    """
    if tool_name in ("ask_user", REVIEW_TOOL_NAME):
        return None
    if tool_name == "web_search":
        return search_deadline_s()
    if tool_name == "browser":
        return browser_deadline_s()
    if tool_name == "wait_for":
        return wait_for_deadline_s(tool_arguments)
    return tool_timeout_s


def build_stopped_call_notice(
    tool_name: str, tool_arguments: dict, timeout_s: float
) -> str:
    """Compose the tool result for a call stopped at its deadline.

    The call honored the stop request: nothing is running in the
    background, so the model can retry or continue without waiting.
    """
    return (
        f"Tool call stopped after {timeout_s} seconds:"
        f" {build_signature(tool_name, tool_arguments)}."
        " The harness asked the call to stop at its deadline and it"
        " ended; nothing is running in the background."
    )


def build_hung_call_notice(
    tool_name: str, tool_arguments: dict, timeout_s: float, grace_s: float
) -> str:
    """Compose the tool result for a call that ignored the stop request.

    The detach is the last resort after the stop grace expires: the
    worker thread cannot be killed, so it may still be running and
    holding its locks. This is an anomaly, not the normal timeout.
    """
    notice = (
        f"Tool call hung: {build_signature(tool_name, tool_arguments)}."
        f" Its deadline of {timeout_s} seconds passed, the stop request"
        f" was not honored within {grace_s} seconds, and the harness"
        " detached from it. The call may still be running in the"
        " background and its result is lost. This is not the normal"
        " timeout: it means the tool stopped responding."
    )
    if tool_name == "execute_command":
        notice += (
            " This is the harness limit, not execute_command's own:"
            " execute_command kills its commands after"
            f" {command_runtime_limit_s()} seconds at most, so a call"
            " that hung means the container call itself stopped"
            " responding."
        )
    return notice


def _collect_outcome(outcome: dict) -> tuple[object, int | None]:
    """Return (output, exit_code) from a finished worker, raising on error.

    On error, the worker's recorded exit code is restored into the
    caller's thread context first, so the batch pipeline can still
    report it: a tool that set an exit code before raising keeps that
    verdict on the rendered row.
    """
    if "error" in outcome:
        set_last_exit_code(outcome.get("exit_code"))
        raise outcome["error"]
    return outcome["output"], outcome.get("exit_code")


def execute_tool_with_timeout(
    tool_name: str,
    tool_arguments: dict,
    timeout_s: float | None,
    output_path: str | None = None,
) -> tuple[object, bool, int | None]:
    """Run a tool in a worker thread and return (output, timed_out, exit_code).

    The worker thread exists to enforce the call's deadline, not to let
    the conversation move on while work continues: the model never sees
    a result while the call is still running. When the deadline passes,
    the harness sets the call's cancel event and waits up to the
    configured stop grace for the worker to exit:

    - A worker that exits within the grace delivers its real outcome:
      a late completion is a result, a failure is an error, except a
      `ToolCancelled` exception, which the harness reports as the
      deadline notice.
    - A worker still running after the grace is detached with the
      hung-call notice: threads cannot be killed, so it may still be
      running and holding its locks. This is an anomaly, not the normal
      timeout.

    A None timeout waits indefinitely; used by ask_user, which must
    outlive any configured budget and block until the user answers, and
    by review_work, whose reviewer request is bounded by the model
    client's own timeout and retry policy.
    """
    tool_fn = TOOL_REGISTRY[tool_name]
    container = get_registered_container()
    responder = get_responder()
    log_provider = current_conversation_log_provider()
    background_level = get_background_level()
    background_allegro = get_background_allegro()
    background_record = get_background_record()
    finished = threading.Event()
    cancel_event = threading.Event()
    outcome: dict = {}

    def worker():
        if container is not None:
            register_container(container)
        if responder is not None:
            register_responder(responder)
        if log_provider is not None:
            set_conversation_log_provider(log_provider)
        set_background_level(background_level)
        set_background_allegro(background_allegro)
        set_background_record(background_record)
        set_call_output_path(output_path)
        set_cancel_event(cancel_event)
        try:
            outcome["output"] = tool_fn(**tool_arguments)
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            outcome["exit_code"] = pop_last_exit_code()
            set_call_output_path(None)
            set_cancel_event(None)
            set_background_level(None)
            set_background_allegro(None)
            set_background_record(None)
            pop_conversation_log_provider()
            unregister_responder()
            if container is not None:
                unregister_container()
            finished.set()

    worker_thread = threading.Thread(
        target=worker, name=f"syna-tool-{tool_name}", daemon=True
    )
    worker_thread.start()

    if timeout_s is None:
        finished.wait()
        output, exit_code = _collect_outcome(outcome)
        return output, False, exit_code

    if finished.wait(timeout_s):
        output, exit_code = _collect_outcome(outcome)
        return output, False, exit_code

    cancel_event.set()
    get_logger().info(
        "Tool call %s reached its %.0fs deadline; asking it to stop.",
        tool_name,
        timeout_s,
    )
    grace_s = load_max_cancel_wait_s()
    if finished.wait(grace_s):
        if "error" in outcome:
            error = outcome["error"]
            if isinstance(error, ToolCancelled):
                get_logger().info("Tool call %s stopped at its deadline.", tool_name)
                return (
                    build_stopped_call_notice(tool_name, tool_arguments, timeout_s),
                    True,
                    None,
                )
            set_last_exit_code(outcome.get("exit_code"))
            raise error
        get_logger().info(
            "Tool call %s finished past its deadline within the stop"
            " grace; delivering its result.",
            tool_name,
        )
        return outcome["output"], False, outcome.get("exit_code")

    get_logger().warning(
        "Tool call %s ignored its stop request for %.0fs; detaching from"
        " it. It may still be running in the background.",
        tool_name,
        grace_s,
    )
    return (
        build_hung_call_notice(tool_name, tool_arguments, timeout_s, grace_s),
        True,
        None,
    )


def _persist_tool_output(
    output_text: str, saved_to: str | None, persist_outputs: bool
) -> str | None:
    """Save a host-side tool output into the pod at its call output path.

    Uses the calling thread's container when one is bound (web sessions);
    with `persist_outputs` (CLI sessions) it falls back to the shared pod.
    The saved path is what the model sees as `saved_to`. Output is
    written as bytes, not with `write_text`: text mode would
    translate line feeds to carriage-return pairs on Windows and
    corrupt the saved copy.
    """
    container = get_registered_container()
    if container is None and persist_outputs:
        container = get_container()
    if container is None or saved_to is None:
        return None
    try:
        pod_exec(container, ["bash", "-c", "mkdir -p /tool_outputs"])
        file_name = PurePosixPath(saved_to).name
        with tempfile.TemporaryDirectory(prefix="syna-persist-") as temp_dir:
            host_file = Path(temp_dir, file_name)
            host_file.write_bytes(output_text.encode("utf-8", errors="replace"))
            pod_copy_to(container, str(host_file), "/tool_outputs")
        try:
            pod_exec(container, ["bash", "-c", f"chmod 444 {shlex.quote(saved_to)}"])
        except Exception:
            get_logger().warning("Could not make %s read-only", saved_to, exc_info=True)
        return saved_to
    except Exception:
        get_logger().exception("Failed to persist tool output to %s", saved_to)
        return None


DOUBLE_ESCAPED_QUOTE = '\\\\"'
SINGLE_ESCAPED_QUOTE = '\\"'
JSON_ERROR_SNIPPET_CHARS = 60


def _repair_double_escaped_json(raw_args: str, tool_name: str) -> dict | None:
    """Parse arguments whose quotes the model double-escaped, or None.

    Some models (observed with GLM Flash) serialize JSON arguments with
    '\\\\"' where '\\"' belongs, so strict parsing fails on the first
    escaped quote inside a string value. Only tried after a strict parse
    already failed, so valid payloads are never rewritten; the repair is
    logged when it fires.
    """
    if DOUBLE_ESCAPED_QUOTE not in raw_args:
        return None
    repaired = raw_args.replace(DOUBLE_ESCAPED_QUOTE, SINGLE_ESCAPED_QUOTE)
    try:
        decoded = json.loads(repaired)
    except json.JSONDecodeError:
        return None
    get_logger().info(
        "Repaired double-escaped JSON arguments for tool '%s'.", tool_name
    )
    return decoded if isinstance(decoded, dict) else None


def _json_error_snippet(raw_args: str, position: int) -> str:
    """Quote the raw arguments around a JSON parse error position."""
    start = max(0, position - JSON_ERROR_SNIPPET_CHARS)
    end = min(len(raw_args), position + JSON_ERROR_SNIPPET_CHARS)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(raw_args) else ""
    return prefix + raw_args[start:end] + suffix


def _unpack_tool_call(raw_call: object) -> tuple[str, str, dict, str, str | None]:
    """Extract (call_id, tool_name, arguments_dict, raw_str, error) from a raw tool call."""
    if isinstance(raw_call, dict):
        call_id = raw_call.get("id") or ""
        fn = raw_call.get("function")
        if isinstance(fn, dict):
            tool_name = fn.get("name") or ""
            raw_args = fn.get("arguments")
        else:
            tool_name = raw_call.get("name") or ""
            raw_args = raw_call.get("arguments")
    else:
        call_id = getattr(raw_call, "id", "") or ""
        fn = getattr(raw_call, "function", None)
        tool_name = getattr(fn, "name", "") if fn else getattr(raw_call, "name", "")
        raw_args = (
            getattr(fn, "arguments", "{}")
            if fn
            else getattr(raw_call, "arguments", "{}")
        )

    if isinstance(raw_args, dict):
        tool_arguments = raw_args
        raw_str = json.dumps(raw_args)
        return call_id, tool_name, tool_arguments, raw_str, None

    if isinstance(raw_args, str):
        raw_str = raw_args
        clean_args = raw_args.strip()
        if not clean_args:
            return call_id, tool_name, {}, raw_str, None
        try:
            decoded = json.loads(clean_args)
        except json.JSONDecodeError as exc:
            decoded = _repair_double_escaped_json(clean_args, tool_name)
            if decoded is None:
                snippet = _json_error_snippet(clean_args, exc.pos)
                return (
                    call_id,
                    tool_name,
                    {},
                    raw_str,
                    (
                        f"The arguments for tool '{tool_name}' could not be parsed"
                        f" as JSON: {exc}. Arguments near the error: {snippet}"
                    ),
                )
        if not isinstance(decoded, dict):
            return (
                call_id,
                tool_name,
                {},
                raw_str,
                f"The arguments for tool '{tool_name}' must be a JSON object; got {_json_type_name(decoded)}.",
            )
        return call_id, tool_name, decoded, raw_str, None

    return (
        call_id,
        tool_name,
        {},
        str(raw_args),
        f"Invalid argument payload for tool '{tool_name}': expected string or dict, got {type(raw_args).__name__}.",
    )


def parse_and_execute_tools(
    tool_calls: list | dict | None,
    on_tool_event: Callable | None = None,
    echo_errors: bool = True,
    tool_timeout_s: float | None = None,
    persist_outputs: bool = False,
) -> ParseOutcome:
    """Execute model tool calls sequentially with stop-on-first-failure.

    The first failing call stops the batch; subsequent calls are recorded as
    skipped so every tool_call_id receives a result message.

    Parameters:
        tool_calls: List of tool call objects/dicts, or an assistant message dict.
        on_tool_event: Optional callback receiving tool progress events.
        echo_errors: Whether to echo failure summaries to standard output.
        tool_timeout_s: Execution timeout per individual tool call in seconds.
        persist_outputs: Whether host-side tool outputs may be saved into a
            lazily created shared pod (CLI).
    """
    if tool_timeout_s is None:
        tool_timeout_s = load_tool_call_timeout()
    logger = get_logger()
    output_limit = load_output_limit_chars()

    if isinstance(tool_calls, dict):
        tool_calls = tool_calls.get("tool_calls")
    if not tool_calls:
        return ParseOutcome(None, [], [])

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

    for position, raw_call in enumerate(tool_calls, start=1):
        call_id, tool_name, tool_arguments, raw_json, decode_error = _unpack_tool_call(
            raw_call
        )
        if not call_id:
            call_id = f"synthesized_{position:04d}"
        signature = build_signature(tool_name, tool_arguments)

        if failure_occurred:
            emit(
                "started",
                tool_name,
                signature,
                raw=raw_json,
                arguments=tool_arguments,
                tool_call_id=call_id,
            )
            emit(
                "skipped",
                tool_name,
                signature,
                output=SKIPPED_CALL_NOTICE,
                raw=raw_json,
                arguments=tool_arguments,
                tool_call_id=call_id,
            )
            outputs.append(
                {
                    "call": position,
                    "tool_call_id": call_id,
                    "name": tool_name,
                    "signature": signature,
                    "output": SKIPPED_CALL_NOTICE,
                    "status": "skipped",
                    "error": True,
                    "raw": raw_json[:6000],
                    "arguments": tool_arguments,
                }
            )
            continue

        if decode_error is not None:
            logger.warning(decode_error)
            if echo_errors:
                print(f"  ! ({decode_error[:80]})")
            emit(
                "started",
                tool_name,
                signature,
                raw=raw_json,
                arguments=tool_arguments,
                tool_call_id=call_id,
            )
            emit(
                "error",
                tool_name,
                signature,
                output=decode_error,
                raw=raw_json,
                arguments=tool_arguments,
                tool_call_id=call_id,
            )
            emit(
                "finished",
                tool_name,
                signature,
                decode_error,
                raw=raw_json,
                arguments=tool_arguments,
                failed=True,
                tool_call_id=call_id,
            )
            outputs.append(
                {
                    "call": position,
                    "tool_call_id": call_id,
                    "name": tool_name,
                    "signature": signature,
                    "output": decode_error,
                    "status": "error",
                    "error": True,
                    "raw": raw_json[:6000],
                    "arguments": tool_arguments,
                }
            )
            failure_occurred = True
            continue

        if tool_name not in TOOL_REGISTRY:
            output = (
                f"Attempted to call a tool that is not registered: '{tool_name}'."
                f" Registered tools: {', '.join(sorted(TOOL_REGISTRY))}"
            )
            logger.warning(output)
            if echo_errors:
                print(f"  ! ({output})")
            emit(
                "started",
                tool_name,
                signature,
                raw=raw_json,
                arguments=tool_arguments,
                tool_call_id=call_id,
            )
            emit(
                "error",
                tool_name,
                signature,
                output=output,
                raw=raw_json,
                arguments=tool_arguments,
                tool_call_id=call_id,
            )
            emit(
                "finished",
                tool_name,
                signature,
                output,
                raw=raw_json,
                arguments=tool_arguments,
                failed=True,
                tool_call_id=call_id,
            )
            outputs.append(
                {
                    "call": position,
                    "tool_call_id": call_id,
                    "name": tool_name,
                    "signature": signature,
                    "output": output,
                    "status": "error",
                    "error": True,
                    "raw": raw_json[:6000],
                    "arguments": tool_arguments,
                }
            )
            failure_occurred = True
            continue

        argument_error = _signature_error(tool_name, tool_arguments)
        if argument_error is not None:
            logger.warning(argument_error)
            if echo_errors:
                print(f"  ! ({argument_error[:80]})")
            emit(
                "started",
                tool_name,
                signature,
                raw=raw_json,
                arguments=tool_arguments,
                tool_call_id=call_id,
            )
            emit(
                "error",
                tool_name,
                signature,
                output=argument_error,
                raw=raw_json,
                arguments=tool_arguments,
                tool_call_id=call_id,
            )
            emit(
                "finished",
                tool_name,
                signature,
                argument_error,
                raw=raw_json,
                arguments=tool_arguments,
                failed=True,
                tool_call_id=call_id,
            )
            outputs.append(
                {
                    "call": position,
                    "tool_call_id": call_id,
                    "name": tool_name,
                    "signature": signature,
                    "output": argument_error,
                    "status": "error",
                    "error": True,
                    "raw": raw_json[:6000],
                    "arguments": tool_arguments,
                }
            )
            failure_occurred = True
            continue

        emit(
            "started",
            tool_name,
            signature,
            raw=raw_json,
            arguments=tool_arguments,
            tool_call_id=call_id,
        )

        call_output_path = f"/tool_outputs/{next_call_index():04d}_{tool_name}.txt"
        call_deadline = call_deadline_s(tool_name, tool_timeout_s, tool_arguments)
        try:
            output, timed_out, exit_code = execute_tool_with_timeout(
                tool_name, tool_arguments, call_deadline, call_output_path
            )
        except Exception as exc:
            if echo_errors:
                print(f"  ! (Tool execution failed: {str(exc)[:64]})")
            output = f"Tool execution error: {exc}"
            timed_out = False
            exit_code = pop_last_exit_code()
            status = "error"
            failure_occurred = True
        else:
            if timed_out:
                status = "timeout"
                failure_occurred = True
            elif exit_code is not None and exit_code != 0:
                status = "timeout" if exit_code in TIMEOUT_EXIT_CODES else "error"
                failure_occurred = True
            else:
                status = "ok"

        # A tool may attach an image to the conversation (inspect_image):
        # the reserved `image` key leaves the result text and becomes a
        # session message, so base64 data never passes through tool
        # output, UI events, or logs.
        image_payload = None
        if isinstance(output, dict) and "image" in output:
            image_payload = output["image"]
            output = str(output.get("note") or "")

        if tool_name == "think":
            think_signatures.append(signature)
            emit(
                "finished",
                tool_name,
                signature,
                output,
                raw=raw_json,
                arguments=tool_arguments,
                tool_call_id=call_id,
            )
            outputs.append(
                {
                    "call": position,
                    "tool_call_id": call_id,
                    "name": tool_name,
                    "signature": signature,
                    "output": str(output) if output else "(noted)",
                    "status": "ok",
                    "raw": raw_json[:6000],
                    "arguments": tool_arguments,
                }
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
                raw=raw_json,
                arguments=tool_arguments,
                tool_call_id=call_id,
            )
        emit(
            "finished",
            tool_name,
            signature,
            model_output,
            raw=raw_json,
            arguments=tool_arguments,
            failed=failed_run,
            tool_call_id=call_id,
            **({"structured_output": output} if isinstance(output, dict) else {}),
        )
        outputs.append(
            {
                "call": position,
                "tool_call_id": call_id,
                "name": tool_name,
                "signature": signature,
                "output": model_output,
                "status": status,
                "raw": raw_json[:6000],
                "arguments": tool_arguments,
                **({"exit_code": exit_code} if exit_code is not None else {}),
                **({"saved_to": saved_to} if saved_to else {}),
                **({"image": image_payload} if image_payload else {}),
            }
        )

    return ParseOutcome(None, outputs, [], think_signatures)


parse_and_execute_reply = parse_and_execute_tools
