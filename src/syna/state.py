"""Global Docker client, per-thread container registry, and per-call context.

Besides binding containers to threads, this module carries the protocol state
of the tool call currently being dispatched: the pod path where the call's
full output must be saved (`/tool_outputs/`), and the exit code reported by
`execute_command`. Tools set these cooperatively so `parse.py` can build the
`<result>` blocks without coupling every tool to the result protocol.
"""

import itertools
import logging
import threading
from contextlib import ExitStack

from bollard import Container, DockerClient

from .config import load_config

CONTAINER_IMAGE = "python:3.14-trixie"

DEFAULT_POD_MEMORY_MB = 8192
DEFAULT_POD_PIDS_LIMIT = 512

_local = threading.local()
_call_context = threading.local()
_call_counter = itertools.count(1)
_exit_stack: ExitStack | None = None
client: DockerClient | None = None
_global_container: Container | None = None


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


def create_container() -> Container:
    """Initialize the shared Docker client if needed and run a new container.

    HostConfig Init runs the container's init process (tini equivalent) so
    orphaned background processes started by model commands are reaped.
    """
    global client
    if client is None:
        init_docker_client()
    return client.run_container(
        CONTAINER_IMAGE,
        command="sleep infinity",
        HostConfig=build_pod_host_config(),
    )


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
    container.stop()
    container.remove(force=True)


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


def init_docker_client(*args, **kwargs):
    """Initialize the global Docker client context.

    Also quiets the bollard logger, whose per-request debug output would
    otherwise flood the syna log file.
    """
    global _exit_stack, client

    if _exit_stack is not None:
        raise RuntimeError("Docker client is already initialized.")

    logging.getLogger("bollard").setLevel(logging.WARNING)
    _exit_stack = ExitStack()
    client = _exit_stack.enter_context(DockerClient(*args, **kwargs))


def close_docker_client():
    """Manually closes the context manager."""
    global _exit_stack, client, _global_container

    if _global_container is not None:
        _global_container.stop()
        _global_container.remove(force=True)
        _global_container = None

    if _exit_stack is not None:
        stack, _exit_stack = _exit_stack, None
        client = None
        stack.close()
