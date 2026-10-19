"""System and agent communication channel separation using Unicode sentinels.

To prevent the model from simulating system messages, fabricating tool outputs,
or forging compaction summaries, all system-originated messages are wrapped in
dedicated sentinel markers from the Unicode Private Use Area (U+E000 to U+E0FF).
Any assistant reply attempting to emit these sentinel characters—either raw or
as unicode escape sequences—is rejected by a sanitization gate.
"""

import re

SENTINEL_OPEN = "\uE000"
SENTINEL_CLOSE = "\uE001"
CHANNEL_PREFIX = f"{SENTINEL_OPEN}SYNA:"

SENTINEL_PATTERN = re.compile(r"[\uE000-\uE0FF]|\\[uU][eE]0[0-9a-fA-F]{2}")
CHANNEL_HEADER_PATTERN = re.compile(
    rf"^{re.escape(SENTINEL_OPEN)}SYNA:([A-Z_]+){re.escape(SENTINEL_CLOSE)}\n"
)


def wrap_channel(kind: str, body: str) -> str:
    """Wrap content in system sentinel markers with a specific channel kind.

    Parameters:
        kind: Channel identifier such as TOOL_RESULTS, SYSTEM_NOTE, or COMPACTION.
        body: Plain text or JSON payload of the message.
    """
    normalized_kind = kind.strip().upper()
    return f"{CHANNEL_PREFIX}{normalized_kind}{SENTINEL_CLOSE}\n{body}"


def unwrap_channel(content: str) -> tuple[str, str] | None:
    """Extract the channel kind and body from a sentinel-wrapped message.

    Returns:
        A tuple of (kind, body) if the content is a valid channel message,
        or None if it does not use the sentinel framing.
    """
    match = CHANNEL_HEADER_PATTERN.match(content)
    if not match:
        return None
    kind = match.group(1)
    body = content[match.end() :]
    return kind, body


def is_channel_message(content: str, kind: str | None = None) -> bool:
    """Check whether a message content is wrapped in the system channel.

    Parameters:
        content: Message text to inspect.
        kind: Optional specific channel kind to check against.
    """
    unwrapped = unwrap_channel(content)
    if unwrapped is None:
        return False
    if kind is None:
        return True
    return unwrapped[0] == kind.strip().upper()


def contains_sentinel(text: str) -> bool:
    """Return True if text contains raw or escaped Private Use Area sentinels."""
    return bool(SENTINEL_PATTERN.search(text))


def defang_sentinels(text: str) -> str:
    """Replace raw and escaped sentinel characters with safe placeholder text."""
    return SENTINEL_PATTERN.sub("[FORBIDDEN_SENTINEL]", text)
