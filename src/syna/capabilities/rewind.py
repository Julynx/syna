"""Workspace snapshots backing the web UI's rewind feature.

Before each user message is sent, the session's worker snapshots the
pod's workspace tree into a shadow git store prepared by the pod's init
(`pod_init`'s rewind step: git plus a git store at `REWIND_SNAP_DIR`
whose work tree is `REWIND_SNAP_ROOT`, driven explicitly per call). A
"Messages & changes" rewind
destructively restores a stored tree; a "Messages only" rewind never
touches the pod.

The store is in-pod and ephemeral by design: a recreated pod has no
snapshots, and the UI hides the rewind buttons for the messages of the
lost ones. Rollback restores exactly the workspace tree - installed
packages, /tmp, /tool_outputs, /user_messages, and /home/user/uploads
live outside `REWIND_SNAP_ROOT` and survive.

Snapshots reuse the previous tree when `git status --porcelain` reports
the workspace unchanged, so an idle workspace costs one cheap status
call per message instead of a full `git add -A` over it. The cache is
seeded at pod creation (`state.create_container` snapshots the pristine
workspace right after the shared copy), so the first user message's
rewind point is the pristine tree at the cost of a status call.

Nested git repositories are protected by git's own semantics: a
snapshot records a nested repo as a gitlink (its contents and history
stay in its own .git directory, untouched on disk), and a rollback's
`git clean -fd` leaves indexed paths alone - a repo that existed at
snapshot time survives; one cloned after the snapshot is removed with
the rest of the post-snapshot changes, which is what rewinding means.
"""

import re
import shlex
import threading

from ..config import get_logger
from ..pod_io import pod_exec
from ..pod_init import REWIND_SNAP_DIR, REWIND_SNAP_ROOT, init_results_for

TREE_HASH_PATTERN = re.compile(r"[0-9a-f]{40}")
SNAP_ERROR_MARK = "SNAP_ERROR"
ROLLBACK_OK_MARK = "ROLLBACK_OK"

_last_tree: dict[int, str] = {}
_last_tree_lock = threading.Lock()


def available_for(container) -> bool:
    """Whether this pod's init installed git and prepared the store."""
    return bool(init_results_for(container).get("rewind", False))


def forget_pod(container) -> None:
    """Drop the last-snapshot cache of a pod (destruction path)."""
    with _last_tree_lock:
        _last_tree.pop(id(container), None)


def _env_prefix(last_tree: str | None) -> str:
    exports = (
        f"GIT_DIR={shlex.quote(REWIND_SNAP_DIR)}"
        f" GIT_WORK_TREE={shlex.quote(REWIND_SNAP_ROOT)}"
        " GIT_TERMINAL_PROMPT=0"
    )
    if last_tree is not None:
        exports += f" SYNA_LAST_TREE={shlex.quote(last_tree)}"
    return "export " + exports


def snapshot(container) -> str | None:
    """Snapshot the workspace tree; reuse the last tree when unchanged.

    Returns the tree hash to store as the rewind point of the message
    about to be sent, or None when the pod cannot snapshot (git missing,
    store broken, pod dead) - the message then simply has no rewind
    point. The reuse path makes an unchanged workspace cost one status
    call; the cache is seeded at pod creation with the pristine
    workspace, so the first send usually reuses instead of writing.
    """
    key = id(container)
    with _last_tree_lock:
        last = _last_tree.get(key)
    script = "\n".join(
        [
            _env_prefix(last),
            "if ! git status --porcelain >/dev/null 2>&1; then",
            f"  echo {SNAP_ERROR_MARK}; exit 0; fi",
            'if [ -n "${SYNA_LAST_TREE:-}" ] &&'
            ' [ -z "$(git status --porcelain)" ]; then',
            '  echo "reused ${SYNA_LAST_TREE}"; exit 0; fi',
            "git add -A 2>/dev/null || { echo " + SNAP_ERROR_MARK + "; exit 0; }",
            "hash=$(git write-tree 2>/dev/null) ||"
            f" {{ echo {SNAP_ERROR_MARK}; exit 0; }}",
            'echo "$hash"',
        ]
    )
    try:
        output = pod_exec(container, ["bash", "-c", script]).strip()
    except Exception:
        get_logger().warning("Workspace snapshot failed", exc_info=True)
        return None
    if output.startswith("reused "):
        tree = output.split(None, 1)[1].strip()
    elif output and SNAP_ERROR_MARK not in output:
        tree = output.splitlines()[-1].strip()
    else:
        return None
    if not TREE_HASH_PATTERN.fullmatch(tree):
        get_logger().warning("Workspace snapshot returned no tree hash: %r", output)
        return None
    with _last_tree_lock:
        _last_tree[key] = tree
    return tree


def rollback(container, tree_hash: str) -> bool:
    """Destructively restore the workspace tree to a stored snapshot.

    `read-tree` resets the index to the snapshot, `checkout-index -a -f`
    rewrites every file the snapshot tracked, and `clean -fd` deletes
    what appeared after it - respecting .gitignore, so ignored files
    (.venv, node_modules, .env) survive and may linger stale after the
    rollback. Returns whether the pod confirmed the restore; a False
    answer leaves the workspace in an unknown state and the caller must
    not truncate history (files first, then messages).
    """
    script = "\n".join(
        [
            _env_prefix(None),
            f"git read-tree {shlex.quote(tree_hash)} 2>/dev/null ||"
            " { echo ROLLBACK_ERROR; exit 0; }",
            "git checkout-index -a -f 2>/dev/null || { echo ROLLBACK_ERROR; exit 0; }",
            "git clean -fd >/dev/null 2>&1 || { echo ROLLBACK_ERROR; exit 0; }",
            f"echo {ROLLBACK_OK_MARK}",
        ]
    )
    try:
        output = pod_exec(container, ["bash", "-c", script]).strip()
    except Exception:
        get_logger().warning("Workspace rollback failed", exc_info=True)
        return False
    if ROLLBACK_OK_MARK not in output:
        get_logger().warning("Workspace rollback did not confirm: %r", output[-300:])
        return False
    with _last_tree_lock:
        _last_tree[id(container)] = tree_hash
    return True
