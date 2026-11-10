"""Terminal front-end: an inbox worker and a live prompt for the CLI.

The web server drives AgentSession turns from a dedicated worker fed by
an inbox; the terminal shares that architecture so the prompt stays
live while a turn runs. Messages typed mid-turn queue FIFO for the
next turn, /stop cancels the running one the way the web stop button
does, and ask_user questions are answered straight from the prompt
through a PendingQuestion. All worker output flows through an injected
printer, keeping the state machine testable without a console.
"""

import threading
import time
import uuid
from collections import deque
from typing import Callable

from ..config import get_logger, load_checkpoints_enabled
from ..capabilities import processes, rewind, subagents
from ..interaction import (
    InteractionCancelled,
    PendingQuestion,
    register_responder,
    unregister_responder,
)
from ..state import get_container, peek_shared_container, reset_shared_container
from .commands import get_command_help, parse_and_execute_command
from .session import AgentSession

PROMPT_TEXT = "(Ask Syna): "
WORKER_JOIN_TIMEOUT_S = 10.0

LIFECYCLE_COMMANDS = frozenset({"stop", "exit", "newchat"})

TERMINAL_COMMANDS = {
    "stop": {"description": "Stop the running task.", "arg_help": ""},
    "jobs": {
        "description": "List background processes and subagents.",
        "arg_help": " [id] (inspect one record's output when given)",
    },
    "kill": {
        "description": "Stop a background process or subagent.",
        "arg_help": " 'id'",
    },
    "undo": {
        "description": "Rewind to just before your last message.",
        "arg_help": "",
    },
    "rewind": {
        "description": "Rewind to just before an earlier message.",
        "arg_help": " [number] (from the /rewind list)",
    },
    "newchat": {
        "description": "Drop the conversation and destroy the pod.",
        "arg_help": "",
    },
    "exit": {"description": "End the chat with Syna.", "arg_help": ""},
}


def terminal_command_help() -> str:
    """The terminal-level commands, formatted like the pod-command help."""
    lines = ["The following commands are supported:"]
    for command, params in TERMINAL_COMMANDS.items():
        lines.append(f"\n  {params['description']}\n    /{command}{params['arg_help']}")
    return "\n".join(lines)


class TerminalSession:
    """One CLI conversation: an inbox worker driving AgentSession turns.

    The worker dequeues inbox items and runs one turn per user message,
    owning the session's cancel state: it clears any pending stop right
    before the turn starts, so a stop typed between turns survives
    until then instead of being erased early. ask_user questions block
    the worker on a PendingQuestion the prompt thread resolves, and a
    rewind requested while a turn is busy is parked in `pending_rewind`
    to run before anything else leaves the inbox.
    """

    def __init__(
        self,
        session: AgentSession,
        notify: Callable[[str], None],
        container_provider: Callable[[], object | None] = peek_shared_container,
        checkpoints_enabled: bool = False,
    ):
        self.session = session
        self.notify = notify
        self.container_provider = container_provider
        self.checkpoints_enabled = checkpoints_enabled
        self.status_text = "Ready"
        self.inbox: deque = deque()
        self.inbox_ready = threading.Condition()
        self.busy = threading.Event()
        self.pending_question = PendingQuestion()
        self.pending_rewind = None
        self.shutdown = False
        self._worker = threading.Thread(
            target=self._run, name="syna-cli-worker", daemon=True
        )
        self._worker.start()

    def _run(self) -> None:
        """Execute queued items until shutdown, one at a time.

        A rewind request runs strictly between turns: it executes
        immediately when dequeued, and a rewind parked while a turn was
        busy runs right after the turn ends, before anything else
        leaves the inbox.
        """
        while True:
            item = self._next_item()
            if item is None:
                break
            if item.get("kind") == "rewind":
                self._execute_rewind(item)
                continue
            self._run_item(item)
            self._run_pending_rewind()

    def _next_item(self) -> dict | None:
        """Block for the next inbox item; None once shutdown is set."""
        with self.inbox_ready:
            while not self.inbox and not self.shutdown:
                self.inbox_ready.wait()
            if self.inbox:
                return self.inbox.popleft()
            return None

    def _run_item(self, item: dict) -> None:
        """Run one user-message turn with the web worker's lifecycle.

        The responder registration, the pending-question release, and
        the busy flag mirror the server's turn wrapper so a stop or a
        cancelled question unwinds the turn the same way in both
        front-ends.
        """
        self.busy.set()
        register_responder(self._answer_question)
        try:
            self.session.clear_cancel()
            self.session.run_turn(item["content"], rewind=self._rewind_info_for())
        except InteractionCancelled:
            self.notify("  + (Task stopped.)")
        except Exception:
            get_logger().exception("Turn failed")
            self.notify("  ! (The task failed unexpectedly. Please retry.)")
        finally:
            unregister_responder()
            self.pending_question.resolve(None)
            self.busy.clear()
            with self.inbox_ready:
                self.inbox_ready.notify_all()

    def _rewind_info_for(self) -> dict | None:
        """Snapshot the workspace before a turn; the rewind point's data.

        Returns None when checkpoints are off or the snapshot fails: the
        message then simply gets no rewind point. The snapshot happens
        after the pending stop is cleared, matching the web worker's
        ordering, so the point records the workspace the turn actually
        starts from.
        """
        if not self.checkpoints_enabled:
            return None
        try:
            container = get_container()
        except Exception:
            get_logger().warning(
                "Rewind snapshot skipped: the pod is unavailable", exc_info=True
            )
            return None
        snap = rewind.snapshot(container)
        if snap is None:
            return None
        return {"key": uuid.uuid4().hex, "snap": snap, "pod": id(container)}

    def _run_pending_rewind(self) -> None:
        """Run the rewind a user requested while the last turn was busy.

        The prompt loop cancelled the turn exactly like the stop button
        does and parked the request; it executes here, when the stop has
        taken effect and before anything else leaves the inbox.
        """
        request = self.pending_rewind
        if request is None:
            return
        self.pending_rewind = None
        self._execute_rewind({"kind": "rewind", **request})

    def _execute_rewind(self, request: dict) -> None:
        """Truncate the conversation back to a rewind point.

        "Messages only" rewrites history alone. "Messages & workspace"
        stops the pod's background work (a rollback must not race it),
        then destructively restores the workspace snapshot - files
        first: a failed restore leaves every message in place. Queued
        messages die with a successful rewind; a queued rewind request
        survives the purge so it is never swallowed.
        """
        key = str(request.get("id", ""))
        mode = "all" if request.get("mode") == "all" else "messages"
        point = self.session.rewind_points.get(key)
        if point is None:
            self.notify("  ! (That message can no longer be rewound.)")
            return
        stopped = 0
        if mode == "all":
            container = self.container_provider()
            if container is None or point["pod"] != id(container):
                self.notify(
                    "  ! (The pod was recreated since that message; its file"
                    " snapshot is gone. Messages can still be rewound.)"
                )
                return
            stopped = processes.stop_running_background_work(
                container, on_stopped=self._announce_stopped_work
            )
            snap = point["snap"]
            if not rewind.rollback(container, snap["tree"], snap.get("nested", {})):
                text = (
                    "Could not restore the workspace files;"
                    " the conversation was left unchanged."
                )
                if stopped:
                    text += " Background work was stopped."
                self.notify(f"  ! ({text})")
                return
        text = self.session.rewind(key)
        if text is None:
            self.notify("  ! (That message can no longer be rewound.)")
            return
        self.session.record_rewind_notice(mode == "all")
        self.purge_inbox(keep_kinds=("rewind",))
        preview = text if len(text) <= 80 else text[:77] + "..."
        self.notify(f"  + ── Rewound to before: {preview}")
        details = []
        if mode == "all":
            details.append("workspace restored")
        if stopped:
            details.append(f"{stopped} background task(s) stopped")
        if details:
            self.notify(f"  + ({'; '.join(details)}. Re-send your message below.)")
        else:
            self.notify("  + (Re-send your message below.)")
        self.notify(f"  > {text}")

    def _announce_stopped_work(self, kind: str, record, summary: str) -> None:
        """Announce one background record stopped before a rollback."""
        self.notify(f"  + │Background {kind} '{record.id}': {summary}")

    def purge_inbox(self, keep_kinds: tuple[str, ...] = ()) -> None:
        """Drop every queued item whose kind is not kept."""
        with self.inbox_ready:
            self.inbox = deque(
                item for item in self.inbox if item.get("kind") in keep_kinds
            )

    def enqueue_rewind(self, request: dict) -> None:
        """Queue a rewind request to run strictly between turns."""
        item = {"kind": "rewind", **request}
        with self.inbox_ready:
            self.inbox.append(item)
            self.inbox_ready.notify_all()

    def rewind_points_oldest_first(self) -> list[tuple[str, dict]]:
        """The session's rewind points, oldest first, for listing."""
        return list(self.session.rewind_points.items())

    def _answer_question(self, question_text: str, choices: list[str]) -> str:
        """Publish one ask_user question and block for the answer.

        Runs on the worker thread inside the ask_user tool. The prompt
        thread resolves the PendingQuestion with the user's line; None
        (a stop) unwinds the tool as InteractionCancelled. A number
        picks one of the shown choices, any other text is the answer.
        """
        answers = self.pending_question.open()
        try:
            self.notify(f"\n  ? {question_text}")
            if choices:
                for number, choice in enumerate(choices, start=1):
                    self.notify(f"    {number}. {choice}")
                self.notify("    Answer with a number, or type your own response.")
            answer = answers.get()
        finally:
            self.pending_question.resolve(None)
        if answer is None:
            raise InteractionCancelled("The user stopped the task before answering.")
        if choices and answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        return answer

    def submit(self, content: str) -> bool:
        """Queue a user message; True when a turn was already running."""
        item = {"kind": "message", "id": uuid.uuid4().hex, "content": content}
        with self.inbox_ready:
            was_busy = self.busy.is_set()
            self.inbox.append(item)
            self.inbox_ready.notify_all()
        return was_busy

    def stop(self) -> None:
        """Cancel the running turn and release a pending question.

        This is the web stop button's semantics: the turn unwinds at
        its next checkpoint recording a harness notice, and a question
        waiting for an answer is released so the blocked tool fails
        cleanly instead of blocking forever.
        """
        self.session.cancel("user interrupt")
        self.pending_question.resolve(None)

    def wait_idle(self, timeout_s: float | None = None) -> bool:
        """Block until no turn is running; False when the timeout passed."""
        with self.inbox_ready:
            deadline = None if timeout_s is None else time.monotonic() + timeout_s
            while self.busy.is_set():
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self.inbox_ready.wait(0.2 if remaining is None else min(0.2, remaining))
        return True

    def close(self, join_timeout_s: float = WORKER_JOIN_TIMEOUT_S) -> None:
        """Stop any running turn and end the worker thread."""
        self.shutdown = True
        self.stop()
        with self.inbox_ready:
            self.inbox_ready.notify_all()
        self._worker.join(join_timeout_s)


class TerminalChat:
    """The prompt loop: routes lines to answers, commands, and the inbox.

    Slash commands dispatch to the terminal-level table first and
    delegate everything else to the pod-command registry. While a
    question is pending, every non-lifecycle line answers it, matching
    the web's behavior of resolving the question with whatever the
    user sends next. Ctrl+C stops the running task and only exits when
    the prompt is idle; closed stdin lets a running turn finish first.
    """

    def __init__(
        self,
        terminal: TerminalSession,
        conversation_factory: Callable[[], TerminalSession],
        say: Callable[[str], None],
        read_line: Callable[[], str],
    ):
        self.terminal = terminal
        self.conversation_factory = conversation_factory
        self.say = say
        self.read_line = read_line
        self.running = True
        self._commands = {
            "stop": self._command_stop,
            "jobs": self._command_jobs,
            "kill": self._command_kill,
            "undo": self._command_undo,
            "rewind": self._command_rewind,
            "newchat": self._command_newchat,
            "exit": self._command_exit,
            "help": self._command_help,
        }

    def run(self) -> None:
        """Read prompt lines until the user exits or closes stdin."""
        while self.running:
            try:
                line = self.read_line()
            except KeyboardInterrupt:
                if not self._handle_interrupt():
                    break
                continue
            except EOFError:
                self._handle_eof()
                break
            self._route_line(line.strip())
        self._shutdown()

    def _route_line(self, text: str) -> None:
        """Send one input line to the question, a command, or the inbox."""
        if not text:
            return
        if text.startswith("/"):
            name = text[1:].partition(" ")[0]
            if name not in LIFECYCLE_COMMANDS and self.terminal.pending_question.active:
                self.terminal.pending_question.resolve(text)
                return
            self._dispatch(text)
            return
        if self.terminal.pending_question.active:
            self.terminal.pending_question.resolve(text)
            return
        was_busy = self.terminal.submit(text)
        if was_busy:
            self.say("  + (Queued - Syna is working; it will arrive next turn.)")

    def _dispatch(self, text: str) -> None:
        """Run a slash command: terminal table first, pod registry second."""
        name = text[1:].partition(" ")[0]
        handler = self._commands.get(name)
        if handler is not None:
            handler(text[len(name) + 1 :].strip())
            return
        try:
            result = parse_and_execute_command(text, session=self.terminal.session)
            self.say(f"  + ({result})")
        except ValueError as exc:
            self.say(f"  ! ({exc})")

    def _handle_interrupt(self) -> bool:
        """Route Ctrl+C; False ends the loop.

        While a task is running (a turn, or a question waiting for an
        answer) this is the stop button; at an idle prompt it exits.
        """
        if self.terminal.pending_question.active or self.terminal.busy.is_set():
            self.terminal.stop()
            self.say("\n  + (Stopping...)")
            return True
        self.say("  + (Goodbye.)")
        return False

    def _handle_eof(self) -> None:
        """Closed stdin: let a running turn finish, then exit.

        A pending question can never be answered without input, so it
        is released and the turn unwinds as stopped.
        """
        if self.terminal.pending_question.active:
            self.terminal.stop()
        if self.terminal.busy.is_set():
            self.say(
                "  + (Input closed; the running task will finish before Syna exits.)"
            )
            self.terminal.wait_idle()
        self.say("  + (Goodbye.)")

    def _command_stop(self, args: str) -> None:
        """Stop the running task, or say so when nothing is running."""
        if (
            not self.terminal.busy.is_set()
            and not self.terminal.pending_question.active
        ):
            self.say("  + (Nothing is running.)")
            return
        self.terminal.stop()
        self.say("  + (Stopping...)")

    def _container(self):
        """The pod container, or None when it is not running."""
        return self.terminal.container_provider()

    def _command_jobs(self, args: str) -> None:
        """List background work, or inspect one record's output."""
        container = self._container()
        if container is None:
            self.say("  ! (The pod is not running.)")
            return
        registry = processes.registry_for(container)
        target = args.strip()
        if target:
            self._show_background_record(registry, container, target)
            return
        with registry.lock:
            process_rows = sorted(registry.processes.values(), key=lambda p: p.started)
            subagent_rows = sorted(registry.subagents.values(), key=lambda s: s.started)
        if not process_rows and not subagent_rows:
            self.say("  + (No background work.)")
            return
        for proc in process_rows:
            self.say(
                f"    process  {proc.id}  {processes.process_status_summary(proc)}"
                f"  -  {proc.command}"
            )
        for sub in subagent_rows:
            self.say(
                f"    subagent {sub.id}  {subagents.subagent_status_summary(sub)}"
                f"  -  {sub.goal}"
            )

    def _show_background_record(self, registry, container, background_id: str) -> None:
        """Print one record's report: state, runtime, and output tails."""
        entry = registry.lookup(background_id)
        if entry is None:
            self.say(f"  ! ({processes.unknown_id_error(background_id)})")
            return
        kind, record = entry
        if kind == "process":
            state, stdout, stderr, _ended = processes.check_process_state(
                container, record.dir
            )
            self.say(processes.format_process_report(record, state, stdout, stderr))
        else:
            self.say(
                subagents.check_subagent(
                    background_id, registry=registry, container=container
                )
            )

    def _command_kill(self, args: str) -> None:
        """Stop one background record; the kill may take a few seconds.

        The kill talks to the pod (signals, artifact copies), so it runs
        on a short-lived thread and the prompt stays live; the outcome
        line arrives when it finishes.
        """
        background_id = args.strip()
        if not background_id:
            self.say("  ! (Usage: /kill <id>)")
            return
        container = self._container()
        if container is None:
            self.say("  ! (The pod is not running.)")
            return
        registry = processes.registry_for(container)
        entry = registry.lookup(background_id)
        if entry is None:
            self.say(f"  ! ({processes.unknown_id_error(background_id)})")
            return
        kind, _record = entry

        def run():
            try:
                if kind == "process":
                    report = processes.kill_process(
                        background_id,
                        registry=registry,
                        container=container,
                        by="user",
                    )
                else:
                    report = subagents.kill_subagent(
                        background_id,
                        registry=registry,
                        container=container,
                        by="user",
                    )
                if report.startswith("ERROR: "):
                    self.say(f"  ! ({report[len('ERROR: ') :]})")
                else:
                    self.say(f"  + (Stopped '{background_id}'.)\n{report}")
            except Exception as exc:
                get_logger().exception("Kill failed for '%s'", background_id)
                self.say(f"  ! (Could not stop '{background_id}': {exc})")

        threading.Thread(target=run, name="syna-cli-kill", daemon=True).start()

    def _command_undo(self, args: str) -> None:
        """Rewind to just before the newest rewind point."""
        points = self.terminal.rewind_points_oldest_first()
        self._start_rewind(points[-1][0] if points else None)

    def _command_rewind(self, args: str) -> None:
        """List rewind points, or rewind to the numbered one."""
        points = self.terminal.rewind_points_oldest_first()
        target = args.strip()
        if not target:
            if not points:
                self._say_no_points()
                return
            for number, (_key, point) in enumerate(points, start=1):
                text = str(point.get("text", ""))
                preview = text if len(text) <= 60 else text[:57] + "..."
                marker = " (compacted)" if point.get("compacted") else ""
                self.say(f"    {number}. {preview}{marker}")
            self.say("    Use /rewind <number> to revert to one.")
            return
        if not target.isdigit():
            self.say("  ! (Usage: /rewind [number])")
            return
        index = int(target)
        if not 1 <= index <= len(points):
            self.say("  ! (No such rewind point; run /rewind to list them.)")
            return
        self._start_rewind(points[index - 1][0])

    def _say_no_points(self) -> None:
        """Explain why there is nothing to rewind to."""
        if not self.terminal.checkpoints_enabled:
            self.say(
                "  + (Checkpoints are disabled; enable them in the web"
                " UI under Settings > Workspace.)"
            )
        else:
            self.say("  + (No rewind points yet.)")

    def _start_rewind(self, key: str | None) -> None:
        """Choose a mode and route the rewind request.

        While a turn is busy the request takes the stop button's path -
        cancel the turn, release a pending question, and park the rewind
        so the worker executes it when the stop has taken effect. While
        idle it is enqueued as a rewind inbox item, so it runs strictly
        between turns instead of racing one.
        """
        if key is None:
            self._say_no_points()
            return
        mode = self._choose_rewind_mode()
        if mode is None:
            self.say("  + (Rewind cancelled.)")
            return
        request = {"id": key, "mode": mode}
        if self.terminal.busy.is_set():
            self.terminal.stop()
            self.terminal.pending_rewind = request
            self.say("  + (Stopping; the rewind runs once the task has stopped.)")
            return
        self.terminal.enqueue_rewind(request)
        self.say("  + (Rewinding...)")

    def _choose_rewind_mode(self) -> str | None:
        """Ask for the rewind mode with the numbered-choice format."""
        self.say("  ? How should Syna rewind?")
        self.say("    1. Messages only")
        self.say("    2. Messages & workspace")
        self.say("    3. Cancel")
        self.say("    Answer with a number.")
        while True:
            try:
                answer = self.read_line().strip()
            except (KeyboardInterrupt, EOFError):
                return None
            if answer == "1":
                return "messages"
            if answer == "2":
                return "all"
            if answer == "3":
                return None
            self.say("    Answer with a number, 1 to 3.")

    def _command_newchat(self, args: str) -> None:
        """Drop the conversation and destroy the pod; start fresh."""
        self.say("  + (Cleaning up the pod, please wait...)")
        old = self.terminal
        self._close_conversation(old)
        try:
            reset_shared_container()
        except Exception:
            self.say(
                "  ! (Could not clean up the old pod; starting a new session anyway.)"
            )
        self.terminal = self.conversation_factory()
        self.say("  + (Started a fresh chat.)")

    def _command_exit(self, args: str) -> None:
        """End the chat with Syna."""
        self.say("  + (Goodbye.)")
        self.running = False

    def _command_help(self, args: str) -> None:
        """Show the pod commands and the terminal commands."""
        self.say(get_command_help())
        self.say(terminal_command_help())

    def _close_conversation(self, terminal: TerminalSession) -> None:
        """Stop one conversation's worker and release its resources."""
        terminal.close()
        try:
            terminal.session.close()
        except Exception:
            pass

    def _shutdown(self) -> None:
        """Tear the current conversation down after the loop ends."""
        self._close_conversation(self.terminal)
