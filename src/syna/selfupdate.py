"""Launch-time self-update for globally installed syna-agent copies.

When Syna starts from a pip, pipx, or uv tool installation (not from a
repository checkout), every entry point checks PyPI for a newer release.
A newer release spawns a detached updater and the launching process
exits immediately: Windows locks the entry-point executable and native
modules of a running process, so upgrading in place fails there, and
exiting first is the one flow that works identically everywhere. The
updater waits for the parent to disappear, runs the installer's own
upgrade command (pipx and uv tool environments must never be mutated
with raw pip: their metadata would desync, and uv tool environments do
not even ship pip), verifies the installed version actually advanced
(advancement, not equality with the checked version: a release
published mid-update is still a success), and relaunches the original
command. Any failure prints the manual command and leaves the old
installation untouched.

Ephemeral `uvx` / `pipx run` executions live in a cache no upgrade
command manages; they get a one-line hint and launch normally.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path

import httpx

from .config import get_logger, load_auto_update_enabled
from .paths import get_project_root

PYPI_PACKAGE = "syna-agent"
PYPI_JSON_URL = f"https://pypi.org/pypi/{PYPI_PACKAGE}/json"
CHECK_TIMEOUT_S = 4.0
UPGRADE_TIMEOUT_S = 180
PARENT_WAIT_TIMEOUT_S = 30.0
NO_UPDATE_ENV = "SYNA_NO_AUTO_UPDATE"
EXECUTABLE_SUFFIXES = (".exe", ".bat", ".cmd")


def _installed_version() -> str | None:
    """The installed syna-agent version, or None outside a real install."""
    try:
        return metadata.version(PYPI_PACKAGE)
    except metadata.PackageNotFoundError:
        return None


def _version_key(version: str) -> tuple:
    """Dotted-numeric sort key; a bare release outranks its prereleases."""
    key = []
    for piece in version.strip().split("."):
        match = re.match(r"^(\d+)(.*)$", piece)
        if match:
            number, suffix = match.group(1), match.group(2)
            key.append((1, int(number), suffix == "", suffix))
        else:
            key.append((0, 0, False, piece))
    return tuple(key)


def _is_stable_release(version: str) -> bool:
    """Only dotted-numeric releases are auto-update targets."""
    return bool(re.fullmatch(r"\d+(\.\d+)*", version.strip()))


def _is_newer(candidate: str, installed: str) -> bool:
    """Whether the candidate strictly outranks the installed version."""
    return _version_key(candidate) > _version_key(installed)


def _latest_version() -> str | None:
    """The newest stable version on PyPI, or None on any failure.

    The check runs on the launch path, so it fails open and fast: a
    slow or unreachable PyPI must never hold the user's launch hostage.
    """
    try:
        response = httpx.get(
            PYPI_JSON_URL,
            timeout=httpx.Timeout(CHECK_TIMEOUT_S, connect=2.0),
            follow_redirects=True,
        )
        response.raise_for_status()
        version = response.json().get("info", {}).get("version")
    except Exception as exc:
        get_logger().debug("PyPI update check failed: %s", exc)
        return None
    if not isinstance(version, str) or not _is_stable_release(version):
        return None
    return version


def _venv_prefix() -> Path:
    """The environment prefix, isolated so tests can point it elsewhere."""
    return Path(sys.prefix)


def _skip_reason() -> str | None:
    """Why the update check must not run, or None when it may."""
    if os.environ.get(NO_UPDATE_ENV):
        return f"{NO_UPDATE_ENV} is set"
    if Path(get_project_root() / "pyproject.toml").is_file():
        return "running from a repository checkout"
    try:
        if not load_auto_update_enabled():
            return "auto_update is disabled in config.json"
    except Exception:
        get_logger().exception(
            "Could not read the auto-update setting; skipping the check"
        )
        return "config unreadable"
    return None


def _ephemeral_note(prefix: Path) -> str | None:
    """The ephemeral-runner name for a cache-resident prefix, else None."""
    parts = [part.lower() for part in prefix.parts]
    if "uv" in parts and any(part.startswith("environments") for part in parts):
        return "uvx"
    if "pipx" in parts and "cache" in parts:
        return "pipx run"
    return None


def _pip_available() -> bool:
    """Whether this environment can run pip (uv tool venvs ship without it)."""
    return importlib.util.find_spec("pip") is not None


def _upgrade_command() -> list[str] | None:
    """The installer-native upgrade command for this environment.

    pipx and uv tool installs are detected by the receipt each manager
    writes into the environment; their own upgrade commands replay the
    recorded install options. A plain venv falls back to its pip, or to
    uv pip when the venv has no pip of its own.
    """
    prefix = _venv_prefix()
    if (prefix / "uv-receipt.toml").is_file():
        uv = shutil.which("uv")
        return [uv, "tool", "upgrade", PYPI_PACKAGE] if uv else None
    if (prefix / "pipx_metadata.json").is_file():
        pipx = shutil.which("pipx")
        if pipx:
            return [pipx, "upgrade", PYPI_PACKAGE]
    if _pip_available():
        return [sys.executable, "-m", "pip", "install", "--upgrade", PYPI_PACKAGE]
    uv = shutil.which("uv")
    if uv:
        return [
            uv,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--upgrade",
            PYPI_PACKAGE,
        ]
    return None


def relaunch_argv(entry_point: str, argv: list[str] | None = None) -> list[str]:
    """Build the argv that re-executes the given entry point.

    Preference order: the current entry point (an entry-point executable
    is re-executed directly, a script through the current interpreter),
    the globally installed shim (any PATH entry outside the active
    virtualenv), `uv run` for checkouts without a resolvable entry
    point, and finally the current interpreter with the original
    arguments.
    """
    argv = list(sys.argv if argv is None else argv)
    argv0 = Path(argv[0])
    candidate = argv0 if argv0.is_file() else argv0.with_suffix(".exe")
    if candidate.is_file():
        if candidate.suffix.lower() in EXECUTABLE_SUFFIXES:
            return [str(candidate), *argv[1:]]
        return [sys.executable, str(candidate), *argv[1:]]
    venv_root = Path(sys.prefix).resolve()
    found = shutil.which(entry_point)
    if found and not Path(found).resolve().is_relative_to(venv_root):
        return [found, *argv[1:]]
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", entry_point, *argv[1:]]
    return [sys.executable, str(argv0), *argv[1:]]


def _spawn_updater(payload: dict) -> bool:
    """Start the detached updater process; False when it cannot start.

    The updater is removed from the terminal's interrupt delivery (its
    own session on POSIX, its own process group on Windows): a Ctrl+C
    meant for the exiting launcher must never reach it mid-upgrade,
    where an interrupted install can leave the environment broken.
    """
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [sys.executable, "-m", "syna.selfupdate", json.dumps(payload)], **kwargs
        )
        return True
    except OSError as exc:
        get_logger().warning("Could not start the updater: %s", exc)
        return False


def maybe_selfupdate() -> bool:
    """Check PyPI and self-update when a newer stable release exists.

    Returns True only when an updater was spawned: the caller must then
    exit immediately, before touching the container engine or the UI.
    Every other outcome (gated, up to date, check failed, no installer)
    returns False and the launch proceeds normally.
    """
    try:
        skip = _skip_reason()
        if skip:
            get_logger().debug("Self-update skipped: %s", skip)
            return False
        installed = _installed_version()
        if not installed:
            return False
        latest = _latest_version()
        if not latest or not _is_newer(latest, installed):
            return False
        runner = _ephemeral_note(_venv_prefix())
        if runner:
            print(
                f"\n  + Syna {latest} is available (this {runner} run uses a"
                " cached copy that refreshes on its own schedule).\n",
                flush=True,
            )
            return False
        command = _upgrade_command()
        if command is None:
            print(
                f"\n  ! Syna {latest} is available, but no supported installer"
                f" was found.\n    Update manually with: pip install --upgrade"
                f" {PYPI_PACKAGE}\n",
                flush=True,
            )
            return False
        payload = {
            "pid": os.getpid(),
            "command": command,
            "argv": relaunch_argv(Path(sys.argv[0]).stem),
            "previous": installed,
        }
        if not _spawn_updater(payload):
            print(
                f"\n  ! Could not start the updater. Update manually with:"
                f" {' '.join(command)}\n",
                flush=True,
            )
            return False
        print(
            f"\n  + Updating Syna {installed} -> {latest};"
            " it will relaunch automatically.\n",
            flush=True,
        )
        return True
    except Exception:
        get_logger().exception(
            "Self-update check failed; continuing with the installed version"
        )
        return False


def _pid_alive(pid: int) -> bool:
    """Whether a process with this PID exists (best-effort, cross-platform)."""
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_parent(pid: int, timeout_s: float = PARENT_WAIT_TIMEOUT_S) -> None:
    """Block until the launching process exits.

    The upgrade must never touch files a running Syna still holds open:
    Windows refuses to replace a live process's executables and native
    modules. A timed-out wait proceeds anyway: the upgrade command then
    fails on its own terms and the updater reports it.
    """
    deadline = time.monotonic() + timeout_s
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)


def _relaunch(argv: list[str]) -> None:
    """Hand the terminal to a fresh Syna process running the update."""
    if sys.platform == "win32":
        sys.exit(subprocess.call(argv))
    os.execv(argv[0], argv)


def _main(argv: list[str]) -> int:
    """Updater entry point: wait for the parent, upgrade, verify, relaunch."""
    try:
        payload = json.loads(argv[1])
        pid = int(payload["pid"])
        command = payload["command"]
        previous = payload["previous"]
        relaunch = payload["argv"]
    except (IndexError, ValueError, KeyError, TypeError):
        print("  ! Updater called without a valid payload.", flush=True)
        return 2
    _wait_for_parent(pid)
    print(f"  + Running: {' '.join(command)}", flush=True)
    try:
        completed = subprocess.run(command, check=False, timeout=UPGRADE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  ! The update could not run: {exc}", flush=True)
        print(
            f"    Update manually with: pip install --upgrade {PYPI_PACKAGE}",
            flush=True,
        )
        return 1
    if completed.returncode != 0:
        print(
            f"  ! The update command failed (exit {completed.returncode}).", flush=True
        )
        print(
            f"    Update manually with: pip install --upgrade {PYPI_PACKAGE}",
            flush=True,
        )
        return 1
    installed = _installed_version()
    if not installed or not _is_newer(installed, previous):
        print(
            f"  ! The update did not land (still"
            f" {installed or 'unknown'}); not relaunching.",
            flush=True,
        )
        return 1
    print(f"  + Updated to {installed}; relaunching.\n", flush=True)
    _relaunch(relaunch)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
