"""Image generation through the OpenRouter Image API.

A text prompt is posted to the dedicated /api/v1/images endpoint and the
first generated image is returned as decoded bytes together with the media
type reported by the provider. The API bills all-or-nothing: a failed
generation produces a 502 response and no charge, so error messages are
safe to surface to the model verbatim. The model is configured in the .env
file (IMAGE_MODEL) and falls back to the bundled default; the feature is
gated by the image_generation_enabled config toggle, off by default.
"""

import base64
from dataclasses import dataclass

import httpx

from .config import (
    DEFAULT_IMAGE_GENERATION_ENABLED,
    DEFAULT_IMAGE_MODEL,
    get_env,
    get_logger,
    load_config,
)

IMAGE_ENDPOINT = "https://openrouter.ai/api/v1/images"

EXTENSION_BY_MEDIA_TYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/svg+xml": "svg",
}


@dataclass(frozen=True)
class ImageSettings:
    """Image generation model read from the .env file."""

    model: str | None


def load_image_settings() -> ImageSettings:
    """Build the image settings from the environment variables in .env.

    Image generation is an OpenRouter-only route; on any other endpoint
    the tool reports itself as not configured. An unset IMAGE_MODEL falls
    back to the default model; the feature itself is gated separately by
    the image_generation_enabled config toggle.
    """
    env = get_env()
    model = (env.get("IMAGE_MODEL") or "").strip() or DEFAULT_IMAGE_MODEL
    from .session import is_openrouter  # noqa: PLC0415

    if not is_openrouter():
        model = None
    return ImageSettings(model=model)


def image_generation_enabled() -> bool:
    """Whether the user enabled image generation in the settings.

    Off by default: generation is expensive and model-initiated, so it is
    strictly opt-in.
    """
    return bool(
        load_config().get("image_generation_enabled", DEFAULT_IMAGE_GENERATION_ENABLED)
    )


class ImageGenerationError(RuntimeError):
    """A failed image generation request; the message is safe to show to the user."""


@dataclass(frozen=True)
class GeneratedImage:
    """Generated image bytes plus the media type reported by the provider."""

    content: bytes
    media_type: str

    @property
    def extension(self) -> str:
        """File extension matching the image's media type."""
        return EXTENSION_BY_MEDIA_TYPE.get(self.media_type, "png")


def generate_image(prompt: str, settings: ImageSettings) -> GeneratedImage:
    """Generate one image from a text prompt through the Image API."""
    if not settings.model:
        raise ImageGenerationError("IMAGE_MODEL is not set in the .env file.")
    api_key = get_env().get("OPENROUTER_API_KEY")
    if not api_key:
        raise ImageGenerationError("OPENROUTER_API_KEY is not set in the .env file.")
    timeout_s = load_config().get("timeout_ms", 120000) / 1000
    try:
        response = httpx.post(
            IMAGE_ENDPOINT,
            json={"model": settings.model, "prompt": prompt},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
        )
    except httpx.HTTPError as exc:
        get_logger().exception("OpenRouter image request to %s failed", IMAGE_ENDPOINT)
        raise ImageGenerationError(f"Could not reach OpenRouter: {exc}") from exc
    if response.status_code != 200:
        detail = response.text[:300]
        get_logger().error(
            "OpenRouter image request failed (%s): %s",
            response.status_code,
            detail,
        )
        raise ImageGenerationError(
            f"OpenRouter rejected the image request ({response.status_code}): {detail}"
        )
    try:
        data = response.json()["data"]
        first = data[0]
        if not isinstance(first, dict):
            raise TypeError(f"image entry is {type(first).__name__}, not an object")
        content = base64.b64decode(first["b64_json"])
        media_type = first.get("media_type") or "image/png"
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        get_logger().exception("Malformed image response from OpenRouter")
        raise ImageGenerationError(
            "OpenRouter returned a malformed image response."
        ) from exc
    get_logger().info(
        "Generated %d bytes of %s image from %d chars of prompt",
        len(content),
        media_type,
        len(prompt),
    )
    return GeneratedImage(content=content, media_type=media_type)
