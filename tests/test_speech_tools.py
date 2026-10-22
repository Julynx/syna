"""Unit tests for the speech_to_text, text_to_speech, and text_to_image tools."""

import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _runner import run_suite
from bollard import DockerException

from syna import tools
from syna.image_generation import GeneratedImage, ImageSettings
from syna.speech import SynthesizedAudio

FAKE_WAV = b"RIFFfake-wav-data"


class FakeContainer:
    """Records pod I/O and materializes copied files instead of using a pod."""

    def __init__(self):
        self.exec_calls = []
        self.copied = []
        self.pod_files = {}

    def exec(self, argv):
        self.exec_calls.append(argv)
        return ""

    def copy_from(self, source, destination):
        name = Path(source).name
        if name not in self.pod_files:
            raise DockerException(
                f"Docker API Error (404): no such file or directory {source}"
            )
        Path(destination, name).write_bytes(self.pod_files[name])

    def copy_to(self, source, destination):
        self.copied.append((source, destination))
        Path(destination).mkdir(parents=True, exist_ok=True)
        Path(destination, Path(source).name).write_bytes(Path(source).read_bytes())


def _copied_bytes(container) -> bytes:
    """Return the bytes of the first file the fake pod received."""
    source, destination = container.copied[0]
    return Path(destination, Path(source).name).read_bytes()


def _pod_path(result: str) -> str:
    """Extract the first /workspace/... path from a tool result string."""
    return re.search(r"/workspace/[\w./-]+", result).group(0)


FAKE_SETTINGS = SimpleNamespace(
    stt_available=True,
    tts_available=True,
    tts_voice="Kore",
    tts_response_format="wav",
)


def _configure(container, monkeypatch, **overrides):
    """Point the tools at a fake pod and fake API functions."""
    monkeypatch.setattr(tools, "get_container", lambda: container)

    if "transcript" in overrides:
        monkeypatch.setattr(tools, "load_speech_settings", lambda: FAKE_SETTINGS)
        monkeypatch.setattr(tools, "voice_enabled", lambda: True)
        monkeypatch.setattr(
            tools,
            "transcribe_audio",
            lambda audio, fmt, settings, language=None: overrides["transcript"],
        )
    if "audio" in overrides:
        monkeypatch.setattr(tools, "load_speech_settings", lambda: FAKE_SETTINGS)
        monkeypatch.setattr(tools, "voice_enabled", lambda: True)
        monkeypatch.setattr(
            tools, "synthesize_speech", lambda text, settings: overrides["audio"]
        )
    if "image" in overrides:
        monkeypatch.setattr(
            tools, "load_image_settings", lambda: ImageSettings(model="test-model")
        )
        monkeypatch.setattr(tools, "image_generation_enabled", lambda: True)
        monkeypatch.setattr(
            tools, "generate_image", lambda prompt, settings: overrides["image"]
        )
    return container


def test_speech_to_text_saves_transcript(monkeypatch):
    container = _configure(FakeContainer(), monkeypatch, transcript="hello world")
    container.pod_files["recording.wav"] = FAKE_WAV

    result = tools.speech_to_text("/workspace/recording.wav")

    assert "hello world" in result
    pod_path = _pod_path(result)
    assert pod_path.startswith("/workspace/transcripts/transcript-")
    assert pod_path.endswith(".txt")
    written = [source for source, _ in container.copied]
    assert len(written) == 1
    assert _copied_bytes(container) == b"hello world"


def test_speech_to_text_rejects_unknown_extension(monkeypatch):
    container = _configure(FakeContainer(), monkeypatch, transcript="ignored")
    container.pod_files["song.xyz"] = b"not-really-audio"

    result = tools.speech_to_text("/workspace/song.xyz")

    assert result["error"].startswith("Unsupported audio file")
    assert "wav" in result["error"]
    assert container.copied == []


def test_speech_to_text_reports_api_errors(monkeypatch):
    container = _configure(FakeContainer(), monkeypatch, transcript="ignored")
    container.pod_files["clip.mp3"] = FAKE_WAV

    def fail(audio_bytes, audio_format, settings, language=None):
        raise tools.SpeechError(
            "OpenRouter rejected the speech request (400): bad format"
        )

    monkeypatch.setattr(tools, "transcribe_audio", fail)
    result = tools.speech_to_text("/workspace/clip.mp3")

    assert "rejected the speech request (400)" in result["error"]
    assert "bad format" in result["error"]


def test_text_to_speech_saves_audio(monkeypatch):
    audio = SynthesizedAudio(content=FAKE_WAV, media_type="audio/wav")
    container = _configure(FakeContainer(), monkeypatch, audio=audio)
    container.pod_files["story.txt"] = b"once upon a time"

    result = tools.text_to_speech("/workspace/story.txt")

    pod_path = _pod_path(result)
    assert pod_path.startswith("/workspace/audio/speech-")
    assert pod_path.endswith(".wav")
    assert "share_file" in result
    written = [source for source, _ in container.copied]
    assert _copied_bytes(container) == FAKE_WAV


def test_text_to_speech_uses_media_type_extension(monkeypatch):
    audio = SynthesizedAudio(content=b"mp3-bytes", media_type="audio/mpeg")
    container = _configure(FakeContainer(), monkeypatch, audio=audio)
    container.pod_files["story.txt"] = b"text"

    pod_path = _pod_path(tools.text_to_speech("/workspace/story.txt"))

    assert pod_path.endswith(".mp3")


def test_text_to_image_saves_image(monkeypatch):
    image = GeneratedImage(content=b"\x89PNG-bytes", media_type="image/png")
    container = _configure(FakeContainer(), monkeypatch, image=image)

    result = tools.text_to_image("a red panda astronaut")

    pod_path = _pod_path(result)
    assert pod_path.startswith("/workspace/images/generated-")
    assert pod_path.endswith(".png")
    assert "describe_image" in result
    written = [source for source, _ in container.copied]
    assert _copied_bytes(container) == b"\x89PNG-bytes"


def test_text_to_image_without_config_reports_user_configuration(monkeypatch):
    container = FakeContainer()
    monkeypatch.setattr(tools, "get_container", lambda: container)
    monkeypatch.setattr(
        tools, "load_image_settings", lambda: ImageSettings(model="test-model")
    )
    monkeypatch.setattr(tools, "image_generation_enabled", lambda: False)

    result = tools.text_to_image("a cat")

    assert "disabled by the user" in result["error"]
    assert "You did nothing wrong" in result["error"]
    assert "Images category" in result["error"]
    assert container.copied == []


def test_text_to_image_requires_openrouter_endpoint(monkeypatch):
    container = FakeContainer()
    monkeypatch.setattr(tools, "get_container", lambda: container)
    monkeypatch.setattr(
        tools, "load_image_settings", lambda: ImageSettings(model=None)
    )
    monkeypatch.setattr(tools, "image_generation_enabled", lambda: True)

    result = tools.text_to_image("a cat")

    assert "requires an OpenRouter endpoint" in result["error"]
    assert container.copied == []


def test_text_to_image_reports_api_errors(monkeypatch):
    container = _configure(FakeContainer(), monkeypatch, image=b"unused")

    def fail(prompt, settings):
        raise tools.ImageGenerationError(
            "OpenRouter rejected the image request (502): provider error"
        )

    monkeypatch.setattr(tools, "generate_image", fail)
    result = tools.text_to_image("a cat")

    assert "rejected the image request (502)" in result["error"]
    assert container.copied == []


def test_missing_pod_file_is_a_clean_error(monkeypatch):
    container = _configure(FakeContainer(), monkeypatch, transcript="ignored")

    result = tools.speech_to_text("/workspace/nothing.wav")

    assert "No such file or directory" in result["error"]


def test_artifact_names_are_unique(monkeypatch):
    audio = SynthesizedAudio(content=FAKE_WAV, media_type="audio/wav")
    container = _configure(FakeContainer(), monkeypatch, audio=audio)
    container.pod_files["story.txt"] = b"text"

    first = _pod_path(tools.text_to_speech("/workspace/story.txt"))
    second = _pod_path(tools.text_to_speech("/workspace/story.txt"))

    assert first != second


def test_speech_to_text_passes_language_through(monkeypatch):
    captured_languages = []

    def record(audio_bytes, audio_format, settings, language):
        captured_languages.append(language)
        return "transcript"

    container = FakeContainer()
    container.pod_files["clip.wav"] = FAKE_WAV
    monkeypatch.setattr(tools, "get_container", lambda: container)
    monkeypatch.setattr(tools, "load_speech_settings", lambda: FAKE_SETTINGS)
    monkeypatch.setattr(tools, "transcribe_audio", record)

    tools.speech_to_text("/workspace/clip.wav")
    tools.speech_to_text("/workspace/clip.wav", language="de")

    assert captured_languages == [None, "de"]


def test_transcribe_audio_omits_language_when_auto(monkeypatch):
    from syna import speech

    captured_payloads = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"text": "hi"}

    def fake_post(endpoint, json=None, headers=None, timeout=None):
        captured_payloads.append(json)
        return FakeResponse()

    monkeypatch.setattr(speech.httpx, "post", fake_post)
    settings = speech.SpeechSettings(
        stt_model="m",
        tts_model=None,
        tts_voice="Kore",
        tts_response_format="pcm",
        language="en",
    )

    speech.transcribe_audio(b"x", "wav", settings)
    speech.transcribe_audio(b"x", "wav", settings, language="de")

    assert "language" not in captured_payloads[0]
    assert captured_payloads[1]["language"] == "de"


def test_text_to_image_malformed_response_is_a_clean_error(monkeypatch):
    from syna import image_generation

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": ["not-an-object"]}

    monkeypatch.setattr(
        image_generation.httpx,
        "post",
        lambda endpoint, json=None, headers=None, timeout=None: FakeResponse(),
    )
    container = FakeContainer()
    monkeypatch.setattr(tools, "get_container", lambda: container)
    monkeypatch.setattr(
        tools, "load_image_settings", lambda: ImageSettings(model="test-model")
    )
    monkeypatch.setattr(tools, "image_generation_enabled", lambda: True)

    result = tools.text_to_image("a cat")

    assert "malformed image response" in result["error"]
    assert container.copied == []


def test_svg_result_omits_describe_image_hint(monkeypatch):
    image = GeneratedImage(content=b"<svg/>", media_type="image/svg+xml")
    _configure(FakeContainer(), monkeypatch, image=image)

    result = tools.text_to_image("a logo")

    assert "describe_image" not in result
    assert "share_file" in result


def main():
    """Run every speech and image tool test and report a summary."""
    run_suite(
        [
            test_speech_to_text_saves_transcript,
            test_speech_to_text_rejects_unknown_extension,
            test_speech_to_text_reports_api_errors,
            test_text_to_speech_saves_audio,
            test_text_to_speech_uses_media_type_extension,
            test_text_to_image_saves_image,
            test_text_to_image_without_config_reports_user_configuration,
            test_text_to_image_requires_openrouter_endpoint,
            test_text_to_image_reports_api_errors,
            test_missing_pod_file_is_a_clean_error,
            test_artifact_names_are_unique,
            test_speech_to_text_passes_language_through,
            test_transcribe_audio_omits_language_when_auto,
            test_text_to_image_malformed_response_is_a_clean_error,
            test_svg_result_omits_describe_image_hint,
        ]
    )


if __name__ == "__main__":
    main()
