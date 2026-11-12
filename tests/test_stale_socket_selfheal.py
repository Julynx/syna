"""Tests for the Windows stale-engine-socket self-heal.

A reboot with the Podman machine running leaks its zero-byte API
forwarding socket under <temp>/podman; every later forwarder start then
dies binding it and the machine runs without an API endpoint
(containers/podman#29971). The cleanup stands down whenever an engine
pipe is reachable or the forwarder's PIDs cannot be determined; a
forwarder found with no reachable pipe is an orphan, so it is stopped
before its sockets are removed. Also covers the user-facing engine
error, which must lead with the bring-up failure instead of generic
virtualization advice.
"""

import atexit
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import MonkeyPatch, run_suite

from syna import state

_TEMP_DIRS = []
atexit.register(
    lambda: [shutil.rmtree(path, ignore_errors=True) for path in _TEMP_DIRS]
)


def _fresh_temp(monkeypatch: MonkeyPatch) -> Path:
    """A throwaway temp root for this test, cleaned up at process exit."""
    path = Path(tempfile.mkdtemp(prefix="syna_selfheal_"))
    _TEMP_DIRS.append(path)
    monkeypatch.setattr(state.tempfile, "gettempdir", lambda: str(path))
    return path


def _plant_socket(name: str) -> Path:
    socket_dir = _TEMP_DIRS[-1] / "podman"
    socket_dir.mkdir(parents=True, exist_ok=True)
    socket_file = socket_dir / name
    socket_file.write_bytes(b"")
    return socket_file


def _sockets_dir() -> Path:
    return _TEMP_DIRS[-1] / "podman"


def test_leaked_sockets_are_removed(monkeypatch: MonkeyPatch):
    _fresh_temp(monkeypatch)
    monkeypatch.setattr(state, "_pipe_reachable", lambda pipe: False)
    monkeypatch.setattr(state, "_forwarder_pids", lambda: [])
    stale = _plant_socket("podman-machine-default-api.sock")
    other = _plant_socket("podman-machine-default-ssh.sock")
    removed = state._clear_stale_api_sockets()
    assert removed == 2, removed
    assert not stale.exists()
    assert not other.exists()


def test_non_socket_files_are_left_alone(monkeypatch: MonkeyPatch):
    _fresh_temp(monkeypatch)
    monkeypatch.setattr(state, "_pipe_reachable", lambda pipe: False)
    monkeypatch.setattr(state, "_forwarder_pids", lambda: [])
    keeper = _sockets_dir() / "notes.txt"
    keeper.parent.mkdir(parents=True, exist_ok=True)
    keeper.write_text("keep me")
    removed = state._clear_stale_api_sockets()
    assert removed == 0, removed
    assert keeper.exists()


def test_live_pipe_stands_the_cleanup_down(monkeypatch: MonkeyPatch):
    _fresh_temp(monkeypatch)
    monkeypatch.setattr(state, "_pipe_reachable", lambda pipe: True)

    def no_probe():
        raise AssertionError("the forwarder must not be probed when a pipe serves")

    monkeypatch.setattr(state, "_forwarder_pids", no_probe)
    stale = _plant_socket("podman-machine-default-api.sock")
    removed = state._clear_stale_api_sockets()
    assert removed == 0, removed
    assert stale.exists()


def test_zombie_forwarder_is_stopped_and_sockets_cleared(monkeypatch: MonkeyPatch):
    """A forwarder serving no pipe is an orphan: stop it, then clean."""
    _fresh_temp(monkeypatch)
    monkeypatch.setattr(state, "_pipe_reachable", lambda pipe: False)
    monkeypatch.setattr(state, "_forwarder_pids", lambda: [4242])
    stopped = []
    monkeypatch.setattr(
        state, "_stop_zombie_forwarder", lambda pids: stopped.append(list(pids))
    )
    stale = _plant_socket("podman-machine-default-api.sock")
    removed = state._clear_stale_api_sockets()
    assert stopped == [[4242]], stopped
    assert removed == 1, removed
    assert not stale.exists()


def test_unknown_forwarder_state_stands_the_cleanup_down(monkeypatch: MonkeyPatch):
    """PIDs that cannot be determined mean no deletion, ever."""
    _fresh_temp(monkeypatch)
    monkeypatch.setattr(state, "_pipe_reachable", lambda pipe: False)
    monkeypatch.setattr(state, "_forwarder_pids", lambda: None)

    def no_stop(pids):
        raise AssertionError("an unknown forwarder must never be signalled")

    monkeypatch.setattr(state, "_stop_zombie_forwarder", no_stop)
    stale = _plant_socket("podman-machine-default-api.sock")
    removed = state._clear_stale_api_sockets()
    assert removed == 0, removed
    assert stale.exists()


def test_forwarder_probe_fails_safe(monkeypatch: MonkeyPatch):
    """An unknown tasklist outcome reports None, not an empty list."""

    def failing_run(*args, **kwargs):
        raise OSError("tasklist missing")

    monkeypatch.setattr(state.subprocess, "run", failing_run)
    assert state._forwarder_pids() is None


def test_forwarder_probe_parses_pids(monkeypatch: MonkeyPatch):
    stdout = (
        '"win-sshproxy.exe","4242","Console","1","10,000 K"\r\n'
        '"explorer.exe","999","Console","1","50,000 K"\r\n'
    )

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=stdout)

    monkeypatch.setattr(state.subprocess, "run", fake_run)
    assert state._forwarder_pids() == [4242]


def test_zombie_forwarder_kill_uses_taskkill(monkeypatch: MonkeyPatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(state.subprocess, "run", fake_run)
    state._stop_zombie_forwarder([4242, 4243])
    assert calls == [
        ["taskkill", "/F", "/PID", "4242"],
        ["taskkill", "/F", "/PID", "4243"],
    ], calls


def test_zombie_forwarder_kill_survives_failure(monkeypatch: MonkeyPatch):
    def failing_run(command, **kwargs):
        raise OSError("access denied")

    monkeypatch.setattr(state.subprocess, "run", failing_run)
    state._stop_zombie_forwarder([4242])


def test_non_windows_never_cleans(monkeypatch: MonkeyPatch):
    _fresh_temp(monkeypatch)
    monkeypatch.setattr(state, "_IS_WINDOWS", False)
    monkeypatch.setattr(state, "_pipe_reachable", lambda pipe: False)
    monkeypatch.setattr(state, "_forwarder_pids", lambda: [])
    stale = _plant_socket("podman-machine-default-api.sock")
    removed = state._clear_stale_api_sockets()
    assert removed == 0, removed
    assert stale.exists()


def test_missing_socket_dir_is_quiet(monkeypatch: MonkeyPatch):
    _fresh_temp(monkeypatch)
    monkeypatch.setattr(state, "_pipe_reachable", lambda pipe: False)
    monkeypatch.setattr(state, "_forwarder_pids", lambda: [])
    _sockets_dir().mkdir(parents=True, exist_ok=True)
    assert state._clear_stale_api_sockets() == 0


def test_ensure_engine_ready_heals_before_connecting(monkeypatch: MonkeyPatch):
    """The boot-time check heals the sockets before bollard connects."""
    _fresh_temp(monkeypatch)
    monkeypatch.setattr(state, "_pipe_reachable", lambda pipe: False)
    monkeypatch.setattr(state, "_forwarder_pids", lambda: [])
    _plant_socket("podman-machine-default-api.sock")
    order = []

    class FakeClient:
        def __init__(self):
            order.append("connect")
            order.append(
                "stale-sockets-present:"
                + str((_sockets_dir() / "podman-machine-default-api.sock").exists())
            )

        def close(self):
            order.append("closed")

    monkeypatch.setattr(state, "DockerClient", FakeClient)
    state.ensure_engine_ready()
    assert order == [
        "connect",
        "stale-sockets-present:False",
        "closed",
    ], order


def test_init_docker_client_heals_before_connecting(monkeypatch: MonkeyPatch):
    """The server's first-call path heals before the client is built."""
    _fresh_temp(monkeypatch)
    monkeypatch.setattr(state, "_pipe_reachable", lambda pipe: False)
    monkeypatch.setattr(state, "_forwarder_pids", lambda: [])
    _plant_socket("podman-machine-default-api.sock")
    monkeypatch.setattr(state, "_exit_stack", None)
    monkeypatch.setattr(state, "client", None)
    order = []

    class FakeClient:
        def __init__(self):
            order.append("connect")
            order.append(
                "stale-sockets-present:"
                + str((_sockets_dir() / "podman-machine-default-api.sock").exists())
            )

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(state, "DockerClient", FakeClient)
    try:
        state.init_docker_client()
        assert order == [
            "connect",
            "stale-sockets-present:False",
        ], order
    finally:
        if state._exit_stack is not None:
            stack, state._exit_stack = state._exit_stack, None
            stack.close()
            state.client = None


def test_pipe_candidates_come_from_bollard():
    """The self-heal probes the same pipes bollard's discovery does."""
    from bollard.const import WINDOWS_PIPE_CANDIDATES

    assert state._WINDOWS_ENGINE_PIPES is WINDOWS_PIPE_CANDIDATES


def test_friendly_engine_error_leads_with_the_cause():
    bollard_shaped = (
        "No Docker or Podman API endpoint is reachable. The Podman"
        " machine could not be started. The last attempt said:"
        " `podman machine start` failed: win-sshproxy.exe failed to"
        " start, exit code: 0 Check that virtualization is enabled and"
        " try running `podman machine start` in a terminal for details."
    )
    message = state.friendly_engine_error(RuntimeError(bollard_shaped))
    assert message.startswith("The container engine is not reachable."), message
    assert "win-sshproxy.exe failed to start" in message, message
    assert "virtualization" not in message, message


def test_friendly_engine_error_passes_other_messages_through():
    plain = "Docker API Error (500): boom"
    assert state.friendly_engine_error(RuntimeError(plain)) == plain


if __name__ == "__main__":
    run_suite(
        [
            test_leaked_sockets_are_removed,
            test_non_socket_files_are_left_alone,
            test_live_pipe_stands_the_cleanup_down,
            test_zombie_forwarder_is_stopped_and_sockets_cleared,
            test_unknown_forwarder_state_stands_the_cleanup_down,
            test_forwarder_probe_fails_safe,
            test_forwarder_probe_parses_pids,
            test_zombie_forwarder_kill_uses_taskkill,
            test_zombie_forwarder_kill_survives_failure,
            test_non_windows_never_cleans,
            test_missing_socket_dir_is_quiet,
            test_ensure_engine_ready_heals_before_connecting,
            test_init_docker_client_heals_before_connecting,
            test_pipe_candidates_come_from_bollard,
            test_friendly_engine_error_leads_with_the_cause,
            test_friendly_engine_error_passes_other_messages_through,
        ]
    )
