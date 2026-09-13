"""Runtime configuration and logging setup for Syna."""

import json
import logging
from pathlib import Path
from typing import Any


def get_project_root() -> Path:
    """Return the absolute path to the project root directory."""
    return Path(__file__).resolve().parent.parent.parent


def load_config() -> dict[str, Any]:
    """Load configuration values from assets/config.json."""
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
