"""THE LEAGUE OF LEGENDS WORLDS BENCHMARK — not a regular test.

This is an expensive, long-horizon probe (typically 30-45 minutes and
hundreds of model steps). It is deliberately NOT wired into any test
loop:

- it is not a file in `tests/`, so no test runner or run-all loop
  discovers it;
- it is not registered in `evals/tasks.py`, so `run_evals.py` can never
  pick it up, not even with `--tasks`;
- it is not scored: there is no false-success record, no history line,
  no exit-code gate.

Run it ONLY when the project changed significantly, or when the user
explicitly asks for it. One live run consumes a fresh pod, a real model
budget, and most of an hour.

What it is for: a single long multi-step task whose facts are scattered
across many websites and API calls. The saved logs let a reviewer trace
the final report back to the tool calls that produced each fact, and
study how the model behaves with hundreds of tool calls and messages in
history: whether compaction and elision hold up, which tools it leans
on, where it repeats itself, stalls, hallucinates, or claims success
without evidence. The reviewer does this analysis by hand; this runner
only produces the logs.

Usage (requires a container runtime and .env):

    uv run python evals/run_worlds_benchmark.py --i-understand-the-cost
    uv run python evals/run_worlds_benchmark.py --i-understand-the-cost --timeout-s 1800

Without --i-understand-the-cost the runner refuses to start: it prints
a cost notice and exits with code 2, before touching Docker or the
model. Exit codes: 0 completed, 1 ran but unfinished, 2 refused. The
flag exists so an agent that runs every test after a change cannot
start this blindly; asking the person who pays the bill first is
advised.

The watchdog is soft: at the budget the agent is asked to interrupt at
its next pause, so a run can overshoot slightly if a model call or a
long tool is still in flight. A run also ends early whenever the model
responds, gives up, or a fatal error halts the turn — none of these is
guaranteed to be a "finished" task.

Outputs land in evals/results/worlds_<timestamp>/:

- transcript.json — meta (model, duration, steps, tool-call count,
  outcome), the full message history, and every event.
- shared/ — every file the agent delivered via share_file, pulled out
  of the pod (the expected deliverable is a PDF).
- syna-run.log — the slice of the project log file written during this
  run.
"""

import argparse
import json
import shutil
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syna.config import get_project_root
from syna.conversation.session import AgentSession, shared_file_metadata
from syna.state import (
    close_docker_client,
    create_container,
    destroy_container,
    init_docker_client,
    register_container,
    unregister_container,
)

PROMPT = (
    "I need you to create a PDF file for me, that contains every team that"
    " won the league of legends worlds, with country flag before the team"
    " name, and league between brackets after it. It should also contain"
    " the runner up and the theme song for that year. Then, in the next"
    " page (to avoid making a table that is too wide and cuts off), for"
    " every year, the composition of the team that won that year (players"
    " and positions)."
)

RESULTS_DIR = Path(__file__).resolve().parent / "results"
DEFAULT_TIMEOUT_S = 2400
DEFAULT_MAX_STEPS = 1000
COST_NOTICE = """\
This is the League of Legends Worlds benchmark, not a regular test.

One run takes 30-45 minutes, spends hundreds of model steps on a real
API budget, and creates a fresh container. It is meant to be run only
when the project changed significantly, or when someone explicitly
asks for it. It is not a regression test; do not run it as part of a
test suite after making a change.

Ask the person who will pay the bill for this run before deciding.
When you have that understanding, re-run with --i-understand-the-cost.
"""


class ConsoleFeed:
    """Print a compact live line per event while collecting them."""

    def __init__(self):
        self.events: list[dict] = []
        self.started = time.monotonic()

    def __call__(self, event: dict):
        self.events.append(event)
        elapsed = int(time.monotonic() - self.started)
        stamp = f"[{elapsed // 60:02d}:{elapsed % 60:02d}]"
        kind = event.get("type")
        if kind == "tool_call":
            output = event.get("output")
            detail = "" if output is None else f" :: {str(output)[:80]}"
            print(f"{stamp} {event.get('name')} -> {event.get('status')}{detail}")
        elif kind == "model_message":
            print(f"{stamp} RESPOND: {str(event.get('content'))[:120]}")
        elif kind == "compaction":
            print(f"{stamp} COMPACTION")
        elif kind == "error":
            print(f"{stamp} ERROR: {event.get('text')}")
        sys.stdout.flush()


def _project_log_path() -> Path:
    """Return the project log file path from the configuration."""
    config_log = json.loads(
        (get_project_root() / "assets" / "config.json").read_text(encoding="utf-8")
    ).get("log_file", "syna.log")
    return get_project_root() / config_log


def _count(events: list[dict], event_type: str, text: str | None = None) -> int:
    """Count events of a type, optionally matching an exact text."""
    return sum(
        1
        for event in events
        if event.get("type") == event_type
        and (text is None or event.get("text") == text)
    )


def _harvest_shared_files(container, shared_dir: Path, events: list[dict]) -> list[str]:
    """Pull every file delivered via share_file out of the pod."""
    shared_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for event in events:
        metadata = shared_file_metadata(event)
        if metadata is not None:
            pod_path = metadata["path"]
            try:
                container.copy_from(pod_path, str(shared_dir))
                saved.append(pod_path)
            except Exception as exc:
                print(f"    could not harvest {pod_path}: {exc}")
    if not saved:
        shutil.rmtree(shared_dir, ignore_errors=True)
    return saved


def _save_log_slice(run_dir: Path, start_offset: int):
    """Copy the bytes this run appended to the project log file."""
    log_path = _project_log_path()
    if not log_path.exists():
        return
    with log_path.open("rb") as log_file:
        log_file.seek(start_offset)
        (run_dir / "syna-run.log").write_bytes(log_file.read())


def run_benchmark(timeout_s: int, max_steps: int) -> int:
    """Run the benchmark in a fresh pod; return the process exit code."""
    log_offset = (
        _project_log_path().stat().st_size if _project_log_path().exists() else 0
    )
    run_stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    run_dir = RESULTS_DIR / f"worlds_{run_stamp}"
    init_docker_client()
    container = create_container()
    register_container(container)
    feed = ConsoleFeed()
    session = None
    shared: list[str] = []
    run_error: str | None = None
    try:
        session = AgentSession(
            on_event=feed,
            emit_tool_errors=True,
            persist_outputs=True,
            session_id="worlds-benchmark",
        )
        session.max_steps_per_turn = max_steps
        print(f"model: {session.model}")
        print(f"budget: {timeout_s // 60} min, {max_steps} steps")
        watchdog = threading.Timer(timeout_s, session.cancel)
        watchdog.start()
        try:
            session.run_turn(PROMPT)
        except KeyboardInterrupt:
            session.cancel()
            print("\nkeyboard interrupt; saving what the run produced")
        finally:
            watchdog.cancel()
        shared = _harvest_shared_files(container, run_dir / "shared", feed.events)
    except Exception as exc:
        run_error = f"{type(exc).__name__}: {exc}"
        print(f"\nrun halted by exception: {run_error}")
    finally:
        if session is not None:
            session.close()
        unregister_container()
        destroy_container(container)
        close_docker_client()

    interrupted = _count(feed.events, "status", "Interrupted.") > 0
    halted = run_error is not None or _count(feed.events, "error") > 0
    completed = _count(feed.events, "model_message") > 0 and not interrupted
    meta = {
        "benchmark": "worlds",
        "model": session.model if session else None,
        "prompt": PROMPT,
        "timeout_s": timeout_s,
        "max_steps": max_steps,
        "duration_s": round(time.monotonic() - feed.started, 1),
        "steps": _count(feed.events, "status", "Thinking..."),
        "tool_calls": _count(feed.events, "tool_call"),
        "interrupted": interrupted,
        "halted_with_error": halted,
        "run_error": run_error,
        "completed": completed,
        "shared_files": shared,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "transcript.json").write_text(
        json.dumps(
            {
                "meta": meta,
                "messages": session.messages if session else [],
                "events": feed.events,
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    _save_log_slice(run_dir, log_offset)

    outcome = "completed" if completed else "unfinished"
    if interrupted:
        outcome += " (interrupted)"
    if run_error:
        outcome += f" ({run_error})"
    elif halted:
        outcome += " (error)"
    print("\n=== Worlds benchmark summary ===")
    print(f"  outcome: {outcome}")
    print(
        f"  duration: {meta['duration_s']}s, steps: {meta['steps']},"
        f" tool calls: {meta['tool_calls']}"
    )
    print(f"  shared: {', '.join(shared) if shared else 'nothing delivered'}")
    print(f"  logs saved to: {run_dir}")
    print("  Analyze transcript.json by hand; this runner scores nothing.")
    return 0 if completed else 1


def main():
    parser = argparse.ArgumentParser(
        description="Long-horizon benchmark; see the module docstring before running.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--timeout-s",
        type=int,
        default=DEFAULT_TIMEOUT_S,
        help="Wall-clock budget in seconds before the agent is asked to"
        f" interrupt (default: {DEFAULT_TIMEOUT_S}).",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=DEFAULT_MAX_STEPS,
        help="Step budget for the turn, raised from the normal harness"
        " default so only the wall clock stops the run (default:"
        f" {DEFAULT_MAX_STEPS}).",
    )
    parser.add_argument(
        "--i-understand-the-cost",
        action="store_true",
        help="Confirm you mean to run this expensive benchmark.",
    )
    arguments = parser.parse_args()
    if not arguments.i_understand_the_cost:
        print(COST_NOTICE)
        sys.exit(2)
    sys.exit(run_benchmark(arguments.timeout_s, arguments.max_steps))


if __name__ == "__main__":
    main()
