"""CLI helpers and model request plumbing."""

import json
import os
import time
import traceback
from collections.abc import Callable
from pathlib import Path

from .config import get_logger, load_config

EMPTY_RESPONSE_MAX_ATTEMPTS = 3
DEFAULT_MAX_OUTPUT_TOKENS = 16384


class ReplyRejected(Exception):
    """Raised when an assistant reply is rejected by the pre-history acceptance gate."""

    def __init__(self, reason: str, raw_content: str):
        super().__init__(reason)
        self.reason = reason
        self.raw_content = raw_content


def show_welcome():
    """Display the welcome header and available interactive commands."""
    from .conversation.commands import get_command_help

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


def _log_response_meta(logger, response, attempt: int) -> None:
    """Log finish reason and the reasoning/completion token split of a reply."""
    choice = response.choices[0]
    parts = [f"attempt={attempt}", f"finish_reason={choice.finish_reason!r}"]
    usage = getattr(response, "usage", None)
    if usage is not None:
        details = getattr(usage, "completion_tokens_details", None)
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", None)
        cached_tokens = getattr(prompt_details, "cached_tokens", None)
        parts.append(
            f"tokens(prompt={prompt_tokens}, completion={completion_tokens},"
            f" reasoning={reasoning_tokens}, cached={cached_tokens})"
        )
    logger.info("Model response meta: %s", ", ".join(parts))


def ask_question(
    client,
    model,
    messages,
    delay=2,
    effort=None,
    dump_path=None,
    response_format: dict | None = None,
    accept_reply: Callable[[str], str | None] | None = None,
    session_id: str | None = None,
    tools: list[dict] | None = None,
    tool_choice: str | dict | None = None,
) -> dict:
    """Send conversation messages to the model and record its response.

    Parameters:
        client: OpenRouter or compatible chat client.
        model: Identifier of the target LLM.
        messages: Conversation history list of role/content dicts.
        delay: Seconds to pause before making the request.
        effort: Optional reasoning effort parameter for reasoning models.
        dump_path: Optional path for diagnostic request dumps.
        response_format: Structured output format dict, or None for unconstrained output.
        accept_reply: Optional callable receiving serialized content and tool calls.
            Returning a non-None string rejects the response and raises ReplyRejected.
        session_id: Optional OpenRouter session id. It pins provider sticky
            routing for the conversation so provider-side prompt caches stay
            warm across requests.
        tools: Optional list of tool schema dictionaries.
        tool_choice: Optional tool choice setting ('auto', 'none', or specific tool).

    Returns:
        The assistant message dictionary appended to history.
    """
    logger = get_logger()
    try:
        for attempt in range(1, EMPTY_RESPONSE_MAX_ATTEMPTS + 1):
            time.sleep(delay)
            request = {
                "model": model,
                "messages": messages,
                "max_tokens": load_config().get(
                    "max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS
                ),
            }
            if response_format is not None:
                request["response_format"] = response_format
            if tools is not None:
                request["tools"] = tools
            if tool_choice is not None:
                request["tool_choice"] = tool_choice
            if effort:
                request["reasoning"] = {"effort": effort}
            if session_id:
                request["session_id"] = session_id
            _write_dump(dump_path, request)
            response = client.chat.send(**request)
            _log_response_meta(logger, response, attempt)
            message = response.choices[0].message
            if message.refusal:
                raise RuntimeError(f"Model refused to respond: {message.refusal}")

            content = _content_text(message.content)
            raw_tool_calls = getattr(message, "tool_calls", None)

            formatted_tool_calls: list[dict] = []
            if raw_tool_calls:
                for index, tc in enumerate(raw_tool_calls, start=1):
                    if isinstance(tc, dict):
                        call = dict(tc)
                    else:
                        fn = getattr(tc, "function", None)
                        fn_name = getattr(fn, "name", "") if fn else ""
                        fn_args = getattr(fn, "arguments", "") if fn else ""
                        tc_id = getattr(tc, "id", "")
                        tc_type = getattr(tc, "type", "function")
                        call = {
                            "id": tc_id,
                            "type": tc_type,
                            "function": {
                                "name": fn_name,
                                "arguments": fn_args,
                            },
                        }
                    if not call.get("id"):
                        call["id"] = f"synthesized_{index:04d}"
                    formatted_tool_calls.append(call)

            if content or formatted_tool_calls:
                text_to_check = content or ""
                if formatted_tool_calls:
                    text_to_check = (
                        text_to_check + " " + json.dumps(formatted_tool_calls)
                    ).strip()

                if accept_reply is not None:
                    rejection_reason = accept_reply(text_to_check)
                    if rejection_reason is not None:
                        logger.warning("Assistant reply rejected: %s", rejection_reason)
                        raise ReplyRejected(rejection_reason, text_to_check)

                msg: dict = {
                    "role": "assistant",
                    "content": content if content else None,
                }
                if formatted_tool_calls:
                    msg["tool_calls"] = formatted_tool_calls

                messages.append(msg)
                logger.info(str(msg))
                _write_dump(dump_path, request)
                return msg

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
