"""System and agent communication channel separation using Unicode sentinels.

To prevent the model from simulating system messages, fabricating tool outputs,
or forging compaction summaries, all system-originated messages are wrapped in
dedicated sentinel markers from the Unicode Private Use Area (U+E000 to U+E0FF).
Any assistant reply attempting to emit these sentinel characters—either raw or
as unicode escape sequences—is rejected by a sanitization gate.
"""

import re

SENTINEL_OPEN = "\ue000"
SENTINEL_CLOSE = "\ue001"
CHANNEL_PREFIX = f"{SENTINEL_OPEN}SYNA:"

SENTINEL_PATTERN = re.compile(r"[\uE000-\uE0FF]|\\[uU][eE]0[0-9a-fA-F]{2}")
CHANNEL_HEADER_PATTERN = re.compile(
    rf"^{re.escape(SENTINEL_OPEN)}SYNA:([A-Z_]+){re.escape(SENTINEL_CLOSE)}\n"
)


def wrap_channel(kind: str, body: str) -> str:
    """Wrap content in system sentinel markers with a specific channel kind.

    Parameters:
        kind: Channel identifier such as TOOL_RESULTS, HARNESS_NOTICE, or
        COMPACTION.
        body: Plain text or JSON payload of the message.
    """
    normalized_kind = kind.strip().upper()
    return f"{CHANNEL_PREFIX}{normalized_kind}{SENTINEL_CLOSE}\n{body}"


def unwrap_channel(content: str) -> tuple[str, str] | None:
    """Extract the channel kind and body from a sentinel-wrapped message.

    Returns:
        A tuple of (kind, body) if the content is a valid channel message,
        or None if it does not use the sentinel framing. None content
        (tool-call-only assistant replies) is never a channel message.
    """
    if not content:
        return None
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


def contains_sentinel(text: str | None) -> bool:
    """Return True if text contains raw or escaped Private Use Area sentinels.

    Assistant messages may legitimately carry None content (tool-call-only
    replies), which simply contain no sentinels.
    """
    if not text:
        return False
    return bool(SENTINEL_PATTERN.search(text))


def defang_sentinels(text: str | None) -> str:
    """Replace raw and escaped sentinel characters with safe placeholder text."""
    if not text:
        return ""
    return SENTINEL_PATTERN.sub("[FORBIDDEN_SENTINEL]", text)


HARNESS_NOTICE_OPEN = "<harness_notice>"
HARNESS_NOTICE_CLOSE = "</harness_notice>"

IMPERSONATABLE_MARKERS = (
    "<tool_results",
    "</tool_results",
    "<result",
    "</result",
    HARNESS_NOTICE_OPEN,
    HARNESS_NOTICE_CLOSE,
)


def harness_notice(text: str) -> str:
    """Wrap a harness message body in the notice tags the system prompt describes."""
    return f"{HARNESS_NOTICE_OPEN}\n{text.strip()}\n{HARNESS_NOTICE_CLOSE}"


def escape_channel_text(text: str) -> str:
    """Neutralize channel markers and sentinels inside untrusted text.

    Tool output is data and may deliberately contain harness marker strings
    or sentinel characters to forge system messages. Escaping them keeps the
    framing of <tool_results> and <harness_notice> blocks unambiguous.
    """
    escaped = defang_sentinels(text)
    for marker in IMPERSONATABLE_MARKERS:
        escaped = escaped.replace(marker, marker.replace("<", "&lt;", 1))
    return escaped


def format_tool_result_content(
    status: str,
    output: str,
    exit_code: int | None = None,
    saved_to: str | None = None,
) -> str:
    """Format a single tool result into sentinel-wrapped channel content."""
    attributes = f'status="{status}"'
    if exit_code is not None:
        attributes += f' exit_code="{exit_code}"'
    if saved_to:
        attributes += f' saved_to="{saved_to}"'
    body = f"<result {attributes}>\n{escape_channel_text(str(output))}\n</result>"
    return wrap_channel("TOOL_RESULTS", body)
