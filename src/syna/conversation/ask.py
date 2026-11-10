"""CLI entry: setup checks, the welcome banner, and the prompt loop.

The conversation itself is driven by the terminal front-end in
`terminal.py`; this module wires the presentation (event rendering,
prompt_toolkit input) to it.
"""

import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from ..ask_utils import show_welcome
from ..config import get_env, load_checkpoints_enabled
from .session import AgentSession, resolve_context_budget
from .terminal import PROMPT_TEXT, TerminalChat, TerminalSession, terminal_command_help

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_last_status: str | None = None

_prompt_session = None


def _prompt_line() -> str:
    """Read one prompt line through the lazily created prompt session.

    Created on first use rather than at import time: importing the CLI
    must stay side-effect-free for tests and non-console environments,
    where no Windows console buffer exists to attach to.
    """
    global _prompt_session
    if _prompt_session is None:
        _prompt_session = PromptSession()
    return _prompt_session.prompt(PROMPT_TEXT)


def _print_status(text: str):
    """Print a status line, deduplicating consecutive repeats."""
    global _last_status
    if _last_status == text:
        return
    _last_status = text
    print(f"  + ({text})")


def _print_tool_call(event):
    """Render one tool_call event: started, finished, failed, or skipped."""
    status = event.get("status")
    signature = str(event.get("signature", "")).strip()[:64].replace("\n", " ")
    if status == "started":
        _print_status(f"Running {signature}...")
        return
    if status == "skipped":
        print(f"  + │Skipped '{signature}...' (an earlier call in the batch failed)")
        _print_status("Thinking...")
        return
    if status != "finished":
        return
    shared = event.get("structured_output")
    if isinstance(shared, dict) and "path" in shared:
        print(f"  + │Shared '{shared.get('name', '')}'")
        print(f"    └─ Save it locally with: /get-from {shared['path']}")
        _print_status("Thinking...")
        return
    output = str(event.get("output_preview", "")).strip()[:64]
    if event.get("failed"):
        print(f"  + ✗Failed '{signature}...'")
    else:
        print(f"  + │Used tool '{signature}...'")
    print(f"    └─ {output}...")
    _print_status("Thinking...")


def _print_event(event):
    """Render session events with the classic CLI look and feel."""
    kind = event["type"]
    if kind == "status":
        if not event.get("quiet"):
            _print_status(event["text"])
    elif kind == "tool_call":
        _print_tool_call(event)
    elif kind == "model_message":
        print(f"\n[Syna]: {event['content']}\n")
    elif kind == "compaction":
        print(f"\n----- Compaction -----\n{event['summary']}\n")
    elif kind == "background_done":
        summary = str(event.get("summary", "")).strip()
        kind_name = str(event.get("kind", "work"))
        background_id = str(event.get("id", ""))
        print(f"  + │Background {kind_name} '{background_id}': {summary}")
    elif kind == "error":
        print(f"\n  ! ({event['text']})")


def _create_conversation() -> TerminalSession:
    """Create an agent session and its terminal worker."""
    from ..config import DEFAULT_MODEL

    model = get_env().get("MODEL") or DEFAULT_MODEL
    session = AgentSession(
        on_event=lambda event: _print_event(event),
        persist_outputs=True,
        context_budget_tokens=resolve_context_budget(model),
    )
    terminal = TerminalSession(
        session,
        notify=print,
        checkpoints_enabled=load_checkpoints_enabled(),
    )
    return terminal


def check_setup() -> None:
    """Verify that Syna is configured before starting the CLI session."""
    from .. import setup  # noqa: PLC0415
    from ..config import get_data_home  # noqa: PLC0415

    if setup.setup_incomplete():
        status = setup.config_status()
        print("\n  ! Syna is not configured yet.\n")
        if not status["has_key"] and status["needs_key"]:
            print("    Missing API key in .env.")
        elif not status["has_model"]:
            print("    Missing MODEL in .env.")
        elif not setup.detect_engine().found:
            print("    No container engine (Podman or Docker) found on your system.")
        print("\n    To configure Syna through the first-run wizard, run:")
        print("      syna         (starts the web server and opens the wizard)")
        print("      syna-setup   (opens the wizard directly)\n")
        print(
            f"    Or configure your settings manually in:\n      {get_data_home() / '.env'}\n"
        )
        sys.exit(1)


def ask_loop():
    """Run the interactive conversation loop."""
    check_setup()
    show_welcome()
    print(terminal_command_help())
    print()
    chat = TerminalChat(
        terminal=_create_conversation(),
        conversation_factory=_create_conversation,
        say=print,
        read_line=_prompt_line,
    )
    with patch_stdout():
        chat.run()
