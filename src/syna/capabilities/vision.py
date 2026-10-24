"""Image understanding through a dedicated single-turn vision request."""

import base64
import tempfile
from pathlib import Path
from posixpath import basename

from PIL import Image

from ..ask_utils import ask_question
from ..config import DEFAULT_MODEL, get_env, get_logger
from .imaging import fit_image_to_box
from ..state import get_container


def _load_model():
    """Return the vision model, falling back to the default main model."""
    return get_env().get("MODEL") or DEFAULT_MODEL


def describe_image(file_path: str, question: str):
    """Describe one image from the pod through a dedicated vision request.

    Pipeline: the image is copied from the container to a temporary host
    folder (same mechanism as 'read_file'/'edit_file'), converted to PNG,
    downscaled with Pillow's 'thumbnail' so it fits inside a 1024x1024
    box, base64-encoded and sent to the model as a separate session: a
    fresh client and a throwaway single-turn conversation. The request
    runs at 'medium' reasoning effort: without it the provider default is
    maximum, which burns the completion budget on hidden reasoning and
    can return no content at all.

    Once the image has been described, its base64 payload is replaced in
    the request's messages array by a compact text placeholder carrying
    the image path and the description, so the bytes never linger in any
    message history. The temporary folder is removed whether the call
    succeeds or fails. Returns the description text, or an {'error': ...}
    dict when the image cannot be read.
    """
    logger = get_logger()
    model = _load_model()
    if not model:
        return {"error": "MODEL is not set in the .env file; cannot describe images."}

    with tempfile.TemporaryDirectory(prefix="syna-vision-") as temp_dir:
        temp_path = Path(temp_dir)

        container = get_container()
        container.copy_from(file_path, str(temp_path))
        source_path = temp_path / basename(file_path)
        png_path = temp_path / f"{source_path.stem or 'image'}.png"

        try:
            fit_image_to_box(source_path, png_path)
            with Image.open(png_path) as png:
                pixel_count = png.width * png.height
        except Exception as exc:
            return {"error": f"Could not read '{file_path}' as an image: {exc}"}

        logger.info(
            "Vision request for '%s': %d pixels, %d bytes of PNG payload",
            file_path,
            pixel_count,
            png_path.stat().st_size,
        )
        encoded = base64.b64encode(png_path.read_bytes()).decode("ascii")
        image_data = f"data:image/png;base64,{encoded}"
        prompt = (
            f"Describe this image in detail. Pay particular attention to: {question}"
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data}},
                ],
            }
        ]

        from ..conversation.session import create_model_client  # noqa: PLC0415

        with create_model_client() as client:
            response_msg = ask_question(
                client, model, messages, delay=0, response_format=None, effort="medium"
            )
            description = response_msg.get("content") or ""

    logger.info(
        "Described image '%s' (%d chars of description)",
        file_path,
        len(description),
    )
    return description
