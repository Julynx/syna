"""CLI helpers and model request plumbing."""

import json
import os
import time
import traceback
from collections.abc import Callable
from pathlib import Path

from .config import get_logger

EMPTY_RESPONSE_MAX_ATTEMPTS = 3
DEFAULT_RESPONSE_FORMAT = {"type": "json_object"}


class ReplyRejected(Exception):
    """Raised when an assistant reply is rejected by the pre-history acceptance gate."""

    def __init__(self, reason: str, raw_content: str):
        super().__init__(reason)
        self.reason = reason
        self.raw_content = raw_content


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


def _write_dump(dump_path: Path | None, payload: dict) -> None:
    """Atomically persist 'payload' to the session dump file."""
    if dump_path is None:
        return
    try:
        tmp_path = dump_path.parent / (dump_path.name + ".tmp")
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp_path, dump_path)
    except Exception:
        get_logger().warning(
            "Failed to write session dump to %s", dump_path, exc_info=True
        )


def ask_question(
    client,
    model,
    messages,
    delay=2,
    effort=None,
    dump_path=None,
    response_format: dict | None = DEFAULT_RESPONSE_FORMAT,
    accept_reply: Callable[[str], str | None] | None = None,
):
    """Send conversation messages to the model and record its response.

    Parameters:
        client: OpenRouter or compatible chat client.
        model: Identifier of the target LLM.
        messages: Conversation history list of role/content dicts.
        delay: Seconds to pause before making the request.
        effort: Optional reasoning effort parameter for reasoning models.
        dump_path: Optional path for diagnostic request dumps.
        response_format: Structured output format dict, or None for unconstrained output.
        accept_reply: Optional callable receiving raw response text. Returning a
            non-None string rejects the response and raises ReplyRejected.
    """
    logger = get_logger()
    try:
        for attempt in range(1, EMPTY_RESPONSE_MAX_ATTEMPTS + 1):
            time.sleep(delay)
            request = {"model": model, "messages": messages}
            if response_format is not None:
                request["response_format"] = response_format
            if effort:
                request["reasoning"] = {"effort": effort}
            _write_dump(dump_path, request)
            response = client.chat.send(**request)
            message = response.choices[0].message
            if message.refusal:
                raise RuntimeError(f"Model refused to respond: {message.refusal}")
            content = _content_text(message.content)
            if content:
                if accept_reply is not None:
                    rejection_reason = accept_reply(content)
                    if rejection_reason is not None:
                        logger.warning("Assistant reply rejected: %s", rejection_reason)
                        raise ReplyRejected(rejection_reason, content)
                msg = {"role": "assistant", "content": content}
                messages.append(msg)
                logger.info(str(msg))
                _write_dump(dump_path, request)
                return content
            logger.warning(
                "Empty model response (attempt %d of %d, finish_reason=%r)",
                attempt,
                EMPTY_RESPONSE_MAX_ATTEMPTS,
                response.choices[0].finish_reason,
            )
    except ReplyRejected:
        raise
    except Exception as exc:
        logger.exception("Failed to get model response")
        print(f"\n  ! (Error communicating with model: {exc})")
        traceback.print_exc()
        raise

    error = RuntimeError(
        f"Model returned no usable content after {EMPTY_RESPONSE_MAX_ATTEMPTS} attempts"
    )
    logger.error("Failed to get model response: %s", error)
    print(f"\n  ! (Error communicating with model: {error})")
    raise error
