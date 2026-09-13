import json
import time
import traceback

from .commands import get_command_help, parse_and_execute_command
from .config import setup_file_logging
from .parse import parse_and_execute_tools


def show_welcome():
    """Display the welcome header and available interactive commands."""
    print()
    print("Welcome to Syna, the AI agent with a pod.")
    print(get_command_help())
    print()


def prepare_question(messages):
    """Prompt the user for input, handle slash commands, and append user message."""
    query = input("(Ask Syna): ")

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
    """Send conversation messages to the model and record its response."""
    time.sleep(delay)
    print("  + (Waiting for model...)", end="\r", flush=True)
    try:
        response = client.chat.send(model=model, messages=messages)
        content = response.choices[0].message.content
        messages.append({"role": "assistant", "content": content})
        print("  + (Processing model response...)", end="\r", flush=True)
        return content
    except Exception as exc:
        logger = setup_file_logging()
        logger.exception("Failed to get model response: %s", exc)
        print(f"\n  ! (Error communicating with model: {exc})")
        traceback.print_exc()
        raise


def use_tools(response, messages):
    """Execute tool calls present in the model response and append tool output."""
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
