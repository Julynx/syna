"""First-run setup: dependency detection, installation, and configuration.

The wizard is part of the web server app (routes under /setup) so the
first run of `syna-server` — or a manual `syna-setup` — walks the user
through dependencies (WSL2, Podman/Docker), the provider endpoint and
API key, and a sanity check, writing the result to the Syna data home.

Detection and installation are per-OS and best-effort: every failure is
returned as data (never raised) so the wizard can show a friendly,
actionable explanation instead of a traceback.
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from .config import (
    DEFAULT_IMAGE_MODEL,
    DEFAULT_MODEL,
    DEFAULT_STT_MODEL,
    DEFAULT_TTS_MODEL,
    DEFAULT_TTS_VOICE,
    get_data_home,
    load_config,
)

ISSUES_URL = "https://github.com/Julynx/syna/issues"

DOC_LINKS = {
    "podman": {
        "windows": "https://podman.io/docs/installation#installing-on-windows",
        "linux": "https://podman.io/docs/installation#installing-on-linux",
        "darwin": "https://podman.io/docs/installation#installing-on-macos",
    },
    "docker": {
        "windows": "https://docs.docker.com/desktop/setup/install/windows-install/",
        "linux": "https://docs.docker.com/engine/install/",
        "darwin": "https://docs.docker.com/desktop/setup/install/mac-install/",
    },
    "wsl": {
        "windows": "https://learn.microsoft.com/en-us/windows/wsl/install",
    },
}

PROVIDERS = {
    "openrouter": {
        "name": "OpenRouter",
        "label": "OpenRouter (Recommended \u00b7 Most tested)",
        "base_url": "https://openrouter.ai/api/v1",
        "keys_url": "https://openrouter.ai/settings/keys",
        "model": DEFAULT_MODEL,
        "stt_model": DEFAULT_STT_MODEL,
        "tts_model": DEFAULT_TTS_MODEL,
        "tts_voice": DEFAULT_TTS_VOICE,
        "image_model": DEFAULT_IMAGE_MODEL,
        "needs_key": True,
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "keys_url": "https://platform.openai.com/api-keys",
        "model": "gpt-4o-mini",
        "needs_key": True,
    },
    "google": {
        "name": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "keys_url": "https://aistudio.google.com/apikey",
        "model": "gemini-2.5-flash",
        "needs_key": True,
    },
    "ollama": {
        "name": "Ollama (local)",
        "base_url": "http://localhost:11434/v1",
        "keys_url": "",
        "model": "llama3.2",
        "needs_key": False,
    },
}


@dataclass(frozen=True)
class DetectResult:
    """Presence of one optional dependency on this machine."""

    name: str
    found: bool
    detail: str = ""


def _run_quiet(command: list[str], timeout: float = 10.0) -> tuple[bool, str]:
    """Run a command and return (ok, last output lines) without raising."""
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    tail = "\n".join(
        ((completed.stdout or "") + (completed.stderr or "")).splitlines()[-12:]
    )
    return completed.returncode == 0, tail


def _refresh_path() -> None:
    """Merge PATH entries written by installers into the running process.

    Windows installers (winget included) update the registry PATH, but a
    long-running process keeps the PATH it was started with, so a freshly
    installed engine stays invisible to shutil.which forever. Re-reading
    the user and machine PATH values and appending the missing entries
    lets the wizard detect an engine right after installing it.
    """
    if sys.platform != "win32":
        return
    import winreg

    current = os.environ["PATH"].split(os.pathsep)
    seen = {entry.casefold() for entry in current}
    registry_paths = (
        (winreg.HKEY_CURRENT_USER, "Environment"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    )
    for root, subkey in registry_paths:
        try:
            with winreg.OpenKey(root, subkey) as key:
                raw, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        expanded = winreg.ExpandEnvironmentStrings(raw)
        for entry in expanded.split(os.pathsep):
            entry = entry.strip()
            if entry and entry.casefold() not in seen:
                seen.add(entry.casefold())
                current.append(entry)
    os.environ["PATH"] = os.pathsep.join(current)


def detect_wsl() -> DetectResult:
    """Whether the Windows Subsystem for Linux is installed."""
    if sys.platform != "win32":
        return DetectResult("wsl", True, "not required on this platform")
    found, _ = _run_quiet(["wsl", "--status"], timeout=5.0)
    return DetectResult("wsl", found)


def detect_podman() -> DetectResult:
    """Whether Podman is on PATH and runnable."""
    _refresh_path()
    binary = shutil.which("podman")
    if not binary:
        return DetectResult("podman", False, "not found on PATH")
    found, tail = _run_quiet(["podman", "--version"], timeout=5.0)
    return DetectResult("podman", found, tail.splitlines()[-1] if tail else "")


def detect_docker() -> DetectResult:
    """Whether Docker is on PATH and runnable."""
    _refresh_path()
    binary = shutil.which("docker")
    if not binary:
        return DetectResult("docker", False, "not found on PATH")
    found, tail = _run_quiet(["docker", "--version"], timeout=5.0)
    return DetectResult("docker", found, tail.splitlines()[-1] if tail else "")


def detect_engine() -> DetectResult:
    """Whether any supported container engine is available."""
    for probe in (detect_podman(), detect_docker()):
        if probe.found:
            return probe
    return DetectResult("engine", False, "neither Podman nor Docker found")


def _doc_platform() -> str:
    """Normalize sys.platform to the key used in DOC_LINKS."""
    if sys.platform == "win32":
        return "windows"
    return sys.platform


def detect_all() -> dict:
    """The dependency status the wizard's first steps render."""
    plat = _doc_platform()
    return {
        "platform": sys.platform,
        "wsl": detect_wsl().__dict__,
        "podman": detect_podman().__dict__,
        "docker": detect_docker().__dict__,
        "docs": {name: links.get(plat) for name, links in DOC_LINKS.items()},
        "issues_url": ISSUES_URL,
    }


def _winget(package_id: str) -> tuple[bool, str]:
    """Install a package through the Windows package manager."""
    if not shutil.which("winget"):
        return False, "winget was not found on this system."
    return _run_quiet(
        [
            "winget",
            "install",
            "--id",
            package_id,
            "-e",
            "--accept-source-agreements",
            "--accept-package-agreements",
            "--disable-interactivity",
        ],
        timeout=180.0,
    )


def _windows_engine_install(package_id: str, probe) -> tuple[bool, str]:
    """Install an engine with winget, then verify by detecting it.

    winget exits non-zero when the package is already installed and no
    update exists (with a localized message), so the verdict comes from
    a fresh detection — which also picks up the registry PATH the
    installer just wrote — instead of from the exit code.
    """
    found, tail = _winget(package_id)
    detected = probe()
    if detected.found:
        return True, detected.detail or tail
    return found, tail


def _linux_install(package: str) -> tuple[bool, str]:
    """Best-effort install through the system package manager."""
    if shutil.which("apt-get"):
        return _run_quiet(
            ["sudo", "-n", "apt-get", "install", "-y", package], timeout=180.0
        )
    if shutil.which("dnf"):
        return _run_quiet(
            ["sudo", "-n", "dnf", "install", "-y", package], timeout=180.0
        )
    return False, "No supported package manager was found (apt-get, dnf)."


def install_wsl() -> tuple[bool, str, bool]:
    """Install the Windows Subsystem for Linux.

    Returns (ok, log tail, reboot_required). WSL needs a restart before
    it is usable; the wizard relaunches the check afterwards.
    """
    if sys.platform != "win32":
        return False, "WSL is only a Windows dependency.", False
    found, tail = _run_quiet(["wsl", "--install", "--no-distribution"], timeout=180.0)
    return found, tail, found


def install_podman() -> tuple[bool, str]:
    """Install Podman, best-effort, per platform."""
    if sys.platform == "win32":
        return _windows_engine_install("RedHat.Podman", detect_podman)
    if sys.platform == "darwin":
        if shutil.which("brew"):
            return _run_quiet(["brew", "install", "podman"])
        return False, "Homebrew was not found on this system."
    return _linux_install("podman")


def install_docker() -> tuple[bool, str]:
    """Install Docker Desktop / Engine, best-effort, per platform."""
    if sys.platform == "win32":
        return _windows_engine_install("Docker.DockerDesktop", detect_docker)
    if sys.platform == "darwin":
        if shutil.which("brew"):
            return _run_quiet(["brew", "install", "--cask", "docker"])
        return False, "Homebrew was not found on this system."
    return _linux_install("docker")


def write_env_file(values: dict[str, str]) -> Path:
    """Merge the given keys into the .env file in the Syna data home.

    A key mapped to an empty value is removed from the file, so a setting
    cleared in the GUI stops overriding its built-in default.
    """
    data_home = get_data_home()
    env_path = data_home / ".env"
    existing = {
        key: value
        for key, value in dotenv_values(env_path).items()
        if value is not None
    }
    for key, value in values.items():
        if value:
            existing[key] = value
        else:
            existing.pop(key, None)
    lines = [f"{key}={value}" for key, value in existing.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if sys.platform != "win32":
        try:
            env_path.chmod(0o600)
        except OSError:
            pass
    from .config import get_env  # noqa: PLC0415

    get_env.cache_clear()
    return env_path


LOCAL_ENDPOINT_MARKERS = ("localhost", "127.0.0.1", "[::1]")


def endpoint_needs_key(base_url: str) -> bool:
    """Local endpoints (Ollama, LM Studio) work without a key."""
    if not base_url:
        return True
    lowered = base_url.lower()
    return not any(marker in lowered for marker in LOCAL_ENDPOINT_MARKERS)


def config_status() -> dict:
    """Whether the provider settings exist and look complete.

    A cleared MODEL never makes the setup incomplete: the runtime falls
    back to DEFAULT_MODEL, so the report reflects the effective value.
    """
    env = dotenv_values(get_data_home() / ".env")
    api_key = (env.get("OPENROUTER_API_KEY") or "").strip()
    model = (env.get("MODEL") or "").strip() or DEFAULT_MODEL
    base_url = (env.get("BASE_URL") or "").strip()
    stt_model = (env.get("STT_MODEL") or "").strip()
    tts_model = (env.get("TTS_MODEL") or "").strip()
    tts_voice = (env.get("TTS_VOICE") or "").strip()
    image_model = (env.get("IMAGE_MODEL") or "").strip()
    thinking_effort = (env.get("THINKING_EFFORT") or "").strip()
    return {
        "has_key": bool(api_key),
        "has_model": bool(model),
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "stt_model": stt_model,
        "tts_model": tts_model,
        "tts_voice": tts_voice,
        "image_model": image_model,
        "thinking_effort": thinking_effort,
        "needs_key": endpoint_needs_key(base_url),
    }


def setup_incomplete() -> bool:
    """Whether the wizard should run instead of the chat UI.

    Setup is complete when a provider key (unless a local endpoint), a
    model, and a container engine are all present. Checked live on every
    request so finishing the wizard flips the server in-process, without
    a restart.
    """
    status = config_status()
    if not status["has_model"]:
        return True
    if not status["has_key"] and status["needs_key"]:
        return True
    return not detect_engine().found


def sanity_check() -> dict:
    """Verify the engine and the configured endpoint actually work.

    Returns per-check results with human-readable details; the endpoint
    check is a one-token chat completion against the configured base
    URL, so a wrong key or URL fails here rather than mid-task.
    """
    engine = detect_engine()
    results = {
        "engine": {
            "ok": engine.found,
            "detail": engine.detail or engine.name,
        },
        "endpoint": {"ok": False, "detail": ""},
    }
    if not engine.found:
        results["endpoint"]["detail"] = "Skipped: no container engine."
        return results
    from .config import get_env  # noqa: PLC0415
    from .conversation.session import create_model_client, load_base_url  # noqa: PLC0415

    get_env.cache_clear()
    model = (get_env().get("MODEL") or "").strip() or DEFAULT_MODEL
    try:
        with create_model_client() as client:
            client.chat.send(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=8,
            )
        results["endpoint"] = {
            "ok": True,
            "detail": f"{load_base_url()} answered via {model}",
        }
    except Exception as exc:
        results["endpoint"] = {
            "ok": False,
            "detail": f"{exc}",
        }
    return results


def main():
    """Start the web server with the setup wizard in the foreground."""
    import secrets
    import socket
    import threading
    import time
    import webbrowser

    import uvicorn

    from . import server

    server.AUTH_TOKEN = secrets.token_urlsafe(32)
    config = load_config()
    port = int(config.get("server_port", 8000))
    url = f"http://127.0.0.1:{port}/setup"

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        print(
            f"\n  ! Syna is already running at http://127.0.0.1:{port}/setup"
            "\n    (another process is using this port)."
            " Open that address in your browser instead."
        )
        return
    finally:
        probe.close()

    def open_browser():
        time.sleep(1.5)
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()
    try:
        uvicorn.run(server.app, host=config.get("server_host", "127.0.0.1"), port=port)
    except KeyboardInterrupt:
        pass
