#!/bin/bash
# Pod rewind bootstrap, run once on container creation.
#
# Installs git and prepares the shadow snapshot store backing the web
# UI's rewind feature: a bare-ish git store whose work tree is the
# workspace root, so a snapshot is `git add -A && git write-tree` and a
# rollback is `read-tree` + `checkout-index` + `clean -fd` against it.
# Fails closed for the feature only: any failure here leaves the pod
# fully usable, merely without rewind (the UI hides the button).

set -u

SNAP_DIR="${SNAP_DIR:?SNAP_DIR is required}"
SNAP_ROOT="${SNAP_ROOT:?SNAP_ROOT is required}"

say() { echo "pod-rewind: $*"; }
fail() { say "FAILED: $*"; exit 1; }

[ "$(id -u)" = "0" ] || fail "must run as root"

export DEBIAN_FRONTEND=noninteractive
if ! command -v git >/dev/null 2>&1; then
  apt-get update -qq >/dev/null 2>&1 || fail "apt-get update"
  apt-get install -y -qq git >/dev/null 2>&1 || fail "apt-get install git"
fi
command -v git >/dev/null 2>&1 || fail "git binary not found"

[ -d "$SNAP_ROOT" ] || mkdir -p "$SNAP_ROOT" || fail "mkdir $SNAP_ROOT"

if [ ! -d "$SNAP_DIR" ]; then
  git init -q --bare "$SNAP_DIR" || fail "git init $SNAP_DIR"
  # A bare-configured store refuses work-tree operations even with
  # GIT_WORK_TREE set; clearing the flag keeps the git dir at $SNAP_DIR
  # while letting every call drive the foreign work tree explicitly.
  git --git-dir="$SNAP_DIR" config core.bare false || fail "git config core.bare"
fi

# Tree writes need no commit identity, but a config makes every future
# git invocation deterministic and quiet about it. Every call passes
# GIT_DIR and GIT_WORK_TREE explicitly (the post-receive-hook pattern),
# so a bare store drives a foreign work tree fine.
git --git-dir="$SNAP_DIR" --work-tree="$SNAP_ROOT" config user.email "syna@localhost" ||
  fail "git config user.email"
git --git-dir="$SNAP_DIR" --work-tree="$SNAP_ROOT" config user.name "Syna" ||
  fail "git config user.name"
git --git-dir="$SNAP_DIR" --work-tree="$SNAP_ROOT" config core.autocrlf false ||
  fail "git config core.autocrlf"

say "shadow store ready at $SNAP_DIR (work tree $SNAP_ROOT)"
echo "pod-init: step OK"
