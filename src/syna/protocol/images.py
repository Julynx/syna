"""Image payloads attached to the conversation history.

The `inspect_image` tool does not run a separate vision request; it
attaches the image to the current session's history so the model sees
it directly. This module owns the two pieces of that mechanism that
more than one consumer needs: the token-cost estimate of an attached
image (context budgeting) and the redaction of image data URLs from
serialized text (session dumps and the work reviewer's log).
"""

import math
import re

# Heuristic pixel budget per token: 750 px/token lands near what
# providers charge for a downscaled 1024x1024 image and errs on the
# high side, the safe direction for elision and compaction timing.
# The estimate only drives budget decisions, so a provider's exact
# tiling rule does not matter.
PIXELS_PER_IMAGE_TOKEN = 750

DATA_URL_PATTERN = re.compile(r"data:image/[^\";]*;base64,[A-Za-z0-9+/=]+")


def image_token_cost(width: int, height: int) -> int:
    """Estimate the token cost of one attached image from its dimensions."""
    return max(1, math.ceil(width * height / PIXELS_PER_IMAGE_TOKEN))


def redact_image_data(text: str) -> str:
    """Replace image data URLs in serialized text with a size placeholder.

    Operates on JSON-serialized payloads: base64 carries no characters
    JSON escapes, so the URL survives serialization verbatim and a regex
    over the serialized text is exact. A data URL left in place would
    balloon every diagnostic dump by a megabyte per image.
    """

    def _placeholder(match: re.Match) -> str:
        return f"[image data redacted: {len(match.group(0))} chars]"

    return DATA_URL_PATTERN.sub(_placeholder, text)
