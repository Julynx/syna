"""Reusable agent session shared by the CLI and the web server."""

import json
import threading

from openrouter import OpenRouter
from openrouter.utils import BackoffStrategy, RetryConfig

from .ask_utils import ask_question
from .compaction import (
    COMPACTION_NOTE_PREFIX,
    build_compaction_messages,
    load_compaction_prompt,
)
from .config import (
    get_env,
    get_logger,
    get_project_root,
    get_session_dump_path,
    load_config,
)
from .parse import parse_and_execute_tools

OUTPUT_PREVIEW_CHARS = 512
DETAIL_CAP_CHARS = 6000
DEFAULT_REQUEST_DELAY_S = 2
DEFAULT_MAX_HISTORY_MESSAGES = 100
DEFAULT_ENABLE_COMPACTION = True
MAX_NO_TOOL_RETRIES = 3
SYSTEM_NOTE_PREFIX = "[System]: "

VALID_THINKING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def load_thinking_effort():
    """Return the THINKING_EFFORT value from .env, or None when unset.

    The value maps straight onto OpenRouter's 'reasoning.effort' parameter;
    unset means the model's default behavior is left untouched. An invalid
    value raises here so a typo fails fast at session creation instead of
    failing every model request.
    """
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


def build_tool_format_warning(response: str) -> str:
    """Compose the retry warning for a response without a tool call.

    The rejected reply stays in the history right before this warning, so
    the model can see and fix its own mistake. When it contains recognizable
    tool-call markup from other agent frameworks, the warning names it
    explicitly so the model can correct the specific mistake instead of
    guessing.
    """
    warning = (
        "WARNING: Your previous message contained no executable tool calls.\n"
        "- To continue working on your task, call at least one tool as such:\n"
        "  ```tool\n"
        r'     {"tool_name": {"argument": "value"}}'
        "\n"
        "  ```\n"
        '- To end the task and talk to the user, call the tool "respond".\n'
        "Repeated violations of this rule will get you terminated."
    )
    if "<tool_call>" in response or "<function=" in response:
        warning += (
            "Your message used tool-call markup such as"
            " <tool_call><function=...><parameter=...>, which is not"
            " recognized and will never be parsed. Do not use it. Write the"
            " JSON object directly inside a ```tool ...``` fenced block"
            " instead.\n"
        )
    return warning


def build_unparsed_tool_warning(reasons: list[str] | None = None) -> str:
    """Compose the corrective warning for tool markup that executed nothing.

    Without this feedback the model cannot tell 'my call was never parsed'
    apart from 'the tools returned nothing', which leads it to invent a
    tool outage and retry the same malformed format for dozens of turns.
    The parser's own reasons (including structural errors) are embedded so
    the model can fix the exact mistake instead of guessing. The failed
    reply stays in the history right before this warning.
    """
    warning = (
        "WARNING: Your reply contained ```tool ...``` markup but no"
        " executable tool call ran."
    )
    if reasons:
        listed = "".join(f"\n- {reason[:200]}" for reason in reasons[:3])
        warning += f" The parser reported:{listed}"
    warning += (
        "\nA tool block must look exactly like this:\n"
        "```tool\n"
        '{"tool_name": {"argument": "value"}}\n'
        "```\n"
        "Rebuild the call(s) in full and send them again."
    )
    return warning


class TurnCancelled(Exception):
    """Raised when the user interrupts the current agent turn."""


def build_prompt():
    """Build the system prompt template populated with tool definitions."""
    root = get_project_root()
    template = (root / "assets" / "prompt.md").read_text(encoding="utf-8")
    tools = _get_tool_metadata(root / "src" / "syna" / "tools.py")

    tool_blocks = []
    for tool in tools:
        args_dict = {arg["name"]: f"<{arg['type']}>" for arg in tool["arguments"]}
        body_json = json.dumps({tool["name"]: args_dict}, indent=2)
        tool_blocks.append(f"### {tool['name']}")
        tool_blocks.append(f"{tool['docstring']}")
        tool_blocks.append(f"```tool\n{body_json}\n```")

    tool_str = "\n\n".join(tool_blocks)
    return template.replace("{tools}", tool_str)


def _get_tool_metadata(tools_path):
    from .inspect_module import get_function_metadata

    return get_function_metadata(tools_path)


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
    """Bound a call-detail string for front-end transport.

    Detail views are indicative only; the full, uncapped text stays in the
    message dump and the log. Truncated tails carry an ellipsis marker.
    """
    text = str(text or "")
    if len(text) > DETAIL_CAP_CHARS:
        return text[:DETAIL_CAP_CHARS] + "..."
    return text


class AgentSession:
    """One conversation with the model plus its tool execution loop.

    'on_event' receives JSON-serializable dicts describing what happens in
    real time (tool calls, model messages, status changes, errors). It is
    invoked synchronously from whatever thread calls 'run_turn'.
    """

    def __init__(
        self,
        on_event=None,
        request_delay: float = DEFAULT_REQUEST_DELAY_S,
        emit_tool_errors: bool = False,
        session_id: str | None = None,
    ):
        self.on_event = on_event or (lambda event: None)
        self.request_delay = request_delay
        self.emit_tool_errors = emit_tool_errors
        self.max_history_messages = load_config().get(
            "max_history_messages", DEFAULT_MAX_HISTORY_MESSAGES
        )
        self.compaction_enabled = load_config().get(
            "enable_compaction", DEFAULT_ENABLE_COMPACTION
        )
        self.compaction_prompt = load_compaction_prompt()
        self.messages: list[dict] = []
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
        """Release the underlying model client's HTTP resources.

        The OpenRouter SDK exposes cleanup through the context-manager
        protocol instead of a public 'close' method, so teardown goes
        through '__exit__'.
        """
        self.client.__exit__(None, None, None)

    def notify_file_upload(self, file_name: str, pod_path: str):
        """Record a user file upload in the conversation history.

        Adds a system note so the model knows the upload happened and where
        the file lives inside the pod.
        """
        logger = get_logger()
        msg = {
            "role": "user",
            "content": (
                f"[System]: The user uploaded the file '{file_name}'."
                f" It is now available in the pod at '{pod_path}'."
            ),
        }
        self.messages.append(msg)
        logger.info(str(msg))

    def run_turn(self, user_text: str):
        """Run one full think/act cycle for a user message. Blocking.

        A response without an executable tool call — no calls at all,
        markup that never parsed, or a structurally broken block — stays in
        the history and is followed directly by a corrective warning, so
        the model can see and fix its own mistake. After
        'MAX_NO_TOOL_RETRIES' consecutive failures the turn is aborted: the
        model receives an ultimatum, the user gets a warning, and control
        returns to the caller. Any reply that dispatches at least one tool
        call resets the failure streak.
        """
        logger = get_logger()
        msg = {"role": "user", "content": user_text}
        self.messages.append(msg)
        logger.info(str(msg))

        no_tool_streak = 0
        while True:
            try:
                self._check_cancelled()
                self._compact_history_if_needed()
                self._emit({"type": "status", "text": "Thinking..."})
                response = ask_question(
                    self.client,
                    self.model,
                    self.messages,
                    delay=self.request_delay,
                    effort=self.thinking_effort,
                    dump_path=self.dump_path,
                )
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
                )
            except TurnCancelled:
                self._finish_cancelled_turn()
                return

            if outcome.structural_error is not None:
                no_tool_streak += 1
                if no_tool_streak >= MAX_NO_TOOL_RETRIES:
                    self._abort_turn(logger)
                    return
                self._append_corrective_warning(
                    response,
                    build_unparsed_tool_warning([outcome.structural_error]),
                    "Malformed tool block, retrying...",
                )
                continue

            if not outcome.outputs:
                no_tool_streak += 1
                if no_tool_streak >= MAX_NO_TOOL_RETRIES:
                    self._abort_turn(logger)
                    return
                self._append_corrective_warning(
                    response,
                    build_tool_format_warning(response),
                    "No tool detected, retrying...",
                )
                continue

            never_ran = [result for result in outcome.outputs if result.get("error")]
            if len(never_ran) == len(outcome.outputs):
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
            tool_results_str = json.dumps(outcome.outputs)
            msg = {"role": "user", "content": f"[Tool]: {tool_results_str}"}
            self.messages.append(msg)
            logger.info(str(msg))

            respond_tool = next(
                (tool for tool in outcome.outputs if tool["name"] == "respond"),
                None,
            )
            if respond_tool:
                self._emit({"type": "model_message", "content": respond_tool["output"]})
                return

    def _append_corrective_warning(self, response: str, warning: str, status_text: str):
        """Append a corrective [System] warning directly after the failed reply.

        The assistant reply stays in the history: the model has no memory
        beyond the message array, so the warning must immediately follow the
        reply it corrects. Also logs a reply excerpt and emits a status
        event for frontends.
        """
        logger = get_logger()
        logger.warning("%s: %.200r", status_text, response)
        self._emit({"type": "status", "text": status_text, "quiet": True})
        msg = {"role": "user", "content": f"[System]: {warning}"}
        self.messages.append(msg)
        logger.info(str(msg))

    def _compact_history_if_needed(self):
        """Replace old history with a summary once it reaches the interval.

        Fires when the non-system history length reaches
        'max_history_messages'. The system prompt, the conversation's first
        real user message and the newest message (the one that crossed the
        threshold) stay outside the summary, which replaces everything else.
        A failed compaction keeps the history intact and is retried at the
        next crossing; it never aborts the turn.
        """
        if not self.compaction_enabled:
            return
        history = self.messages[1:]
        if len(history) < self.max_history_messages:
            return
        compactable = self._select_compactable(history)
        if not compactable:
            return
        logger = get_logger()
        self._emit({"type": "status", "text": "Compacting history..."})
        try:
            summary = ask_question(
                self.client,
                self.model,
                build_compaction_messages(self.compaction_prompt, compactable),
                delay=self.request_delay,
                effort=self.thinking_effort,
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
            "content": (
                f"{COMPACTION_NOTE_PREFIX}The conversation grew too long, so"
                " the earlier messages (except the first) were replaced by"
                f" the following summary:\n\n{summary}"
            ),
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
        self._emit({"type": "compaction", "summary": summary})
        logger.info(
            "Compacted %d messages into a summary (interval=%d)",
            len(compactable),
            self.max_history_messages,
        )

    def _select_compactable(self, history: list[dict]) -> list[dict]:
        """Return the history slice eligible for compaction.

        Everything except the first real user message and the newest message;
        system notes that precede the first user message take part in the
        compaction so no context is silently lost.
        """
        first_user_idx = self._first_user_index(history)
        return [
            message
            for index, message in enumerate(history[: len(history) - 1])
            if index != first_user_idx
        ]

    @staticmethod
    def _first_user_index(history: list[dict]) -> int | None:
        """Return the index of the first non-system message in the history."""
        return next(
            (
                index
                for index, message in enumerate(history)
                if not message["content"].startswith(SYSTEM_NOTE_PREFIX)
            ),
            None,
        )

    def _abort_turn(self, logger):
        """Record the abort in the history and hand control back to the user."""
        msg = {
            "role": "user",
            "content": (
                "[System]: The task was aborted because you weren't able to"
                " follow the instructions to call a tool properly."
            ),
        }
        self.messages.append(msg)
        logger.info(str(msg))
        self._emit(
            {
                "type": "error",
                "text": (
                    "Task aborted: no valid tool call after"
                    f" {MAX_NO_TOOL_RETRIES} consecutive attempts."
                ),
            }
        )

    def _finish_cancelled_turn(self):
        """Record the interruption in the conversation and notify listeners."""
        logger = get_logger()
        msg = {"role": "user", "content": "[System]: The user interrupted the task."}
        self.messages.append(msg)
        logger.info(str(msg))
        self._emit({"type": "status", "text": "Interrupted."})

    def _on_tool_event(
        self, status, name, signature, output=None, raw="", arguments=None
    ):
        """Translate parser tool events into session events.

        Tool calls that never reached dispatch (undecodable markup, wrong
        shape, unregistered tools, signature violations) have no open row:
        when the session reports errors to front-ends ('emit_tool_errors'),
        they become a started/finished pair carrying 'failed': True so the
        client renders a failed tool row instead of a critical error pill.
        Execution failures land between an open row's started and finished
        events and are flagged on that finished event instead. With
        'emit_tool_errors' False (the CLI) the parser echoes failures to
        stdout and no rows are synthesized.

        'raw' (the block's exact JSON text) and 'arguments' (the parsed
        argument mapping) ride along capped so front-ends can render call
        details without extra requests.
        """
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
        if self._tool_errored:
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
