"""Detached background processes running in the pod.

The `process` tool's spawn/check/kill operations live here, together
with the per-session background registry shared with the subagent
capability (`subagents.py` imports the registry and id helpers from
here; this module never imports session machinery, so there is no
import cycle).

A spawned process is detached from the tool call that started it: the
pod-side launcher script runs the command under `setsid` in its own
process group, under `timeout` with the configured runtime cap, with
stdout and stderr line-buffered into the process directory. Nothing on
the host reaps it; it dies when the pod dies, and a `timeout`-wrapped
orphan survives a host restart until its cap fires or it finishes.

The registry is in memory per session, keyed by the pod container.
A server restart loses it; the processes keep running under their
`timeout` wrapper, and `ps` inside the pod is the recovery path.
"""

import base64
import itertools
import re
import shlex
import threading
import time
from dataclasses import dataclass, field

from ..config import (
    get_logger,
    load_check_tail_chars,
    load_process_max_runtime_s,
    load_wait_for_timeout,
)
from ..state import get_background_record, get_container

# Both foreground (execute_command) and background (spawn) pod commands
# run non-interactively: no pagers, no progress bars, unbuffered Python.
NONINTERACTIVE_ENV_EXPORTS = (
    "DEBIAN_FRONTEND=noninteractive PAGER=cat GIT_PAGER=cat"
    " GIT_TERMINAL_PROMPT=0 PIP_NO_INPUT=1 PIP_PROGRESS_BAR=off"
    " PYTHONUNBUFFERED=1 TERM=dumb NO_COLOR=1"
)

ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*")

MAX_ID_CHARS = 64

PROCESS_DIR_ROOT = "/workspace/processes"
SUBAGENT_DIR_ROOT = "/workspace/subagents"

TIMEOUT_KILL_GRACE_S = 10
CAP_EXIT_CODE = 124

# The wait_for harness deadline exceeds the tool's own wait budget by
# this slack, so the tool's graceful "still running" return always wins
# over the harness stop: a wait that expires is a result, not a failure.
WAIT_FOR_DEADLINE_SLACK_S = 10
WAIT_FOR_MAX_S = 3600
WAIT_FOR_POLL_INTERVAL_S = 1.0

# How often the completion watcher probes the pod for exit markers.
WATCHER_POLL_INTERVAL_S = 3.0
# A watcher poll that raises is retried; only this many consecutive
# failures declare the pod gone. One transient failure (a moment of
# connection pressure, a slow probe) must not silence completion
# notices for the rest of the session.
WATCHER_MAX_CONSECUTIVE_FAILURES = 3
# How long spawn waits for the process group id to appear.
PGID_WAIT_S = 4.0


# Every container call (exec, copy) multiplexes over one HTTP
# connection in bollard's client, so concurrent callers corrupt the
# connection state (observed live: CannotSendRequest mid-request when
# the process watcher raced a tool call's exec). All pod calls in the
# codebase go through the shared helpers in `pod_io`; this module
# re-exports them for `subagents.py`.
from ..pod_io import pod_copy_to, pod_exec  # noqa: F401  (re-export)


@dataclass
class ProcessRecord:
    """One detached process: identity, pod artifacts, and outcome."""

    id: str
    command: str
    dir: str
    started: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    parent_id: str | None = None
    pgid: int | None = None
    runtime_cap_s: int | None = None
    state: str = "running"
    exit_code: int | None = None
    killed_by: str | None = None
    notified: bool = False
    outcome_collected: bool = False
    last_wait_ended_at: float | None = None


def process_runtime_cap_s(proc: ProcessRecord) -> int:
    """The process's effective runtime cap in seconds.

    Every report reads the record's own cap, so a spawn with a custom
    cap is reported and enforced consistently; the configured default
    covers records built without one.
    """
    if proc.runtime_cap_s is not None:
        return proc.runtime_cap_s
    return load_process_max_runtime_s()


class PodRegistry:
    """In-memory background-work registry for one session's pod.

    Holds the process and subagent records behind one lock, the id
    reservation set shared by both kinds, and the two hooks the server
    registers for UI streaming and completion notices. A CLI session
    leaves the hooks unset: background work still works, nothing
    streams.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.processes: dict[str, ProcessRecord] = {}
        self.subagents: dict[str, object] = {}
        self.taken_ids: set[str] = set()
        self._process_counter = itertools.count(1)
        self._subagent_counter = itertools.count(1)
        self.watcher_lock = threading.Lock()
        self.watcher_started = False
        self.active = True
        self.event_sink = None
        self.completion_sink = None

    def lookup(self, background_id: str):
        """Return (kind, record) for a process or subagent id, or None."""
        with self.lock:
            if background_id in self.processes:
                return "process", self.processes[background_id]
            if background_id in self.subagents:
                return "subagent", self.subagents[background_id]
        return None


_registries: dict[int, PodRegistry] = {}
_registries_lock = threading.Lock()


def registry_for(container) -> PodRegistry:
    """Return the registry bound to a pod container, creating it once.

    Every thread that resolves the same container object (the session
    worker, subagent runners, server request threads) shares one
    registry, so `wait_for` ids are unambiguous across kinds.
    """
    with _registries_lock:
        registry = _registries.get(id(container))
        if registry is None:
            registry = _registries[id(container)] = PodRegistry()
        return registry


def peek_registry(container) -> PodRegistry | None:
    """Return the registry bound to a container, or None — never create.

    Read-only probes (the server's stale-notice check) use this after a
    possible teardown: registry_for would silently resurrect a dropped
    registry, breaking its create-once contract.
    """
    with _registries_lock:
        return _registries.get(id(container))


def drop_registry(container) -> None:
    """Forget the registry bound to a container (session teardown).

    Setting `active` False first ends the completion watcher's loop; the
    pod is being destroyed, so there is nothing left to watch.
    """
    with _registries_lock:
        registry = _registries.pop(id(container), None)
    if registry is not None:
        registry.active = False


def reserve_id(registry: PodRegistry, kind: str, name: str | None) -> tuple:
    """Resolve and reserve a background id; return (id, error_message).

    A user-provided name becomes the id verbatim so the model can keep
    processes and subagents straight; auto ids come from per-kind
    counters. Ids are never released: a finished process's id stays
    reserved so a later spawn cannot alias its output directory.
    """
    if name is not None:
        name = str(name).strip()
        if len(name) > MAX_ID_CHARS or not ID_PATTERN.fullmatch(name):
            return None, (
                f"You cannot use '{name}' as a name: names may be 1 to"
                f" {MAX_ID_CHARS} characters of letters, digits,"
                " underscores, and hyphens, with single dots between"
                " them (no leading, trailing, or doubled dots). Pick"
                " another name, or omit it to get an automatic one."
            )
        with registry.lock:
            if name in registry.taken_ids:
                return None, (
                    f"You cannot reuse the name '{name}': it is already"
                    " taken by a background process or subagent, and names"
                    " stay reserved for the whole session. Pick another"
                    " name."
                )
            registry.taken_ids.add(name)
        return name, None
    prefix = f"{kind}_"
    with registry.lock:
        counter = (
            registry._process_counter
            if kind == "process"
            else registry._subagent_counter
        )
        while True:
            candidate = f"{prefix}{next(counter):04d}"
            if candidate not in registry.taken_ids:
                registry.taken_ids.add(candidate)
                return candidate, None


def unknown_id_error(background_id: str) -> str:
    """The what/why/how error for an id no registry entry matches."""
    return (
        f"You cannot check or stop '{background_id}': no process or"
        " subagent with that name is registered in this session (names"
        " are per-session, and a server restart clears the registry). If the"
        " work predates the restart, find it in the pod with `ps` or"
        " `ls /workspace/processes /workspace/subagents` through"
        " execute_command."
    )


def requested_wait_budget_s(arguments: dict | None) -> int | None:
    """The positive wait budget a validated wait_for call asked for.

    The single coercion shared by the deadline builder and the tool
    body, so both read `max_wait_s` the same way. None means "use the
    configured default" (absent) or "invalid" (zero, negative, or the
    wrong type slipped past validation); the tool body rejects the
    invalid case, the deadline builder just falls back to the default.
    """
    if not arguments:
        return None
    value = arguments.get("max_wait_s")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = int(value)
    return value if value > 0 else None


def wait_for_deadline_s(arguments: dict | None) -> float:
    """The harness deadline of one wait_for call, from its arguments.

    The tool's own budget is `max_wait_s` (or the configured default),
    capped at WAIT_FOR_MAX_S; the harness deadline adds slack so the
    tool's graceful "still running" return wins over the stop request.
    """
    requested = requested_wait_budget_s(arguments)
    budget = requested if requested else load_wait_for_timeout()
    return float(min(budget, WAIT_FOR_MAX_S) + WAIT_FOR_DEADLINE_SLACK_S)


def _spawn_launcher(proc: ProcessRecord, command: str, max_runtime_s: int) -> str:
    """Build the pod-side launcher that runs the command detached.

    Layout, chosen around GNU timeout's process-group semantics
    (verified live: timeout makes ITSELF the process group leader with
    `setpgid(0,0)`; the command and its children land in that group):

    - The tool's exec starts `setsid bash -c <launcher>`: the launcher
      becomes a session leader fully detached from the exec call, and
      survives it.
    - The launcher backgrounds `timeout` directly (no setsid here, so
      `$!` and `wait` still target timeout itself), records timeout's
      pid as the process group id (it is the group leader, so
      `kill -- -PGID` reaches the whole command tree, timeout
      included), and, after it exits, records its status as the
      process's exit code.
    - The command runs line-buffered (`stdbuf -oL -eL`: redirected
      output is block-buffered otherwise) with output redirected to
      the files.

    The runtime cap exits with code 124; a manual kill of the group
    leaves the launcher to report 128+signal through the same
    exit_code file. The launcher stamps the start and the true end
    time (epoch seconds, sub-second where `date` supports %N) in the
    pod's own clock, so reports quote the real runtime regardless of
    host/pod clock skew or spawn-dispatch latency, even when the
    completion watcher polls late (a long-running execute_command on
    the same pod delays its next poll).
    """
    dir_q = shlex.quote(proc.dir)
    inner = (
        "exec stdbuf -oL -eL bash -c"
        f" {shlex.quote(command)} </dev/null"
        f" > {dir_q}/stdout 2> {dir_q}/stderr"
    )
    launcher = "\n".join(
        [
            f"mkdir -p {dir_q}",
            "cd /workspace || cd /",
            f"export {NONINTERACTIVE_ENV_EXPORTS}",
            f"date +%s.%N > {dir_q}/started_at",
            f"setsid timeout -k {TIMEOUT_KILL_GRACE_S} {max_runtime_s}"
            f" bash -c {shlex.quote(inner)} </dev/null >/dev/null 2>&1 &",
            "bg=$!",
            f'printf %s "$bg" > {dir_q}/pgid',
            'wait "$bg"',
            f'printf %s "$?" > {dir_q}/exit_code',
            f"date +%s.%N > {dir_q}/finished_at",
        ]
    )
    return f"setsid bash -c {shlex.quote(launcher)} </dev/null >/dev/null 2>&1 &"


def spawn_process(
    command: str,
    name: str | None = None,
    registry: PodRegistry | None = None,
    container=None,
    runtime_cap_s: int | None = None,
) -> dict:
    """Start a detached process in the pod; return its spawn report.

    Detached means detached: the exec call returns as soon as the
    launcher is running, stdout/stderr stream into the process
    directory, and the completion watcher (started lazily) notices the
    exit and fires the registry's completion sink. Failures return
    {"error": ...} in the what/why/how style; the caller turns that
    into the tool's error result.
    """
    if container is None:
        container = get_container()
    if registry is None:
        registry = registry_for(container)
    if runtime_cap_s is None:
        runtime_cap_s = load_process_max_runtime_s()
    process_id, error = reserve_id(registry, "process", name)
    if error:
        return {"error": error}
    parent = get_background_record()
    proc = ProcessRecord(
        id=process_id,
        command=command,
        dir=f"{PROCESS_DIR_ROOT}/{process_id}",
        runtime_cap_s=runtime_cap_s,
        parent_id=parent.id if parent is not None else None,
    )
    exec_script = _spawn_launcher(proc, command, runtime_cap_s)
    try:
        pod_exec(container, ["bash", "-c", exec_script])
        pgid, started_at = _read_spawn_markers(container, proc.dir)
    except Exception as exc:
        with registry.lock:
            registry.processes.pop(process_id, None)
            registry.taken_ids.discard(process_id)
        return {"error": f"The process '{process_id}' could not start: {exc}"}
    if pgid is None:
        with registry.lock:
            registry.processes.pop(process_id, None)
            registry.taken_ids.discard(process_id)
        return {
            "error": (
                f"The process '{process_id}' did not start: its process"
                " group never appeared. The command may be failing"
                " immediately; run it once through execute_command to"
                " see why."
            )
        }
    proc.pgid = pgid
    proc.started_at = started_at
    with registry.lock:
        registry.processes[process_id] = proc
    _ensure_watcher(registry, container)
    get_logger().info(
        "Spawned process '%s' (pgid %s, cap %ss): %s",
        process_id,
        pgid,
        runtime_cap_s,
        command[:200],
    )
    return {
        "name": process_id,
        "dir": proc.dir,
        "max_runtime_s": runtime_cap_s,
    }


def _read_spawn_markers(container, pod_dir: str) -> tuple[int | None, float | None]:
    """Poll for the launcher's pgid and started_at markers.

    Returns (pgid, started_at); pgid None when it never appears (the
    launcher never got that far), started_at None when the stamp is
    missing or unparseable. The launcher writes started_at before it
    starts `timeout` and pgid right after, so a pgid implies the start
    stamp on any launcher that has one.
    """
    pgid_q = shlex.quote(f"{pod_dir}/pgid")
    stamp_q = shlex.quote(f"{pod_dir}/started_at")
    deadline = time.monotonic() + PGID_WAIT_S
    while time.monotonic() < deadline:
        try:
            out = pod_exec(
                container,
                [
                    "bash",
                    "-c",
                    f'printf "PGID %s\\n" "$(cat {pgid_q} 2>/dev/null)";'
                    f' printf "STARTED %s" "$(cat {stamp_q} 2>/dev/null)"',
                ],
            ).strip()
        except Exception:
            return None, None
        pgid_value, stamp_value = "", ""
        for line in out.splitlines():
            if line.startswith("PGID "):
                pgid_value = line[len("PGID ") :].strip()
            elif line.startswith("STARTED "):
                stamp_value = line[len("STARTED ") :].strip()
        if pgid_value.isdigit():
            return int(pgid_value), _parse_epoch(stamp_value)
        time.sleep(0.1)
    return None, None


def _parse_epoch(text: str) -> float | None:
    """Parse a launcher epoch stamp; None when absent or malformed.

    Stamps are written with `date +%s.%N`; a `date` without %N support
    appends a literal N, which fails the float parse and falls back to
    the host-clock measurement instead of poisoning the runtime.
    """
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _ensure_watcher(registry: PodRegistry, container) -> None:
    """Start the completion watcher unless one is already watching.

    The watcher lives for the registry's lifetime and keeps polling
    while processes exist, so a spawn never races a watcher that is
    winding down: it is either looping (it will see the new process) or
    it was never started.
    """
    with registry.watcher_lock:
        if registry.watcher_started:
            return
        registry.watcher_started = True
    threading.Thread(
        target=_watch_processes,
        args=(registry, container),
        name="syna-process-watcher",
        daemon=True,
    ).start()


def _watch_processes(registry: PodRegistry, container) -> None:
    """Notice process exits and fire completion notices.

    One watcher per registry runs until the registry is dropped,
    polling the pod for exit_code markers every few seconds whenever
    processes exist. A poll that raises is retried: transient failures
    (connection pressure under concurrent pod work) must not silence
    completion notices, so the pod is only declared gone after
    consecutive failures give up.
    """
    logger = get_logger()
    failures = 0
    try:
        while registry.active:
            with registry.lock:
                running = [
                    proc
                    for proc in list(registry.processes.values())
                    if proc.state in ("running", "killed")
                ]
            if not running:
                time.sleep(WATCHER_POLL_INTERVAL_S)
                continue
            for proc in running:
                exit_q = shlex.quote(f"{proc.dir}/exit_code")
                stamp_q = shlex.quote(f"{proc.dir}/finished_at")
                try:
                    out = pod_exec(
                        container,
                        [
                            "bash",
                            "-c",
                            f'printf "EXIT %s\\n" "$(cat {exit_q} 2>/dev/null)";'
                            f' printf "STAMP %s" "$(cat {stamp_q} 2>/dev/null)"',
                        ],
                    ).strip()
                except Exception as exc:
                    failures += 1
                    if failures >= WATCHER_MAX_CONSECUTIVE_FAILURES:
                        logger.warning(
                            "Process watcher stopped: %s consecutive pod"
                            " probes failed; last error: %s",
                            failures,
                            exc,
                        )
                        return
                    logger.info(
                        "Process watcher poll failed (%s of %d): %s",
                        failures,
                        WATCHER_MAX_CONSECUTIVE_FAILURES,
                        exc,
                    )
                    break
                failures = 0
                exit_marker, stamp = "", ""
                for line in out.splitlines():
                    if line.startswith("EXIT "):
                        exit_marker = line[len("EXIT ") :].strip()
                    elif line.startswith("STAMP "):
                        stamp = line[len("STAMP ") :].strip()
                if exit_marker:
                    _finalize_process(registry, proc, exit_marker, stamp)
            time.sleep(WATCHER_POLL_INTERVAL_S)
    finally:
        with registry.watcher_lock:
            registry.watcher_started = registry.active


def _finalize_process(
    registry: PodRegistry,
    proc: ProcessRecord,
    exit_marker: str,
    finished_stamp: str = "",
):
    """Record a process's exit and fire its one completion notice.

    The model is woken through the completion sink with the artifact
    paths, unless it already learned the outcome (it killed the process
    itself, a kill call set the flag first, or wait_for already returned
    the final report). A kill call marks the record killed before the pod
    confirms the exit, so this also runs for killed processes: the real
    exit code still lands in the record. The launcher's end-time stamp,
    when present, sets `finished_at` to the true end rather than the
    watcher's (possibly late) poll time.
    """
    with registry.lock:
        if proc.state not in ("running", "killed"):
            return
        try:
            proc.exit_code = int(exit_marker)
        except ValueError:
            proc.exit_code = None
        proc.state = "exited"
        try:
            stamp = float(finished_stamp) if finished_stamp else None
        except ValueError:
            stamp = None
        adopt_end_time(proc, "exited", stamp)
        already_known = proc.notified or proc.outcome_collected
        proc.notified = True
    if already_known:
        return
    if proc.killed_by == "user":
        get_logger().info(
            "Process '%s' ended after the user stopped it (exit %s).",
            proc.id,
            proc.exit_code,
        )
    else:
        get_logger().info(
            "Process '%s' finished with exit code %s.", proc.id, proc.exit_code
        )
    fire_completion(
        registry,
        "process",
        proc,
        process_status_summary(proc),
        process_completion_notice(proc),
    )


def fire_completion(
    registry: PodRegistry,
    kind: str,
    record,
    summary: str,
    notice: str,
) -> None:
    """Deliver one completion notice through the registry's sink.

    The payload carries the short UI summary, the model-facing notice,
    and the record's parent id separately: the server pushes the
    summary as a background event, then routes the notice to the
    spawner (a nested subagent's inbox) or to the top-level worker.
    """
    sink = registry.completion_sink
    if sink is None:
        return
    try:
        sink(
            {
                "kind": kind,
                "id": record.id,
                "parent_id": record.parent_id,
                "summary": summary,
                "model_notice": notice,
            }
        )
    except Exception:
        get_logger().exception("Background completion sink failed")


def process_completion_notice(proc: ProcessRecord) -> str:
    """The model-facing wake notice for a finished process."""
    runtime = _ended_runtime_s(proc)
    if proc.exit_code == CAP_EXIT_CODE:
        outcome = (
            "hit its runtime cap and was killed"
            f" (the cap is {process_runtime_cap_s(proc)} seconds)"
        )
    elif proc.killed_by == "user":
        outcome = "was stopped by the user"
    elif proc.exit_code == 0:
        outcome = "finished successfully (exit code 0)"
    else:
        outcome = f"finished with exit code {proc.exit_code}"
    return (
        f"Background process '{proc.id}' {outcome} after {runtime:.0f}s."
        f" Full output: {proc.dir}/stdout and {proc.dir}/stderr (in the"
        " pod; read them with read_file or pipe them into a script)."
        " If you already collected this outcome with wait_for or check,"
        " you can ignore this notice."
    )


def _ended_runtime_s(record) -> float:
    """Seconds a finished piece of work actually ran.

    Prefers the launcher's own pod-clock stamps (start and end), which
    are immune to host/pod clock skew and to the spawn dispatch latency
    baked into the host-side `started`. A record missing either stamp
    falls back: the recorded end time against the host start when the
    end is known, else now, which is the old at-report measurement and
    still better than nothing.
    """
    if record.started_at is not None and record.finished_at is not None:
        return record.finished_at - record.started_at
    end = record.finished_at if record.finished_at is not None else time.time()
    return end - record.started


def observe_end_time(record, state: str) -> None:
    """Mark the end time the first time a report path sees finished work.

    Finalize normally records the end; a check or kill that probes the
    pod before the watcher catches up adopts its observation so later
    reports cannot keep stretching the runtime.
    """
    if state != "running" and record.finished_at is None:
        record.finished_at = time.time()


def adopt_end_time(record, state: str, finished_at: float | None) -> None:
    """Record the end time from the most trustworthy source available.

    The launcher's own `finished_at` stamp is the true end (written in
    the pod the moment `timeout` returned); the watcher's and report
    paths' wall-clock observations are the fallback. The first value
    set wins, except that a pod stamp adopted later still replaces a
    fallback that is provably later than the truth.
    """
    if state == "running":
        return
    if finished_at is not None:
        if record.finished_at is None or record.finished_at > finished_at:
            record.finished_at = finished_at
        return
    observe_end_time(record, state)


def process_status_summary(proc: ProcessRecord) -> str:
    """One-line UI summary of a process's outcome."""
    if proc.state == "running":
        return "running"
    if proc.exit_code == CAP_EXIT_CODE:
        return "killed by its runtime cap"
    if proc.killed_by == "user":
        return "stopped by the user"
    return f"exited with code {proc.exit_code}"


def _tail_script(pod_dir: str) -> str:
    """The pod-side script returning state plus base64 output tails.

    Tails travel base64-encoded so the host-side split on marker lines
    cannot be fooled by the process's own output. Byte counts bound the
    transfer (4 bytes per character covers any UTF-8); the character
    truncation to the configured tail size happens host-side. The
    launcher's `finished_at` stamp (pod-clock epoch, sub-second where
    supported) rides along so a late report can quote the true end
    time.
    """
    dir_q = shlex.quote(pod_dir)
    byte_cap = load_check_tail_chars() * 4
    return "\n".join(
        [
            f"dir={dir_q}",
            'code=$(cat "$dir/exit_code" 2>/dev/null || true)',
            'if [ -n "$code" ]; then printf \'STATE exited:%s\\n\' "$code";'
            " else printf 'STATE running\\n'; fi",
            "printf 'FINISHED_AT '",
            'cat "$dir/finished_at" 2>/dev/null || true',
            "printf '\\n'",
            "printf 'STDOUT\\n'",
            f'tail -c {byte_cap} "$dir/stdout" 2>/dev/null | base64 -w0 || true',
            "printf '\\nSTDERR\\n'",
            f'tail -c {byte_cap} "$dir/stderr" 2>/dev/null | base64 -w0 || true',
            "printf '\\n'",
        ]
    )


def _decode_tails(view: str) -> tuple[str, str]:
    """Split the status probe's output into (stdout_tail, stderr_tail)."""
    lines = view.splitlines()
    parts: dict[str, str] = {}
    current = None
    for line in lines:
        if line in ("STDOUT", "STDERR"):
            current = line
            parts[current] = ""
        elif current is not None and line:
            parts[current] += line
    tails = []
    for key in ("STDOUT", "STDERR"):
        blob = parts.get(key, "")
        try:
            tails.append(base64.b64decode(blob).decode("utf-8", errors="replace"))
        except (ValueError, TypeError):
            tails.append("")
    stdout, stderr = tails
    tail_chars = load_check_tail_chars()
    return (
        stdout[-tail_chars:] if stdout else "",
        stderr[-tail_chars:] if stderr else "",
    )


def check_process_state(container, pod_dir: str) -> tuple[str, str, str, float | None]:
    """Probe a process directory.

    Returns (state_marker, stdout_tail, stderr_tail, finished_at_epoch)
    — finished_at is None while running or when the stamp predates the
    stamp's introduction. A missing directory surfaces as an exception
    from the exec; callers report it.
    """
    view = pod_exec(container, ["bash", "-c", _tail_script(pod_dir)])
    lines = view.splitlines()
    state = "running"
    if lines and lines[0].startswith("STATE "):
        state = lines[0][len("STATE ") :].strip()
    finished_at = None
    for line in lines[1:]:
        if line.startswith("FINISHED_AT "):
            try:
                finished_at = float(line[len("FINISHED_AT ") :].strip())
            except ValueError:
                finished_at = None
        elif line == "STDOUT":
            break
    stdout, stderr = _decode_tails(view)
    return state, stdout, stderr, finished_at


def format_process_report(
    proc: ProcessRecord, state: str, stdout: str, stderr: str
) -> str:
    """Render check/kill output: state, runtime, tails, artifact paths."""
    if state == "running":
        runtime = time.time() - proc.started
    else:
        runtime = _ended_runtime_s(proc)
    if state == "running":
        header = (
            f"Process '{proc.id}': running (runtime {runtime:.0f}s of a"
            f" {process_runtime_cap_s(proc)}s cap)."
        )
    else:
        code = state.split(":", 1)[1] if ":" in state else "?"
        if code == str(CAP_EXIT_CODE):
            header = (
                f"Process '{proc.id}': finished; the runtime cap killed"
                f" it (exit code {CAP_EXIT_CODE}) after {runtime:.0f}s."
            )
        elif proc.killed_by == "user":
            header = (
                f"Process '{proc.id}': stopped by the user (exit code"
                f" {code}) after {runtime:.0f}s."
            )
        else:
            header = (
                f"Process '{proc.id}': finished with exit code {code}"
                f" after {runtime:.0f}s."
            )
    sections = []
    for label, tail in (("stdout", stdout), ("stderr", stderr)):
        if tail:
            sections.append(f"--- {label} (last characters) ---\n{tail}")
        else:
            sections.append(f"--- {label}: (no output yet) ---")
    paths = f"Full output files: {proc.dir}/stdout and {proc.dir}/stderr (in the pod)."
    return "\n".join([header, *sections, paths])


def kill_process(
    process_id: str,
    registry: PodRegistry | None = None,
    container=None,
    by: str = "model",
) -> str:
    """Kill a process tree and report the outcome.

    Killing signals the command's process group (TERM, then SIGKILL
    after a grace, both pod-side), never destroys artifacts. The whole
    operation, state mutation through probe and report, holds the
    registry lock, so it cannot interleave with the completion
    watcher's finalize pass (the lock never waits on anything that
    would take it again; the pod call lock is strictly inner).

    The exit marker lands when the launcher notices `timeout` exit,
    usually within moments; a kill call reports the state as of right
    now and the completion machinery keeps the record honest
    afterwards.
    """
    if container is None:
        container = get_container()
    if registry is None:
        registry = registry_for(container)
    with registry.lock:
        proc = registry.processes.get(process_id)
        if proc is None:
            return "ERROR: " + unknown_id_error(process_id)
        if proc.state != "running":
            state, stdout, stderr, ended = check_process_state(container, proc.dir)
            if state != "running":
                adopt_end_time(proc, state, ended)
                proc.outcome_collected = True
            return (
                format_process_report(proc, state, stdout, stderr)
                + " It had already finished before this kill call."
            )
        proc.state = "killed"
        proc.killed_by = by
        if by == "model":
            proc.notified = True
        dir_q = shlex.quote(proc.dir)
        script = "\n".join(
            [
                f"dir={dir_q}",
                'pgid=$(cat "$dir/pgid" 2>/dev/null || true)',
                'code=$(cat "$dir/exit_code" 2>/dev/null || true)',
                'if [ -z "$code" ] && [ -n "$pgid" ]; then',
                '  kill -- -"$pgid" 2>/dev/null || true',
                '  (sleep 2; kill -9 -- -"$pgid" 2>/dev/null || true) &',
                "fi",
            ]
        )
        try:
            pod_exec(container, ["bash", "-c", script])
        except Exception as exc:
            get_logger().warning("Kill signal for '%s' failed: %s", process_id, exc)
            return (
                f"ERROR: the kill signal for process '{process_id}' failed:"
                f" {exc}. It may already be gone with the pod; check with"
                " process check."
            )
        get_logger().info("Process '%s' killed by %s.", process_id, by)
        state, stdout, stderr, ended = check_process_state(container, proc.dir)
        adopt_end_time(proc, state, ended)
        return (
            format_process_report(proc, state, stdout, stderr)
            + " Stop signals sent to the whole process group; the exit code"
            " lands in the record when the process actually ends."
        )
