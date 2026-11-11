"""Offline tests for syna's integration with the auto-self-update package.

The update machinery itself lives in the auto-self-update package and is
tested there; this suite covers only what syna adds on top: the config
and environment gates, the settings-restart exemption, the entry-point
wiring, and the relaunch builder syna borrows for its server restart.
"""

import os
import sys
import tempfile
from contextlib import redirect_stdout
from importlib import metadata
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import selfupdate
from _runner import run_suite

import syna.config as config_module


def test_dependency_is_installed():
    assert metadata.version("auto-self-update")
    assert callable(selfupdate.check)


def _no_env(monkeypatch):
    monkeypatch.setitem(os.environ, config_module.AUTO_UPDATE_ENV, "")


def test_gate_defaults_to_enabled(monkeypatch):
    _no_env(monkeypatch)
    monkeypatch.setattr(config_module, "load_config", lambda: {"auto_update": True})
    assert config_module.load_auto_update_enabled() is True


def test_gate_config_disabled(monkeypatch):
    _no_env(monkeypatch)
    monkeypatch.setattr(config_module, "load_config", lambda: {"auto_update": False})
    assert config_module.load_auto_update_enabled() is False


def test_gate_env_overrides_config(monkeypatch):
    monkeypatch.setattr(config_module, "load_config", lambda: {"auto_update": True})
    monkeypatch.setitem(os.environ, config_module.AUTO_UPDATE_ENV, "1")
    assert config_module.load_auto_update_enabled() is False


def test_gate_fails_closed_on_broken_config(monkeypatch):
    _no_env(monkeypatch)

    def boom():
        raise ValueError("broken config.json")

    monkeypatch.setattr(config_module, "load_config", boom)
    assert config_module.load_auto_update_enabled() is False, (
        "an unreadable config skips the check instead of crashing the launch"
    )


def test_check_is_gated_in_a_checkout():
    out = StringIO()
    with redirect_stdout(out):
        result = selfupdate.check("syna-agent", enabled=True)
    assert result is False, (
        "this suite runs from the syna checkout: the package must detect"
        " the pyproject.toml above the caller and skip without network"
    )


def test_settings_restart_skips_selfupdate(monkeypatch):
    from syna import server

    monkeypatch.setattr(server, "shutdown_all_sessions", lambda: None)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    seen = []

    def fake_execv(path, argv):
        seen.append(os.environ.get(config_module.AUTO_UPDATE_ENV))

    monkeypatch.setattr(server.os, "execv", fake_execv)
    _no_env(monkeypatch)
    server._delayed_restart()
    assert seen == ["1"], "a settings-save restart must never trigger the update flow"


def test_relaunch_argv_prefers_the_running_shim():
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
    """Run every integration test and report a summary."""
    run_suite(
        [
            test_dependency_is_installed,
            test_gate_defaults_to_enabled,
            test_gate_config_disabled,
            test_gate_env_overrides_config,
            test_gate_fails_closed_on_broken_config,
            test_check_is_gated_in_a_checkout,
            test_settings_restart_skips_selfupdate,
            test_relaunch_argv_prefers_the_running_shim,
            test_relaunch_argv_script_goes_through_the_interpreter,
            test_relaunch_argv_falls_back_to_path_shim,
        ]
    )


if __name__ == "__main__":
    main()
