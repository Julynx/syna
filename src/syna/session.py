"""Reusable agent session shared by the CLI and the web server."""

import threading
from datetime import date

from openrouter import OpenRouter
from openrouter.utils import BackoffStrategy, RetryConfig

from .ask_utils import ReplyRejected, ask_question
from .channel import (
    contains_sentinel,
    defang_sentinels,
    escape_channel_text,
    harness_notice,
    is_channel_message,
    wrap_channel,
)
from .compaction import (
    build_compaction_messages,
    format_compaction_message,
    load_compaction_prompt,
)
from .context import elide_tool_outputs, estimate_history_tokens
from .config import (
    get_env,
    get_logger,
    get_project_root,
    get_session_dump_path,
    load_command_timeout_s,
    load_config,
    load_output_limit_chars,
)
from .parse import parse_and_execute_tools

OUTPUT_PREVIEW_CHARS = 512
DETAIL_CAP_CHARS = 6000
DEFAULT_REQUEST_DELAY_S = 2
DEFAULT_MAX_HISTORY_MESSAGES = 100
DEFAULT_ENABLE_COMPACTION = True
DEFAULT_MAX_STEPS_PER_TURN = 100
DEFAULT_STEP_WARNING_REMAINING = 20
DEFAULT_CONTEXT_BUDGET_TOKENS = 100000
DEFAULT_ELIDE_KEEP_RECENT = 12
MAX_NO_TOOL_RETRIES = 3
REPEATED_CALL_LIMIT = 3

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
    """Compose the corrective warning for tool markup that executed nothing."""
    warning = "Your reply could not be executed as a valid tool call."
    if reasons:
        listed = "".join(f"\n- {reason[:250]}" for reason in reasons[:3])
        warning += f" The parser reported:{listed}"
    warning += (
        "\n\nEvery reply must be a JSON array of one or more tool calls"
        ' of the form {"tool_name": {"argument": "value"}} (a single JSON'
        " object is also tolerated). Nothing outside the JSON is executed."
        " After repeated violations the task is ended and control returns to the user."
    )
    return warning


def build_discarded_text_warning(warnings: list[str]) -> str:
    """Compose the notice for a think-only reply that carried extra text."""
    listed = " ".join(warnings)[:400]
    return (
        "Your reply contained only 'think' calls, which is valid, but text"
        f" around the JSON was discarded: {listed}"
    )


def build_sentinel_violation_warning() -> str:
    """Compose the corrective warning for channel impersonation attempts."""
    return (
        "Your reply contained reserved system channel sentinels."
        " Results and notices arrive through a separate channel that you"
        " cannot write. Provide pure JSON tool calls only."
        " After repeated violations the task is ended and control returns to the user."
    )


def build_tool_results_body(outputs: list[dict], warnings: list[str]) -> str:
    """Render tool results and parser warnings in the protocol's markers.

    Each result is labeled with its position in the model's array, the tool
    name, its status, and — when available — exit code and the pod path
    holding the full output. Untrusted output text is escaped so it cannot
    forge the surrounding markers.
    """
    parts = ["<tool_results>"]
    for entry in outputs:
        attributes = (
            f'call="{entry["call"]}" tool="{entry["name"]}" status="{entry["status"]}"'
        )
        if entry.get("exit_code") is not None:
            attributes += f' exit_code="{entry["exit_code"]}"'
        if entry.get("saved_to"):
            attributes += f' saved_to="{entry["saved_to"]}"'
        parts.append(f"<result {attributes}>")
        parts.append(escape_channel_text(str(entry["output"])))
        parts.append("</result>")
    parts.append("</tool_results>")
    for warning in warnings:
        parts.append(harness_notice(warning))
    return "\n".join(parts)


class TurnCancelled(Exception):
    """Raised when the user interrupts the current agent turn."""


def build_prompt():
    """Build the system prompt with tool docs, date, and limits filled in."""
    root = get_project_root()
    template = (root / "assets" / "prompt.md").read_text(encoding="utf-8")
    tools = _get_tool_metadata(root / "src" / "syna" / "tools.py")

    tool_blocks = [f"### {tool['name']}\n{tool['docstring']}" for tool in tools]
    prompt = template.replace("{tools}", "\n\n".join(tool_blocks))
    prompt = prompt.replace("{{CURRENT_DATE}}", date.today().isoformat())
    prompt = prompt.replace("{{COMMAND_TIMEOUT}}", str(load_command_timeout_s()))
    prompt = prompt.replace("{{OUTPUT_LIMIT}}", str(load_output_limit_chars()))
    return prompt


def _get_tool_metadata(tools_path):
    from .inspect_module import get_function_metadata

    return get_function_metadata(tools_path)


def create_model_client():
    """Instantiate an OpenRouter client configured with timeout and retry limits."""
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
        timeout_ms=config["timeout_ms"],
        retry_config=retry_config,
    )


def _preview_text(output) -> str:
    """Flatten a tool output into a single-line bounded preview string."""
    text = str(output or "").replace("\n", " ").strip()
    return text[:OUTPUT_PREVIEW_CHARS]


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
        self.compaction_prompt = load_compaction_prompt()
        self.messages: list[dict] = []
        self._pending_failed_messages: list[dict] = []
        self.dump_path = get_session_dump_path(session_id)
        self._cancel_event = threading.Event()
        self._tool_call_seq = 0
        self._tool_errored = False
        self._row_open = False
        self.thinking_effort = load_thinking_effort()
        self.client = create_model_client()
        self.model = self._require_model()
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

    def _sanitize_reply(self, response: str) -> str | None:
        """Sanitize gate checking whether response attempts channel impersonation."""
        if contains_sentinel(response):
            return (
                "Channel impersonation detected: reply contains reserved"
                " system sentinel characters."
            )
        return None

    def _purge_pending_failures(self):
        """Splice out consecutive failed attempts and their lectures from history."""
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

    def run_turn(self, user_text: str):
        """Run one full think/act cycle for a user message."""
        logger = get_logger()
        msg = {"role": "user", "content": user_text}
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
                    self._abort_turn(
                        logger,
                        "The step budget for this task ran out. Report what was"
                        " done, what was verified, and what remains.",
                        f"Step budget exhausted after {self.max_steps_per_turn} steps.",
                    )
                    return
                if (
                    not step_warning_sent
                    and remaining_steps <= self.step_warning_remaining
                ):
                    step_warning_sent = True
                    self._append_harness_notice(
                        f"You have {remaining_steps} steps left. Finish up:"
                        " save your notes, verify your results, and respond"
                        " with your status.",
                        "Step budget warning sent.",
                    )
                self._compact_history_if_needed()
                self._emit({"type": "status", "text": "Thinking..."})
                response = ask_question(
                    self.client,
                    self.model,
                    self.messages,
                    delay=self.request_delay,
                    effort=self.thinking_effort,
                    dump_path=self.dump_path,
                    accept_reply=self._sanitize_reply,
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

            try:
                outcome = parse_and_execute_tools(
                    response,
                    on_tool_event=self._on_tool_event,
                    echo_errors=not self.emit_tool_errors,
                    persist_outputs=self.persist_outputs,
                )
            except TurnCancelled:
                self._finish_cancelled_turn()
                return

            if outcome.structural_error is not None:
                self._pending_failed_messages.append(self.messages[-1])
                no_tool_streak += 1
                if no_tool_streak >= MAX_NO_TOOL_RETRIES:
                    self._abort_turn(logger)
                    return
                self._append_corrective_warning(
                    response,
                    build_unparsed_tool_warning([outcome.structural_error]),
                    "Malformed tool call, retrying...",
                )
                continue

            if not outcome.outputs and not outcome.think_signatures:
                self._pending_failed_messages.append(self.messages[-1])
                no_tool_streak += 1
                if no_tool_streak >= MAX_NO_TOOL_RETRIES:
                    self._abort_turn(logger)
                    return
                self._append_corrective_warning(
                    response,
                    build_unparsed_tool_warning(),
                    "No executable tool call, retrying...",
                )
                continue

            if not outcome.outputs:
                no_tool_streak = 0
                self._purge_pending_failures()
                self._nudge_on_repeated_thinks(
                    outcome.think_signatures, repeated_thinks
                )
                if outcome.warnings:
                    self._append_corrective_warning(
                        response,
                        build_discarded_text_warning(outcome.warnings),
                        "Text discarded beside think calls.",
                    )
                continue

            never_ran = [result for result in outcome.outputs if result.get("error")]
            if len(never_ran) == len(outcome.outputs):
                self._pending_failed_messages.append(self.messages[-1])
                no_tool_streak += 1
                if no_tool_streak >= MAX_NO_TOOL_RETRIES:
                    self._abort_turn(logger)
                    return
                self._append_corrective_warning(
                    response,
                    build_unparsed_tool_warning(
                        [result["output"] for result in never_ran]
                    ),
                    "No executable tool call detected, retrying...",
                )
                continue

            no_tool_streak = 0
            self._purge_pending_failures()

            respond_tool = next(
                (tool for tool in outcome.outputs if tool["name"] == "respond"),
                None,
            )
            if respond_tool:
                self._emit({"type": "model_message", "content": respond_tool["output"]})
                return

            tool_results_str = build_tool_results_body(
                outcome.outputs, outcome.warnings
            )
            msg = {
                "role": "user",
                "content": wrap_channel("TOOL_RESULTS", tool_results_str),
            }
            self.messages.append(msg)
            logger.info(str(msg))
            self._nudge_on_repeated_calls(outcome.outputs, repeated_calls)
            self._nudge_on_repeated_thinks(outcome.think_signatures, repeated_thinks)

    def _nudge_on_repeated_calls(self, outputs: list[dict], counts: dict[str, int]):
        """Warn once when identical calls repeat within the same turn."""
        for result in outputs:
            if result["name"] in ("think", "respond") or result.get("error"):
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

    def _compact_history_if_needed(self):
        """Keep history within budget: elide old outputs, then summarize.

        Eliding is lossless — elided outputs remain readable in the pod at
        their saved_to paths. Summarization is the lossy fallback and only
        runs when the message count hits its interval or the token budget
        is still exceeded after eliding.
        """
        if not self.compaction_enabled:
            return
        logger = get_logger()
        history = self.messages[1:]
        total_tokens = estimate_history_tokens(self.messages)
        if total_tokens > self.context_budget_tokens:
            elided = elide_tool_outputs(self.messages, self.elide_keep_recent_messages)
            if elided:
                logger.info("Elided %d old tool outputs from history", elided)
                self._emit(
                    {
                        "type": "status",
                        "text": "Trimmed old tool outputs...",
                        "quiet": True,
                    }
                )
                total_tokens = estimate_history_tokens(self.messages)
        if (
            len(history) < self.max_history_messages
            and total_tokens <= self.context_budget_tokens
        ):
            return
        compactable = self._select_compactable(history)
        if not compactable:
            return
        self._emit({"type": "status", "text": "Compacting history..."})
        try:
            summary = ask_question(
                self.client,
                self.model,
                build_compaction_messages(self.compaction_prompt, compactable),
                delay=self.request_delay,
                effort=self.thinking_effort,
                response_format=None,
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
        first_user_idx = self._first_user_index(history)
        kept = [history[first_user_idx]] if first_user_idx is not None else []
        self.messages[:] = [
            self.messages[0],
            *kept,
            summary_message,
            history[-1],
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
        return [
            message
            for index, message in enumerate(history[: len(history) - 1])
            if index != first_user_idx
        ]

    @staticmethod
    def _first_user_index(history: list[dict]) -> int | None:
        """Return the index of the first non-system user message in the history."""
        return next(
            (
                index
                for index, message in enumerate(history)
                if message["role"] == "user"
                and not is_channel_message(message["content"])
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
        self, status, name, signature, output=None, raw="", arguments=None, failed=False
    ):
        """Translate parser tool events into session events."""
        if name == "respond":
            return
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
        if name == "share_file":
            event["output"] = output
        self._emit(event)

    def _check_cancelled(self):
        if self._cancel_event.is_set():
            raise TurnCancelled

    def _emit(self, event):
        self.on_event(event)

    def _require_model(self):
        model = get_env().get("MODEL")
        if not model:
            raise ValueError("MODEL not found in .env file.")
        return model
