"""CLI helpers and model request plumbing."""

import json
import os
import time
import traceback
from pathlib import Path

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


def _write_dump(dump_path: Path | None, payload: dict) -> None:
    """Atomically persist 'payload' to the session dump file.

    The dump is a diagnostic side channel: any failure (serialization,
    disk full, permissions) is logged and swallowed so it can never abort
    the model request itself. Writes go through a temp file plus
    'os.replace', so readers never see a half-written file and a crash
    mid-write cannot corrupt the previous dump.
    """
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


def ask_question(client, model, messages, delay=2, effort=None, dump_path=None):
    """Send conversation messages to the model and record its response.

    'effort' optionally sets OpenRouter's reasoning effort, sent as the
    unified 'reasoning' parameter ({"effort": ...}); None leaves the
    model's default behavior untouched.

    'dump_path' optionally points at the session dump file: when given,
    the exact request payload (model, reasoning effort and messages) is
    atomically written there right before each call and again once the
    assistant reply has been appended to the history. Pass None to skip
    dumping entirely, which the compaction request does so it never
    hijacks the live conversation's dump.

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
            request = {"model": model, "messages": messages}
            if effort:
                request["reasoning"] = {"effort": effort}
            _write_dump(dump_path, request)
            response = client.chat.send(**request)
            message = response.choices[0].message
            if message.refusal:
                raise RuntimeError(f"Model refused to respond: {message.refusal}")
            content = _content_text(message.content)
            if content:
                msg = {"role": "assistant", "content": content}
                messages.append(msg)
                logger.info(str(msg))
                # request["messages"] aliases 'messages', so this dump now
                # also carries the assistant reply alongside model and effort.
                _write_dump(dump_path, request)
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
        f"Model returned no usable content after {EMPTY_RESPONSE_MAX_ATTEMPTS} attempts"
    )
    logger.error("Failed to get model response: %s", error)
    print(f"\n  ! (Error communicating with model: {error})")
    raise error
