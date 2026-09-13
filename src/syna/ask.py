"""Agent interaction loop and prompt preparation."""

import json

from dotenv import dotenv_values
from openrouter import OpenRouter
from openrouter.utils import BackoffStrategy, RetryConfig

from . import ask_utils
from .config import get_project_root, load_config
from .inspect_module import get_function_metadata

dotenv = dotenv_values(get_project_root() / ".env")


def build_prompt():
    """Build the system prompt template populated with tool definitions."""
    root = get_project_root()
    template = (root / "assets" / "prompt.md").read_text(encoding="utf-8")
    tools = get_function_metadata(root / "src" / "syna" / "tools.py")

    tool_blocks = []
    for tool in tools:
        args_dict = {arg["name"]: arg["type"] for arg in tool["arguments"]}
        body_json = json.dumps({tool["name"]: args_dict}, indent=2)
        tool_blocks.append(f"### {tool['name']}")
        tool_blocks.append(f"{tool['docstring']}")
        tool_blocks.append(f"```tool\n{body_json}\n```")

    tool_str = "\n\n".join(tool_blocks)
    return template.replace("{tools}", tool_str)


def create_model_client():
    """Instantiate an OpenRouter client configured with timeout and
    bounded retry limits."""
    config = load_config()
    retry_settings = config["retry"]
    retry_config = RetryConfig(
        strategy=retry_settings["strategy"],
        backoff=BackoffStrategy(
            initial_interval=retry_settings["initial_interval_ms"],
            max_interval=retry_settings["max_interval_ms"],
            exponent=retry_settings["exponent"],
            max_elapsed_time=retry_settings["max_elapsed_time_ms"],
        ),
        retry_connection_errors=retry_settings["retry_connection_errors"],
    )
    api_key = dotenv.get("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY not found in .env file.")

    return OpenRouter(
        api_key=api_key,
        timeout_ms=config["timeout_ms"],
        retry_config=retry_config,
    )


def ask_loop():
    """Execute the interactive conversation and tool execution loop."""
    model = dotenv.get("MODEL")
    if not model:
        raise ValueError("MODEL not found in .env file.")
    prompt = build_prompt()
    messages = [{"role": "system", "content": prompt}]
    ask_utils.show_welcome()

    client = create_model_client()
    with client:
        ask_utils.prepare_question(messages)
        while True:
            try:
                response = ask_utils.ask_question(client, model, messages)
            except Exception:
                print("  ! (Request failed. Enter a new instruction or retry.)")
                ask_utils.prepare_question(messages)
                continue

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
