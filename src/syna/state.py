"""Global Docker client, per-thread container registry, and per-call context.

Besides binding containers to threads, this module carries the protocol state
of the tool call currently being dispatched: the pod path where the call's
full output must be saved (`/tool_outputs/`), the exit code reported by
`execute_command`, and the cancel event the harness sets when the call
passes its deadline. Tools set these cooperatively so `parse.py` can build
the `<result>` blocks without coupling every tool to the result protocol.
"""

import itertools
import logging
import threading
import time
from contextlib import ExitStack

from bollard import Container, DockerClient

from . import pod_init
from .config import get_logger, load_config
from .pod_io import pod_call

CONTAINER_IMAGE = "python:3.14-trixie"

DEFAULT_POD_MEMORY_MB = 8192
DEFAULT_POD_PIDS_LIMIT = 512

_local = threading.local()
_call_context = threading.local()
_call_counter = itertools.count(1)
_exit_stack: ExitStack | None = None
client: DockerClient | None = None
_global_container: Container | None = None
_init_lock = threading.Lock()


def next_call_index() -> int:
    """Return a monotonically increasing index for numbering tool outputs."""
    return next(_call_counter)


def set_call_output_path(path: str | None) -> None:
    """Record the pod path where the current call's full output belongs."""
    _call_context.output_path = path


def get_call_output_path() -> str | None:
    """Return the output path of the call being dispatched, if any."""
    return getattr(_call_context, "output_path", None)


def set_last_exit_code(exit_code: int | None) -> None:
    """Record the exit code reported by the tool that just finished."""
    _call_context.exit_code = exit_code


def pop_last_exit_code() -> int | None:
    """Return and clear the exit code recorded by the last dispatched tool."""
    exit_code = getattr(_call_context, "exit_code", None)
    _call_context.exit_code = None
    return exit_code


class ToolCancelled(Exception):
    """Raised by a tool that honored the harness's stop request.

    The harness sets the call's cancel event when the deadline passes and
    waits for the worker to exit. A tool that checks the event stops
    promptly and raises this class, so the harness can report the
    deadline notice, the harness stopped the call at its limit, rather
    than an incidental failure message.
    """


def set_cancel_event(event: threading.Event | None) -> None:
    """Record the cancel event of the tool call being dispatched."""
    _call_context.cancel_event = event


def get_cancel_event() -> threading.Event | None:
    """Return the cancel event of the call being dispatched, if any.

    The event is set when the call passes its deadline; sleeps and long
    operations of the tool should end early through it and raise
    `ToolCancelled` once they have unwound. Untimed dispatches register
    no event.
    """
    return getattr(_call_context, "cancel_event", None)


def set_background_level(level: int | None) -> None:
    """Record the subagent nesting depth of the current context.

    Main-agent turns leave it unset (meaning 1). The dispatch layer
    propagates it onto tool worker threads alongside the container, so
    a `subagent` tool call executing deep inside a batch still knows
    how deep the spawning agent itself is.
    """
    _call_context.background_level = level


def get_background_level() -> int:
    """Return the subagent nesting depth of the calling context, from 1."""
    return getattr(_call_context, "background_level", None) or 1


def set_background_allegro(enabled: bool | None) -> None:
    """Record whether the running turn is in Allegro mode.

    Set by `run_turn` and propagated by the dispatch layer like the
    nesting level, so spawned subagents inherit the mode (its prompt
    suffix and timings).
    """
    _call_context.background_allegro = enabled


def get_background_allegro() -> bool:
    """Whether the spawning turn runs in Allegro mode."""
    return bool(getattr(_call_context, "background_allegro", None))


def set_background_record(record) -> None:
    """Record the background-work record of the current context.

    The subagent runner sets its own record before the turn starts, so
    a `process` or `subagent` call executing inside that turn can stamp
    the spawner's name as the spawned work's parent. Main-agent turns
    leave it unset (meaning no parent).
    """
    _call_context.background_record = record


def get_background_record():
    """Return the calling context's own background record, or None."""
    return getattr(_call_context, "background_record", None)


def register_container(container: Container) -> None:
    """Bind a container to the calling thread so tools can resolve it."""
    _local.container = container


def unregister_container() -> None:
    """Unbind the container of the calling thread without destroying it."""
    _local.container = None


def get_registered_container() -> Container | None:
    """Return the container bound to the calling thread, if any."""
    return getattr(_local, "container", None)


def build_pod_host_config(
    memory_mb: int | None = None, pids_limit: int | None = None
) -> dict:
    """Assemble the pod's HostConfig: init reaping plus resource ceilings.

    The limits protect the host from runaway processes inside the pod; they
    do not restrict what the model may do inside the pod. Values of 0 or
    None leave the corresponding ceiling unset.
    """
    if memory_mb is None:
        memory_mb = load_config().get("pod_memory_mb", DEFAULT_POD_MEMORY_MB)
    if pids_limit is None:
        pids_limit = load_config().get("pod_pids_limit", DEFAULT_POD_PIDS_LIMIT)

    host_config: dict = {"Init": True}
    if memory_mb:
        host_config["Memory"] = int(memory_mb) * 1024 * 1024
    if pids_limit:
        host_config["PidsLimit"] = int(pids_limit)
    return host_config


def _is_connection_error(exc: BaseException) -> bool:
    """Whether an exception looks like a dead engine endpoint."""
    if isinstance(exc, OSError):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("pipe", "socket", "connection", "endpoint", "unreachable")
    )


def _reset_dead_client(*, strict: bool = False) -> None:
    """Drop a Docker client whose engine endpoint went stale.

    The single reset path for the global client, used by
    `create_container`'s self-heal and by `close_docker_client`: the
    client can outlive the engine (Docker Desktop quit, Podman machine
    stopped, host resumed from sleep), leaving every request failing on
    a dead pipe or socket until a restart. The shared pod, if any, is
    destroyed first. With `strict` a failed destroy propagates (shutdown
    path); otherwise it is logged and dropped (self-heal path: with the
    engine gone the pod is unreachable anyway, and the next
    `get_container()` call lazily starts a replacement).
    """
    global _exit_stack, client, _global_container
    log = get_logger()
    with _init_lock:
        if _global_container is not None:
            if strict:
                destroy_container(_global_container)
            else:
                try:
                    destroy_container(_global_container)
                except Exception:
                    log.info(
                        "Could not destroy the shared pod during engine reset.",
                        exc_info=True,
                    )
            _global_container = None
        if _exit_stack is not None:
            stack, _exit_stack = _exit_stack, None
            client = None
            stack.close()


def create_container(shared_copy_sink=None) -> Container:
    """Initialize the shared Docker client if needed and run a new container.

    HostConfig Init runs the container's init process (tini equivalent) so
    orphaned background processes started by model commands are reaped.

    Pod initialization steps (`pod_init`) run right after start and
    block until finished, so the pod is never handed over in a
    half-initialized state; each step logs its own duration and fails
    open. The user's shared directory (`~/.syna/shared`) is copied into
    the pod the same way; when a `shared_copy_sink` is given it receives
    the copy report, so callers with a UI can tell the user what
    landed.

    A connection-level failure triggers one client reset and retry: a
    mid-session engine death (endpoint gone stale) self-heals through
    bollard's discovery instead of failing every call until restart.
    """
    if client is None:
        init_docker_client()
    try:
        container = client.run_container(
            CONTAINER_IMAGE,
            command="sleep infinity",
            HostConfig=build_pod_host_config(),
        )
    except Exception as exc:
        if not _is_connection_error(exc):
            raise
        get_logger().warning(
            "The container engine endpoint appears to have gone stale (%s);"
            " re-initializing the client and retrying once.",
            exc,
        )
        _reset_dead_client()
        init_docker_client()
        container = client.run_container(
            CONTAINER_IMAGE,
            command="sleep infinity",
            HostConfig=build_pod_host_config(),
        )
    pod_init.run_pod_init_steps(container)
    shared_report = pod_init.copy_shared_dir(container)
    if shared_report is not None and shared_copy_sink is not None:
        try:
            shared_copy_sink(shared_report)
        except Exception:
            get_logger().exception("Shared-copy sink failed")
    return container


def get_container() -> Container:
    """Return the calling thread's container, falling back to a shared one."""
    global _global_container
    container = getattr(_local, "container", None)
    if container is not None:
        return container
    if _global_container is None:
        _global_container = create_container()
    return _global_container


def destroy_container(container: Container) -> None:
    """Stop and force-remove a container."""
    pod_call(container.stop)
    pod_call(lambda: container.remove(force=True))


def reset_shared_container() -> None:
    """Destroy the shared host-side container so a fresh one is created.

    Used by the CLI's new-chat reset: the next get_container() call
    lazily starts a replacement pod. Thread-bound containers are the
    caller's responsibility and are left untouched.
    """
    global _global_container
    if _global_container is not None:
        destroy_container(_global_container)
        _global_container = None


def ensure_engine_ready() -> None:
    """Verify a container engine endpoint is reachable, bringing it up.

    Delegates to bollard's endpoint discovery, which starts (and
    initializes, when it does not exist yet) the Podman machine when no
    Docker/Podman pipe or socket is available. Raises an exception with
    an actionable message when no endpoint can be reached.
    """
    log = get_logger()
    log.info("Checking the container engine endpoint...")
    started = time.monotonic()
    DockerClient().close()
    log.info("Container engine endpoint ready in %.1fs.", time.monotonic() - started)


def init_docker_client(*args, **kwargs):
    """Initialize the global Docker client context.

    Serialized: a concurrent caller waits for an in-flight engine
    bring-up instead of failing. A failed constructor rolls the state
    back so the next call retries bringing the engine up instead of
    finding the half-initialized leftovers forever. Only bollard's
    wire-level logger is quieted here: its per-request debug output
    would flood the syna log file, while bollard's operational messages
    (machine bring-up, container creation, image pulls) are worth
    keeping.
    """
    global _exit_stack, client

    with _init_lock:
        if _exit_stack is not None:
            if client is not None:
                return
            stack, _exit_stack = _exit_stack, None
            stack.close()
        logging.getLogger("bollard.wire").setLevel(logging.WARNING)
        stack = ExitStack()
        try:
            client = stack.enter_context(DockerClient(*args, **kwargs))
        except BaseException:
            stack.close()
            raise
        _exit_stack = stack


def close_docker_client():
    """Manually closes the context manager.

    Delegates to the shared reset path with strict semantics: a failed
    shared-pod destroy propagates, so shutdown errors stay visible.
    """
    _reset_dead_client(strict=True)
