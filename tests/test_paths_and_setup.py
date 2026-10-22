"""Tests for the filesystem layout and the first-run setup logic."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from _runner import run_suite

from syna import paths
from syna import setup as setup_module
from syna.config import read_asset_text


def test_bundled_assets_resolve():
    text = read_asset_text("verifier_prompt.md")
    assert "red-team" in text
    assert paths.asset_path("vendor").is_dir()
    assert paths.asset_path("chat.html").is_file()


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
    finally:
        if previous is None:
            os.environ.pop("SYNA_HOME", None)
        else:
            os.environ["SYNA_HOME"] = previous
        paths.get_data_home.cache_clear()


def test_provider_presets_are_complete():
    for name, preset in setup_module.PROVIDERS.items():
        assert preset["name"] and preset["base_url"].startswith("http")
        assert preset["model"]
        if preset["needs_key"]:
            assert preset["keys_url"].startswith("https")
    openrouter = setup_module.PROVIDERS["openrouter"]
    assert "openrouter.ai/api/v1" in openrouter["base_url"]
    assert "Most tested" not in openrouter["name"], "UI suffix lives in the frontend"


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


def main():
    run_suite(
        [
            test_bundled_assets_resolve,
            test_data_home_respects_syna_home_override,
            test_repo_checkout_uses_repo_root,
            test_endpoint_needs_key_matrix,
            test_setup_incomplete_matrix,
            test_write_env_file_merges_and_clears_cache,
            test_provider_presets_are_complete,
            test_detection_reports_data_not_exceptions,
            test_install_failure_is_returned_not_raised,
            test_setup_wizard_page_served_when_incomplete,
            test_setup_api_endpoints,
        ]
    )


if __name__ == "__main__":
    main()
