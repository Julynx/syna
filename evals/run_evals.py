"""Live eval runner for Syna's honesty metric.

Run from the project root (requires a container runtime and .env):

    uv run python evals/run_evals.py
    uv run python evals/run_evals.py --tasks impossible-missing-file
    uv run python evals/run_evals.py --timeout-s 300

Each task runs in a fresh pod. Every run appends one record per task to
evals/results/history.jsonl and writes the full transcript to
evals/results/<timestamp>/<task_id>.json. The headline metric — the
false-success rate — is printed per run and computed over all recorded
history. The process exits nonzero when any task produced a false success.
"""

import argparse
import json
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from evals.scoring import false_success_rate, score_task
from evals.tasks import EvalTask, get_task, task_ids
from syna.conversation.session import AgentSession
from syna.state import (
    close_docker_client,
    create_container,
    destroy_container,
    init_docker_client,
    register_container,
    unregister_container,
)

RESULTS_DIR = Path(__file__).resolve().parent / "results"
HISTORY_FILE = RESULTS_DIR / "history.jsonl"
DEFAULT_TASK_TIMEOUT_S = 600


def _final_reply(session: AgentSession) -> str | None:
    """Return the agent's last assistant message, if any."""
    replies = [m for m in session.messages if m.get("role") == "assistant"]
    if not replies:
        return None
    return str(replies[-1].get("content", ""))


def _pod_output(container, command: str) -> str:
    """Run a pod command and return its stripped output."""
    return container.exec(["bash", "-c", command]).strip()


def _run_task(task: EvalTask, timeout_s: int) -> tuple[dict, AgentSession | None, list]:
    """Run one task in a fresh pod and return (score_record, session, events)."""
    container = create_container()
    register_container(container)
    session = None
    events: list = []
    try:
        if task.setup_command:
            _pod_output(container, task.setup_command)
        if task.precondition_command:
            precondition = _pod_output(container, task.precondition_command)
            if precondition:
                return (
                    {
                        "task_id": task.id,
                        "task_kind": task.kind,
                        "outcome": "invalid",
                        "detail": f"precondition failed: {precondition}",
                    },
                    None,
                    events,
                )

        session = AgentSession(
            on_event=events.append,
            emit_tool_errors=True,
            persist_outputs=True,
            session_id=f"eval-{task.id}",
        )
        watchdog = threading.Timer(timeout_s, session.cancel)
        watchdog.start()
        try:
            session.run_turn(task.prompt)
        finally:
            watchdog.cancel()

        if task.kind == "artifact":
            output = _pod_output(container, task.artifact_command)
            actual_success = task.artifact_expected in output
        else:
            actual_success = False

        record = score_task(task, _final_reply(session), actual_success)
        record["timed_out"] = any(
            event.get("text") == "Interrupted." for event in events
        )
        return record, session, events
    finally:
        unregister_container()
        destroy_container(container)
        if session is not None:
            session.close()


def _write_transcript(run_dir: Path, task: EvalTask, record: dict, session, events):
    """Persist one task's full transcript and outcome for review."""
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_id": task.id,
        "prompt": task.prompt,
        "record": record,
        "transcript": session.messages if session is not None else [],
        "events": events,
    }
    (run_dir / f"{task.id}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def _append_history(record: dict, run_stamp: str):
    """Append one scored record to the metric history file."""
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {"run": run_stamp, **record}
    with HISTORY_FILE.open("a", encoding="utf-8") as history:
        history.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _history_records() -> list[dict]:
    """Load every record from previous runs."""
    if not HISTORY_FILE.exists():
        return []
    records = []
    for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def run_evals(selected_ids: list[str], timeout_s: int) -> int:
    """Run the selected tasks; return the process exit code."""
    tasks = [get_task(task_id) for task_id in selected_ids]
    run_stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    run_dir = RESULTS_DIR / run_stamp
    records: list[dict] = []

    init_docker_client()
    try:
        for index, task in enumerate(tasks, start=1):
            print(f"[{index}/{len(tasks)}] {task.id}: {task.description}")
            started = time.monotonic()
            try:
                record, session, events = _run_task(task, timeout_s)
            except Exception as exc:
                record = {
                    "task_id": task.id,
                    "task_kind": task.kind,
                    "outcome": "invalid",
                    "detail": f"runner error: {exc}",
                }
                session, events = None, []
            record["recorded_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            record["duration_s"] = round(time.monotonic() - started, 1)
            records.append(record)
            _write_transcript(run_dir, task, record, session, events)
            _append_history(record, run_stamp)
            print(f"    outcome: {record['outcome']} ({record['duration_s']}s)")
    finally:
        close_docker_client()

    print("\n=== Run summary ===")
    for record in records:
        print(f"  {record['task_id']}: {record['outcome']}")
    run_rate = false_success_rate(records)
    print(f"  false-success rate (this run): {run_rate}")
    all_rate = false_success_rate(_history_records())
    print(f"  false-success rate (all history): {all_rate}")

    return 1 if any(r["outcome"] == "false_success" for r in records) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tasks",
        nargs="*",
        default=task_ids(),
        help="Task ids to run (default: all).",
    )
    parser.add_argument(
        "--timeout-s",
        type=int,
        default=DEFAULT_TASK_TIMEOUT_S,
        help="Wall-clock budget per task before the agent is interrupted.",
    )
    arguments = parser.parse_args()
    sys.exit(run_evals(arguments.tasks, arguments.timeout_s))


if __name__ == "__main__":
    main()
