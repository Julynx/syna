"""Detached AI subagents: background instances of the agent itself.

The `subagent` tool's spawn/check/kill operations live here. A subagent
is a full `AgentSession` on its own thread: same system prompt, same
model configuration, same shared pod, and every tool except the ones a
background worker cannot use (asking the user, speech and image
generation, self-review). Its first user message is a nesting header
followed by the spawn goal.

Artifacts mirror the process capability: a real-time host-side message
dump (via the session dump machinery, one file per subagent), copied
into the pod at check, kill, and natural termination together with the
final response, so the paths reported to the model are always true.
Artifacts are never destroyed by a kill.

The registry itself (`PodRegistry`, id reservation, completion firing)
comes from `processes.py`. `AgentSession` is imported lazily inside the
runner: importing it at module level would close an import cycle
(tools -> here -> session -> parse -> tools).
"""

import json
import shlex
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import (
    get_logger,
    get_session_dump_path,
    load_check_tail_chars,
    load_max_concurrent_subagents,
    load_max_subagent_depth,
    load_subagent_max_runtime_s,
)
from ..state import (
    get_background_allegro,
    get_background_level,
    get_background_record,
    get_container,
    register_container,
    set_background_allegro,
    set_background_level,
    set_background_record,
    unregister_container,
)
from .processes import (
    SUBAGENT_DIR_ROOT,
    PodRegistry,
    _ended_runtime_s,
    fire_completion,
    pod_copy_to,
    pod_exec,
    registry_for,
    reserve_id,
    unknown_id_error,
)

# Tools a background worker cannot use: asking the user (it must decide
# autonomously), the expensive media generators, self-review (its work
# is judged by the spawning agent, not a reviewer of its own), and
# share_file (what reaches the user is the spawning agent's call,
# based on the subagent's report; a detached worker does not decide
# what the user downloads).
SUBAGENT_EXCLUDED_TOOLS = (
    "ask_user",
    "text_to_speech",
    "text_to_image",
    "review_work",
    "share_file",
)

# Cap on the streamed event history kept per subagent for the UI modal.
EVENT_HISTORY_LIMIT = 800
STREAM_CAP_CHARS = 6000
RESPONSE_COPY_NAME = "response.md"
DUMP_COPY_NAME = "messages.json"


@dataclass
class SubagentRecord:
    """One subagent: identity, session handle, and outcome."""

    id: str
    goal: str
    level: int
    started: float = field(default_factory=time.time)
    finished_at: float | None = None
    parent_id: str | None = None
    runtime_cap_s: int | None = None
    state: str = "running"
    end_reason: str | None = None
    stopped_by: str | None = None
    response: str | None = None
    session: object | None = None
    dump_host_path: Path | None = None
    kill_requested: threading.Event = field(default_factory=threading.Event)
    cap_fired: threading.Event = field(default_factory=threading.Event)
    events: list = field(default_factory=list)
    events_lock: threading.Lock = field(default_factory=threading.Lock)
    event_seq: int = 0
    notified: bool = False
    outcome_collected: bool = False
    allegro: bool = False


def subagent_runtime_cap_s(record: SubagentRecord) -> int:
    """The subagent's effective runtime cap in seconds.

    Every report and the cap timer read the record's own cap, so a
    spawn with a custom cap is reported and enforced consistently; the
    configured default covers records built without one.
    """
    if record.runtime_cap_s is not None:
        return record.runtime_cap_s
    return load_subagent_max_runtime_s()


def _cap(text: str, limit: int = STREAM_CAP_CHARS) -> str:
    """Bound a streamed string for transport and history memory."""
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "..."


def subagent_header(level: int, max_depth: int) -> str:
    """The nesting header prepended to every subagent's first message."""
    return (
        f"You are a background subagent spawned by the main agent"
        f" (nesting level {level} of {max_depth}). Complete the goal"
        " below autonomously: you cannot ask the user questions. Reply"
        " with a final answer reporting the outcome and the files you"
        " produced. You share the pod with the agent that spawned you:"
        " keep to the paths this goal names.\n\n"
        "## Goal\n\n"
    )


def spawn_subagent(
    goal: str,
    name: str | None = None,
    registry: PodRegistry | None = None,
    container=None,
    session_factory=None,
    runtime_cap_s: int | None = None,
) -> dict:
    """Start a detached subagent; return its spawn report.

    Gating runs before anything expensive: depth first, then the
    concurrent cap, then id reservation. The session is built on the
    runner thread (lazy import, client setup); spawn returns as soon as
    the thread is running. Failures return {"error": ...} in the
    what/why/how style.
    """
    if container is None:
        container = get_container()
    if registry is None:
        registry = registry_for(container)
    level = get_background_level()
    max_depth = load_max_subagent_depth()
    if level + 1 > max_depth:
        return {
            "error": (
                "You cannot spawn more subagents: this agent is at the"
                f" maximum depth of the agent tree (nesting level {level}"
                f" of {max_depth}). Handle the task yourself."
            )
        }
    if not isinstance(goal, str) or not goal.strip():
        return {
            "error": (
                "You cannot spawn a subagent without a goal: the goal is"
                " the task it will work on. Write a self-contained goal"
                " naming the work and its deliverables."
            )
        }
    with registry.lock:
        running = sum(
            1 for record in registry.subagents.values() if record.state == "running"
        )
    concurrent_cap = load_max_concurrent_subagents()
    if running >= concurrent_cap:
        return {
            "error": (
                f"You cannot spawn more subagents right now: {running} are"
                f" already running (the limit is {concurrent_cap}). Wait"
                " for one to finish with wait_for, stop one with subagent"
                " kill, or do the work yourself."
            )
        }
    subagent_id, error = reserve_id(registry, "subagent", name)
    if error:
        return {"error": error}
    parent = get_background_record()
    record = SubagentRecord(
        id=subagent_id,
        goal=goal,
        level=level + 1,
        runtime_cap_s=runtime_cap_s,
        parent_id=parent.id if parent is not None else None,
        allegro=get_background_allegro(),
        dump_host_path=get_session_dump_path(f"subagent-{subagent_id}"),
    )
    with registry.lock:
        registry.subagents[subagent_id] = record
    threading.Thread(
        target=_run_subagent,
        args=(record, registry, container, session_factory),
        name=f"syna-subagent-{subagent_id}",
        daemon=True,
    ).start()
    get_logger().info(
        "Spawned subagent '%s' (level %s of %s): %.200s",
        subagent_id,
        record.level,
        max_depth,
        goal,
    )
    return {
        "name": subagent_id,
        "level": record.level,
        "max_runtime_s": subagent_runtime_cap_s(record),
    }


def _default_session_factory(record: SubagentRecord, registry: PodRegistry):
    """Build the real agent session for a subagent record."""
    from ..conversation.session import AgentSession

    return AgentSession(
        on_event=lambda event: _on_subagent_event(registry, record, event),
        emit_tool_errors=True,
        persist_outputs=True,
        session_id=f"subagent-{record.id}",
        excluded_tools=SUBAGENT_EXCLUDED_TOOLS,
        injection_filter=_injection_filter(registry),
    )


def _injection_filter(registry: PodRegistry):
    """The apply-time drop rule for messages injected into a subagent.

    A completion notice injected at a request boundary may have lost
    the race to the parent's own wait_for in the meantime; apply time
    is the last chance to drop it, so the outcome never reaches the
    model twice. Non-notice messages always pass. The viewer's notice
    entry is emitted by the session at the same apply moment (see
    `AgentSession.apply_injections`), so it only ever shows applied
    wakes.
    """

    def keep(item: dict) -> bool:
        background_id = item.get("bg_id")
        if not background_id:
            return True
        entry = registry.lookup(background_id)
        return not (entry is not None and entry[1].outcome_collected)

    return keep


def _on_cap_fired(record: SubagentRecord, session):
    """The runtime cap callback: mark it and stop the turn gracefully."""
    record.cap_fired.set()
    get_logger().info("Subagent '%s' hit its runtime cap; stopping it.", record.id)
    session.cancel()


def _run_subagent(
    record: SubagentRecord, registry: PodRegistry, container, session_factory
):
    """The runner thread: own turn, runtime cap, artifacts, completion."""
    logger = get_logger()
    if session_factory is None:
        session_factory = _default_session_factory
    try:
        register_container(container)
        set_background_level(record.level)
        set_background_record(record)
        set_background_allegro(record.allegro)
        session = session_factory(record, registry)
        record.session = session
        if record.kill_requested.is_set():
            raise _KilledBeforeStart()
        # Allegro inheritance: the spawning turn's mode (captured at
        # spawn time from the caller's thread) applies to the
        # subagent's system prompt and timings (effort, delays, step
        # budget, timeout scale). Its runtime cap is its own, not the
        # halved per-call scale.
        if record.allegro:
            session.set_allegro(True)
        cap_timer = threading.Timer(
            subagent_runtime_cap_s(record),
            _on_cap_fired,
            args=(record, session),
        )
        cap_timer.daemon = True
        cap_timer.start()
        try:
            session.run_turn(
                subagent_header(record.level, load_max_subagent_depth()) + record.goal
            )
        except Exception:
            logger.exception("Subagent '%s' turn failed", record.id)
        finally:
            cap_timer.cancel()
        if record.kill_requested.is_set():
            record.state = "killed"
            record.end_reason = f"stopped by the {record.stopped_by or 'user'}"
        elif record.cap_fired.is_set():
            record.state = "finished"
            record.end_reason = "runtime cap"
        else:
            record.state = "finished"
            record.end_reason = "response"
        if not record.response:
            record.response = _last_assistant_content(session)
    except _KilledBeforeStart:
        record.state = "killed"
        record.end_reason = f"stopped by the {record.stopped_by or 'user'}"
    except Exception:
        logger.exception("Subagent '%s' runner failed", record.id)
        record.state = "finished"
        record.end_reason = "error"
    finally:
        set_background_level(None)
        set_background_record(None)
        set_background_allegro(None)
        unregister_container()
        if record.state != "running" and record.finished_at is None:
            record.finished_at = time.time()
        if record.session is not None:
            try:
                record.session.close()
            except Exception:
                logger.exception("Subagent '%s' client cleanup failed", record.id)
        _finalize_subagent(registry, record, container)


class _KilledBeforeStart(Exception):
    """Internal: the subagent was killed before its session existed."""


def _last_assistant_content(session) -> str | None:
    """The subagent's last assistant text, as a response fallback.

    A turn ended by the runtime cap or a kill has no final
    model_message event; the last assistant message in history is the
    best available stand-in.
    """
    for message in reversed(getattr(session, "messages", [])):
        if message.get("role") == "assistant":
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return None


def _on_subagent_event(registry: PodRegistry, record: SubagentRecord, event: dict):
    """Translate one session event into the subagent's compact stream."""
    if event.get("type") == "model_message":
        record.response = event.get("content")
    compact = _compact_event(event)
    if compact is None:
        return
    _emit_subagent_event(registry, record, compact)


def _compact_event(event: dict) -> dict | None:
    """Keep only what the subagent viewer renders; None drops the event."""
    event_type = event.get("type")
    if event_type == "model_message":
        return {"kind": "model", "content": _cap(event.get("content"))}
    if event_type == "injected_notice":
        return {"kind": "notice", "text": _cap(event.get("content"), 500)}
    if event_type == "error":
        return {"kind": "error", "text": _cap(event.get("text"), 2000)}
    if event_type == "tool_call":
        compact = {
            "kind": "tool",
            "status": event.get("status"),
            "name": event.get("name"),
            "signature": _cap(event.get("signature"), 500),
        }
        if event.get("status") == "finished":
            compact["output_preview"] = _cap(event.get("output_preview"), 2000)
            if event.get("failed"):
                compact["failed"] = True
        return compact
    return None


def _emit_subagent_event(
    registry: PodRegistry, record: SubagentRecord, compact: dict
) -> None:
    """Append a compact event to the record's history and stream it."""
    with record.events_lock:
        record.event_seq += 1
        entry = {"seq": record.event_seq, **compact}
        record.events.append(entry)
        if len(record.events) > EVENT_HISTORY_LIMIT:
            del record.events[: len(record.events) - EVENT_HISTORY_LIMIT]
    sink = registry.event_sink
    if sink is not None:
        try:
            sink(record.id, entry)
        except Exception:
            get_logger().exception("Subagent event sink failed")


def subagent_status_summary(record: SubagentRecord) -> str:
    """One-line UI summary of a subagent's outcome."""
    if record.state == "running":
        return "running"
    if record.end_reason == "runtime cap":
        return "stopped by its runtime cap"
    if record.state == "killed":
        return f"stopped by the {record.stopped_by or 'user'}"
    if record.end_reason == "error":
        return "ended with an error"
    return "finished with a response"


def subagent_completion_notice(record: SubagentRecord) -> str:
    """The model-facing wake notice for a finished subagent."""
    runtime = _ended_runtime_s(record)
    if record.end_reason == "runtime cap":
        outcome = (
            "was stopped by its runtime cap (the cap is"
            f" {subagent_runtime_cap_s(record)} seconds)"
        )
    elif record.state == "killed":
        outcome = f"was stopped by the {record.stopped_by or 'user'}"
    elif record.end_reason == "error":
        outcome = "ended with an internal error"
    else:
        outcome = "finished with a response"
    pod_dir = f"{SUBAGENT_DIR_ROOT}/{record.id}"
    return (
        f"Background subagent '{record.id}' {outcome} after {runtime:.0f}s."
        f" Its response: {pod_dir}/{RESPONSE_COPY_NAME} and its message"
        f" log: {pod_dir}/{DUMP_COPY_NAME} (in the pod; read them with"
        " read_file). If you already collected this outcome with"
        " wait_for or check, you can ignore this notice."
    )


def _finalize_subagent(registry: PodRegistry, record: SubagentRecord, container):
    """Copy artifacts into the pod and fire the completion notice.

    Runs on the runner thread at every end: natural, runtime cap, kill,
    or error. Artifact copies are best-effort (a dead pod cannot receive
    them); the host-side dump always remains. Notice firing is the same
    single-producer path the process capability uses, skipped when the
    model already learned the outcome (it killed the subagent itself, or
    wait_for already returned the final report).
    """
    try:
        copy_subagent_artifacts(record, container)
    except Exception:
        get_logger().exception("Subagent '%s' artifact copy failed", record.id)
    _emit_subagent_event(
        registry, record, {"kind": "done", "summary": subagent_status_summary(record)}
    )
    with registry.lock:
        already_known = record.notified or record.outcome_collected
        record.notified = True
    get_logger().info("Subagent '%s' %s.", record.id, subagent_status_summary(record))
    if not already_known:
        fire_completion(
            registry,
            "subagent",
            record,
            subagent_status_summary(record),
            subagent_completion_notice(record),
        )


def copy_subagent_artifacts(record: SubagentRecord, container) -> None:
    """Copy the message dump and response into the pod, read-only.

    Called at check, kill, and natural termination so every path
    reported to the model exists by the time the report reaches it.
    The pod copy is a snapshot; the host dump keeps updating.
    """
    if record.dump_host_path is None or not record.dump_host_path.is_file():
        return
    pod_dir = f"{SUBAGENT_DIR_ROOT}/{record.id}"
    pod_exec(container, ["bash", "-c", f"mkdir -p {shlex.quote(pod_dir)}"])
    _copy_file_into_pod(container, record.dump_host_path, pod_dir, DUMP_COPY_NAME)
    if record.response:
        host_response = record.dump_host_path.parent / (
            f"{record.dump_host_path.stem}-response.md"
        )
        host_response.write_text(record.response, encoding="utf-8")
        _copy_file_into_pod(container, host_response, pod_dir, RESPONSE_COPY_NAME)


def _copy_file_into_pod(container, source: Path, pod_dir: str, name: str) -> None:
    """Place one host file into the pod read-only (artifact convention)."""
    with tempfile.TemporaryDirectory(prefix="syna-subagent-") as temp_dir:
        host_file = Path(temp_dir, name)
        host_file.write_bytes(source.read_bytes())
        pod_copy_to(container, str(host_file), pod_dir)
    try:
        pod_exec(
            container,
            ["bash", "-c", f"chmod 444 {shlex.quote(f'{pod_dir}/{name}')}"],
        )
    except Exception:
        get_logger().warning(
            "Could not make %s/%s read-only", pod_dir, name, exc_info=True
        )


def _dump_messages_tail(dump_host_path: Path) -> str | None:
    """The message log tail from the host dump, oldest to newest.

    Whole messages are kept (never cut mid-message) until the
    configured tail budget is spent, so each JSON line parses.
    """
    try:
        payload = json.loads(dump_host_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    budget = load_check_tail_chars()
    pieces: list[str] = []
    used = 0
    for message in reversed(messages):
        piece = json.dumps(message, ensure_ascii=False)
        if pieces and used + len(piece) > budget:
            break
        pieces.insert(0, piece)
        used += len(piece)
    prefix = (
        "" if len(pieces) == len(messages) else "[... older messages omitted ...]\n"
    )
    return prefix + "\n".join(pieces)


def subagent_is_settled(record: SubagentRecord) -> bool:
    """Whether a subagent has ended, or a kill has already been asked."""
    return record.state != "running" or record.kill_requested.is_set()


def check_subagent(
    subagent_id: str,
    registry: PodRegistry | None = None,
    container=None,
) -> str:
    """Report a subagent's state, tail of its message log, and paths."""
    if container is None:
        container = get_container()
    if registry is None:
        registry = registry_for(container)
    with registry.lock:
        record = registry.subagents.get(subagent_id)
    if record is None:
        return "ERROR: " + unknown_id_error(subagent_id)
    try:
        copy_subagent_artifacts(record, container)
    except Exception:
        get_logger().exception("Subagent '%s' artifact copy failed", record.id)
    runtime = time.time() - record.started
    if record.state != "running":
        runtime = _ended_runtime_s(record)
        record.outcome_collected = True
    max_depth = load_max_subagent_depth()
    if record.state == "running":
        header = (
            f"Subagent '{subagent_id}': running (nesting level"
            f" {record.level} of {max_depth}, runtime {runtime:.0f}s of a"
            f" {subagent_runtime_cap_s(record)}s cap)."
        )
    elif record.state == "killed":
        header = (
            f"Subagent '{subagent_id}': stopped by the"
            f" {record.stopped_by or 'user'} after {runtime:.0f}s."
        )
    elif record.end_reason == "runtime cap":
        header = (
            f"Subagent '{subagent_id}': was stopped by its runtime cap"
            f" after {runtime:.0f}s."
        )
    else:
        header = (
            f"Subagent '{subagent_id}': finished with a response after"
            f" {runtime:.0f}s (nesting level {record.level} of {max_depth})."
        )
    sections = [header]
    tail = (
        _dump_messages_tail(record.dump_host_path)
        if record.dump_host_path is not None
        else None
    )
    if tail:
        sections.append("--- message log tail ---\n" + tail)
    else:
        sections.append("--- message log: (no model requests yet) ---")
    pod_dir = f"{SUBAGENT_DIR_ROOT}/{subagent_id}"
    paths = f"Transcript (in the pod): {pod_dir}/{DUMP_COPY_NAME}."
    if record.response:
        sections.append("--- response ---\n" + _cap(record.response))
        paths += f" Response: {pod_dir}/{RESPONSE_COPY_NAME}."
    elif record.state == "running":
        paths += (
            f" The response lands at {pod_dir}/{RESPONSE_COPY_NAME} when it finishes."
        )
    sections.append(paths)
    return "\n\n".join(sections)


def kill_subagent(
    subagent_id: str,
    registry: PodRegistry | None = None,
    container=None,
    by: str = "model",
) -> str:
    """Stop a subagent and report its artifacts; never destroys them.

    The stop is a graceful cancel: the subagent ends at its next pause,
    usually within moments. The whole operation holds the registry
    lock, so it cannot interleave with the runner's finalize pass; the
    runner (which needs the same lock to record its completion) waits
    and finalizes cleanly afterwards. Artifacts are copied into the pod
    right here so the reported paths are true immediately, and again
    when the runner actually ends (with the final transcript and
    response). The settled path re-reads through `check_subagent`
    outside the lock: a settled state is monotonic, so the racy window
    cannot change the answer.
    """
    if container is None:
        container = get_container()
    if registry is None:
        registry = registry_for(container)
    with registry.lock:
        record = registry.subagents.get(subagent_id)
        if record is None:
            return "ERROR: " + unknown_id_error(subagent_id)
        settled = record.state != "running"
        if not settled:
            record.stopped_by = by
            record.kill_requested.set()
            if by == "model":
                record.notified = True
            if record.session is not None:
                record.session.cancel()
            try:
                copy_subagent_artifacts(record, container)
            except Exception:
                get_logger().exception("Subagent '%s' artifact copy failed", record.id)
    if settled:
        # A settled state is monotonic, so re-reading outside the lock
        # (check_subagent takes it itself) cannot change the answer.
        return (
            check_subagent(subagent_id, registry, container)
            + "\nIt had already finished before this kill call."
        )
    runtime = time.time() - record.started
    pod_dir = f"{SUBAGENT_DIR_ROOT}/{subagent_id}"
    get_logger().info("Subagent '%s' kill requested by %s.", subagent_id, by)
    return (
        f"Subagent '{subagent_id}': stop requested (stopped by the {by},"
        f" runtime {runtime:.0f}s). It ends at its next pause; its"
        f" transcript is at {pod_dir}/{DUMP_COPY_NAME} and its response"
        f" will land at {pod_dir}/{RESPONSE_COPY_NAME} (in the pod). A"
        " notice arrives here when it has fully stopped."
    )
