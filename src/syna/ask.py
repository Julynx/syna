"""CLI interaction loop built on top of the shared AgentSession."""

from .ask_utils import show_welcome
from .commands import parse_and_execute_command
from .session import AgentSession


def _print_event(event):
    """Render session events with the classic CLI look and feel."""
    kind = event["type"]
    if kind == "status":
        print(f"  + ({event['text']})", end="\r", flush=True)
    elif kind == "tool_call" and event["status"] == "finished":
        signature = event["signature"].strip()[:64].replace("\n", " ")
        output = event["output_preview"].strip()[:64]
        print(f"  + │Used tool '{signature}...'")
        print(f"    └─ {output}...")
        print("  + (Thinking...)", end="\r", flush=True)
    elif kind == "model_message":
        print(f"\n[Syna]: {event['content']}\n")
    elif kind == "error":
        print(f"\n  ! ({event['text']})")


def _handle_slash_command(query: str, session: AgentSession):
    """Execute a slash command and echo its result or error."""
    try:
        result = parse_and_execute_command(query, session=session)
        print(f"  + ({result})")
    except ValueError as exc:
        print(f"  ! ({exc})")


def ask_loop():
    """Execute the interactive conversation and tool execution loop."""
    show_welcome()
    session = AgentSession(on_event=_print_event)
    with session.client:
        while True:
            query = input("(Ask Syna): ")
            if not query.strip():
                continue
            if query.strip().startswith("/"):
                _handle_slash_command(query, session)
                continue
            session.run_turn(query)
