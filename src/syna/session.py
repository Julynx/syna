"""Reusable agent session shared by the CLI and the web server."""

import json
import threading

from dotenv import dotenv_values
from openrouter import OpenRouter
from openrouter.utils import BackoffStrategy, RetryConfig

from .ask_utils import ask_question
from .compaction import (
    COMPACTION_NOTE_PREFIX,
    build_compaction_messages,
    load_compaction_prompt,
)
from .config import get_logger, get_project_root, load_config
from .parse import parse_and_execute_tools

dotenv = dotenv_values(get_project_root() / ".env")

OUTPUT_PREVIEW_CHARS = 512
DEFAULT_REQUEST_DELAY_S = 2
DEFAULT_MAX_HISTORY_MESSAGES = 100
DEFAULT_ENABLE_COMPACTION = True
MAX_NO_TOOL_RETRIES = 3
SYSTEM_NOTE_PREFIX = "[System]: "


def build_tool_format_warning(response: str) -> str:
    """Compose the retry warning for a response without a tool call.

    When the rejected response contains recognizable tool-call markup from
    other agent frameworks, the warning names it explicitly so the model can
    correct the specific mistake instead of guessing.
    """
    warning = (
        "WARNING: Your previous message contained no tool call, so it was"
        " discarded and never reached the user. To act you must include a"
        ' fenced block in this exact format:\n```tool\n{"tool_name":'
        ' {"argument": "value"}}\n```\n'
    )
    if "<tool_call>" in response or "<function=" in response:
        warning += (
            "Your message used tool-call markup such as"
            " <tool_call><function=...><parameter=...>, which is not"
            " recognized and will never be parsed. Do not use it. Write the"
            " JSON object directly inside the ```tool fenced block"
            " instead.\n"
        )
    warning += (
        "To speak to the user, call the 'respond' tool with your message"
        " as its 'text' argument."
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
        args_dict = {arg["name"]: arg["type"] for arg in tool["arguments"]}
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
    api_key = dotenv.get("OPENROUTER_API_KEY")
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
        self._cancel_event = threading.Event()
        self._tool_call_seq = 0
        self._tool_errored = False
        self.client = create_model_client()
        self.model = self._require_model()
        self.messages.append({"role": "system", "content": build_prompt()})

    def cancel(self):
        """Request the interruption of the current turn at its next pause."""
        self._cancel_event.set()

    def clear_cancel(self):
        """Forget any pending cancellation request."""
        self._cancel_event.clear()

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

        A response without a tool call is discarded from the history (it
        would otherwise teach the model its own malformed format by
        example) and retried with a corrective warning. After
        'MAX_NO_TOOL_RETRIES' consecutive failures the turn is aborted: the
        model receives an ultimatum, the user gets a warning, and control
        returns to the caller.
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
                    self.client, self.model, self.messages, delay=self.request_delay
                )
            except TurnCancelled:
                self._finish_cancelled_turn()
                return
            except Exception as exc:
                logger.exception("Model request failed")
                self._emit({"type": "error", "text": f"Model request failed: {exc}"})
                return

            if "```tool" not in response:
                no_tool_streak += 1
                self._discard_last_assistant_message()
                self._emit({"type": "status", "text": "No tool detected, retrying..."})
                if no_tool_streak >= MAX_NO_TOOL_RETRIES:
                    self._abort_turn(logger)
                    return
                msg = {
                    "role": "user",
                    "content": f"[System]: {build_tool_format_warning(response)}",
                }
                self.messages.append(msg)
                logger.info(str(msg))
                continue
            no_tool_streak = 0

            try:
                tool_results = parse_and_execute_tools(
                    response,
                    on_tool_event=self._on_tool_event,
                    echo_errors=not self.emit_tool_errors,
                )
            except TurnCancelled:
                self._finish_cancelled_turn()
                return

            tool_results_str = json.dumps(tool_results)
            msg = {"role": "user", "content": f"[Tool]: {tool_results_str}"}
            self.messages.append(msg)
            logger.info(str(msg))

            respond_tool = next(
                (tool for tool in tool_results if tool["name"] == "respond"),
                None,
            )
            if respond_tool:
                self._emit(
                    {"type": "model_message", "content": respond_tool["output"]}
                )
                return

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
            for index, message in enumerate(history[:len(history) - 1])
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

    def _discard_last_assistant_message(self):
        """Remove the assistant response that failed the tool-call check.

        Keeping it would leave malformed serialization examples in the
        history, which the model tends to imitate on the next attempt.
        """
        if self.messages and self.messages[-1]["role"] == "assistant":
            self.messages.pop()

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

    def _on_tool_event(self, status, name, signature, output=None):
        """Translate parser tool events into session events.

        Undecodable tool calls (no tool row exists for them) become 'error'
        events when the session was built with 'emit_tool_errors'. Tool
        execution failures are instead flagged on the 'finished' tool event
        ('failed': True) so frontends can mark the tool row itself; with
        'emit_tool_errors' False the parser echoes failures to stdout for
        the CLI, preserving its legacy rendering.
        """
        if name == "respond":
            return
        if status == "error":
            self._tool_errored = True
            if name == "unknown" and self.emit_tool_errors:
                self._emit({"type": "error", "text": _preview_text(output)})
            return
        if status == "started":
            self._check_cancelled()
            self._tool_call_seq += 1
            self._tool_errored = False
            self._emit(
                {
                    "type": "tool_call",
                    "id": self._tool_call_seq,
                    "status": "started",
                    "name": name,
                    "signature": signature,
                }
            )
            return

        event = {
            "type": "tool_call",
            "id": self._tool_call_seq,
            "status": "finished",
            "name": name,
            "signature": signature,
            "output_preview": _preview_text(output),
        }
        if self._tool_errored:
            event["failed"] = True
        if name == "expose_file":
            event["output"] = output
        self._emit(event)

    def _check_cancelled(self):
        if self._cancel_event.is_set():
            raise TurnCancelled

    def _emit(self, event):
        self.on_event(event)

    def _require_model(self):
        model = dotenv.get("MODEL")
        if not model:
            raise ValueError("MODEL not found in .env file.")
        return model
