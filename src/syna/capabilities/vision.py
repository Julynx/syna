"""Image inspection by attaching images to the current session's history.

`inspect_image` copies an image out of the pod, converts it to PNG,
downscales it with Pillow so it fits inside a 1024x1024 box, and hands
it to the harness, which appends it to the conversation as a message
carrying the image data: the model looks at the image itself in its
next reply instead of reading a second-hand description from a
throwaway vision session. The image stays in history until compaction
summarizes it away; re-inspecting the same path attaches a fresh copy,
which is correct when the file changed and cheap when it did not.

The tool returns a dict with a short `note` for the tool result and
the `image` payload for the harness; `parse.py` extracts the payload
into the call entry and `session.py` builds the history message from
it, so the base64 data never passes through tool-result text, UI
events, or logs.
"""

import base64
import tempfile
from pathlib import Path
from posixpath import basename

from PIL import Image

from ..config import (
    DEFAULT_MODEL,
    get_env,
    get_logger,
)
from ..pod_io import pod_copy_from
from ..protocol.images import image_token_cost
from ..state import get_container
from .imaging import fit_image_to_box


def _main_model() -> str | None:
    """Return the session's main model id."""
    return get_env().get("MODEL") or DEFAULT_MODEL


def inspect_image(file_path: str):
    """Attach one image from the pod to the conversation history.

    The image is downscaled to fit a 1024x1024 box (same behavior the
    old single-turn vision request had) and base64-encoded. When the
    models list has proven the configured model cannot accept images,
    the call fails with an error dict before touching the pod, so the
    turn survives; unknown support (fetch pending, failed, or a
    non-OpenRouter endpoint) proceeds optimistically. Returns the note
    and image payload dict, or an {'error': ...} dict when the model
    cannot see images or the image cannot be read.
    """
    logger = get_logger()
    model = _main_model()
    if not model:
        return {"error": "MODEL is not set in the .env file; cannot inspect images."}

    from ..conversation.session import model_accepts_images  # noqa: PLC0415

    if model_accepts_images(model) is False:
        return {
            "error": (
                f"The configured model '{model}' does not accept image input;"
                " it cannot inspect images. Extract the information with text"
                " tools instead, or tell the user a vision-capable model is"
                " needed."
            )
        }

    with tempfile.TemporaryDirectory(prefix="syna-vision-") as temp_dir:
        temp_path = Path(temp_dir)

        container = get_container()
        pod_copy_from(container, file_path, str(temp_path))
        source_path = temp_path / basename(file_path)
        png_path = temp_path / f"{source_path.stem or 'image'}.png"

        try:
            fit_image_to_box(source_path, png_path)
            with Image.open(png_path) as png:
                width, height = png.width, png.height
        except Exception as exc:
            return {"error": f"Could not read '{file_path}' as an image: {exc}"}

        encoded = base64.b64encode(png_path.read_bytes()).decode("ascii")

    tokens = image_token_cost(width, height)
    logger.info(
        "Attached image '%s' to the session: %dx%d, %d bytes of PNG payload,"
        " ~%d tokens",
        file_path,
        width,
        height,
        len(encoded) * 3 // 4,
        tokens,
    )
    return {
        "note": (
            f"Image attached to the conversation ({width}x{height});"
            " you can see it now."
        ),
        "image": {
            "path": file_path,
            "width": width,
            "height": height,
            "data_url": f"data:image/png;base64,{encoded}",
            "image_tokens": tokens,
        },
    }
