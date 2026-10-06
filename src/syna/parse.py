"""Tool call parsing and execution for agent responses."""

import json
import threading
from typing import Callable

from string_grab import grab_all

from . import tools
from .config import get_logger, load_config
from .state import get_registered_container, register_container, unregister_container

DEFAULT_TOOL_CALL_TIMEOUT_S = 240


def build_signature(tool_name, tool_arguments):
    """Render a tool call as a single-line python-like signature."""
    return (
        f"{tool_name}("
        + ", ".join(
            f"{arg_name}={arg_value!r}" if isinstance(arg_value, str)
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
    tool_fn = getattr(tools, tool_name)
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
):
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
    """
    outputs = []
    if tool_timeout_s is None:
        tool_timeout_s = load_tool_call_timeout()
    logger = get_logger()

    for tool_call in grab_all(text, start="```tool", end="```"):
        try:
            tool = json.loads(tool_call)
            tool_name = next(iter(tool.keys()))
            tool_arguments = tool[tool_name]
        except json.decoder.JSONDecodeError as exc:
            if echo_errors:
                print(f"  ! (Tool call failed: {str(exc)[:32]})")
            tool_name = "unknown"
            signature = build_signature(tool_name, {})
            output = f"A tool call could not be decoded as JSON: {exc}"
            if on_tool_event is not None:
                on_tool_event("error", tool_name, signature, output=output)
            outputs.append(
                {"name": tool_name, "signature": signature, "output": output}
            )
            continue

        signature = build_signature(tool_name, tool_arguments)
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

        outputs.append(
            {"name": tool_name, "signature": signature, "output": output}
        )

    return outputs
