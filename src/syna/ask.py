import json
from pathlib import Path

import ask_utils
from dotenv import dotenv_values
from inspect_module import get_function_metadata
from openrouter import OpenRouter

dotenv = dotenv_values(".env")


def build_prompt():
    template = Path("assets", "prompt.md").read_text()
    tools = get_function_metadata(Path("src/syna/tools.py"))

    tool_blocks = []
    for tool in tools:
        args_dict = {arg["name"]: arg["type"] for arg in tool["arguments"]}
        body_json = json.dumps({tool["name"]: args_dict}, indent=2)
        tool_blocks.append(f"### {tool['name']}")
        tool_blocks.append(f"{tool['docstring']}")
        tool_blocks.append(f"```tool\n{body_json}\n```")

    tool_str = "\n\n".join(tool_blocks)
    return template.replace("{tools}", tool_str)


def ask_loop(model="google/gemini-3.8-flash"):

    prompt = build_prompt()
    messages = [{"role": "system", "content": prompt}]
    ask_utils.show_welcome()

    ask_utils.prepare_question(messages)
    while True:
        with OpenRouter(api_key=dotenv["OPENROUTER_API_KEY"]) as client:
            response = ask_utils.ask_question(client, model, messages)

            if "```tool" not in response:
                print("  + (No tool detected)")
                message = (
                    "WARNING: No tool calls were found in your response."
                    " Your responses must contain at least one tool call."
                )
                messages.append({"role": "user", "content": f"[System]: {message}"})
                continue

            tool_results = ask_utils.use_tools(response, messages)

            used_respond_tool = next(
                (tool for tool in tool_results if tool["name"] == "respond"),
                None,
            )

            if used_respond_tool:
                print(f"[Syna]: {used_respond_tool['output']}")
                ask_utils.prepare_question(messages)
