"""Speech transcription and synthesis through the OpenRouter audio endpoints.

Transcription posts a base64-encoded clip to the audio transcriptions
endpoint and returns its text. Synthesis posts a passage of text to the
audio speech endpoint and returns raw MP3 bytes. Both endpoints are
OpenAI-compatible REST calls driven by the models configured in the .env
file (STT_MODEL / TTS_MODEL).
"""

import base64
from dataclasses import dataclass

import httpx

from .config import get_env, get_logger, load_config

TRANSCRIPTIONS_ENDPOINT = "https://openrouter.ai/api/v1/audio/transcriptions"
SPEECH_ENDPOINT = "https://openrouter.ai/api/v1/audio/speech"

DEFAULT_TTS_VOICE = "Kore"
DEFAULT_SPEECH_LANGUAGE = "en"
DEFAULT_SILENCE_TIMEOUT_MS = 2000
DEFAULT_SILENCE_THRESHOLD_DBFS = -45
DEFAULT_MAX_RECORDING_MS = 60000

AUDIO_FORMATS = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/flac": "flac",
    "audio/aac": "aac",
}


@dataclass(frozen=True)
class SpeechSettings:
    """Speech models and locale read from the .env file."""

    stt_model: str | None
    tts_model: str | None
    tts_voice: str
    language: str

    @property
    def stt_available(self) -> bool:
        """Whether a transcription model is configured."""
        return bool(self.stt_model)

    @property
    def tts_available(self) -> bool:
        """Whether a synthesis model is configured."""
        return bool(self.tts_model)


def load_speech_settings() -> SpeechSettings:
    """Build the speech settings from the environment variables in .env."""
    env = get_env()
    return SpeechSettings(
        stt_model=(env.get("STT_MODEL") or "").strip() or None,
        tts_model=(env.get("TTS_MODEL") or "").strip() or None,
        tts_voice=(env.get("TTS_VOICE") or "").strip() or DEFAULT_TTS_VOICE,
        language=(env.get("SPEECH_LANGUAGE") or "").strip() or DEFAULT_SPEECH_LANGUAGE,
    )


def audio_format_for(content_type: str | None) -> str | None:
    """Map an uploaded clip's MIME type to an OpenRouter audio format."""
    if not content_type:
        return None
    return AUDIO_FORMATS.get(content_type.split(";")[0].strip().lower())


class SpeechError(RuntimeError):
    """A failed speech request; the message is safe to show to the user."""


def _post_to_openrouter(endpoint: str, payload: dict) -> httpx.Response:
    """POST a JSON payload to one audio endpoint and validate the response."""
    api_key = get_env().get("OPENROUTER_API_KEY")
    if not api_key:
        raise SpeechError("OPENROUTER_API_KEY is not set in the .env file.")
    timeout_s = load_config().get("timeout_ms", 120000) / 1000
    try:
        response = httpx.post(
            endpoint,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
        )
    except httpx.HTTPError as exc:
        get_logger().exception("OpenRouter speech request to %s failed", endpoint)
        raise SpeechError(f"Could not reach OpenRouter: {exc}") from exc
    if response.status_code != 200:
        detail = response.text[:300]
        get_logger().error(
            "OpenRouter speech request failed (%s): %s",
            response.status_code,
            detail,
        )
        raise SpeechError(
            f"OpenRouter rejected the speech request ({response.status_code}): {detail}"
        )
    return response


def transcribe_audio(
    audio_bytes: bytes, audio_format: str, settings: SpeechSettings
) -> str:
    """Transcribe one recorded clip through the configured STT model."""
    if not settings.stt_available:
        raise SpeechError("STT_MODEL is not set in the .env file.")
    response = _post_to_openrouter(
        TRANSCRIPTIONS_ENDPOINT,
        {
            "model": settings.stt_model,
            "input_audio": {
                "data": base64.b64encode(audio_bytes).decode("ascii"),
                "format": audio_format,
            },
            "language": settings.language,
        },
    )
    try:
        text = response.json()["text"]
    except (KeyError, TypeError, ValueError) as exc:
        get_logger().exception("Malformed transcription response from OpenRouter")
        raise SpeechError("OpenRouter returned a malformed transcription.") from exc
    get_logger().info(
        "Transcribed %d bytes of %s audio into %d chars",
        len(audio_bytes),
        audio_format,
        len(text),
    )
    return text


def synthesize_speech(text: str, settings: SpeechSettings) -> bytes:
    """Synthesize one passage of spoken audio through the configured TTS model."""
    if not settings.tts_available:
        raise SpeechError("TTS_MODEL is not set in the .env file.")
    response = _post_to_openrouter(
        SPEECH_ENDPOINT,
        {
            "model": settings.tts_model,
            "input": text,
            "voice": settings.tts_voice,
            "response_format": "mp3",
        },
    )
    get_logger().info(
        "Synthesized %d chars into %d bytes of speech", len(text), len(response.content)
    )
    return response.content


def client_configuration(settings: SpeechSettings, app_config: dict) -> dict:
    """Collect the settings the web client needs to run voice mode."""
    voice_config = app_config.get("voice", {})
    return {
        "stt_available": settings.stt_available,
        "tts_available": settings.tts_available,
        "language": settings.language,
        "silence_timeout_ms": voice_config.get(
            "silence_timeout_ms", DEFAULT_SILENCE_TIMEOUT_MS
        ),
        "silence_threshold_dbfs": voice_config.get(
            "silence_threshold_dbfs", DEFAULT_SILENCE_THRESHOLD_DBFS
        ),
        "max_recording_ms": voice_config.get(
            "max_recording_ms", DEFAULT_MAX_RECORDING_MS
        ),
    }
