"""Workspace snapshots backing the web UI's rewind feature.

Before each user message is sent, the session's worker snapshots the
pod's workspace tree into a shadow git store prepared by the pod's init
(`pod_init`'s rewind step: git plus a git store at `REWIND_SNAP_DIR`
whose work tree is `REWIND_SNAP_ROOT`, driven explicitly per call). A
"Messages & workspace" rewind destructively restores a stored tree; a
"Messages only" rewind never touches the pod.

The store is in-pod and ephemeral by design: a recreated pod has no
snapshots, and the UI hides the rewind buttons for the messages of the
lost ones. Rollback restores exactly the workspace tree - installed
packages, /tmp, /tool_outputs, /user_messages, and /home/user/uploads
live outside `REWIND_SNAP_ROOT` and survive.

Snapshots reuse the previous trees when nothing changed, so an idle
workspace costs one cheap status call per message instead of a full
`git add -A` over it. The cache is seeded at pod creation
(`state.create_container` snapshots the pristine workspace right after
the shared copy), so the first user message's rewind point is the
pristine tree at the cost of a status call.

Git-tracked projects inside the workspace get file-level treatment:
a snapshot captures each nested repository's tracked files through a
temporary index (the repo's own index and HEAD are never touched), and
a rollback restores them the same way. The nested `.git` directory and
everything the repo gitignores are respected - a restore leaves the
repository's working tree dirty relative to its own HEAD, which is the
honest consequence of reverting changes inside a tracked project.
"""

import re
import shlex
import threading
import time

from ..config import get_logger
from ..pod_io import pod_exec
from ..pod_init import REWIND_SNAP_DIR, REWIND_SNAP_ROOT, init_results_for

TREE_HASH_PATTERN = re.compile(r"[0-9a-f]{40}")
SNAP_ERROR_MARK = "SNAP_ERROR"
ROLLBACK_OK_MARK = "ROLLBACK_OK"
# Scratch index file for nested-repository snapshot and restore
# operations; the real index of the nested repository is never touched.
NESTED_INDEX_PATH = "/var/lib/syna/snap-rw.idx"

_last_trees: dict[int, dict] = {}
_last_trees_lock = threading.Lock()


def available_for(container) -> bool:
    """Whether this pod's init installed git and prepared the store."""
    return bool(init_results_for(container).get("rewind", False))


def forget_pod(container) -> None:
    """Drop the last-snapshot cache of a pod (destruction path)."""
    with _last_trees_lock:
        _last_trees.pop(id(container), None)


def _outer_env(last: dict | None) -> str:
    exports = (
        f"GIT_DIR={shlex.quote(REWIND_SNAP_DIR)}"
        f" GIT_WORK_TREE={shlex.quote(REWIND_SNAP_ROOT)}"
        " GIT_TERMINAL_PROMPT=0"
    )
    if last is not None:
        if last.get("tree"):
            exports += f" SYNA_LAST_TREE={shlex.quote(last['tree'])}"
        nested = "\n".join(
            f"{path}\t{tree}" for path, tree in last.get("nested", {}).items()
        )
        if nested:
            exports += f" SYNA_LAST_NESTED={shlex.quote(nested)}"
    return "export " + exports


def _parse_snapshot_output(output: str) -> dict | None:
    """Parse one snapshot exec's output into the snapshot mapping."""
    if SNAP_ERROR_MARK in output or not output.strip():
        return None
    snapshot: dict = {"tree": None, "nested": {}}

    def tree_of(value: str) -> str | None:
        value = value.split(" ", 1)[1] if value.startswith("reused ") else value
        return value if TREE_HASH_PATTERN.fullmatch(value) else None

    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("outer "):
            tree = tree_of(stripped[len("outer ") :])
            if tree is None:
                return None
            snapshot["tree"] = tree
        elif stripped.startswith("nested "):
            rest = stripped[len("nested ") :]
            if rest.startswith("reused "):
                rest = rest[len("reused ") :]
            path, separator, tree = rest.partition("\t")
            if not separator or not TREE_HASH_PATTERN.fullmatch(tree):
                return None
            snapshot["nested"][path] = tree
        else:
            return None
    if snapshot["tree"] is None:
        return None
    return snapshot


def snapshot(container) -> dict | None:
    """Snapshot the workspace tree; reuse the last snapshot when unchanged.

    Returns ``{"tree": <outer tree>, "nested": {path: tree}}`` to store
    as the rewind point of the message about to be sent, or None when
    the pod cannot snapshot (git missing, store broken, pod dead) - the
    message then simply has no rewind point.

    The outer reuse path only fires when the workspace AND every nested
    repository are unchanged: a nested repo's internal edits do not
    dirty the outer gitlink, so they are checked explicitly. Each nested
    repository's tracked files are staged through a temporary index, so
    the repository's own index and HEAD stay untouched.
    """
    with _last_trees_lock:
        last = _last_trees.get(id(container))
    script = "\n".join(
        [
            _outer_env(last),
            "dirty=0",
            'repos=""',
            "while IFS= read -r -d '' g; do",
            '  repo="${g%/.git}";',
            '  repos="$repos$repo\\n";',
            '  [ -n "$(env -u GIT_DIR -u GIT_WORK_TREE git -C "$repo"'
            ' status --porcelain 2>/dev/null)" ] && dirty=1;',
            "done < <(find "
            + shlex.quote(REWIND_SNAP_ROOT)
            + " \\( -name .git -type d -o -name .git -type f \\) -print0 2>/dev/null)",
            "if ! git status --porcelain >/dev/null 2>&1; then",
            f"  echo {SNAP_ERROR_MARK}; exit 0; fi",
            'if [ -n "${SYNA_LAST_TREE:-}" ] && [ "$dirty" = 0 ] &&'
            ' [ -z "$(git status --porcelain)" ]; then',
            '  echo "outer reused ${SYNA_LAST_TREE}";',
            '  [ -n "${SYNA_LAST_NESTED:-}" ] && printf "%s\\n" "${SYNA_LAST_NESTED}"'
            ' | while IFS= read -r line; do [ -n "$line" ]'
            ' && echo "nested reused $line"; done;',
            "  exit 0; fi",
            "git add -A 2>/dev/null || { echo " + SNAP_ERROR_MARK + "; exit 0; }",
            "outer=$(git write-tree 2>/dev/null) ||"
            f" {{ echo {SNAP_ERROR_MARK}; exit 0; }}",
            'echo "outer $outer"',
            'printf "%b" "$repos" | while IFS= read -r repo; do',
            '  [ -n "$repo" ] || continue;',
            "  env -u GIT_DIR -u GIT_WORK_TREE"
            f" GIT_INDEX_FILE={shlex.quote(NESTED_INDEX_PATH)}"
            ' git -C "$repo" add -A 2>/dev/null'
            f" || {{ echo {SNAP_ERROR_MARK}; exit 0; }}",
            "  ntree=$(env -u GIT_DIR -u GIT_WORK_TREE GIT_INDEX_FILE="
            + shlex.quote(NESTED_INDEX_PATH)
            + ' git -C "$repo" write-tree 2>/dev/null)'
            f" || {{ echo {SNAP_ERROR_MARK}; exit 0; }}",
            '  echo "nested $repo"$\'\\t\'"$ntree";',
            "done",
        ]
    )
    try:
        output = pod_exec(container, ["bash", "-c", script]).strip()
    except Exception:
        get_logger().warning("Workspace snapshot failed", exc_info=True)
        return None
    snapshot = _parse_snapshot_output(output)
    if snapshot is None:
        get_logger().warning("Workspace snapshot returned no tree hash: %r", output)
        return None
    with _last_trees_lock:
        _last_trees[id(container)] = snapshot
    return snapshot


def rollback(container, tree_hash: str, nested: dict | None = None) -> bool:
    """Destructively restore the workspace tree to a stored snapshot.

    The outer restore is `read-tree` + `checkout-index -a -f` +
    `clean -fd` against the shadow store: every file the snapshot
    tracked is rewritten, everything that appeared after it is deleted,
    and `.gitignore` is respected so ignored files (.venv,
    node_modules, .env) survive and may linger stale. Directories
    containing a nested `.git` are never removed by the clean.

    Each nested repository is then restored from its own snapshotted
    tree through a temporary index: tracked files return to their
    snapshot state, the repository's `.git` (history, HEAD, index) and
    its gitignored files are untouched, and untracked non-ignored files
    added after the snapshot are removed. Returns whether the pod
    confirmed the restore; a False answer leaves the workspace in an
    unknown state and the caller must not truncate history (files
    first, then messages).
    """
    script = "\n".join(
        [
            _outer_env(None),
            f"git read-tree {shlex.quote(tree_hash)} 2>/dev/null ||"
            " { echo ROLLBACK_ERROR; exit 0; }",
            "git checkout-index -a -f 2>/dev/null || { echo ROLLBACK_ERROR; exit 0; }",
            "git clean -fd >/dev/null 2>&1 || { echo ROLLBACK_ERROR; exit 0; }",
            *(
                line
                for path, tree in (nested or {}).items()
                for line in (
                    f"if [ -d {shlex.quote(path)} ] && env -u GIT_DIR"
                    " -u GIT_WORK_TREE git -C "
                    + shlex.quote(path)
                    + " rev-parse --git-dir >/dev/null 2>&1; then",
                    "  env -u GIT_DIR -u GIT_WORK_TREE"
                    f" GIT_INDEX_FILE={shlex.quote(NESTED_INDEX_PATH)}"
                    f" git -C {shlex.quote(path)} read-tree {shlex.quote(tree)}"
                    " 2>/dev/null || { echo ROLLBACK_ERROR; exit 0; }",
                    "  env -u GIT_DIR -u GIT_WORK_TREE"
                    f" GIT_INDEX_FILE={shlex.quote(NESTED_INDEX_PATH)}"
                    f" git -C {shlex.quote(path)} checkout-index -a -f"
                    " 2>/dev/null || { echo ROLLBACK_ERROR; exit 0; }",
                    "  env -u GIT_DIR -u GIT_WORK_TREE"
                    f" GIT_INDEX_FILE={shlex.quote(NESTED_INDEX_PATH)}"
                    f" git -C {shlex.quote(path)} clean -fd >/dev/null 2>&1"
                    " || { echo ROLLBACK_ERROR; exit 0; }",
                    "fi",
                )
            ),
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
    with _last_trees_lock:
        _last_trees[id(container)] = {
            "tree": tree_hash,
            "nested": dict(nested or {}),
        }
    return True
