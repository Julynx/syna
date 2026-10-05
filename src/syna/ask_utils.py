"""CLI helpers and model request plumbing."""

import time
import traceback

from .config import get_logger


def show_welcome():
    """Display the welcome header and available interactive commands."""
    from .commands import get_command_help

    print()
    print("Welcome to Syna, the AI agent with a pod.")
    print(get_command_help())
    print()


def ask_question(client, model, messages, delay=2):
    """Send conversation messages to the model and record its response."""
    logger = get_logger()
    time.sleep(delay)
    try:
        response = client.chat.send(model=model, messages=messages)
        content = response.choices[0].message.content
        msg = {"role": "assistant", "content": content}
        messages.append(msg)
        logger.info(str(msg))
        return content
    except Exception as exc:
        logger.exception("Failed to get model response")
        print(f"\n  ! (Error communicating with model: {exc})")
        traceback.print_exc()
        raise
