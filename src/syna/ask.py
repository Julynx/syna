"""CLI interaction loop built on top of the shared AgentSession."""

import sys

from .ask_utils import show_welcome
from .commands import parse_and_execute_command
from .interaction import InteractionCancelled, register_responder
from .interaction import unregister_responder
from .session import AgentSession
from .state import reset_shared_container

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

NEW_CHAT_COMMAND = "/newchat"


def _print_status_line(text: str):
    """Overwrite the progress line so repeated statuses don't scroll."""
    print(f"  + ({text})", end="\r", flush=True)


def _print_tool_call(event):
    """Render one tool_call event: started, finished, failed, or skipped."""
    status = event.get("status")
    signature = str(event.get("signature", "")).strip()[:64].replace("\n", " ")
    if status == "started":
        _print_status_line(f"Running {signature}...")
        return
    if status == "skipped":
        print(f"  + │Skipped '{signature}...' (an earlier call in the batch failed)")
        _print_status_line("Thinking...")
        return
    if status != "finished":
        return
    shared = event.get("structured_output")
    if isinstance(shared, dict) and "path" in shared:
        print(f"  + │Shared '{shared.get('name', '')}'")
        print(f"    └─ Save it locally with: /get-from {shared['path']}")
        _print_status_line("Thinking...")
        return
    output = str(event.get("output_preview", "")).strip()[:64]
    if event.get("failed"):
        print(f"  + ✗Failed '{signature}...'")
    else:
        print(f"  + │Used tool '{signature}...'")
    print(f"    └─ {output}...")
    _print_status_line("Thinking...")


def _print_event(event):
    """Render session events with the classic CLI look and feel."""
    kind = event["type"]
    if kind == "status":
        _print_status_line(event["text"])
    elif kind == "tool_call":
        _print_tool_call(event)
    elif kind == "model_message":
        print(f"\n[Syna]: {event['content']}\n")
    elif kind == "compaction":
        print(f"\n----- Compaction -----\n{event['summary']}\n")
    elif kind == "error":
        print(f"\n  ! ({event['text']})")


def _terminal_responder(session: AgentSession):
    """Build an ask_user responder that reads answers from the terminal.

    A number picks one of the shown choices; any other text is delivered
    as a custom answer. Ctrl+C (or a closed stdin) cancels the pending
    question and stops the task instead of killing the application.
    """

    def responder(question_text: str, choices: list[str]) -> str:
        print(f"\n  ? {question_text}")
        if choices:
            for number, choice in enumerate(choices, start=1):
                print(f"    {number}. {choice}")
            print("    Answer with a number, or type your own response.")
        try:
            while True:
                answer = input("  > ").strip()
                if not answer:
                    continue
                if choices and answer.isdigit() and 1 <= int(answer) <= len(choices):
                    return choices[int(answer) - 1]
                return answer
        except (KeyboardInterrupt, EOFError):
            session.cancel()
            raise InteractionCancelled(
                "The user stopped the task before answering."
            ) from None

    return responder


def _handle_slash_command(query: str, session: AgentSession):
    """Execute a slash command and echo its result or error."""
    try:
        result = parse_and_execute_command(query, session=session)
        print(f"  + ({result})")
    except ValueError as exc:
        print(f"  ! ({exc})")


def _create_session() -> AgentSession:
    """Create an agent session that renders its events on the terminal."""
    return AgentSession(on_event=_print_event, persist_outputs=True)


def _reset_session(session: AgentSession) -> AgentSession:
    """Drop the conversation and destroy the shared pod (/newchat)."""
    print("  + (Cleaning up the pod, please wait...)")
    try:
        session.close()
    except Exception:
        pass
    try:
        reset_shared_container()
    except Exception:
        print("  ! (Could not clean up the old pod; starting a new session anyway.)")
    print("  + (Started a fresh chat.)")
    return _create_session()


def ask_loop():
    """Execute the interactive conversation and tool execution loop."""
    show_welcome()
    session = _create_session()
    register_responder(_terminal_responder(session))
    try:
        while True:
            try:
                query = input("(Ask Syna): ")
            except EOFError:
                break
            if not query.strip():
                continue
            if query.strip() == NEW_CHAT_COMMAND:
                session = _reset_session(session)
                register_responder(_terminal_responder(session))
                continue
            if query.strip().startswith("/"):
                _handle_slash_command(query, session)
                continue
            session.clear_cancel()
            try:
                session.run_turn(query)
            except KeyboardInterrupt:
                session.cancel()
                print("\n  + (Task stopped. Press CTRL+C again to quit.)")
            except InteractionCancelled:
                print("\n  + (Task stopped.)")
    finally:
        unregister_responder()
        try:
            session.close()
        except Exception:
            pass
