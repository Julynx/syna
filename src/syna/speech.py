"""Speech transcription and synthesis through the OpenRouter audio endpoints.

Transcription posts a base64-encoded clip to the audio transcriptions
endpoint and returns its text. Synthesis posts a passage of text to the
audio speech endpoint and returns the synthesized audio together with the
MIME type the browser should receive. Both endpoints are OpenAI-compatible
REST calls driven by the models configured in the .env file
(STT_MODEL / TTS_MODEL / TTS_RESPONSE_FORMAT).

The default TTS response format is 'pcm': Gemini TTS models only accept
it, and wrap_pcm_in_wav makes the raw samples playable in every browser.
"""

import base64
import io
import wave
from dataclasses import dataclass

import httpx

from .config import get_env, get_logger, load_config

TRANSCRIPTIONS_ENDPOINT = "https://openrouter.ai/api/v1/audio/transcriptions"
SPEECH_ENDPOINT = "https://openrouter.ai/api/v1/audio/speech"

DEFAULT_TTS_VOICE = "Kore"
DEFAULT_TTS_RESPONSE_FORMAT = "pcm"
DEFAULT_SPEECH_LANGUAGE = "en"
DEFAULT_SILENCE_TIMEOUT_MS = 2000
DEFAULT_SILENCE_THRESHOLD_DBFS = -45
DEFAULT_MAX_RECORDING_MS = 60000

PCM_SAMPLE_RATE_HZ = 24000
PCM_SAMPLE_WIDTH_BYTES = 2
PCM_CHANNELS = 1

MEDIA_TYPES_BY_FORMAT = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "pcm": "audio/wav",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
}

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
    tts_response_format: str
    language: str

    @property
    def stt_available(self) -> bool:
        """Whether a transcription model is configured."""
        return bool(self.stt_model)

    @property
    def tts_available(self) -> bool:
        """Whether a synthesis model is configured."""
        return bool(self.tts_model)


@dataclass(frozen=True)
class SynthesizedAudio:
    """Synthesized speech bytes plus the MIME type to serve them with."""

    content: bytes
    media_type: str


def load_speech_settings() -> SpeechSettings:
    """Build the speech settings from the environment variables in .env."""
    env = get_env()
    return SpeechSettings(
        stt_model=(env.get("STT_MODEL") or "").strip() or None,
        tts_model=(env.get("TTS_MODEL") or "").strip() or None,
        tts_voice=(env.get("TTS_VOICE") or "").strip() or DEFAULT_TTS_VOICE,
        tts_response_format=(
            env.get("TTS_RESPONSE_FORMAT") or ""
        ).strip().lower()
        or DEFAULT_TTS_RESPONSE_FORMAT,
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


def wrap_pcm_in_wav(pcm_bytes: bytes, sample_rate_hz: int) -> bytes:
    """Wrap raw 16-bit mono PCM samples in a RIFF/WAVE container.

    The OpenAI-compatible speech endpoint returns headerless samples for
    response_format 'pcm', which browsers cannot play directly; the WAV
    header gives them the sample rate and bit depth they need. A response
    that already carries a RIFF header is returned unchanged.
    """
    if pcm_bytes[:4] == b"RIFF":
        return pcm_bytes
    container = io.BytesIO()
    with wave.open(container, "wb") as wav_file:
        wav_file.setnchannels(PCM_CHANNELS)
        wav_file.setsampwidth(PCM_SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(sample_rate_hz)
        wav_file.writeframes(pcm_bytes)
    return container.getvalue()


def synthesize_speech(text: str, settings: SpeechSettings) -> SynthesizedAudio:
    """Synthesize one passage of spoken audio through the configured TTS model."""
    if not settings.tts_available:
        raise SpeechError("TTS_MODEL is not set in the .env file.")
    audio_format = settings.tts_response_format
    response = _post_to_openrouter(
        SPEECH_ENDPOINT,
        {
            "model": settings.tts_model,
            "input": text,
            "voice": settings.tts_voice,
            "response_format": audio_format,
        },
    )
    content = response.content
    if audio_format == "pcm":
        content = wrap_pcm_in_wav(content, PCM_SAMPLE_RATE_HZ)
    media_type = MEDIA_TYPES_BY_FORMAT.get(
        audio_format, f"audio/{audio_format}"
    )
    get_logger().info(
        "Synthesized %d chars into %d bytes of %s speech",
        len(text),
        len(content),
        audio_format,
    )
    return SynthesizedAudio(content=content, media_type=media_type)


def voice_flag(value) -> bool:
    """Interpret a voice_mode_on config value as a boolean.

    Accepts real booleans and the strings "true"/"false" (any casing, so a
    hand-edited config.json cannot accidentally enable voice mode), and
    defaults to off for anything else.
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def client_configuration(settings: SpeechSettings, app_config: dict) -> dict:
    """Collect the settings the web client needs to run voice mode."""
    voice_config = app_config.get("voice", {})
    return {
        "stt_available": settings.stt_available,
        "tts_available": settings.tts_available,
        "voice_mode_on": voice_flag(voice_config.get("voice_mode_on")),
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
