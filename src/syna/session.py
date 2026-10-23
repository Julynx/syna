"""Reusable agent session shared by the CLI and the web server."""

import json
import secrets
import threading
from datetime import date
from pathlib import Path

from openrouter import OpenRouter
from openrouter.utils import BackoffStrategy, RetryConfig

import syna.tools as tools

from .ask_utils import ReplyRejected, ask_question
from .channel import (
    contains_sentinel,
    defang_sentinels,
    format_tool_result_content,
    harness_notice,
    is_channel_message,
    wrap_channel,
)
from .compaction import (
    build_compaction_messages,
    format_compaction_message,
    load_compaction_prompt,
)
from .config import (
    DEFAULT_MODEL,
    get_env,
    get_logger,
    get_session_dump_path,
    load_command_timeout_s,
    load_config,
    load_elide_user_message_head_chars,
    load_output_limit_chars,
    load_user_message_archive_min_chars,
    read_asset_text,
)
from .context import (
    elide_archived_user_messages,
    elide_tool_outputs,
    estimate_history_tokens,
)
from .parse import parse_and_execute_tools
from .state import get_container
from .tool_schemas import get_tool_definitions
from .user_messages import USER_MESSAGE_MARKER_TEMPLATE, archive_user_message
from .verification import (
    REVIEW_TOOL_NAME,
    pop_conversation_log_provider,
    set_conversation_log_provider,
)

OUTPUT_PREVIEW_CHARS = 512
DETAIL_CAP_CHARS = 6000
DEFAULT_REQUEST_DELAY_S = 2
DEFAULT_MAX_HISTORY_MESSAGES = 300
DEFAULT_ENABLE_COMPACTION = True
DEFAULT_MAX_STEPS_PER_TURN = 100
DEFAULT_STEP_WARNING_REMAINING = 20
DEFAULT_CONTEXT_BUDGET_TOKENS = 100000
DEFAULT_ELIDE_KEEP_RECENT = 12
DEFAULT_ELIDE_INTERVAL_MESSAGES = 30
DEFAULT_ELIDE_EVERY_REQUESTS = 10
MAX_NO_TOOL_RETRIES = 3
REPEATED_CALL_LIMIT = 3

STEP_BUDGET_OUT_NOTICE = (
    "The step budget for this task ran out. Deliver your final status"
    " report now: what was done, what was verified, and what remains, so the"
    " user knows where things stand. Do not call any tools."
)

VALID_THINKING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def load_thinking_effort():
    """Return the THINKING_EFFORT value from .env, or None when unset."""
    value = get_env().get("THINKING_EFFORT")
    if not value:
        return None
    effort = value.strip().lower()
    if effort not in VALID_THINKING_EFFORTS:
        raise ValueError(
            f"THINKING_EFFORT '{value}' is invalid. Use one of:"
            f" {', '.join(VALID_THINKING_EFFORTS)} (or remove it from .env)."
        )
    return effort


def build_unparsed_tool_warning(reasons: list[str] | None = None) -> str:
    """Compose the corrective warning for tool calls that failed validation."""
    warning = "Your tool call could not be executed."
    if reasons:
        listed = "".join(f"\n- {reason[:250]}" for reason in reasons[:3])
        warning += f" The parser reported:{listed}"
    warning += (
        "\n\nEnsure tool arguments match the declared tool parameters."
        " After repeated violations the task is ended and control returns to the user."
    )
    return warning


def build_sentinel_violation_warning() -> str:
    """Compose the corrective warning for channel impersonation attempts."""
    return (
        "Your reply contained reserved system channel sentinels."
        " Results and notices arrive through a separate channel that you"
        " cannot write. Provide clean messages only."
        " After repeated violations the task is ended and control returns to the user."
    )


class TurnCancelled(Exception):
    """Raised when the user interrupts the current agent turn."""


def build_prompt():
    """Build the system prompt with tool docs, date, and limits filled in."""
    template = read_asset_text("prompt.md")
    tools = _get_tool_metadata(_tools_path())

    tool_blocks = [f"### {tool['name']}\n{tool['docstring']}" for tool in tools]
    prompt = template.replace("{tools}", "\n\n".join(tool_blocks))  # noqa: RUF027
    prompt = prompt.replace("{{CURRENT_DATE}}", date.today().isoformat())
    prompt = prompt.replace("{{COMMAND_TIMEOUT}}", str(load_command_timeout_s()))
    prompt = prompt.replace("{{OUTPUT_LIMIT}}", str(load_output_limit_chars()))
    return prompt


def _tools_path() -> Path:
    """Return the path of the tools module for metadata extraction."""
    return Path(tools.__file__)


def _get_tool_metadata(tools_path):
    from .inspect_module import get_function_metadata

    return get_function_metadata(tools_path)


DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def load_base_url() -> str:
    """Return the OpenAI-like endpoint URL the model client talks to.

    Defaults to OpenRouter; any OpenAI-compatible endpoint works (OpenAI,
    Gemini's compatibility layer, a local Ollama), set via BASE_URL in
    the .env file. A trailing slash is stripped so URL joins never
    produce double slashes.
    """
    return (
        (get_env().get("BASE_URL") or "").strip() or DEFAULT_OPENROUTER_BASE_URL
    ).rstrip("/")


def is_openrouter() -> bool:
    """Whether the configured endpoint is OpenRouter itself.

    OpenRouter-only features (image generation, speech transcription and
    synthesis) report themselves as not configured on other endpoints.
    """
    return load_base_url() == DEFAULT_OPENROUTER_BASE_URL


def create_model_client():
    """Instantiate an OpenRouter client configured with timeout and retry limits.

    The client speaks the OpenAI wire format, so any OpenAI-compatible
    endpoint works: BASE_URL selects it, OPENROUTER_API_KEY carries the
    key (the variable name predates multi-endpoint support; every
    provider's key goes there).
    """
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
    api_key = get_env().get("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY not found in .env file.")

    return OpenRouter(
        api_key=api_key,
        server_url=load_base_url(),
        timeout_ms=config["timeout_ms"],
        retry_config=retry_config,
    )


def _preview_text(output) -> str:
    """Flatten a tool output into a single-line bounded preview string."""
    text = str(output or "").replace("\n", " ").strip()
    return text[:OUTPUT_PREVIEW_CHARS]


def shared_file_metadata(event: dict) -> dict | None:
    """Return the delivery metadata of a finished share_file call, if any.

    The parser carries a tool's raw return value in 'structured_output' only
    when it is a dict, so a successful share_file yields
    {'path', 'name', 'is_dir'} here and everything else yields None.
    """
    metadata = event.get("structured_output")
    if (
        event.get("type") == "tool_call"
        and event.get("status") == "finished"
        and event.get("name") == "share_file"
        and isinstance(metadata, dict)
        and "path" in metadata
    ):
        return metadata
    return None


def _detail_text(text) -> str:
    """Bound a call-detail string for front-end transport."""
    text = str(text or "")
    if len(text) > DETAIL_CAP_CHARS:
        return text[:DETAIL_CAP_CHARS] + "..."
    return text


class AgentSession:
    """One conversation with the model plus its tool execution loop.

    'on_event' receives JSON-serializable dicts describing real-time events.
    """

    def __init__(
        self,
        on_event=None,
        request_delay: float = DEFAULT_REQUEST_DELAY_S,
        emit_tool_errors: bool = False,
        persist_outputs: bool = False,
        session_id: str | None = None,
    ):
        self.on_event = on_event or (lambda event: None)
        self.request_delay = request_delay
        self.emit_tool_errors = emit_tool_errors
        self.persist_outputs = persist_outputs
        self.max_history_messages = load_config().get(
            "max_history_messages", DEFAULT_MAX_HISTORY_MESSAGES
        )
        self.compaction_enabled = load_config().get(
            "enable_compaction", DEFAULT_ENABLE_COMPACTION
        )
        self.max_steps_per_turn = load_config().get(
            "max_steps_per_turn", DEFAULT_MAX_STEPS_PER_TURN
        )
        self.step_warning_remaining = load_config().get(
            "step_warning_remaining", DEFAULT_STEP_WARNING_REMAINING
        )
        self.context_budget_tokens = load_config().get(
            "context_budget_tokens", DEFAULT_CONTEXT_BUDGET_TOKENS
        )
        self.elide_keep_recent_messages = load_config().get(
            "elide_keep_recent_messages", DEFAULT_ELIDE_KEEP_RECENT
        )
        self.elide_interval_messages = load_config().get(
            "elide_interval_messages", DEFAULT_ELIDE_INTERVAL_MESSAGES
        )
        self.elide_every_requests = load_config().get(
            "elide_every_requests", DEFAULT_ELIDE_EVERY_REQUESTS
        )
        self.elide_user_message_head_chars = load_elide_user_message_head_chars()
        self.compaction_prompt = load_compaction_prompt()
        self.session_id = session_id
        self.messages: list[dict] = []
        self._pending_failed_messages: list[dict] = []
        self._user_message_seq = 0
        self._archive_nonce = secrets.token_hex(8)
        self.dump_path = get_session_dump_path(session_id)
        self._cancel_event = threading.Event()
        self._tool_call_seq = 0
        self._tool_errored = False
        self._row_open = False
        self._requests_since_elide = 0
        self.thinking_effort = load_thinking_effort()
        self.client = create_model_client()
        self.model = self._require_model()
        self.tool_definitions = get_tool_definitions()
        msg = {"role": "system", "content": build_prompt()}
        self.messages.append(msg)
        get_logger().info(str(msg))

    def cancel(self):
        """Request the interruption of the current turn at its next pause."""
        self._cancel_event.set()

    def clear_cancel(self):
        """Forget any pending cancellation request."""
        self._cancel_event.clear()

    def close(self):
        """Release the underlying model client's HTTP resources."""
        self.client.__exit__(None, None, None)

    def notify_file_upload(self, file_name: str, pod_path: str):
        """Record a user file upload in the conversation history."""
        logger = get_logger()
        notice = (
            f"The user uploaded the file '{file_name}'."
            f" It is now available in the pod at '{pod_path}'."
        )
        msg = {
            "role": "user",
            "content": wrap_channel("HARNESS_NOTICE", harness_notice(notice)),
        }
        self.messages.append(msg)
        logger.info(str(msg))

    def _prepare_user_message(self, user_text: str) -> str:
        """Archive a large user message verbatim and annotate it with the path.

        The archive lives in the pod so the model can pipe the exact data
        into scripts; the marker tells the model the path immediately and
        drives the elision of the message later on. Archiving never blocks
        the turn: on any failure the message travels unannotated, exactly
        as it would without this feature.
        """
        if (
            not self.persist_outputs
            or len(user_text) < load_user_message_archive_min_chars()
        ):
            return user_text
        self._user_message_seq += 1
        pod_path = archive_user_message(
            user_text, get_container(), self._user_message_seq
        )
        if pod_path is None:
            self._user_message_seq -= 1
            return user_text
        return (
            user_text
            + "\n\n"
            + USER_MESSAGE_MARKER_TEMPLATE.format(
                path=pod_path, nonce=self._archive_nonce
            )
        )

    def _reviewer_log(self) -> list[dict]:
        """The conversation snapshot the work reviewer receives.

        The pending review_work call is filtered out of the trailing
        assistant message — the reviewer should judge the task's results
        and methodology, not the review request itself — while its
        sibling calls in the same batch (think notes, other tools) stay
        in the snapshot. The message disappears entirely only when the
        review call was its sole content.
        """
        messages = list(self.messages)
        last = messages[-1] if messages else None
        if last is None or last.get("role") != "assistant":
            return messages
        calls = last.get("tool_calls") or []
        kept = [
            call
            for call in calls
            if not (
                isinstance(call, dict)
                and (call.get("function") or {}).get("name") == REVIEW_TOOL_NAME
            )
        ]
        if len(kept) == len(calls):
            return messages
        trimmed = dict(last)
        if kept:
            trimmed["tool_calls"] = kept
            messages[-1] = trimmed
            return messages
        trimmed.pop("tool_calls", None)
        if not (trimmed.get("content") or "").strip():
            messages.pop()
        else:
            messages[-1] = trimmed
        return messages

    def _sanitize_reply(self, response: str) -> str | None:
        """Sanitize gate checking whether response attempts channel impersonation."""
        if contains_sentinel(response):
            return (
                "Channel impersonation detected: reply contains reserved"
                " system sentinel characters."
            )
        return None

    def _purge_pending_failures(self):
        """Splice out consecutive failed attempts and their notices from history."""
        if not self._pending_failed_messages:
            return
        purged_count = len(self._pending_failed_messages)
        failed_ids = {id(msg) for msg in self._pending_failed_messages}
        self.messages[:] = [msg for msg in self.messages if id(msg) not in failed_ids]
        self._pending_failed_messages.clear()
        get_logger().info(
            "Purged %d malformed attempt messages from history after successful tool call",
            purged_count,
        )

    CANCELLED_CALL_NOTICE = (
        "Skipped: the user interrupted the task before this call ran."
    )
    BUDGET_CALL_NOTICE = "Skipped: the step budget ran out; no tools may run."
    INTERNAL_CALL_NOTICE = (
        "Skipped: an internal error occurred while running this batch."
    )

    def _close_tool_call_group(self, assistant_msg, notice: str) -> int:
        """Answer every tool_call of an assistant message left without a result.

        Providers reject a request whose assistant tool_calls are not all
        answered by role:"tool" messages, so any abnormal exit between the
        reply and the result loop (user interruption, internal error, a
        stray tool call the provider emitted despite tool_choice "none")
        must close the group with synthetic skipped results. Also fills in
        missing call ids in place so the pair stays consistent. Returns the
        number of synthetic messages appended.
        """
        tool_calls = (assistant_msg or {}).get("tool_calls") or []
        if not tool_calls:
            return 0
        try:
            start = next(
                index
                for index, message in enumerate(self.messages)
                if message is assistant_msg
            )
        except StopIteration:
            return 0
        answered = {
            message.get("tool_call_id")
            for message in self.messages[start + 1 :]
            if message.get("role") == "tool"
        }
        logger = get_logger()
        appended = 0
        for position, call in enumerate(tool_calls, start=1):
            if isinstance(call, dict):
                if not call.get("id"):
                    call["id"] = f"synthesized_{position:04d}"
                call_id = call["id"]
            else:
                call_id = getattr(call, "id", "") or f"synthesized_{position:04d}"
            if call_id in answered:
                continue
            message = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": format_tool_result_content(status="skipped", output=notice),
            }
            self.messages.append(message)
            logger.info(str(message))
            appended += 1
        return appended

    def run_turn(self, user_text: str):
        """Run one full think/act cycle for a user message."""
        logger = get_logger()
        msg = {"role": "user", "content": self._prepare_user_message(user_text)}
        self.messages.append(msg)
        logger.info(str(msg))

        no_tool_streak = 0
        steps_used = 0
        step_warning_sent = False
        repeated_calls: dict[str, int] = {}
        repeated_thinks: dict[str, int] = {}
        while True:
            try:
                self._check_cancelled()
                steps_used += 1
                remaining_steps = self.max_steps_per_turn - steps_used
                if remaining_steps < 0:
                    self._end_turn_at_step_budget(logger)
                    return
                if (
                    not step_warning_sent
                    and remaining_steps <= self.step_warning_remaining
                ):
                    step_warning_sent = True
                    self._append_harness_notice(
                        f"You have {remaining_steps} steps left. Finish up:"
                        " save your notes, verify your results, and deliver"
                        " your final answer.",
                        "Step budget warning sent.",
                    )
                self._requests_since_elide += 1
                self._compact_history_if_needed()
                self._emit({"type": "status", "text": "Thinking..."})
                response_msg = ask_question(
                    self.client,
                    self.model,
                    self.messages,
                    delay=self.request_delay,
                    effort=self.thinking_effort,
                    dump_path=self.dump_path,
                    accept_reply=self._sanitize_reply,
                    session_id=self.session_id,
                    tools=self.tool_definitions,
                    tool_choice="auto",
                )
            except ReplyRejected as exc:
                defanged = defang_sentinels(exc.raw_content)
                assistant_msg = {"role": "assistant", "content": defanged}
                self.messages.append(assistant_msg)
                self._pending_failed_messages.append(assistant_msg)
                logger.info(str(assistant_msg))

                no_tool_streak += 1
                if no_tool_streak >= MAX_NO_TOOL_RETRIES:
                    self._abort_turn(logger)
                    return
                self._append_corrective_warning(
                    exc.raw_content,
                    build_sentinel_violation_warning(),
                    "Reserved sentinel detected, retrying...",
                )
                continue
            except TurnCancelled:
                self._finish_cancelled_turn()
                return
            except Exception as exc:
                logger.exception("Model request failed")
                self._emit({"type": "error", "text": f"Model request failed: {exc}"})
                return

            tool_calls = response_msg.get("tool_calls")
            if not tool_calls:
                content = response_msg.get("content") or ""
                self._purge_pending_failures()
                self._emit({"type": "model_message", "content": content})
                return

            set_conversation_log_provider(self._reviewer_log)
            try:
                outcome = parse_and_execute_tools(
                    tool_calls,
                    on_tool_event=self._on_tool_event,
                    echo_errors=not self.emit_tool_errors,
                    persist_outputs=self.persist_outputs,
                )
            except TurnCancelled:
                self._close_tool_call_group(response_msg, self.CANCELLED_CALL_NOTICE)
                self._finish_cancelled_turn()
                return
            except Exception as exc:
                logger.exception("Tool batch execution failed")
                self._close_tool_call_group(response_msg, self.INTERNAL_CALL_NOTICE)
                self._emit(
                    {
                        "type": "error",
                        "text": f"Tool batch execution failed: {exc}",
                    }
                )
                return
            finally:
                pop_conversation_log_provider()

            tool_messages: list[dict] = []
            for entry in outcome.outputs:
                tool_content = format_tool_result_content(
                    status=entry["status"],
                    output=entry["output"],
                    exit_code=entry.get("exit_code"),
                    saved_to=entry.get("saved_to"),
                )
                tmsg = {
                    "role": "tool",
                    "tool_call_id": entry["tool_call_id"],
                    "content": tool_content,
                }
                self.messages.append(tmsg)
                tool_messages.append(tmsg)
                logger.info(str(tmsg))

            never_ran = [result for result in outcome.outputs if result.get("error")]
            if len(never_ran) == len(outcome.outputs) and outcome.outputs:
                self._pending_failed_messages.append(response_msg)
                self._pending_failed_messages.extend(tool_messages)
                no_tool_streak += 1
                if no_tool_streak >= MAX_NO_TOOL_RETRIES:
                    self._abort_turn(logger)
                    return
                self._append_corrective_warning(
                    json.dumps(tool_calls),
                    build_unparsed_tool_warning(
                        [result["output"] for result in never_ran]
                    ),
                    "No executable tool call detected, retrying...",
                )
                continue

            no_tool_streak = 0
            self._purge_pending_failures()
            self._nudge_on_repeated_calls(outcome.outputs, repeated_calls)
            self._nudge_on_repeated_thinks(outcome.think_signatures, repeated_thinks)

    def _nudge_on_repeated_calls(self, outputs: list[dict], counts: dict[str, int]):
        """Warn once when identical calls repeat within the same turn."""
        for result in outputs:
            if result["name"] == "think" or result.get("error"):
                continue
            signature = result["signature"]
            counts[signature] = counts.get(signature, 0) + 1
            if counts[signature] == REPEATED_CALL_LIMIT:
                short_signature = signature[:160]
                self._append_harness_notice(
                    "You have made this identical call"
                    f" {REPEATED_CALL_LIMIT} times: {short_signature}."
                    " Repeating it will not change the result. Read the error,"
                    " change something, or try a different approach.",
                    "Repeated identical call detected.",
                )

    def _nudge_on_repeated_thinks(self, signatures: list[str], counts: dict[str, int]):
        """Warn once when identical think notes repeat within the same turn."""
        for signature in signatures:
            counts[signature] = counts.get(signature, 0) + 1
            if counts[signature] == REPEATED_CALL_LIMIT:
                self._append_harness_notice(
                    "You have recorded this identical think note"
                    f" {REPEATED_CALL_LIMIT} times: {signature[:160]}."
                    " Repeating it will not change the result. Act on it, or"
                    " move on.",
                    "Repeated identical think note detected.",
                )

    def _append_harness_notice(self, notice: str, status_text: str):
        """Append a mid-turn harness notice that does not count as a failure."""
        logger = get_logger()
        logger.info("%s: %s", status_text, notice)
        self._emit({"type": "status", "text": status_text, "quiet": True})
        msg = {
            "role": "user",
            "content": wrap_channel("HARNESS_NOTICE", harness_notice(notice)),
        }
        self.messages.append(msg)
        logger.info(str(msg))

    def _append_corrective_warning(self, response: str, warning: str, status_text: str):
        """Append a corrective harness notice directly after the failed reply."""
        logger = get_logger()
        logger.warning("%s: %.200r", status_text, response)
        self._emit({"type": "status", "text": status_text, "quiet": True})
        msg = {
            "role": "user",
            "content": wrap_channel("HARNESS_NOTICE", harness_notice(warning)),
        }
        self.messages.append(msg)
        self._pending_failed_messages.append(msg)
        logger.info(str(msg))

    def _elide_old_outputs(self, history, total_tokens, logger) -> int:
        """Elide saved tool outputs and archived user messages once history passes its interval.

        Elision runs when the message count reaches `elide_interval_messages`
        — keeping prompts small throughout a long run without waiting for the
        token budget — and whenever the budget is exceeded regardless of
        count. To keep the provider prompt cache warm, the rewrite itself is
        batched: outside the budget-exceeded path it only runs every
        `elide_every_requests` requests. Every output still becomes a pointer
        eventually; it simply stays full-text a few requests longer. Lossless:
        elided output stays readable in the pod at its saved_to path, and an
        archived user message at its archive path.
        """
        interval = self.elide_interval_messages
        interval_reached = bool(interval) and len(history) >= interval
        budget_exceeded = total_tokens > self.context_budget_tokens
        if not (interval_reached or budget_exceeded):
            return 0
        batch_due = self._requests_since_elide >= self.elide_every_requests
        if not (budget_exceeded or batch_due):
            return 0
        elided = elide_tool_outputs(self.messages, self.elide_keep_recent_messages)
        elided += elide_archived_user_messages(
            self.messages,
            self.elide_keep_recent_messages,
            self.elide_user_message_head_chars,
            self._archive_nonce,
        )
        self._requests_since_elide = 0
        if elided:
            logger.info("Elided %d old tool outputs from history", elided)
            self._emit(
                {
                    "type": "status",
                    "text": "Trimmed old tool outputs...",
                    "quiet": True,
                }
            )
        return elided

    @staticmethod
    def _tail_atomic_slice(history: list[dict]) -> list[dict]:
        """Return the trailing message group that must stay intact."""
        if not history:
            return []
        if history[-1].get("role") == "tool":
            idx = len(history) - 1
            while idx >= 0 and history[idx].get("role") == "tool":
                idx -= 1
            if idx >= 0 and history[idx].get("role") == "assistant":
                return history[idx:]
        return [history[-1]]

    def _compact_history_if_needed(self):
        """Keep history within budget: elide old outputs, then summarize.

        Eliding is lossless — elided outputs remain readable in the pod at
        their saved_to paths. It runs on a batched message-count interval so
        prompts stay small throughout a long run without rewriting history
        before every request, and additionally whenever the token budget is
        exceeded. Summarization is the lossy fallback and only runs when the
        message count hits its interval or the token budget is still exceeded
        after eliding. The first user message is excluded from summarization
        and stays verbatim in history, but it is shown to the summarizer as a
        labeled block so the summary represents the user's request faithfully.
        """
        if not self.compaction_enabled:
            return
        logger = get_logger()
        history = self.messages[1:]
        total_tokens = estimate_history_tokens(self.messages)
        if self._elide_old_outputs(history, total_tokens, logger):
            total_tokens = estimate_history_tokens(self.messages)
        if (
            len(history) < self.max_history_messages
            and total_tokens <= self.context_budget_tokens
        ):
            return
        compactable = self._select_compactable(history)
        if not compactable:
            return
        pinned_index = self._first_user_index(history)
        pinned_content = (
            history[pinned_index].get("content") if pinned_index is not None else None
        )
        pinned_message = pinned_content if isinstance(pinned_content, str) else None
        self._emit({"type": "status", "text": "Compacting history..."})
        try:
            summary_msg = ask_question(
                self.client,
                self.model,
                build_compaction_messages(
                    self.compaction_prompt, compactable, pinned_message
                ),
                delay=self.request_delay,
                effort=self.thinking_effort,
                response_format=None,
                session_id=self.session_id,
            )
            summary = (
                summary_msg.get("content") or ""
                if isinstance(summary_msg, dict)
                else str(summary_msg)
            )
        except Exception as exc:
            logger.exception("History compaction failed")
            self._emit(
                {
                    "type": "error",
                    "text": (
                        "History compaction failed; continuing with full"
                        f" history: {exc}"
                    ),
                }
            )
            return
        summary_message = {
            "role": "user",
            "content": format_compaction_message(summary),
        }
        logger.info(str(summary_message))
        tail_group = self._tail_atomic_slice(history)
        first_user_idx = self._first_user_index(history)
        kept = [history[first_user_idx]] if first_user_idx is not None else []
        self.messages[:] = [
            self.messages[0],
            *kept,
            summary_message,
            *tail_group,
        ]
        self._pending_failed_messages.clear()
        self._emit({"type": "compaction", "summary": summary})
        logger.info(
            "Compacted %d messages into a summary (interval=%d)",
            len(compactable),
            self.max_history_messages,
        )

    def _select_compactable(self, history: list[dict]) -> list[dict]:
        """Return the history slice eligible for compaction."""
        first_user_idx = self._first_user_index(history)
        tail_group = self._tail_atomic_slice(history)
        cutoff = len(history) - len(tail_group)
        return [
            message
            for index, message in enumerate(history[:cutoff])
            if index != first_user_idx
        ]

    @staticmethod
    def _first_user_index(history: list[dict]) -> int | None:
        """Return the index of the first non-system user message in the history."""
        return next(
            (
                index
                for index, message in enumerate(history)
                if message.get("role") == "user"
                and not is_channel_message(message.get("content") or "")
            ),
            None,
        )

    def _abort_turn(
        self, logger, notice: str | None = None, error_text: str | None = None
    ):
        """Record the abort in the history and hand control back to the user."""
        if notice is None:
            notice = (
                "The task was aborted because you weren't able to"
                " follow the instructions to call a tool properly."
            )
        if error_text is None:
            error_text = (
                "Task aborted: no valid tool call after"
                f" {MAX_NO_TOOL_RETRIES} consecutive attempts."
            )
        msg = {
            "role": "user",
            "content": wrap_channel("HARNESS_NOTICE", harness_notice(notice)),
        }
        self.messages.append(msg)
        self._pending_failed_messages.clear()
        logger.info(str(msg))
        self._emit({"type": "error", "text": error_text})

    def _end_turn_at_step_budget(self, logger):
        """End the turn at budget exhaustion after one final status report.

        The step budget is spent, so no tool may run: the model gets a single
        request with tool_choice='none' delivering the status report the
        wrap-up warning asked it to keep ready.
        """
        logger.info("Step budget exhausted; requesting final status report.")
        self._append_harness_notice(
            STEP_BUDGET_OUT_NOTICE,
            "Step budget exhausted; requesting final status report.",
        )
        try:
            self._emit({"type": "status", "text": "Thinking..."})
            response_msg = ask_question(
                self.client,
                self.model,
                self.messages,
                delay=self.request_delay,
                effort=self.thinking_effort,
                dump_path=self.dump_path,
                accept_reply=self._sanitize_reply,
                session_id=self.session_id,
                tools=self.tool_definitions,
                tool_choice="none",
            )
        except TurnCancelled:
            self._finish_cancelled_turn()
            return
        except Exception as exc:
            logger.exception("Final status report request failed")
            self._emit({"type": "error", "text": f"Model request failed: {exc}"})
            return

        if isinstance(response_msg, dict):
            tool_calls = response_msg.get("tool_calls") or []
            if tool_calls:
                self._close_tool_call_group(response_msg, self.BUDGET_CALL_NOTICE)
            report = response_msg.get("content") or ""
        else:
            tool_calls = []
            report = str(response_msg)
        if not report.strip():
            if (
                not tool_calls
                and self.messages
                and self.messages[-1].get("role") == "assistant"
            ):
                self.messages.pop()
            self._pending_failed_messages.clear()
            self._emit(
                {
                    "type": "error",
                    "text": (
                        f"Step budget exhausted after {self.max_steps_per_turn} steps."
                    ),
                }
            )
            return
        self._purge_pending_failures()
        self._emit({"type": "model_message", "content": report})

    def _finish_cancelled_turn(self):
        """Record the interruption in the conversation and notify listeners."""
        logger = get_logger()
        msg = {
            "role": "user",
            "content": wrap_channel(
                "HARNESS_NOTICE",
                harness_notice("The user interrupted the task."),
            ),
        }
        self.messages.append(msg)
        logger.info(str(msg))
        self._emit({"type": "status", "text": "Interrupted."})

    def _on_tool_event(
        self,
        status,
        name,
        signature,
        output=None,
        raw="",
        arguments=None,
        failed=False,
        structured_output=None,
        **extra,
    ):
        """Translate parser tool events into session events."""
        if status == "error":
            self._tool_errored = True
            if self._row_open or not self.emit_tool_errors:
                return
            self._tool_call_seq += 1
            base = {
                "type": "tool_call",
                "id": self._tool_call_seq,
                "name": name,
                "signature": signature,
                "raw": _detail_text(raw),
                "arguments": arguments,
                "parse_error": True,
            }
            self._emit({**base, "status": "started"})
            self._emit(
                {
                    **base,
                    "status": "finished",
                    "output": _detail_text(output),
                    "output_preview": _preview_text(output),
                    "failed": True,
                }
            )
            return
        if status == "started":
            self._check_cancelled()
            self._tool_call_seq += 1
            self._tool_errored = False
            self._row_open = True
            self._emit(
                {
                    "type": "tool_call",
                    "id": self._tool_call_seq,
                    "status": "started",
                    "name": name,
                    "signature": signature,
                    "raw": _detail_text(raw),
                    "arguments": arguments,
                }
            )
            return

        self._row_open = False
        event = {
            "type": "tool_call",
            "id": self._tool_call_seq,
            "status": "finished",
            "name": name,
            "signature": signature,
            "raw": _detail_text(raw),
            "arguments": arguments,
            "output": _detail_text(output),
            "output_preview": _preview_text(output),
        }
        if self._tool_errored or failed:
            event["failed"] = True
        if structured_output is not None:
            event["structured_output"] = structured_output
        self._emit(event)

    def _check_cancelled(self):
        if self._cancel_event.is_set():
            raise TurnCancelled

    def _emit(self, event):
        self.on_event(event)

    def _require_model(self):
        model = get_env().get("MODEL") or DEFAULT_MODEL
        return model
