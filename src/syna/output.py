"""Cleaning and bounded rendering of tool output shown to the model.

Tool results must be bounded before they enter the conversation: long output
is cut to its beginning and end, control noise is removed, and empty results
are replaced by an explicit placeholder. The full text always lives at the
`saved_to` path inside the pod.
"""

import re

ANSI_ESCAPE_PATTERN = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])"
)

NO_OUTPUT_TEXT = "(no output)"


def strip_ansi_escapes(text: str) -> str:
    """Remove ANSI terminal escape sequences (colors, cursor moves)."""
    return ANSI_ESCAPE_PATTERN.sub("", text)


def collapse_progress_lines(text: str) -> str:
    """Collapse carriage-return progress updates to their last segment."""
    lines = text.split("\n")
    collapsed = (
        line.rsplit("\r", 1)[-1] if "\r" in line else line for line in lines
    )
    return "\n".join(collapsed)


def clean_output(text: str) -> str:
    """Strip terminal noise from raw tool output."""
    return collapse_progress_lines(strip_ansi_escapes(text))


def head_and_tail(text: str, limit_chars: int, saved_to: str | None = None) -> str:
    """Cut text to its beginning and end, keeping at most limit_chars characters.

    The omitted middle is replaced by a marker pointing at the full output.
    """
    if limit_chars <= 0 or len(text) <= limit_chars:
        return text
    half = max(limit_chars // 2, 1)
    omitted = len(text) - 2 * half
    location = f" in {saved_to}" if saved_to else ""
    marker = f"\n[... {omitted} characters omitted{location} ...]\n"
    return text[:half] + marker + text[-half:]


def bounded_output(text: str, limit_chars: int, saved_to: str | None = None) -> str:
    """Render a tool output for the conversation: cleaned, bounded, never empty."""
    cleaned = clean_output(text)
    if not cleaned.strip():
        return NO_OUTPUT_TEXT
    return head_and_tail(cleaned, limit_chars, saved_to)
