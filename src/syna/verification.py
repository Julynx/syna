"""Adversarial work verification through an isolated reviewer request.

The review_work tool hands the latest task's results to a separate
reviewer model session: a fresh client, a single-turn conversation, and
no tools. The reviewer receives its system prompt, the last
`verify_log_tail_chars` characters of the agent's serialized message
history, and the summary the agent intends to report. Nothing from this
exchange touches the agent's own history or the chat UI: the tool call
and its returned report are the only trace, exactly like any other tool.
"""

import json
import threading

from .ask_utils import ask_question
from .config import (
    DEFAULT_MODEL,
    get_env,
    get_logger,
    load_verify_log_tail_chars,
    read_asset_text,
)

REVIEW_TOOL_NAME = "review_work"
DEFAULT_VERIFIER_EFFORT = "high"

_review_log = threading.local()


def set_conversation_log_provider(provider) -> None:
    """Record the callable returning the conversation snapshot to review."""
    _review_log.provider = provider


def pop_conversation_log_provider() -> None:
    """Forget the conversation snapshot provider on this thread."""
    _review_log.provider = None


def current_conversation_log_provider():
    """Return this thread's provider, or None outside an agent turn."""
    return getattr(_review_log, "provider", None)


def load_verifier_model() -> str | None:
    """Return the reviewer model, defaulting to the agent's own model."""
    env = get_env()
    return env.get("VERIFIER_MODEL") or env.get("MODEL") or DEFAULT_MODEL


def load_verifier_effort() -> str:
    """Return the reviewer's reasoning effort (VERIFIER_MODEL_EFFORT)."""
    value = (get_env().get("VERIFIER_MODEL_EFFORT") or "").strip().lower()
    return value or DEFAULT_VERIFIER_EFFORT


def build_verifier_messages(log_tail: str, summary: str) -> list[dict]:
    """Assemble the reviewer's two-message conversation."""
    verifier_prompt = read_asset_text("verifier_prompt.md")
    user_body = (
        "```text\n"
        f"Last {len(log_tail)} chars of agent execution log\n"
        f"{log_tail}\n"
        "```\n\n"
        "What the model intends to respond to the user as a result of the"
        " task (excluding deliverables to save bandwidth):\n\n"
        f"{summary}"
    )
    return [
        {"role": "system", "content": verifier_prompt},
        {"role": "user", "content": user_body},
    ]


def run_verification(summary: str):
    """Run the isolated review request; return its text or an error dict.

    Follows the describe_image convention: never raises, never stops the
    tool batch — failures come back as {'error': ...} so the agent knows
    verification did not happen.
    """
    logger = get_logger()
    provider = current_conversation_log_provider()
    if provider is None:
        return {"error": "review_work is only available during an agent turn."}
    model = load_verifier_model()
    if not model:
        return {
            "error": (
                "MODEL is not set in the .env file; there is no reviewer"
                " model to run. You did nothing wrong; tell the user."
            )
        }
    effort = load_verifier_effort()

    from .session import VALID_THINKING_EFFORTS, create_model_client  # noqa: PLC0415

    if effort not in VALID_THINKING_EFFORTS:
        return {
            "error": (
                f"VERIFIER_MODEL_EFFORT '{effort}' is invalid. Use one of:"
                f" {', '.join(VALID_THINKING_EFFORTS)} (or remove it from"
                " the .env file)."
            )
        }

    messages = provider()
    tail_chars = load_verify_log_tail_chars()
    log_tail = json.dumps(messages, ensure_ascii=False)[-tail_chars:]
    try:
        with create_model_client() as client:
            response = ask_question(
                client,
                model,
                build_verifier_messages(log_tail, summary),
                delay=0,
                response_format=None,
                effort=effort,
            )
    except Exception as exc:
        logger.exception("Work verification request failed")
        return {"error": f"The verification request failed: {exc}"}
    return response.get("content") or ""
