"""Tool call parsing and execution for agent responses."""

import json

from string_grab import grab_all

from . import tools


def parse_and_execute_tools(text):
    """Parse tool invocations from text and execute their corresponding functions."""
    outputs = []

    for tool_call in grab_all(text, start="```tool", end="```"):
        try:
            tool = json.loads(tool_call)
            tool_name = next(iter(tool.keys()))
            tool_arguments = tool[tool_name]

            func = getattr(tools, tool_name)
            output = func(**tool_arguments)

        except json.decoder.JSONDecodeError as exc:
            print(f"  ! (Tool call failed: {str(exc)[:32]})")
            tool_name = "unknown"
            tool_arguments = {"unknown": "unknown"}
            output = f"A tool call could not be decoded as JSON: {exc}"
        except Exception as exc:
            print(f"  ! (Tool execution failed: {str(exc)[:64]})")
            output = f"Tool execution error: {exc}"

        outputs.append(
            {
                "name": tool_name,
                "signature": f"{tool_name}("
                + ", ".join(
                    f"{arg_name}={arg_value}"
                    for arg_name, arg_value in tool_arguments.items()
                )
                + ")",
                "output": output,
            }
        )

    return outputs
