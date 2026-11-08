"""Serialized pod I/O for every container API call.

Bollard's client multiplexes all container calls over one persistent
HTTP connection, so concurrent callers corrupt the connection state
(observed live: `CannotSendRequest` mid-request when the process
watcher's poll raced a tool call's exec). Every container API call in
the codebase must go through the helpers here; a direct call anywhere
else reintroduces the race.

The lock is a leaf: code holding it never waits on another lock, so no
lock ordering can invert. The registry lock of the background-work
module is always taken before this one, never after.
"""

import threading

_pod_call_lock = threading.Lock()


def pod_call(operation):
    """Run one container operation under the client-wide lock.

    For container API calls that have no dedicated wrapper here (stop,
    remove, one-off reads). The operation must be a single call: keep
    multi-step sequences outside, each step wrapped separately.
    """
    with _pod_call_lock:
        return operation()


def pod_exec(container, argv: list[str]) -> str:
    """Run one pod exec under the client-wide connection lock."""
    return pod_call(lambda: container.exec(argv))


def pod_copy_to(container, host_path: str, pod_dir: str) -> None:
    """Copy one host file into the pod under the connection lock."""
    pod_call(lambda: container.copy_to(host_path, pod_dir))


def pod_copy_from(container, pod_path: str, host_dir: str) -> None:
    """Copy one pod path out to the host under the connection lock."""
    pod_call(lambda: container.copy_from(pod_path, host_dir))
