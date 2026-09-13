import json
import time

from commands import get_command_help, parse_and_execute_command
from parse import parse_and_execute_tools


def show_welcome():
    print()
    print("Welcome to Syna, the AI agent with a pod.")
    print(get_command_help())
    print()


def prepare_question(messages):

    query = input("(Ask Syna): ")

    # Resolve command
    if query.strip().startswith("/"):
        try:
            query = f"  + ({parse_and_execute_command(query)})"
            print(query)
        except ValueError as exc:
            print(f"  ! ({exc})")
        return prepare_question(messages)

    messages.append({"role": "user", "content": query})
    print("  + (Thinking...)", end="\r", flush=True)


def ask_question(client, model, messages, delay=2):
    time.sleep(2)
    print("  + (Waiting for model...)", end="\r", flush=True)
    response = client.chat.send(model=model, messages=messages)
    response = response.choices[0].message.content
    messages.append({"role": "assistant", "content": response})
    print("  + (Processing model response...)", end="\r", flush=True)
    return response


def use_tools(response, messages):
    tool_results = parse_and_execute_tools(response)
    tool_results_str = json.dumps(tool_results)

    for tool_result in tool_results:
        if tool_result["name"] == "respond":
            continue
        signature_summary = tool_result["signature"].strip()[:64]
        output_summary = tool_result["output"].replace("\n", " ")
        output_summary = output_summary.strip()[:64]
        print(f"  + │Used tool '{signature_summary}...'")
        print(f"    └─ {output_summary}...")
        print("  + (Thinking...)", end="\r", flush=True)

    messages.append({"role": "user", "content": f"[Tool]: {tool_results_str}"})

    return tool_results
