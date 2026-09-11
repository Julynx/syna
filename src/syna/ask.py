import json
from pathlib import Path

from dotenv import dotenv_values
from inspect_module import get_function_metadata
from openrouter import OpenRouter
from parse import parse_and_execute_tools

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

    # First question
    query = input("(Enter your question): ")
    messages.append({"role": "user", "content": query})
    print("  + (Thinking...)", end="\r", flush=True)

    while True:
        with OpenRouter(api_key=dotenv["OPENROUTER_API_KEY"]) as client:
            print("  + (Waiting for model...)", end="\r", flush=True)
            response = client.chat.send(
                model=model,
                messages=messages,
            )
            response = response.choices[0].message.content
            messages.append({"role": "assistant", "content": response})
            print("  + (Processing model response...)", end="\r", flush=True)

            if "```tool" in response:
                tool_results = parse_and_execute_tools(response)
                tool_results_str = json.dumps(tool_results)

                for tool_result in tool_results:
                    if tool_result["name"] == "finish":
                        continue
                    signature_summary = tool_result["signature"].strip()[:64]
                    output_summary = tool_result["output"].replace("\n", " ")
                    output_summary = output_summary.strip()[:64]
                    print(f"  + │Used tool '{signature_summary}...'")
                    print(f"    └─ {output_summary}...")
                    print("  + (Thinking...)", end="\r", flush=True)

                messages.append(
                    {
                        "role": "user",
                        "content": f"[Tool output]: {tool_results_str}",
                    }
                )

                finish = next(
                    (tool for tool in tool_results if tool["name"] == "finish"),
                    None,
                )

                if finish:
                    print(f"[{model}]: {finish['output']}")
                    query = input("(Enter your question): ")
                    messages.append({"role": "user", "content": query})
                    print("  + (Thinking...)", end="\r", flush=True)
            else:
                print("  + (No tool detected)")
                message = (
                    "WARNING: No tool in response. Your responses must contain a tool."
                )
                messages.append(
                    {
                        "role": "user",
                        "content": f"[System]: {message}",
                    }
                )
