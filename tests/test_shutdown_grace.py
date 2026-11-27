"""Tests for the server's shutdown handling.

`build_server_config` carries the bounded graceful-shutdown timeout
(configurable through `shutdown_graceful_timeout_s`), and the
shutdown-aware server announces the signal through uvicorn's console
logger — so the terminal shows it — while distinguishing a second
press (uvicorn's force quit) from the first.
"""

import logging
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import MonkeyPatch, run_suite

import syna.server as server_module
from syna.server import (
    DEFAULT_SHUTDOWN_GRACE_SECONDS,
    _ShutdownAwareServer,
    build_server_config,
)


class _ListHandler(logging.Handler):
    """Capture log messages for assertion."""

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _patch_console_logger(monkeypatch: MonkeyPatch) -> list[str]:
    """Route the shutdown console logger through a capturing handler."""
    handler = _ListHandler()
    console_logger = logging.getLogger("syna-test.shutdown-console")
    console_logger.setLevel(logging.INFO)
    console_logger.addHandler(handler)
    monkeypatch.setattr(server_module, "_shutdown_console_logger", console_logger)
    return handler.messages


def _make_server() -> _ShutdownAwareServer:
    return _ShutdownAwareServer(build_server_config("127.0.0.1", 8000))


def test_config_carries_the_configured_grace(monkeypatch):
    monkeypatch.setitem(server_module.config, "shutdown_graceful_timeout_s", 25)
    config = build_server_config("127.0.0.1", 8000)
    assert config.timeout_graceful_shutdown == 25, config.timeout_graceful_shutdown
    assert config.host == "127.0.0.1"
    assert config.port == 8000


def test_config_defaults_to_ten_seconds(monkeypatch):
    without_key = {
        key: value
        for key, value in server_module.config.items()
        if key != "shutdown_graceful_timeout_s"
    }
    monkeypatch.setattr(server_module, "config", without_key)
    config = build_server_config("127.0.0.1", 8000)
    assert config.timeout_graceful_shutdown == DEFAULT_SHUTDOWN_GRACE_SECONDS == 10, (
        config.timeout_graceful_shutdown
    )


def test_first_shutdown_signal_is_announced(monkeypatch):
    console_messages = _patch_console_logger(monkeypatch)
    server = _make_server()
    assert not server.should_exit

    server.handle_exit(signal.SIGINT, None)

    assert server.should_exit is True, "the first signal starts graceful shutdown"
    assert server.force_exit is False, "only the second signal force quits"
    assert console_messages == [
        "Shutdown signal received (signal 2), stopping server..."
    ], console_messages


def test_second_shutdown_signal_announces_force_exit(monkeypatch):
    console_messages = _patch_console_logger(monkeypatch)
    server = _make_server()

    server.handle_exit(signal.SIGINT, None)
    server.handle_exit(signal.SIGINT, None)

    assert server.force_exit is True, "the second signal is uvicorn's force quit"
    assert console_messages[-1] == "Second shutdown signal received, forcing exit...", (
        console_messages
    )


def main():
    run_suite(
        [
            test_config_carries_the_configured_grace,
            test_config_defaults_to_ten_seconds,
            test_first_shutdown_signal_is_announced,
            test_second_shutdown_signal_announces_force_exit,
        ]
    )


if __name__ == "__main__":
    main()
