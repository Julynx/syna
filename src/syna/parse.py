"""Tool call parsing and execution for agent responses."""

import json
from typing import Callable

from string_grab import grab_all

from . import tools


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


def parse_and_execute_tools(text, on_tool_event: Callable | None = None):
    """Parse tool invocations from text and execute their corresponding functions.

    'on_tool_event' is called as on_tool_event(status, name, signature, output)
    with status "started" before each tool runs (it may raise to abort the
    remaining calls) and with status "finished" once its output is available.
    """
    outputs = []

    for tool_call in grab_all(text, start="```tool", end="```"):
        try:
            tool = json.loads(tool_call)
            tool_name = next(iter(tool.keys()))
            tool_arguments = tool[tool_name]
        except json.decoder.JSONDecodeError as exc:
            print(f"  ! (Tool call failed: {str(exc)[:32]})")
            tool_name = "unknown"
            tool_arguments = {"unknown": "unknown"}
            signature = build_signature(tool_name, tool_arguments)
            output = f"A tool call could not be decoded as JSON: {exc}"
            outputs.append(
                {"name": tool_name, "signature": signature, "output": output}
            )
            continue

        signature = build_signature(tool_name, tool_arguments)
        if on_tool_event is not None:
            on_tool_event("started", tool_name, signature)

        try:
            output = getattr(tools, tool_name)(**tool_arguments)
        except Exception as exc:
            print(f"  ! (Tool execution failed: {str(exc)[:64]})")
            output = f"Tool execution error: {exc}"

        if on_tool_event is not None:
            on_tool_event("finished", tool_name, signature, output)

        outputs.append(
            {"name": tool_name, "signature": signature, "output": output}
        )

    return outputs
