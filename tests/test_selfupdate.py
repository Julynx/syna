"""Offline tests for the launch-time self-update machinery.

Every boundary (PyPI, the filesystem markers, the process table, the
installer subprocess, the relaunch) is faked: no network, no real
upgrade, no process spawn.
"""

import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

from syna import selfupdate


def test_version_key_orders_releases():
    assert selfupdate._is_newer("2.0.8", "2.0.7")
    assert selfupdate._is_newer("2.1.0", "2.0.99")
    assert selfupdate._is_newer("10.0.0", "9.9.9"), "numeric, not lexicographic"
    assert not selfupdate._is_newer("2.0.7", "2.0.7")
    assert not selfupdate._is_newer("2.0.7", "2.0.8")


def test_is_stable_release():
    assert selfupdate._is_stable_release("2.0.8")
    assert selfupdate._is_stable_release("2.0")
    assert not selfupdate._is_stable_release("2.0.8rc1")
    assert not selfupdate._is_stable_release("2.0.8.post1")
    assert not selfupdate._is_stable_release("not-a-version")


def _no_checkout(tmp_root: Path) -> Path:
    """A project root without a pyproject.toml: an installed copy."""
    return tmp_root


def _gate_patches(monkeypatch, tmp_root):
    monkeypatch.setattr(selfupdate, "get_project_root", lambda: tmp_root)
    monkeypatch.setattr(selfupdate, "load_auto_update_enabled", lambda: True)


def test_skip_reason_env_var(monkeypatch):
    monkeypatch.setitem(os.environ, selfupdate.NO_UPDATE_ENV, "1")
    assert selfupdate._skip_reason() is not None


def test_skip_reason_checkout(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "pyproject.toml").write_text("", encoding="utf-8")
        monkeypatch.setattr(selfupdate, "get_project_root", lambda: root)
        assert "checkout" in selfupdate._skip_reason()


def test_skip_reason_config_disabled(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        _gate_patches(monkeypatch, Path(temp_dir))
        monkeypatch.setattr(selfupdate, "load_auto_update_enabled", lambda: False)
        assert "disabled" in selfupdate._skip_reason()


def test_skip_reason_none(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        _gate_patches(monkeypatch, Path(temp_dir))
        assert selfupdate._skip_reason() is None


def test_latest_version_parses_pypi(monkeypatch):
    def fake_get(url, **kwargs):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"info": {"version": "2.0.8"}},
        )

    monkeypatch.setattr(selfupdate.httpx, "get", fake_get)
    assert selfupdate._latest_version() == "2.0.8"


def test_latest_version_rejects_prerelease(monkeypatch):
    def fake_get(url, **kwargs):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"info": {"version": "2.1.0rc1"}},
        )

    monkeypatch.setattr(selfupdate.httpx, "get", fake_get)
    assert selfupdate._latest_version() is None


def test_latest_version_network_failure(monkeypatch):
    def fake_get(url, **kwargs):
        raise OSError("no network")

    monkeypatch.setattr(selfupdate.httpx, "get", fake_get)
    assert selfupdate._latest_version() is None


def test_ephemeral_note():
    uvx = Path("/home/u/.cache/uv/environments-v2/syna-abc")
    assert selfupdate._ephemeral_note(uvx) == "uvx"
    pipx_run = Path("/home/u/.local/share/pipx/cache/syna-abc")
    assert selfupdate._ephemeral_note(pipx_run) == "pipx run"
    assert selfupdate._ephemeral_note(Path("/home/u/.venv")) is None


def _prefix_with(monkeypatch, marker: str | None):
    temp_dir = tempfile.TemporaryDirectory()
    prefix = Path(temp_dir.name)
    if marker:
        (prefix / marker).write_text("", encoding="utf-8")
    monkeypatch.setattr(selfupdate, "_venv_prefix", lambda: prefix)
    return temp_dir


def test_upgrade_command_uv_tool(monkeypatch):
    holder = _prefix_with(monkeypatch, "uv-receipt.toml")
    with holder:
        monkeypatch.setattr(selfupdate.shutil, "which", lambda name: f"/bin/{name}")
        command = selfupdate._upgrade_command()
        assert command == ["/bin/uv", "tool", "upgrade", "syna-agent"]


def test_upgrade_command_pipx(monkeypatch):
    holder = _prefix_with(monkeypatch, "pipx_metadata.json")
    with holder:
        monkeypatch.setattr(selfupdate.shutil, "which", lambda name: f"/bin/{name}")
        command = selfupdate._upgrade_command()
        assert command == ["/bin/pipx", "upgrade", "syna-agent"]


def test_upgrade_command_plain_pip(monkeypatch):
    holder = _prefix_with(monkeypatch, None)
    with holder:
        monkeypatch.setattr(selfupdate, "_pip_available", lambda: True)
        command = selfupdate._upgrade_command()
        assert command == [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "syna-agent",
        ]


def test_upgrade_command_uv_pip_last_resort(monkeypatch):
    holder = _prefix_with(monkeypatch, None)
    with holder:
        monkeypatch.setattr(selfupdate, "_pip_available", lambda: False)
        monkeypatch.setattr(selfupdate.shutil, "which", lambda name: "/bin/uv")
        command = selfupdate._upgrade_command()
        assert command == [
            "/bin/uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--upgrade",
            "syna-agent",
        ]


def test_upgrade_command_none(monkeypatch):
    holder = _prefix_with(monkeypatch, None)
    with holder:
        monkeypatch.setattr(selfupdate, "_pip_available", lambda: False)
        monkeypatch.setattr(selfupdate.shutil, "which", lambda name: None)
        assert selfupdate._upgrade_command() is None


def _update_available(monkeypatch, tmp_root):
    _gate_patches(monkeypatch, tmp_root)
    monkeypatch.setattr(selfupdate, "_installed_version", lambda: "1.0.0")
    monkeypatch.setattr(selfupdate, "_latest_version", lambda: "1.1.0")
    monkeypatch.setattr(selfupdate, "_ephemeral_note", lambda prefix: None)


def test_maybe_selfupdate_happy_path(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        _update_available(monkeypatch, Path(temp_dir))
        spawned = []
        monkeypatch.setattr(selfupdate, "_upgrade_command", lambda: ["fake", "upgrade"])
        monkeypatch.setattr(
            selfupdate,
            "_spawn_updater",
            lambda payload: spawned.append(payload) or True,
        )
        monkeypatch.setattr(selfupdate, "relaunch_argv", lambda name: ["shim"])
        out = StringIO()
        with redirect_stdout(out):
            assert selfupdate.maybe_selfupdate() is True
        payload = spawned[0]
        assert payload["command"] == ["fake", "upgrade"]
        assert payload["argv"] == ["shim"]
        assert payload["previous"] == "1.0.0"
        assert payload["pid"] == os.getpid()
        assert "1.0.0 -> 1.1.0" in out.getvalue()


def test_maybe_selfupdate_up_to_date(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        _gate_patches(monkeypatch, Path(temp_dir))
        monkeypatch.setattr(selfupdate, "_installed_version", lambda: "1.1.0")
        monkeypatch.setattr(selfupdate, "_latest_version", lambda: "1.1.0")
        assert selfupdate.maybe_selfupdate() is False


def test_maybe_selfupdate_ephemeral_note(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        _update_available(monkeypatch, Path(temp_dir))
        monkeypatch.setattr(selfupdate, "_ephemeral_note", lambda prefix: "uvx")
        out = StringIO()
        with redirect_stdout(out):
            assert selfupdate.maybe_selfupdate() is False
        assert "uvx" in out.getvalue()


def test_maybe_selfupdate_no_installer(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        _update_available(monkeypatch, Path(temp_dir))
        monkeypatch.setattr(selfupdate, "_upgrade_command", lambda: None)
        out = StringIO()
        with redirect_stdout(out):
            assert selfupdate.maybe_selfupdate() is False
        assert "pip install --upgrade syna-agent" in out.getvalue()


def test_maybe_selfupdate_spawn_failure(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        _update_available(monkeypatch, Path(temp_dir))
        monkeypatch.setattr(selfupdate, "_upgrade_command", lambda: ["fake"])
        monkeypatch.setattr(selfupdate, "_spawn_updater", lambda payload: False)
        monkeypatch.setattr(selfupdate, "relaunch_argv", lambda name: ["shim"])
        out = StringIO()
        with redirect_stdout(out):
            assert selfupdate.maybe_selfupdate() is False
        assert "manually" in out.getvalue()


def test_maybe_selfupdate_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("unexpected")

    monkeypatch.setattr(selfupdate, "_skip_reason", boom)
    assert selfupdate.maybe_selfupdate() is False


def test_wait_for_parent_returns_when_gone(monkeypatch):
    states = iter([True, True, False])
    monkeypatch.setattr(selfupdate, "_pid_alive", lambda pid: next(states, False))
    selfupdate._wait_for_parent(1234)


def test_wait_for_parent_times_out(monkeypatch):
    monkeypatch.setattr(selfupdate, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(selfupdate.time, "sleep", lambda s: None)
    selfupdate._wait_for_parent(1234, timeout_s=0.05)


def _updater_patches(monkeypatch, returncode=0, installed="1.1.0"):
    monkeypatch.setattr(selfupdate, "_wait_for_parent", lambda pid: None)
    monkeypatch.setattr(
        selfupdate.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=returncode),
    )
    monkeypatch.setattr(selfupdate, "_installed_version", lambda: installed)
    relaunched = []
    monkeypatch.setattr(selfupdate, "_relaunch", relaunched.append)
    return relaunched


def _payload(**overrides):
    payload = {
        "pid": 4321,
        "command": ["fake", "upgrade"],
        "argv": ["shim"],
        "previous": "1.0.0",
    }
    payload.update(overrides)
    return payload


def test_updater_main_success(monkeypatch):
    relaunched = _updater_patches(monkeypatch)
    out = StringIO()
    with redirect_stdout(out):
        code = selfupdate._main(["updater", json.dumps(_payload())])
    assert code == 0
    assert relaunched == [["shim"]]
    assert "relaunching" in out.getvalue()


def test_updater_main_upgrade_failure(monkeypatch):
    relaunched = _updater_patches(monkeypatch, returncode=1)
    out = StringIO()
    with redirect_stdout(out):
        code = selfupdate._main(["updater", json.dumps(_payload())])
    assert code == 1
    assert relaunched == [], "a failed upgrade never relaunches"
    assert "manually" in out.getvalue()


def test_updater_main_command_cannot_run(monkeypatch):
    monkeypatch.setattr(selfupdate, "_wait_for_parent", lambda pid: None)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="fake", timeout=1)

    monkeypatch.setattr(selfupdate.subprocess, "run", boom)
    relaunched = []
    monkeypatch.setattr(selfupdate, "_relaunch", relaunched.append)
    code = selfupdate._main(["updater", json.dumps(_payload())])
    assert code == 1
    assert relaunched == []


def test_updater_main_newer_release_mid_update(monkeypatch):
    relaunched = _updater_patches(monkeypatch, installed="1.2.0")
    out = StringIO()
    with redirect_stdout(out):
        code = selfupdate._main(["updater", json.dumps(_payload())])
    assert code == 0
    assert relaunched == [["shim"]], (
        "a release published between check and upgrade is still a success"
    )
    assert "Updated to 1.2.0" in out.getvalue()


def test_updater_main_version_did_not_advance(monkeypatch):
    relaunched = _updater_patches(monkeypatch, installed="1.0.0")
    code = selfupdate._main(["updater", json.dumps(_payload())])
    assert code == 1
    assert relaunched == [], "a version that did not advance never relaunches"


def test_updater_main_bad_payload():
    assert selfupdate._main(["updater"]) == 2
    assert selfupdate._main(["updater", "not json"]) == 2
    assert selfupdate._main(["updater", json.dumps({"pid": 1})]) == 2
    assert selfupdate._main(["updater", json.dumps(["not", "a", "dict"])]) == 2


def test_upgrade_command_pipx_falls_through_without_binary(monkeypatch):
    holder = _prefix_with(monkeypatch, "pipx_metadata.json")
    with holder:
        monkeypatch.setattr(selfupdate.shutil, "which", lambda name: None)
        monkeypatch.setattr(selfupdate, "_pip_available", lambda: True)
        command = selfupdate._upgrade_command()
        assert command[1:] == ["-m", "pip", "install", "--upgrade", "syna-agent"], (
            "a pipx venv whose pipx left PATH falls back to its own pip"
        )


def test_spawn_updater_detaches_from_terminal_signals(monkeypatch):
    captured = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(selfupdate.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(selfupdate.sys, "platform", "linux")
    assert selfupdate._spawn_updater({"pid": 1})
    assert captured[-1].get("start_new_session") is True, (
        "POSIX: the updater must not receive the terminal's Ctrl+C"
    )
    monkeypatch.setattr(selfupdate.sys, "platform", "win32")
    assert selfupdate._spawn_updater({"pid": 1})
    flags = captured[-1].get("creationflags", 0)
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP, (
        "Windows: the updater leaves the console's Ctrl+C target set"
    )


def test_spawn_updater_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr(selfupdate.subprocess, "Popen", boom)
    assert selfupdate._spawn_updater({"pid": 1}) is False


def test_package_init_stays_light():
    import syna

    assert not hasattr(syna, "ask_loop"), (
        "the package init must not pull the agent stack: the updater"
        " process pays for it and a broken optional import would kill it"
    )


def test_settings_restart_skips_selfupdate(monkeypatch):
    from syna import server

    monkeypatch.setattr(server, "shutdown_all_sessions", lambda: None)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    exec_calls = []

    def fake_execv(path, argv):
        exec_calls.append(os.environ.get(selfupdate.NO_UPDATE_ENV))

    monkeypatch.setattr(server.os, "execv", fake_execv)
    monkeypatch.setitem(os.environ, selfupdate.NO_UPDATE_ENV, "")
    server._delayed_restart()
    assert exec_calls == ["1"], (
        "a settings-save restart must never trigger the update flow"
    )


def test_relaunch_argv_prefers_the_running_shim(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        shim = Path(temp_dir) / "syna-cli.exe"
        shim.write_text("", encoding="utf-8")
        argv = selfupdate.relaunch_argv("syna-cli", [str(shim), "--flag"])
        assert argv == [str(shim), "--flag"]


def test_relaunch_argv_script_goes_through_the_interpreter():
    with tempfile.TemporaryDirectory() as temp_dir:
        script = Path(temp_dir) / "syna-cli"
        script.write_text("", encoding="utf-8")
        argv = selfupdate.relaunch_argv("syna-cli", [str(script)])
        assert argv == [sys.executable, str(script)]


def test_relaunch_argv_falls_back_to_path_shim(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        shim = Path(temp_dir) / "syna-cli"
        monkeypatch.setattr(selfupdate.shutil, "which", lambda name: str(shim))
        argv = selfupdate.relaunch_argv("syna-cli", ["nonexistent-name"])
        assert argv == [str(shim)]


def main():
    """Run every self-update test and report a summary."""
    run_suite(
        [
            test_version_key_orders_releases,
            test_is_stable_release,
            test_skip_reason_env_var,
            test_skip_reason_checkout,
            test_skip_reason_config_disabled,
            test_skip_reason_none,
            test_latest_version_parses_pypi,
            test_latest_version_rejects_prerelease,
            test_latest_version_network_failure,
            test_ephemeral_note,
            test_upgrade_command_uv_tool,
            test_upgrade_command_pipx,
            test_upgrade_command_pipx_falls_through_without_binary,
            test_upgrade_command_plain_pip,
            test_upgrade_command_uv_pip_last_resort,
            test_upgrade_command_none,
            test_maybe_selfupdate_happy_path,
            test_maybe_selfupdate_up_to_date,
            test_maybe_selfupdate_ephemeral_note,
            test_maybe_selfupdate_no_installer,
            test_maybe_selfupdate_spawn_failure,
            test_maybe_selfupdate_never_raises,
            test_spawn_updater_detaches_from_terminal_signals,
            test_spawn_updater_failure,
            test_package_init_stays_light,
            test_settings_restart_skips_selfupdate,
            test_wait_for_parent_returns_when_gone,
            test_wait_for_parent_times_out,
            test_updater_main_success,
            test_updater_main_upgrade_failure,
            test_updater_main_command_cannot_run,
            test_updater_main_newer_release_mid_update,
            test_updater_main_version_did_not_advance,
            test_updater_main_bad_payload,
            test_relaunch_argv_prefers_the_running_shim,
            test_relaunch_argv_script_goes_through_the_interpreter,
            test_relaunch_argv_falls_back_to_path_shim,
        ]
    )


if __name__ == "__main__":
    main()
