"""Image processing shared by the vision pipeline and inline image serving.

The web client renders images that live inside the pod by fetching them
through the server; oversized or heavy sources are normalized here so the
browser receives a bounded, decodable payload regardless of what the pod
contains.
"""

from pathlib import Path

from PIL import Image

MAX_IMAGE_BOX = (1024, 1024)
RECOMPRESS_BYTES = 5 * 1024 * 1024
MAX_SERVE_BYTES = 50 * 1024 * 1024


def image_exceeds_box(
    img: Image.Image, max_box: tuple[int, int] = MAX_IMAGE_BOX
) -> bool:
    """Whether an open Pillow image does not fit inside max_box."""
    return img.width > max_box[0] or img.height > max_box[1]


def fit_image_to_box(
    source_path: Path, target_path: Path, max_box: tuple[int, int] = MAX_IMAGE_BOX
) -> None:
    """Write a PNG copy of the source image downscaled into max_box.

    CMYK sources are converted to RGB first; other modes (including alpha)
    survive the PNG round-trip. The source must be readable by Pillow.
    """
    with Image.open(source_path) as img:
        converted = img.convert("RGB") if img.mode == "CMYK" else img
        converted.thumbnail(max_box)
        converted.save(target_path, format="PNG")
