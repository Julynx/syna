"""Runtime configuration and logging setup for Syna."""

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import dotenv_values


@lru_cache(maxsize=1)
def get_env() -> dict[str, str]:
    """Read the project .env file once and return its values."""
    return dotenv_values(get_project_root() / ".env")


def get_project_root() -> Path:
    """Return the absolute path to the project root directory."""
    return Path(__file__).resolve().parent.parent.parent


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    """Load configuration values from assets/config.json.

    The configuration is cached after the first read; treat the returned
    dict as read-only and expect a server restart to pick up edits.
    """
    config_path = get_project_root() / "assets" / "config.json"
    with open(config_path, "r", encoding="utf-8") as config_file:
        return json.load(config_file)


def get_logger() -> logging.Logger:
    """Configure file-based logging for the application and return the logger."""
    config = load_config()
    log_file_name = config.get("log_file", "syna.log")
    log_file_path = get_project_root() / log_file_name

    logger = logging.getLogger("syna")
    if not logger.handlers:
        file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        logger.setLevel(logging.INFO)

    return logger


def get_session_dump_path(session_id: str | None = None) -> Path:
    """Get the path to the session dump file, where the messages array will be saved.

    When 'session_id' is given, it is woven into the file name (e.g.
    'message-session-log-<id>.json') so that concurrent sessions in server
    mode never overwrite each other's dumps. The id is sanitized to safe
    file-name characters and capped in length first.
    """
    config = load_config()
    dump_file_name = config.get("session_dump_file", "message-session-log.json")
    if session_id:
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "-", str(session_id))[:64]
        configured_path = Path(dump_file_name)
        suffix = configured_path.suffix or ".json"
        dump_file_name = f"{configured_path.stem}-{safe_id}{suffix}"
    return get_project_root() / dump_file_name


def truncate_command_output(output: str, max_chars: int | None = None) -> str:
    """Truncate command output exceeding character limit with an explanatory notice."""
    if not isinstance(output, str):
        output = str(output)

    if max_chars is None:
        config = load_config()
        max_chars = config.get("max_command_output_chars", 4000)

    if len(output) <= max_chars:
        return output

    total_chars = len(output)
    truncated_content = output[:max_chars]
    notice = (
        f"\n\n[Output truncated: displaying first {max_chars}"
        f" of {total_chars} characters. "
        "Filter or refine your command to view specific output.]"
    )
    return f"{truncated_content}{notice}"
