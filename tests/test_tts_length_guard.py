"""Tests for the text_to_speech input-length guard.

A too-long text must be rejected up front with a clear, actionable error
(the speech endpoint itself answers an over-long request with an opaque
provider 502), and an under-limit text must reach synthesis.
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite

import syna.capabilities.tools as tools
from syna.capabilities.speech import MAX_TTS_INPUT_CHARS, SpeechError
from syna.capabilities.tools import text_to_speech
from syna.protocol.tool_schemas import get_tool_definitions
from syna.state import register_container, unregister_container


class FakePod:
    """Serves one text file out of the pod; stands in for a Container."""

    def __init__(self, content: str):
        self.content = content

    def copy_from(self, pod_path, host_dir):
        Path(host_dir, Path(pod_path).name).write_text(self.content, encoding="utf-8")


def _patch_speech(monkeypatch):
    """Make the tool's availability checks pass without an endpoint."""

    class _Settings:
        tts_available = True

    monkeypatch.setattr(tools, "load_speech_settings", lambda: _Settings())
    monkeypatch.setattr(tools, "voice_enabled", lambda: True)


def test_over_limit_text_is_rejected_before_synthesis(monkeypatch):
    _patch_speech(monkeypatch)
    register_container(FakePod("x" * (MAX_TTS_INPUT_CHARS + 10)))
    try:
        result = text_to_speech("/workspace/audio/long.txt")
    finally:
        unregister_container()
    assert isinstance(result, dict) and "error" in result, result
    assert str(MAX_TTS_INPUT_CHARS) in result["error"], result
    assert "Split the text into parts" in result["error"], result


def test_under_limit_text_reaches_synthesis(monkeypatch):
    _patch_speech(monkeypatch)

    def fake_synthesis(text, settings):
        raise SpeechError("reached synthesis")

    monkeypatch.setattr(tools, "synthesize_speech", fake_synthesis)
    register_container(FakePod("x" * (MAX_TTS_INPUT_CHARS - 10)))
    try:
        result = text_to_speech("/workspace/audio/ok.txt")
    finally:
        unregister_container()
    assert result == {"error": "reached synthesis"}, result


def test_docstring_states_the_configured_limit():
    definition = next(
        tool
        for tool in get_tool_definitions()
        if tool["function"]["name"] == "text_to_speech"
    )
    assert str(MAX_TTS_INPUT_CHARS) in definition["function"]["description"]


def main():
    run_suite(
        [
            test_over_limit_text_is_rejected_before_synthesis,
            test_under_limit_text_reaches_synthesis,
            test_docstring_states_the_configured_limit,
        ]
    )


if __name__ == "__main__":
    main()
