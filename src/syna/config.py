"""Runtime configuration and logging setup for Syna."""

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .paths import asset_path, get_data_home, read_asset_text

DEFAULT_COMMAND_TIMEOUT_S = 50
DEFAULT_TOOL_CALL_TIMEOUT_S = 60
DEFAULT_OUTPUT_LIMIT_CHARS = 40000

# Canonical provider model defaults. The first-run wizard, the settings
# modal, and the runtime all read these; an unset .env value means the
# default applies.
DEFAULT_MODEL = "~z-ai/glm-flash-latest"
DEFAULT_STT_MODEL = "google/chirp-3"
DEFAULT_TTS_MODEL = "google/gemini-3.8-flash-lite-tts"
DEFAULT_TTS_VOICE = "Kore"
DEFAULT_IMAGE_MODEL = "google/gemini-3.1-flash-image"

# Feature toggles edited in the settings modal. Voice stays available with
# default models but voice mode itself starts off; image generation is
# opt-in because it is expensive and model-initiated.
DEFAULT_VOICE_ENABLED = True
DEFAULT_IMAGE_GENERATION_ENABLED = False


def load_output_limit_chars() -> int:
    """Return the character limit at which tool output is cut to head and tail."""
    return load_config().get("max_command_output_chars", DEFAULT_OUTPUT_LIMIT_CHARS)


@lru_cache(maxsize=1)
def get_env() -> dict[str, str]:
    """Read the .env file in the Syna data home and return its values."""
    return dotenv_values(get_data_home() / ".env")


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    """Load configuration values from config.json in the Syna data home.

    The data home is seeded with the bundled defaults on first run. The
    configuration is cached after the first read; treat the returned dict
    as read-only and expect a restart to pick up edits.
    """
    config_path = get_data_home() / "config.json"
    with open(config_path, "r", encoding="utf-8") as config_file:
        return json.load(config_file)


def save_config(updates: dict[str, Any]) -> Path:
    """Merge updates into config.json in the Syna data home and clear cache."""
    config_path = get_data_home() / "config.json"
    current = {}
    if config_path.is_file():
        try:
            with open(config_path, "r", encoding="utf-8") as config_file:
                current = json.load(config_file)
        except Exception:
            current = {}
    current.update(updates)
    with open(config_path, "w", encoding="utf-8") as config_file:
        json.dump(current, config_file, indent=2)
    load_config.cache_clear()
    return config_path


def get_logger() -> logging.Logger:
    """Configure file-based logging for the application and return the logger."""
    config = load_config()
    log_file_name = config.get("log_file", "syna.log")
    log_file_path = get_data_home() / log_file_name

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
    return get_data_home() / dump_file_name


def load_command_timeout_s() -> int:
    """Return the per-command timeout in seconds from the configuration."""
    return load_config().get("command_timeout_s", DEFAULT_COMMAND_TIMEOUT_S)


def load_tool_call_timeout_s() -> float:
    """Return the per-tool-call timeout in seconds from the configuration."""
    return load_config().get("tool_call_timeout_s", DEFAULT_TOOL_CALL_TIMEOUT_S)


DEFAULT_USER_MESSAGE_ARCHIVE_MIN_CHARS = 2000
DEFAULT_USER_MESSAGES_DEST = "/user_messages"
DEFAULT_ELIDE_USER_MESSAGE_HEAD_CHARS = 300
DEFAULT_VERIFY_LOG_TAIL_CHARS = 128000


def load_user_message_archive_min_chars() -> int:
    """Return the message length at which user messages are archived verbatim."""
    return load_config().get(
        "user_message_archive_min_chars", DEFAULT_USER_MESSAGE_ARCHIVE_MIN_CHARS
    )


def load_user_messages_dest() -> str:
    """Return the pod directory holding the archived user messages."""
    return load_config().get("user_messages_dest", DEFAULT_USER_MESSAGES_DEST)


def load_elide_user_message_head_chars() -> int:
    """Return how much of an archived user message stays in history."""
    return load_config().get(
        "elide_user_message_head_chars", DEFAULT_ELIDE_USER_MESSAGE_HEAD_CHARS
    )


def load_verify_log_tail_chars() -> int:
    """Return how much of the conversation log the work reviewer receives."""
    return load_config().get("verify_log_tail_chars", DEFAULT_VERIFY_LOG_TAIL_CHARS)


__all__ = [
    "asset_path",
    "get_data_home",
    "read_asset_text",
    "load_command_timeout_s",
    "load_config",
    "load_output_limit_chars",
    "load_tool_call_timeout_s",
]
