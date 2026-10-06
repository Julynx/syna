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


def _content_text(content) -> str:
    """Flatten chat content (string or list of content parts) into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, dict):
            parts.append(item.get("text") or "")
        else:
            parts.append(getattr(item, "text", None) or "")
    return "".join(parts)


def ask_question(client, model, messages, delay=2):
    """Send conversation messages to the model and record its response.

    Raises RuntimeError when the model returns no usable content, so that
    an empty assistant message is never added to the conversation history.
    """
    logger = get_logger()
    time.sleep(delay)
    try:
        response = client.chat.send(model=model, messages=messages)
        message = response.choices[0].message
        if message.refusal:
            raise RuntimeError(f"Model refused to respond: {message.refusal}")
        content = _content_text(message.content)
        if not content:
            finish_reason = response.choices[0].finish_reason
            raise RuntimeError(
                "Model returned an empty response"
                f" (finish_reason={finish_reason!r})"
            )
        msg = {"role": "assistant", "content": content}
        messages.append(msg)
        logger.info(str(msg))
        return content
    except Exception as exc:
        logger.exception("Failed to get model response")
        print(f"\n  ! (Error communicating with model: {exc})")
        traceback.print_exc()
        raise
