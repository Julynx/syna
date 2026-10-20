"""Context-budget management for long conversations.

Two mechanisms keep the conversation within the model's context budget:

1. Eliding old tool outputs: every executed call's full output is saved in
   the pod (`saved_to`), so old outputs in history can be replaced by a
   one-line pointer without losing information. Only recoverable outputs —
   those carrying a `saved_to` path — are elided.
2. Summarization (compaction, in `session.py`): the lossy fallback that
   rewrites older turns, applied only when the budget is still exceeded
   after elision.
"""

import re

from .channel import is_channel_message, unwrap_channel, wrap_channel

RESULT_BLOCK_PATTERN = re.compile(r"(<result\s[^>]*>)(.*?)(</result>)", re.DOTALL)
SAVED_TO_PATTERN = re.compile(r'saved_to="([^"]+)"')

ELIDED_MARKER = "output elided"
TOKENS_PER_CHAR = 4
MESSAGE_OVERHEAD_TOKENS = 4


def estimate_tokens(text: str) -> int:
    """Rough token estimate for one piece of message content."""
    return max(1, (len(text) + TOKENS_PER_CHAR - 1) // TOKENS_PER_CHAR)


def estimate_history_tokens(messages: list[dict]) -> int:
    """Estimate the total token size of a message list."""
    return sum(
        estimate_tokens(message.get("content", "")) + MESSAGE_OVERHEAD_TOKENS
        for message in messages
    )


def elide_tool_outputs(messages: list[dict], keep_recent: int) -> int:
    """Replace recoverable old tool outputs in history with pointers.

    Message contents are updated in place. The system message and the most
    recent `keep_recent` history messages are left untouched. Only results
    carrying a `saved_to` path are elided — anything unrecoverable stays.
    Already-elided messages are skipped, making the pass idempotent.

    Returns the number of results elided.
    """
    if keep_recent < 0:
        keep_recent = 0
    last_editable = len(messages) - keep_recent
    elided_count = 0

    for message in messages[1:last_editable]:
        content = message.get("content", "")
        if ELIDED_MARKER in content:
            continue
        if not is_channel_message(content, "TOOL_RESULTS"):
            continue

        kind, body = unwrap_channel(content)
        new_body, count = _elide_result_blocks(body)
        if count:
            message["content"] = wrap_channel(kind, new_body)
            elided_count += count
    return elided_count


def _elide_result_blocks(body: str) -> tuple[str, int]:
    """Elide every saved_to-bearing result output inside one message body."""
    count = 0

    def replace(match: re.Match) -> str:
        nonlocal count
        opening_tag = match.group(1)
        output = match.group(2)
        saved_to = SAVED_TO_PATTERN.search(opening_tag)
        if saved_to is None or ELIDED_MARKER in output:
            return match.group(0)
        count += 1
        return (
            f"{opening_tag}[{ELIDED_MARKER}; full text in {saved_to.group(1)}]"
            f"{match.group(3)}"
        )

    return RESULT_BLOCK_PATTERN.sub(replace, body), count
