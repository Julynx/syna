"""Tests for the filesystem layout and the first-run setup logic."""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from _runner import run_suite

from syna import paths
from syna import setup as setup_module
from syna.config import read_asset_text
from syna.conversation.session import DEFAULT_MAX_HISTORY_MESSAGES


def test_bundled_assets_resolve():
    text = read_asset_text("verifier_prompt.md")
    assert "red-team" in text
    assert paths.asset_path("vendor").is_dir()
    assert paths.asset_path("chat.html").is_file()


def test_bundled_config_matches_code_defaults():
    bundled = json.loads(read_asset_text("config.json"))
    assert bundled["max_history_messages"] == DEFAULT_MAX_HISTORY_MESSAGES == 300


def test_load_config_merges_defaults_under_user_values():
    """Upgrades reach old installs: missing keys get defaults, user wins."""
    from syna.config import load_config

    home_override = tempfile.mkdtemp(prefix="syna-merge-")
    bundled = json.loads(read_asset_text("config.json"))
    user_config = {
        "model": "user/custom-model",
        "retry": {"max_elapsed_time_ms": 999},
    }
    (Path(home_override) / "config.json").write_text(
        json.dumps(user_config), encoding="utf-8"
    )
    previous = os.environ.get("SYNA_HOME")
    os.environ["SYNA_HOME"] = home_override
    paths.get_data_home.cache_clear()
    load_config.cache_clear()
    try:
        merged = load_config()
        assert merged["model"] == "user/custom-model", "user values must win"
        assert "retry" in merged, (
            "an old config.json missing a new required key must still"
            " receive the bundled default (create_model_client indexes"
            " config['retry'] directly)"
        )
        assert merged["retry"]["max_elapsed_time_ms"] == 999, (
            "the user's nested override must survive the merge"
        )
        assert (
            merged["retry"]["initial_interval_ms"]
            == bundled["retry"]["initial_interval_ms"]
        ), "retry keys the user did not set keep their bundled values"
        assert merged["max_history_messages"] == bundled["max_history_messages"], (
            "keys absent from the user's file appear with bundled values"
        )
    finally:
        if previous is None:
            os.environ.pop("SYNA_HOME", None)
        else:
            os.environ["SYNA_HOME"] = previous
        paths.get_data_home.cache_clear()
        load_config.cache_clear()


def test_data_home_respects_syna_home_override(monkeypatch):
    home_override = tempfile.mkdtemp(prefix="syna-home-")
    paths.get_data_home.cache_clear()
    previous = os.environ.get("SYNA_HOME")
    os.environ["SYNA_HOME"] = home_override
    try:
        home = paths.get_data_home()
        assert home == Path(home_override)
        assert (home / "config.json").is_file(), "first run seeds the defaults"
        seeded = (home / "config.json").read_text(encoding="utf-8")
        assert "max_steps_per_turn" in seeded
    finally:
        if previous is None:
            os.environ.pop("SYNA_HOME", None)
        else:
            os.environ["SYNA_HOME"] = previous
        paths.get_data_home.cache_clear()


def test_repo_checkout_uses_repo_root():
    paths.get_data_home.cache_clear()
    previous = os.environ.pop("SYNA_HOME", None)
    try:
        assert paths.get_data_home() == paths.get_project_root()
    finally:
        if previous is not None:
            os.environ["SYNA_HOME"] = previous
        paths.get_data_home.cache_clear()


def test_endpoint_needs_key_matrix():
    assert setup_module.endpoint_needs_key("")  # default = OpenRouter
    assert setup_module.endpoint_needs_key("https://api.openai.com/v1")
    assert not setup_module.endpoint_needs_key("http://localhost:11434/v1")
    assert not setup_module.endpoint_needs_key("http://127.0.0.1:1234/v1")


def test_setup_incomplete_matrix(monkeypatch):
    monkeypatch.setattr(
        setup_module,
        "config_status",
        lambda: {
            "has_key": True,
            "has_model": True,
            "base_url": "",
            "model": "m",
            "needs_key": True,
        },
    )
    monkeypatch.setattr(
        setup_module, "detect_engine", lambda: setup_module.DetectResult("engine", True)
    )
    assert not setup_module.setup_incomplete()

    monkeypatch.setattr(
        setup_module,
        "detect_engine",
        lambda: setup_module.DetectResult("engine", False),
    )
    assert setup_module.setup_incomplete(), "a missing engine sends the wizard"

    monkeypatch.setattr(
        setup_module,
        "config_status",
        lambda: {
            "has_key": False,
            "has_model": True,
            "base_url": "",
            "model": "m",
            "needs_key": True,
        },
    )
    monkeypatch.setattr(
        setup_module, "detect_engine", lambda: setup_module.DetectResult("engine", True)
    )
    assert setup_module.setup_incomplete(), "a missing key sends the wizard"

    monkeypatch.setattr(
        setup_module,
        "config_status",
        lambda: {
            "has_key": False,
            "has_model": True,
            "base_url": "http://localhost:11434/v1",
            "model": "m",
            "needs_key": False,
        },
    )
    assert not setup_module.setup_incomplete(), "local endpoints need no key"


def test_write_env_file_merges_and_clears_cache(monkeypatch):
    home_override = Path(tempfile.mkdtemp(prefix="syna-home-"))
    paths.get_data_home.cache_clear()
    previous = os.environ.get("SYNA_HOME")
    os.environ["SYNA_HOME"] = str(home_override)
    try:
        from syna.config import get_env

        env_path = setup_module.write_env_file(
            {"OPENROUTER_API_KEY": "sk-test", "MODEL": "m1"}
        )
        assert env_path == home_override / ".env"
        assert "OPENROUTER_API_KEY=sk-test" in env_path.read_text(encoding="utf-8")
        assert get_env()["MODEL"] == "m1"

        setup_module.write_env_file({"MODEL": "m2"})
        assert get_env()["OPENROUTER_API_KEY"] == "sk-test", "merges, not replaces"
        assert get_env()["MODEL"] == "m2"

        setup_module.write_env_file({"MODEL": "", "OPENROUTER_API_KEY": ""})
        assert "MODEL" not in get_env(), "an empty value deletes the key"
        assert "OPENROUTER_API_KEY" not in get_env()
        assert "MODEL=" not in env_path.read_text(encoding="utf-8")
    finally:
        if previous is None:
            os.environ.pop("SYNA_HOME", None)
        else:
            os.environ["SYNA_HOME"] = previous
        paths.get_data_home.cache_clear()

        from syna.config import get_env

        get_env.cache_clear()


def test_provider_presets_are_complete():
    for _name, preset in setup_module.PROVIDERS.items():
        assert preset["name"] and preset["base_url"].startswith("http")
        assert preset["model"]
        if preset["needs_key"]:
            assert preset["keys_url"].startswith("https")
    openrouter = setup_module.PROVIDERS["openrouter"]
    assert "openrouter.ai/api/v1" in openrouter["base_url"]
    assert "Most tested" not in openrouter["name"], "UI suffix lives in the label"

    from syna.config import (
        DEFAULT_IMAGE_MODEL,
        DEFAULT_MODEL,
        DEFAULT_STT_MODEL,
        DEFAULT_TTS_MODEL,
        DEFAULT_TTS_VOICE,
    )

    assert openrouter["model"] == DEFAULT_MODEL
    assert openrouter["stt_model"] == DEFAULT_STT_MODEL
    assert openrouter["tts_model"] == DEFAULT_TTS_MODEL
    assert openrouter["tts_voice"] == DEFAULT_TTS_VOICE
    assert openrouter["image_model"] == DEFAULT_IMAGE_MODEL


def test_detection_reports_data_not_exceptions():
    result = setup_module.detect_all()
    assert result["platform"] == sys.platform
    for name in ("wsl", "podman", "docker"):
        assert isinstance(result[name]["found"], bool)
    assert result["issues_url"].endswith("/issues")


def test_install_failure_is_returned_not_raised(monkeypatch):
    monkeypatch.setattr(setup_module.shutil, "which", lambda name: None)
    ok, log = setup_module.install_podman()
    assert not ok
    assert log


def test_setup_wizard_page_served_when_incomplete(monkeypatch):
    from fastapi.testclient import TestClient

    from syna import server

    monkeypatch.setattr(server, "AUTH_TOKEN", None)
    monkeypatch.setattr(server.setup, "setup_incomplete", lambda: True)
    client = TestClient(server.app)

    response = client.get("/setup", follow_redirects=False)
    assert response.status_code == 200
    assert "setup-window" in response.text

    redirected = client.get("/", follow_redirects=False)
    assert redirected.status_code in (307, 302)
    assert redirected.headers["location"] == "/setup"

    monkeypatch.setattr(server.setup, "setup_incomplete", lambda: False)
    flipped = client.get("/setup", follow_redirects=False)
    assert flipped.status_code in (307, 302), "complete setup leaves the wizard"


def test_setup_api_endpoints(monkeypatch):
    from fastapi.testclient import TestClient

    from syna import server
    from syna.config import get_env

    home_override = tempfile.mkdtemp(prefix="syna-home-")
    paths.get_data_home.cache_clear()
    get_env.cache_clear()
    previous = os.environ.get("SYNA_HOME")
    os.environ["SYNA_HOME"] = home_override
    monkeypatch.setattr(server, "AUTH_TOKEN", None)
    monkeypatch.setattr(
        server.setup,
        "detect_all",
        lambda: {"platform": "test", "podman": {"found": True}},
    )
    client = TestClient(server.app)
    try:
        detect = client.get("/setup/api/detect")
        assert detect.status_code == 200
        assert detect.json()["platform"] == "test"

        unknown = client.post("/setup/api/install/not-a-dep")
        assert unknown.status_code == 404

        config = client.post(
            "/setup/api/config",
            json={"provider": "openrouter", "api_key": "", "model": "m"},
        )
        assert config.status_code == 400, "OpenRouter requires a key"

        custom = client.post(
            "/setup/api/config",
            json={"provider": "custom", "base_url": "not-a-url", "model": "m"},
        )
        assert custom.status_code == 400

        ok = client.post(
            "/setup/api/config",
            json={"provider": "ollama", "api_key": "", "model": "llama3.2"},
        )
        assert ok.status_code == 200
        written = (Path(home_override) / ".env").read_text(encoding="utf-8")
        assert "BASE_URL=http://localhost:11434/v1" in written
    finally:
        if previous is None:
            os.environ.pop("SYNA_HOME", None)
        else:
            os.environ["SYNA_HOME"] = previous
        paths.get_data_home.cache_clear()
        get_env.cache_clear()


class _FakeRegistryKey:
    """Context-manager stand-in for a winreg key handle."""

    def __init__(self, value):
        self._value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_refresh_path_merges_registry_entries(monkeypatch):
    """Entries an installer writes to the registry PATH become visible.

    This is the regression lock for the wizard's install flow: a
    winget-driven install updates the registry, not the running process,
    so detection must re-read the registry to see the new engine.
    """
    if sys.platform != "win32":
        return
    user_dir = tempfile.mkdtemp(prefix="syna-path-user-")
    machine_dir = tempfile.mkdtemp(prefix="syna-path-machine-")
    values = {
        ("HKCU", "Environment"): "%SYNA_TEST_NEW%",
        (
            "HKLM",
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ): machine_dir,
    }
    fake = type(sys)("winreg")
    fake.HKEY_CURRENT_USER = "HKCU"
    fake.HKEY_LOCAL_MACHINE = "HKLM"
    fake.OpenKey = lambda root, subkey: _FakeRegistryKey(values[(root, subkey)])
    fake.QueryValueEx = lambda key, name: (key._value, None)
    fake.ExpandEnvironmentStrings = lambda raw: raw.replace("%SYNA_TEST_NEW%", user_dir)
    monkeypatch.setitem(sys.modules, "winreg", fake)

    previous = os.environ["PATH"]
    try:
        setup_module._refresh_path()
        entries = os.environ["PATH"].split(os.pathsep)
        assert user_dir in entries, "user PATH entry merged and expanded"
        assert machine_dir in entries, "machine PATH entry merged"
        assert "%SYNA_TEST_NEW%" not in entries

        setup_module._refresh_path()
        refreshed = os.environ["PATH"].split(os.pathsep)
        assert refreshed.count(user_dir) == 1, "refresh is idempotent"
        assert refreshed == entries, "second refresh adds nothing"
    finally:
        os.environ["PATH"] = previous


def test_windows_engine_install_verdict(monkeypatch):
    """The install verdict comes from detection, not winget's exit code.

    winget exits non-zero for the localized "already installed, no
    update" case, which must not surface as a failed installation.
    """
    if sys.platform != "win32":
        return
    localized_no_update = "Se encontró un paquete existente ya instalado."
    monkeypatch.setattr(
        setup_module, "_winget", lambda package_id: (False, localized_no_update)
    )
    installed = setup_module.DetectResult("podman", True, "podman version 5.4.0")
    ok, detail = setup_module._windows_engine_install(
        "RedHat.Podman", lambda: installed
    )
    assert ok, "an engine present after the run is a success"
    assert detail == "podman version 5.4.0"

    missing = setup_module.DetectResult("podman", False, "not found on PATH")
    ok, tail = setup_module._windows_engine_install("RedHat.Podman", lambda: missing)
    assert not ok, "no engine and a failed winget is a failure"
    assert tail == localized_no_update, "the winget log reaches the wizard"


def test_sanity_check_requires_a_reachable_engine(monkeypatch):
    """A runnable engine binary is not enough: the endpoint must answer.

    `podman --version` succeeds even with no machine initialized, which
    once let users into the chat only to fail their first tool call.
    """
    monkeypatch.setattr(
        setup_module, "detect_engine", lambda: setup_module.DetectResult("podman", True)
    )

    class _FakeChat:
        def send(self, model, messages, max_tokens):
            pass

    class _FakeClient:
        chat = _FakeChat()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    from syna import state as state_module
    from syna.conversation import session as session_module

    class _FakeChat:
        def send(self, model, messages, max_tokens):
            pass

    class _FakeClient:
        chat = _FakeChat()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(session_module, "create_model_client", lambda: _FakeClient())
    monkeypatch.setattr(session_module, "load_base_url", lambda: "http://test")

    ready = []
    monkeypatch.setattr(state_module, "ensure_engine_ready", lambda: ready.append(True))
    results = setup_module.sanity_check()
    assert results["engine"]["ok"]
    assert ready, "the sanity check verifies the engine endpoint"
    assert results["endpoint"]["ok"]

    def _broken():
        raise RuntimeError("no pipe available")

    monkeypatch.setattr(state_module, "ensure_engine_ready", _broken)
    failed = setup_module.sanity_check()
    assert not failed["engine"]["ok"]
    assert "no pipe available" in failed["engine"]["detail"]
    assert failed["endpoint"]["detail"] == "Skipped: the engine is not reachable."
    assert not failed["endpoint"]["ok"]


def test_ensure_engine_ready_delegates_to_bollard(monkeypatch):
    """Readiness is bollard's discovery running end to end."""
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            calls.append("constructed")

        def close(self):
            calls.append("closed")

    import syna.state

    monkeypatch.setattr(syna.state, "DockerClient", FakeClient)
    syna.state.ensure_engine_ready()
    assert calls == ["constructed", "closed"]


def test_init_docker_client_recovers_from_failure(monkeypatch):
    """A failed client construction must not brick initialization.

    This is the regression behind "Docker client is already
    initialized.": the init state was marked before the constructor
    finished, so a failure left every later call erroring forever.
    """
    import syna.state as state_module

    saved = (
        state_module._exit_stack,
        state_module.client,
        state_module._global_container,
    )
    state_module._exit_stack = None
    state_module.client = None
    state_module._global_container = None

    class _FakeEngine:
        def __init__(self):
            state["attempts"] += 1
            if state["attempts"] == 1:
                raise FileNotFoundError("no endpoint yet")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    state = {"attempts": 0}
    monkeypatch.setattr(state_module, "DockerClient", _FakeEngine)
    try:
        raised = False
        try:
            state_module.init_docker_client()
        except FileNotFoundError:
            raised = True
        assert raised, "the constructor's failure surfaces"
        assert state_module.client is None, "failure leaves nothing behind"
        assert state_module._exit_stack is None

        state_module.init_docker_client()
        assert state_module.client is not None, "the next call retries cleanly"
        assert state["attempts"] == 2
    finally:
        if state_module._exit_stack is not None:
            state_module._exit_stack.close()
        (
            state_module._exit_stack,
            state_module.client,
            state_module._global_container,
        ) = saved


def test_init_docker_client_serializes_concurrent_calls(monkeypatch):
    """Concurrent sessions wait for one bring-up instead of racing it."""
    import syna.state as state_module

    saved = (
        state_module._exit_stack,
        state_module.client,
        state_module._global_container,
    )
    state_module._exit_stack = None
    state_module.client = None
    state_module._global_container = None

    constructed = []

    class _SlowEngine:
        def __init__(self):
            constructed.append(True)
            time.sleep(0.4)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(state_module, "DockerClient", _SlowEngine)

    def _init():
        state_module.init_docker_client()

    threads = [threading.Thread(target=_init) for _ in range(3)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert len(constructed) == 1, "exactly one bring-up runs"
        assert state_module.client is not None
    finally:
        if state_module._exit_stack is not None:
            state_module._exit_stack.close()
        (
            state_module._exit_stack,
            state_module.client,
            state_module._global_container,
        ) = saved


def main():
    run_suite(
        [
            test_bundled_assets_resolve,
            test_bundled_config_matches_code_defaults,
            test_load_config_merges_defaults_under_user_values,
            test_data_home_respects_syna_home_override,
            test_repo_checkout_uses_repo_root,
            test_endpoint_needs_key_matrix,
            test_setup_incomplete_matrix,
            test_write_env_file_merges_and_clears_cache,
            test_provider_presets_are_complete,
            test_detection_reports_data_not_exceptions,
            test_install_failure_is_returned_not_raised,
            test_refresh_path_merges_registry_entries,
            test_windows_engine_install_verdict,
            test_sanity_check_requires_a_reachable_engine,
            test_ensure_engine_ready_delegates_to_bollard,
            test_init_docker_client_recovers_from_failure,
            test_init_docker_client_serializes_concurrent_calls,
            test_setup_wizard_page_served_when_incomplete,
            test_setup_api_endpoints,
        ]
    )


if __name__ == "__main__":
    main()
