"""Live end-to-end check of the CLI front-end (real pod, real model).

Run explicitly: uv run python tests/test_live_cli.py

Drives the real TerminalChat/TerminalSession stack with a scripted input
source, exactly as the offline tests do, but against a real AgentSession,
a real pod, and a real model. Covers, in order:
1. A submitted message runs a turn and its events render.
2. A message typed while a turn is busy queues FIFO and runs next.
3. /stop cancels a running turn; the harness notice is recorded.
4. /jobs lists a real background process and /kill stops it.
5. /undo with "Messages & workspace" stops nothing, restores the
   workspace (a marker file created after the snapshot disappears),
   truncates the history, and records the rewind notice.
6. Closed stdin lets everything finish and exits with a farewell.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from syna.config import get_env

key = get_env().get("OPENROUTER_API_KEY") or ""
if not key.startswith("sk-or"):
    print("SKIP test_live_cli (needs a real OPENROUTER_API_KEY)")
    sys.exit(0)

from syna.capabilities.processes import registry_for
from syna.capabilities.tools import process as process_tool
from syna.conversation.session import AgentSession, resolve_context_budget
from syna.conversation.terminal import TerminalChat, TerminalSession
from syna.state import close_docker_client, get_container, reset_shared_container

RESULTS = []


def report(label, ok, detail=""):
    RESULTS.append(ok)
    print(
        f"{'PASS' if ok else 'FAIL'} {label}" + (f" -- {detail}" if detail else ""),
        flush=True,
    )


def wait_for(predicate, timeout_s, what, said=None):
    """Poll a predicate; raise when it never holds, naming the phase."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    tail = "\n".join(said[-8:]) if said else ""
    raise AssertionError(f"timed out waiting for {what}\nlast output:\n{tail}")


def note(text):
    """Print a phase marker and yield an empty prompt line."""
    print(f"--- {text}", flush=True)
    return ""


class ScriptedInput:
    """A prompt stand-in: lines, EOF, and synchronization hooks.

    A callable hook runs and yields an empty line (the loop ignores
    it), unless it returns a string, which becomes the typed line - so
    a hook can both synchronize and produce input computed at run time.
    """

    def __init__(self, script):
        self.script = list(script)

    def __call__(self, prompt=""):
        item = self.script.pop(0)
        if item is EOFError:
            raise EOFError
        if callable(item):
            result = item()
            return result if isinstance(result, str) else ""
        return item


def main():
    model = get_env().get("MODEL")
    container = get_container()
    events = []
    said = []

    session = AgentSession(
        on_event=events.append,
        persist_outputs=True,
        context_budget_tokens=resolve_context_budget(model),
    )
    conversation = TerminalSession(
        session,
        notify=said.append,
        container_provider=lambda: container,
        checkpoints_enabled=True,
    )
    chat = TerminalChat(
        terminal=conversation,
        conversation_factory=lambda: conversation,
        say=said.append,
        read_line=ScriptedInput(
            [
                # Phase 1: a plain turn
                lambda: note("phase 1: plain turn"),
                "Reply with exactly: OK",
                lambda: wait_for(
                    lambda: (
                        any(e.get("type") == "model_message" for e in events)
                        and not conversation.active.is_set()
                    ),
                    180,
                    "the first turn's reply",
                    said,
                ),
                # Phase 2: queue while busy
                lambda: note("phase 2: queue while busy"),
                "Use the execute_command tool to run this exact command: sleep 5. Then reply DONE.",
                lambda: wait_for(
                    lambda: any(
                        e.get("type") == "tool_call"
                        and e.get("status") == "started"
                        and "sleep 5" in str(e.get("signature", ""))
                        for e in events
                    ),
                    180,
                    "the sleep 5 tool call to start",
                    said,
                ),
                "Reply with exactly: SECOND",
                lambda: wait_for(
                    lambda: any("Queued" in line for line in said),
                    10,
                    "the queue notice",
                    said,
                ),
                lambda: wait_for(
                    lambda: (
                        sum(1 for e in events if e.get("type") == "model_message") >= 2
                        and not conversation.active.is_set()
                    ),
                    180,
                    "the queued message's turn to finish",
                    said,
                ),
                # Phase 3: /stop a running turn
                lambda: note("phase 3: /stop"),
                "Use the execute_command tool to run this exact command: sleep 5. Then reply DONE.",
                lambda: wait_for(
                    lambda: (
                        sum(
                            1
                            for e in events
                            if e.get("type") == "tool_call"
                            and e.get("status") == "started"
                            and "sleep 5" in str(e.get("signature", ""))
                        )
                        >= 2
                    ),
                    180,
                    "the second sleep 5 tool call to start",
                    said,
                ),
                "/stop",
                lambda: wait_for(
                    lambda: (
                        any(
                            e.get("type") == "status"
                            and e.get("text") == "Interrupted."
                            for e in events
                        )
                        and not conversation.active.is_set()
                    ),
                    120,
                    "the interrupted turn to unwind",
                    said,
                ),
                lambda: (
                    _capture_stop_notice(session)
                    or note("stop notice captured before the rewind")
                ),
                # Phase 4: /jobs and /kill on a real background process
                lambda: note("phase 4: /jobs and /kill"),
                lambda: wait_for(
                    lambda: _spawn_and_grab_id(container) is not None,
                    30,
                    "the background process to register",
                    said,
                ),
                "/jobs",
                lambda: wait_for(
                    lambda: any(
                        _spawned_id in line and "running" in line for line in said
                    ),
                    30,
                    "/jobs to list the running process",
                    said,
                ),
                lambda: f"/kill {_spawned_id}",
                lambda: wait_for(
                    lambda: any(f"Stopped '{_spawned_id}'" in line for line in said),
                    60,
                    "the kill to finish",
                    said,
                ),
                # Phase 5: rewind, messages & workspace
                lambda: note("phase 5: /undo with workspace restore"),
                lambda: _make_marker(container),
                "/undo",
                "2",
                lambda: wait_for(
                    lambda: any("Rewound to before" in line for line in said),
                    120,
                    "the rewind to run",
                    said,
                ),
                # Phase 6: EOF
                lambda: note("phase 6: EOF"),
                EOFError,
            ]
        ),
    )
    try:
        # Phase 1
        chat.run()
    finally:
        conversation.close()
        try:
            session.close()
        except Exception:
            pass

    joined = "\n".join(said)
    replies = [e["content"] for e in events if e.get("type") == "model_message"]

    report(
        "a submitted message runs a turn and renders its reply",
        len(replies) >= 1 and not conversation.active.is_set(),
        repr(replies[0][:80]) if replies else "no reply",
    )
    report(
        "a message typed mid-turn queues and runs next",
        any("Queued" in line for line in said) and len(replies) >= 2,
        f"{len(replies)} replies",
    )
    interrupted = any(
        e.get("type") == "status" and e.get("text") == "Interrupted." for e in events
    )
    stop_notice = any(
        m.get("role") == "user" and "interrupt" in str(m.get("content", ""))
        for m in _stop_notice_snapshot
    )
    report(
        "/stop cancels the running turn and records the notice",
        interrupted and stop_notice and any("Stopping..." in line for line in said),
    )
    registry = registry_for(container)
    killed = _spawned_id is not None and (
        _spawned_id not in registry.processes
        or registry.processes[_spawned_id].state != "running"
    )
    report(
        "/jobs lists and /kill stops a background process",
        killed and any(f"Stopped '{_spawned_id}'" in line for line in said),
    )
    marker_gone = _marker_absent(container)
    rewound = any("Rewound to before" in line for line in said) and marker_gone
    notice_recorded = any(
        m.get("role") == "user"
        and "rewound the conversation" in str(m.get("content", ""))
        for m in session.messages
    )
    report(
        "/undo with workspace restore reverts files and records the notice",
        rewound and notice_recorded,
        "marker still present" if not marker_gone else "",
    )
    report(
        "closed stdin exits with a farewell",
        any("Goodbye" in line for line in said),
    )

    if all(RESULTS):
        print(f"ALL {len(RESULTS)} LIVE CLI CHECKS PASSED")
    else:
        print(f"{RESULTS.count(False)} OF {len(RESULTS)} LIVE CLI CHECKS FAILED")
        sys.exit(1)


_spawned_id = None
_stop_notice_snapshot: list = []


def _capture_stop_notice(session):
    """Snapshot the history before the rewind truncates it.

    The /stop harness notice is the evidence the stop was recorded;
    the phase-5 rewind deliberately deletes it from history, so it is
    captured here, right after the interrupted turn unwinds.
    """
    _stop_notice_snapshot.extend(session.messages)
    return ""


def _spawn_and_grab_id(container):
    """Spawn one background process and remember its registry id."""
    global _spawned_id
    if _spawned_id is not None:
        return _spawned_id
    registry = registry_for(container)
    with registry.lock:
        if registry.processes:
            _spawned_id = next(iter(registry.processes))
            return _spawned_id
    process_tool("spawn", {"command": "sleep 30", "name": "clijob"})
    with registry.lock:
        if registry.processes:
            _spawned_id = next(iter(registry.processes))
    return _spawned_id


_MARKER = "/workspace/e2e_cli_marker.txt"


def _make_marker(container):
    """Create a workspace file after the newest rewind point's snapshot."""
    from syna.pod_io import pod_exec

    pod_exec(container, ["bash", "-c", f"echo e2e > {_MARKER}"])


def _marker_absent(container):
    """Whether the marker file was reverted out of the workspace."""
    from syna.pod_io import pod_exec

    view = pod_exec(container, ["bash", "-c", f"test -e {_MARKER} && echo present"])
    return "present" not in view


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            reset_shared_container()
        except Exception:
            pass
        try:
            close_docker_client()
        except Exception:
            pass
