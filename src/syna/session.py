"""Reusable agent session shared by the CLI and the web server."""

import json
import threading

from dotenv import dotenv_values
from openrouter import OpenRouter
from openrouter.utils import BackoffStrategy, RetryConfig

from .ask_utils import ask_question
from .config import get_logger, get_project_root, load_config
from .parse import parse_and_execute_tools

dotenv = dotenv_values(get_project_root() / ".env")

OUTPUT_PREVIEW_CHARS = 512
DEFAULT_REQUEST_DELAY_S = 2
DEFAULT_MAX_HISTORY_MESSAGES = 30
HISTORY_NOTE_PREFIX = "[System]: [Context] "


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
        self.messages: list[dict] = []
        self._cancel_event = threading.Event()
        self._tool_call_seq = 0
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
        """Run one full think/act cycle for a user message. Blocking."""
        logger = get_logger()
        msg = {"role": "user", "content": user_text}
        self.messages.append(msg)
        logger.info(str(msg))
        self._emit({"type": "status", "text": "Thinking..."})

        while True:
            try:
                self._check_cancelled()
                self._trim_history()
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
                self._emit({"type": "status", "text": "No tool detected, retrying..."})
                warning = (
                    "WARNING: Your previous message contained no tool call, so it was"
                    " discarded and never reached the user. To act you must include a"
                    ' fenced block in this exact format:\n```tool\n{"tool_name":'
                    " {\"argument\": \"value\"}}\n```\n"
                    "To speak to the user, call the 'respond' tool with your message"
                    " as its 'text' argument."
                )
                msg = {"role": "user", "content": f"[System]: {warning}"}
                self.messages.append(msg)
                logger.info(str(msg))
                continue

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

    def _trim_history(self):
        """Drop the oldest messages once the history exceeds the configured cap.

        The system prompt is never trimmed. A single omission note is kept at
        the front of the trimmed region and merged instead of duplicated.
        """
        cap = self.max_history_messages
        history = self.messages[1:]
        if len(history) <= cap:
            return
        if history[0]["content"].startswith(HISTORY_NOTE_PREFIX):
            history.pop(0)
        omitted = len(history) - (cap - 1)
        note = {
            "role": "user",
            "content": (
                f"{HISTORY_NOTE_PREFIX}{omitted} earlier messages were omitted"
                " to save context. Their outcome is reflected in later messages."
            ),
        }
        self.messages[:] = [self.messages[0], note, *history[omitted:]]
        get_logger().info(
            "Trimmed %d messages from history (cap=%d)", omitted, cap
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

        Parser failures become 'error' events only when the session was built
        with 'emit_tool_errors'; otherwise the parser echoes them to stdout
        for the CLI, preserving its legacy rendering.
        """
        if name == "respond":
            return
        if status == "error":
            if self.emit_tool_errors:
                self._emit({"type": "error", "text": _preview_text(output)})
            return
        if status == "started":
            self._check_cancelled()
            self._tool_call_seq += 1
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
