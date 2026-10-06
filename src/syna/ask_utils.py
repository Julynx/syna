"""CLI helpers and model request plumbing."""

import time
import traceback

from .config import get_logger

EMPTY_RESPONSE_MAX_ATTEMPTS = 3


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

    Transient empty responses (no content, e.g. finish_reason 'error')
    are retried up to EMPTY_RESPONSE_MAX_ATTEMPTS times; request failures
    and refusals raise immediately. Raises RuntimeError when the model
    returns no usable content, so an empty assistant message is never
    added to the conversation history.
    """
    logger = get_logger()
    try:
        for attempt in range(1, EMPTY_RESPONSE_MAX_ATTEMPTS + 1):
            time.sleep(delay)
            response = client.chat.send(model=model, messages=messages)
            message = response.choices[0].message
            if message.refusal:
                raise RuntimeError(f"Model refused to respond: {message.refusal}")
            content = _content_text(message.content)
            if content:
                msg = {"role": "assistant", "content": content}
                messages.append(msg)
                logger.info(str(msg))
                return content
            logger.warning(
                "Empty model response (attempt %d of %d, finish_reason=%r)",
                attempt,
                EMPTY_RESPONSE_MAX_ATTEMPTS,
                response.choices[0].finish_reason,
            )
    except Exception as exc:
        logger.exception("Failed to get model response")
        print(f"\n  ! (Error communicating with model: {exc})")
        traceback.print_exc()
        raise

    error = RuntimeError(
        "Model returned no usable content"
        f" after {EMPTY_RESPONSE_MAX_ATTEMPTS} attempts"
    )
    logger.error("Failed to get model response: %s", error)
    print(f"\n  ! (Error communicating with model: {error})")
    raise error
