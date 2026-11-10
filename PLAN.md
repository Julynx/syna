# Rewind feature — implementation plan

Decisions locked in via Q&A (2026-11-10). Anything marked OPEN was not asked
and must be verified during implementation.

## Overview

A rewind button on user messages lets the user revert the conversation to just
before a chosen message, optionally also reverting the pod's workspace tree to
the snapshot taken before that message was sent. Snapshots are a shadow git
tree over the pod's WORKDIR, stored inside the pod.

## Pod side (new module: `src/syna/capabilities/rewind.py`)

### Git availability

- `pod_init` step installs git: `apt-get install -y git`.
- The image (`python:3.14-trixie`) ships without git.
- If the install fails, rewind is unavailable for that pod's lifetime; the UI
  hides the button (see UI section). A capability flag must reach the server
  (per-session availability probe — verify where pod-init step results surface).

### Shadow store

- Location: `/var/lib/syna/snap.git` (in-pod, ephemeral by design).
- One-time init per pod: `GIT_DIR=/var/lib/syna/snap.git GIT_WORK_TREE=<WORKDIR> git init -q`.
- WORKDIR is captured once at pod init (commands could cd elsewhere; the
  snapshot root is fixed, not per-command). Verify the container's actual
  WORKDIR resolution at init time.

### Snapshot (taken before each user message is sent)

1. `git status --porcelain` — if empty and a previous hash exists, reuse it
   (near-zero cost on idle trees; avoids `add -A` on huge trees every send).
2. Otherwise `git add -A && git write-tree`; the printed tree hash is stored.
3. Nested `.git` directories are protected: excluded from the snapshot
   (git records a nested repo as a gitlink, not contents) and never deleted on
   rollback (a plain `clean -fdx` would destroy the user's own repo history).

### Rollback ("Messages & changes")

```
GIT_DIR=/var/lib/syna/snap.git GIT_WORK_TREE=<WORKDIR>
git read-tree <tree> && git checkout-index -a -f && git clean -fd
```

- `-fd`, not `-fdx`: `.gitignore` is respected — `.venv`, `node_modules`, `.env`
  survive. Stale ignored files may linger after rollback (accepted trade-off).
- Failure ordering — files first: workspace rollback runs; only on success is
  history truncated. On failure nothing is deleted and the UI toasts the error.
- The pod keeps running (no restart). `/tool_outputs`, `/user_messages`,
  `/home/user/uploads`, installed packages, `/tmp` are NOT touched — the revert
  scope is exactly the WORKDIR tree. Model-facing docs should state this scope.

## Session / server side

### Hash storage

- Tree hashes live in a parallel metadata dict keyed by a per-message id —
  never in message content (no prompt bloat, no cache-prefix pollution, the
  model never sees it; survives compaction/elision rewrites).
- Verify whether messages already carry stable ids; add one if not.

### Snapshot points

- Before sending each user message (the turn start).
- One extra snapshot at session init: the pristine WORKDIR, stored on the
  first user message — rewind-to-first = undo everything the agent did.

### Rewind semantics (both modal options)

- Delete all messages after the chosen one, delete the chosen message, paste
  its text into the input field, overwriting any existing draft.
- File-upload messages are deleted like any other message; the uploaded files
  stay in `/home/user/uploads` (outside the reverted tree).
- Purge pending/failed-message queues and stale injection state so no notice
  lands after the rewind.

### "Messages & changes" extras

- Busy sessions: the button stays visible while busy. Clicking it emits the
  same cancel event as the web stop button (hands control back to the user)
  and queues the rewind to run when the stop takes effect.
- The inbox queue is purged (queued "Send now" items have no place in the
  rewound timeline).

### Multi-tab

- New event type (e.g. `history_rewind`) broadcast to all subscribers;
  other tabs rebuild their transcript from the surviving history.

## UI (`webui/js/chat.js`, `chat.html`)

- New hover button on user messages, same styling as copy/download; icon:
  "undo".
- Hidden on messages whose snapshot is gone (pod recreated, janitor reap,
  server restart); visible otherwise, including while busy.
- Clicking opens the modal "Choose what to revert": three vertically stacked
  wide buttons with centered text — "Messages only", "Messages & changes",
  "Cancel".
- "Messages only": history truncation + input refill, no pod contact.
- "Messages & changes": cancel-if-busy, workspace rollback first (files
  first), then history truncation on success.

## Out of scope (survive a revert by design)

Installed packages, `/tmp`, `/tool_outputs`, `/user_messages`,
`/home/user/uploads`. Snapshots are in-pod and die with the pod — rewind is
unavailable for messages older than the current pod's lifetime.

## Test plan

Offline suites (fake pod recording exec):
- git command composition (snapshot, unchanged-tree skip, rollback, nested-repo
  protection)
- metadata storage keyed by message id
- truncation + queue purge + injection-state clearing
- failure ordering: rollback fails -> nothing deleted, error surfaced
- `history_rewind` event broadcast shape

Live (on request): real pod — snapshot, modify WORKDIR, rollback, verify file
states; a short real task with a rewind through AgentSession.

## OPEN items to verify during implementation

1. Actual WORKDIR of the container and how to capture it at pod init.
2. Whether messages already have stable ids (for the metadata key).
3. Where pod-init step results surface, so "git available" can gate the UI.
4. Interaction of history truncation with `_pending_failed_messages` and the
   archive nonce (rewind must clear both safely).

## Round-2 fixes (adversarial review, 2026-11-10)

The first implementation was reviewed red-team style; seven findings follow
with their fixes. All are incorporated in this revision.

### 1. Background work races "Messages & changes" (most important)

The busy path cancels the turn, but detached pod processes and subagents
outlive turns by design: a rollback would revert files underneath running
work, delete its output artifacts out from under it, and a completion notice
arriving after the rewind would wake the model about work whose spawn the
rewound history no longer shows.

Fix: for "Messages & changes", stop all background work before the rollback
- mark each record `outcome_collected` FIRST (the exactly-once machinery
  then suppresses the model-facing notice; the rewound timeline never knew
  about the work, so silence is the honest state), kill processes via
  `kill_process` (blocks until exit, bounded by the SIGKILL follow-up),
  stop subagents via `kill_subagent` (graceful; ends at its next pause),
  wait up to ~10s for subagents to settle, then roll back. A synthetic
  `background_done` event per killed record keeps the UI panel honest
  immediately (the suppressed notice would have carried it). If the
  rollback then fails, the toast says background work was stopped.
- "Messages only" keeps work running: the workspace is untouched, so
  notices stay truthful. Documented.
- Known wart, documented: a killed subagent's runner finalize re-copies
  its artifacts into `/workspace/subagents/{id}/` after the rollback, so
  orphan artifact dirs can reappear; they are untracked files, not code.

### 2. A rewind click can be silently swallowed by another rewind

`busy` clears in `_run_turn`'s finally, before `_run_pending_rewind` runs;
a click in that window enqueues a rewind item that the parked rewind's
`inbox.clear()` then deletes without a trace.

Fix: the rewind purge keeps `kind == "rewind"` items. A second rewind then
executes after the first and either rewinds further or fails with an
honest toast.

### 3. `rewind_state` payload grows with history

Every emission carried every point id and is persistent; at 300+ messages
that is ~12 KB per event and a replay that ships hundreds of them.

Fix: delta events. `rewind_state` now carries `added` and `removed` id
arrays plus availability; replay applies them in order and converges.
Turn start emits one added id; purges emit the removed ids.

### 4. The pristine snapshot was not eager

The plan stored a pristine snapshot on the first user message; the
implementation derived it from the first send, so a failed first snapshot
lost rewind-to-first for the pod's lifetime.

Fix: `create_container` snapshots the workspace right after the shared
copy when the rewind step went active, seeding the last-tree cache. The
first send then reuses the pristine tree (and costs one status call).

### 5. `rewindTranscript` steals focus on every tab

Fix: only focus the input when not replaying history. The cross-tab draft
overwrite stays (locked decision, documented).

### 6. A "Messages only" rewind could trigger a container start

Fix: the worker starts the container for a rewind item only when the
mode needs the pod ("all").

### 7. Test gaps

Fix: extract the WebSocket dispatch decision into `_route_rewind`
(testable offline) and add tests: busy/idle routing, a second click
overwriting a parked rewind, a cancelled turn still recording its point,
rewind items surviving the purge, delta shapes, and the background-work
stop (fake registry).

## Round-3 changes (user decisions, 2026-11-10)

1. **Git-tracked projects revert file-level.** The old behavior (nested
   repos entirely untouched) was baffling. Now a snapshot captures each
   nested repository's tracked files through a temporary index and a
   rollback restores them per repo: the `.git` directory (history, HEAD,
   index) and the repo's gitignored files are respected, everything
   else returns to the snapshot state and shows as a dirty working tree.
   A post-snapshot clone is not in the snapshot's nested map and
   survives untouched.
2. **Rewind past compaction.** Compaction marks a point `compacted`
   instead of purging it (the UI still shows the message); rewinding to
   it cuts the history back to the verbatim beginning (system prompt +
   first real user message — the summary covering the point cannot be
   split) and its tree still restores exactly.
3. **The modal's second button reads "Messages & workspace".**
4. **Checkpoints are opt-in.** New `checkpoints_enabled` setting
   (default false) plus a Checkpoints section in the settings GUI with
   an "Enable checkpoints" dropdown: "No (Default)" / "Yes (checkpoints
   carry some delay)". When off, no snapshots are taken and availability
   reports false, so no buttons appear.
5. **The model is told.** After a successful rewind the session appends
   a harness notice: with a revert, history and workspace agree (with
   the gitignored/nested/outside-workspace caveats); without one, the
   workspace may contain files from the undone work and the model is
   told to inspect before relying on it.

