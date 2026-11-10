"""Live check of the rewind feature (real pod, real git, real model).

Run explicitly: uv run python tests/test_live_rewind.py

Covers, in order:
1. Pod ground truth: the image's nominal WORKDIR is not the workspace
   (commands run in /workspace), and the rewind init step installs git
   and prepares the shadow store in a real pod.
2. Pristine seeding: pod creation snapshots the workspace, so the next
   snapshot reuses the tree without writing one.
3. Snapshot / change / snapshot / rollback round trip: file creation,
   modification, and deletion inside /workspace are all reverted; a
   nested repository's tracked files revert too while its .git and
   history survive (leaving a dirty working tree); gitignored and
   post-snapshot clone directories are respected.
4. A real AgentSession turn records a rewind point carrying the
   pristine tree, and a "Messages & workspace" rewind through the
   record stops a running background process (with its model notice
   suppressed), restores the workspace, truncates the history, and
   leaves a harness notice explaining the rewound state.
"""

import sys
import threading
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from syna.config import get_env

if not (get_env().get("OPENROUTER_API_KEY") or "").startswith("sk-or"):
    print("SKIP live rewind test (needs a real OPENROUTER_API_KEY)")
    sys.exit(0)

from syna import pod_init
from syna.capabilities import processes, rewind
from syna.capabilities.tools import process as process_tool
from syna.conversation.session import AgentSession
from syna.protocol.channel import is_channel_message, unwrap_channel
from syna.server import SessionRecord
from syna.state import (
    create_container,
    destroy_container,
    register_container,
    unregister_container,
)

results = []


def report(name, ok, detail=""):
    results.append(ok)
    print(
        f"{'PASS' if ok else 'FAIL'} {name}" + (f" - {detail}" if detail else ""),
        flush=True,
    )


def pod_run(container, script):
    from syna.pod_io import pod_exec

    return pod_exec(container, ["bash", "-c", script])


def main():
    container = create_container()
    register_container(container)
    try:
        check_ground_truth(container)
        check_pristine_seed(container)
        check_snapshot_round_trip(container)
        check_session_rewind(container)
        check_rewind_stops_background_work(container)
    finally:
        unregister_container()
        try:
            processes.drop_registry(container)
        except Exception:
            pass
        try:
            destroy_container(container)
        except Exception:
            pass
    report("ALL CHECKS PASSED", all(results))
    return 0 if all(results) else 1


def check_ground_truth(container):
    started = time.monotonic()
    results_map = pod_init.run_pod_init_steps(container)
    elapsed = time.monotonic() - started
    report(
        "the rewind init step installs git and prepares the store",
        results_map.get("rewind") is True,
        f"results={results_map}, {elapsed:.1f}s",
    )
    workdir = pod_run(container, "pwd").strip()
    report(
        "the nominal WORKDIR is not the workspace root",
        workdir == "/" and workdir != pod_init.REWIND_SNAP_ROOT,
        f"pwd={workdir}, snapshot root={pod_init.REWIND_SNAP_ROOT}",
    )
    git_version = pod_run(container, "git --version").strip()
    report("git is usable in the pod", "git version" in git_version, git_version)


def check_pristine_seed(container):
    scripts = []
    real_exec = rewind.pod_exec

    def recording_exec(container_, argv):
        scripts.append(argv[-1])
        return real_exec(container_, argv)

    rewind.pod_exec = recording_exec
    try:
        seeded = rewind.snapshot(container)
    finally:
        rewind.pod_exec = real_exec
    reused = any("SYNA_LAST_TREE" in script for script in scripts)
    report(
        "pod creation seeded the pristine tree",
        seeded is not None
        and len(seeded["tree"]) == 40
        and seeded["nested"] == {}
        and reused,
        f"tree={seeded and seeded['tree']}, reused={reused}",
    )


def check_snapshot_round_trip(container):
    base = (
        "mkdir -p /workspace/probe && echo pristine > /workspace/probe/keep.txt"
        " && printf 'v1\\n' > /workspace/probe/edited.txt"
    )
    pod_run(container, base)
    pod_run(
        container,
        "cd /workspace/probe && git init -q nested &&"
        " git -C nested config user.email n@l && git -C nested config user.name n"
        " && echo inside > nested/inner.txt && git -C nested add -A"
        " && git -C nested commit -qm seed",
    )

    first = rewind.snapshot(container)
    report(
        "the first snapshot captures the pristine tree and the nested repo",
        first is not None
        and len(first["tree"]) == 40
        and len(first["nested"].get("/workspace/probe/nested", "")) == 40,
        f"snap tree={first and first['tree'][:8]},"
        f" nested={list((first or {}).get('nested', {}))}",
    )

    again = rewind.snapshot(container)
    report(
        "an unchanged workspace reuses the previous snapshot",
        again == first,
        f"second={again and again['tree'][:8]}",
    )

    pod_run(
        container,
        "echo added > /workspace/probe/added.txt"
        " && echo v2 > /workspace/probe/edited.txt"
        " && rm /workspace/probe/keep.txt"
        " && echo changed-inside > /workspace/probe/nested/inner.txt"
        " && git -C /workspace/probe/nested add -A"
        " && git -C /workspace/probe/nested commit -qm change"
        " && cd /workspace && git clone -q probe/nested probe/cloned",
    )

    second = rewind.snapshot(container)
    report(
        "a changed workspace produces a new tree hash",
        second is not None and second["tree"] != first["tree"],
        f"tree={second and second['tree'][:8]}",
    )

    ok = rewind.rollback(container, first["tree"], first["nested"])
    report("the rollback confirms success", ok)

    # `git clean -fd` deletes untracked plain files but refuses to remove
    # directories containing a nested .git: a post-snapshot clone is not
    # in the snapshot's nested map, so it survives as it was left, while
    # the tracked files of the pre-existing repo revert to the snapshot.
    state = pod_run(
        container,
        "cat /workspace/probe/keep.txt 2>/dev/null;"
        " cat /workspace/probe/edited.txt 2>/dev/null;"
        " cat /workspace/probe/nested/inner.txt 2>/dev/null;"
        " test -d /workspace/probe/nested/.git && echo NESTED_GIT_OK;"
        " git -C /workspace/probe/nested status --porcelain | head -2;"
        " git -C /workspace/probe/nested log --oneline | wc -l;"
        " test -d /workspace/probe/cloned/.git && echo CLONED_OK;"
        " ls /workspace/probe/added.txt 2>/dev/null; echo END",
    )
    lines = [line.strip() for line in state.splitlines() if line.strip()]
    report(
        "created files are reverted and edits restored",
        lines[:2] == ["pristine", "v1"]
        and lines[2] == "inside"
        and "added.txt" not in " ".join(lines),
        f"state={lines}",
    )
    report(
        "the nested repo keeps its history but its files reverted",
        "NESTED_GIT_OK" in lines and lines[-3] == "2",
        f"state={lines}",
    )
    report(
        "the reverted tracked file shows as a dirty working tree",
        any("M inner.txt" in line for line in lines),
        f"status={[line for line in lines if 'inner' in line]}",
    )
    report(
        "a post-snapshot clone survives untouched",
        "CLONED_OK" in lines,
        f"state={lines}",
    )
    return first


def check_session_rewind(container):
    events = []
    session = AgentSession(
        on_event=events.append,
        persist_outputs=True,
        session_id="live-rewind",
        context_budget_tokens=100_000,
    )
    session.request_delay = 0.2
    session.run_turn(
        "Run this exact command with execute_command: "
        "`echo alpha > /workspace/rewind_probe.txt`. Then reply done.",
        rewind={
            "key": "live-msg-1",
            "snap": rewind.snapshot(container),
            "pod": id(container),
        },
    )
    point = session.rewind_points.get("live-msg-1")
    report(
        "the turn recorded a rewind point with a real snapshot",
        point is not None and len(point["snap"]["tree"]) == 40,
        f"tree={point and point['snap']['tree'][:8]}",
    )
    probe = pod_run(container, "cat /workspace/rewind_probe.txt").strip()
    report("the task's file exists before the rewind", probe == "alpha", probe)

    rolled = rewind.rollback(
        container, point["snap"]["tree"], point["snap"].get("nested", {})
    )
    gone = pod_run(
        container, "ls /workspace/rewind_probe.txt 2>/dev/null; echo ."
    ).strip()
    report(
        "the rollback removed the task's file",
        rolled and gone == ".",
        f"ls={gone!r}",
    )
    text = session.rewind("live-msg-1")
    report(
        "the session history truncated back to the pristine point",
        text
        == (
            "Run this exact command with execute_command: "
            "`echo alpha > /workspace/rewind_probe.txt`. Then reply done."
        )
        and len(session.messages) == 1,
        f"{len(session.messages)} message(s) kept",
    )
    state_events = [event for event in events if event.get("type") == "rewind_state"]
    report(
        "rewind_state events tracked the point lifecycle",
        len(state_events) >= 2
        and state_events[0]["added"] == ["live-msg-1"]
        and state_events[-1]["removed"] == ["live-msg-1"],
        f"{len(state_events)} delta event(s)",
    )
    session.close()


def check_rewind_stops_background_work(container):
    """A "Messages & workspace" rewind stops running work.

    A detached process is spawned after the snapshot; the rewind must
    kill it before rolling back, suppress its model-facing completion
    notice (the rewound timeline never knew about it), still tell the
    work panel, restore the workspace, truncate the history, and append
    the harness notice that explains the rewound state to the model.
    """
    notices = []
    pushed = []
    record = SessionRecord.__new__(SessionRecord)
    record.client_id = "live-rewind-bg"
    record.inbox = deque()
    record.inbox_ready = threading.Condition()
    record.event_log = []
    record.event_log_lock = threading.Lock()
    record.subscribers = []
    record.pending_rewind = None
    record.container = container
    record.push = lambda event, persistent=True: pushed.append(event)

    events = []
    session = AgentSession(
        on_event=events.append,
        persist_outputs=True,
        session_id="live-rewind-bg",
        context_budget_tokens=100_000,
    )
    session.request_delay = 0.2
    record.session = session

    registry = processes.registry_for(container)
    registry.completion_sink = lambda payload: notices.append(payload)

    tree = rewind.snapshot(container)
    report(
        "background check: snapshot taken before the spawn",
        tree is not None and len(tree["tree"]) == 40,
        f"tree={tree['tree'][:8]}",
    )
    spawn_report = process_tool(
        "spawn",
        {
            "command": "echo fromproc > /workspace/fromproc.txt && sleep 300",
            "name": "rewindp",
        },
    )
    report(
        "the background process spawned",
        "rewindp" in str(spawn_report),
        str(spawn_report)[:120],
    )
    for _ in range(40):
        if (
            pod_run(
                container, "ls /workspace/fromproc.txt >/dev/null 2>&1; echo ."
            ).strip()
            == "/workspace/fromproc.txt"
        ):
            break
        time.sleep(0.25)
    written = pod_run(container, "cat /workspace/fromproc.txt 2>/dev/null").strip()
    report("the process wrote its file", written == "fromproc", written)

    try:
        session.run_turn(
            "Run this exact command with execute_command: "
            "`echo beta > /workspace/bg_probe.txt`. Then reply done.",
            rewind={"key": "bg-msg-1", "snap": tree, "pod": id(container)},
        )
    finally:
        pass
    deltas = [event for event in events if event.get("type") == "rewind_state"]
    report(
        "rewind_state events are deltas",
        bool(deltas)
        and all("added" in event and "removed" in event for event in deltas)
        and deltas[0]["added"] == ["bg-msg-1"],
        f"{len(deltas)} delta event(s)",
    )
    probe = pod_run(container, "cat /workspace/bg_probe.txt 2>/dev/null").strip()
    report("the task's file exists before the rewind", probe == "beta", probe)

    record._execute_rewind({"id": "bg-msg-1", "mode": "all"})

    with registry.lock:
        proc = registry.processes.get("rewindp")
    report(
        "the background process was stopped by the rewind",
        proc is not None and proc.state != "running" and proc.killed_by == "user",
        f"state={proc and proc.state}, killed_by={proc and proc.killed_by}",
    )
    report(
        "no model notice fired for the stopped process",
        notices == [],
        f"{len(notices)} notice(s)",
    )
    done = [event for event in pushed if event.get("type") == "background_done"]
    report(
        "the work panel learned of the stop",
        len(done) == 1 and done[0]["id"] == "rewindp",
        str(done[0]) if done else "none",
    )
    leftovers = pod_run(
        container,
        "ls /workspace/bg_probe.txt /workspace/fromproc.txt 2>/dev/null; echo .",
    ).strip()
    report(
        "post-snapshot files are gone after the rollback",
        leftovers == ".",
        f"ls={leftovers!r}",
    )
    report(
        "history truncated to the system message plus the rewind notice",
        len(session.messages) == 2
        and is_channel_message(
            session.messages[1].get("content") or "", "HARNESS_NOTICE"
        )
        and "reverted" in (unwrap_channel(session.messages[1]["content"])[1]),
        f"{len(session.messages)} message(s)",
    )
    rewinds = [event for event in pushed if event.get("type") == "history_rewind"]
    report(
        "history_rewind event carried the original text",
        len(rewinds) == 1 and rewinds[0]["content"].startswith("Run this exact"),
        str(rewinds[0])[:100] if rewinds else "none",
    )
    session.close()


if __name__ == "__main__":
    sys.exit(main())
