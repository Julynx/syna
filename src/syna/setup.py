"""First-run setup: dependency detection, installation, and configuration.

The wizard is part of the web server app (routes under /setup) so the
first run of `syna-server` — or a manual `syna-setup` — walks the user
through dependencies (WSL2, Podman/Docker), the provider endpoint and
API key, and a sanity check, writing the result to the Syna data home.

Detection and installation are per-OS and best-effort: every failure is
returned as data (never raised) so the wizard can show a friendly,
actionable explanation instead of a traceback.
"""

import shutil
import subprocess
import sys
from dataclasses import dataclass

from dotenv import dotenv_values

from .config import get_data_home, load_config

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
        "base_url": "https://openrouter.ai/api/v1",
        "keys_url": "https://openrouter.ai/settings/keys",
        "model": "z-ai/glm-5.3-flash",
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


def _run_quiet(command: list[str]) -> tuple[bool, str]:
    """Run a command and return (ok, last output lines) without raising."""
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    tail = "\n".join(
        ((completed.stdout or "") + (completed.stderr or "")).splitlines()[-12:]
    )
    return completed.returncode == 0, tail


def detect_wsl() -> DetectResult:
    """Whether the Windows Subsystem for Linux is installed."""
    if sys.platform != "win32":
        return DetectResult("wsl", True, "not required on this platform")
    found, _ = _run_quiet(["wsl", "--status"])
    return DetectResult("wsl", found)


def detect_podman() -> DetectResult:
    """Whether Podman is on PATH and runnable."""
    binary = shutil.which("podman")
    if not binary:
        return DetectResult("podman", False, "not found on PATH")
    found, tail = _run_quiet(["podman", "--version"])
    return DetectResult("podman", found, tail.splitlines()[-1] if tail else "")


def detect_docker() -> DetectResult:
    """Whether Docker is on PATH and runnable."""
    binary = shutil.which("docker")
    if not binary:
        return DetectResult("docker", False, "not found on PATH")
    found, tail = _run_quiet(["docker", "--version"])
    return DetectResult("docker", found, tail.splitlines()[-1] if tail else "")


def detect_engine() -> DetectResult:
    """Whether any supported container engine is available."""
    for probe in (detect_podman(), detect_docker()):
        if probe.found:
            return probe
    return DetectResult("engine", False, "neither Podman nor Docker found")


def detect_all() -> dict:
    """The dependency status the wizard's first steps render."""
    return {
        "platform": sys.platform,
        "wsl": detect_wsl().__dict__,
        "podman": detect_podman().__dict__,
        "docker": detect_docker().__dict__,
        "docs": {name: links.get(sys.platform) for name, links in DOC_LINKS.items()},
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
        ]
    )


def _linux_install(package: str) -> tuple[bool, str]:
    """Best-effort install through the system package manager."""
    if shutil.which("apt-get"):
        return _run_quiet(["sudo", "-n", "apt-get", "install", "-y", package])
    if shutil.which("dnf"):
        return _run_quiet(["sudo", "-n", "dnf", "install", "-y", package])
    return False, "No supported package manager was found (apt-get, dnf)."


def install_wsl() -> tuple[bool, str, bool]:
    """Install the Windows Subsystem for Linux.

    Returns (ok, log tail, reboot_required). WSL needs a restart before
    it is usable; the wizard relaunches the check afterwards.
    """
    if sys.platform != "win32":
        return False, "WSL is only a Windows dependency.", False
    found, tail = _run_quiet(["wsl", "--install", "--no-distribution"])
    return found, tail, found


def install_podman() -> tuple[bool, str]:
    """Install Podman, best-effort, per platform."""
    if sys.platform == "win32":
        return _winget("RedHat.Podman")
    if sys.platform == "darwin":
        if shutil.which("brew"):
            return _run_quiet(["brew", "install", "podman"])
        return False, "Homebrew was not found on this system."
    return _linux_install("podman")


def install_docker() -> tuple[bool, str]:
    """Install Docker Desktop / Engine, best-effort, per platform."""
    if sys.platform == "win32":
        return _winget("Docker.DockerDesktop")
    if sys.platform == "darwin":
        if shutil.which("brew"):
            return _run_quiet(["brew", "install", "--cask", "docker"])
        return False, "Homebrew was not found on this system."
    return _linux_install("docker")


def write_env_file(values: dict[str, str]) -> Path:
    """Merge the given keys into the .env file in the Syna data home."""
    data_home = get_data_home()
    env_path = data_home / ".env"
    existing = {
        key: value
        for key, value in dotenv_values(env_path).items()
        if value is not None
    }
    existing.update({key: value for key, value in values.items() if value})
    lines = [f"{key}={value}" for key, value in existing.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
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
    """Whether the provider settings exist and look complete."""
    env = dotenv_values(get_data_home() / ".env")
    api_key = (env.get("OPENROUTER_API_KEY") or "").strip()
    model = (env.get("MODEL") or "").strip()
    base_url = (env.get("BASE_URL") or "").strip()
    return {
        "has_key": bool(api_key),
        "has_model": bool(model),
        "base_url": base_url,
        "model": model,
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
    from .session import create_model_client, load_base_url  # noqa: PLC0415

    from .config import get_env  # noqa: PLC0415

    get_env.cache_clear()
    model = (get_env().get("MODEL") or "").strip()
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
