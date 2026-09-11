import json

import tools
from string_grab import grab_all


def parse_and_execute_tools(text):

    outputs = []

    for tool_call in grab_all(text, start="```tool", end="```"):
        try:
            # Extract the tool information
            tool = json.loads(tool_call)
            tool_name = next(iter(tool.keys()))
            tool_arguments = tool[tool_name]

            # Call the tool
            func = getattr(tools, tool_name)
            output = func(**tool_arguments)

        except json.decoder.JSONDecodeError as exc:
            print(f"  ! (Tool call failed: {exc[:32]})")
            tool_name = "unknown"
            tool_arguments = {"unknown": "unknown"}
            output = f"A tool call could not be decoded as JSON: {exc}"

        # Format the output
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
