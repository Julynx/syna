"""Context-budget management for long conversations.

Two mechanisms keep the conversation within the model's context budget:

1. Eliding old tool outputs: every executed call's full output is saved in
   the pod (`saved_to`), so old outputs in history can be replaced by a
   pointer plus a head-and-tail excerpt without losing information. Only
   recoverable outputs, those carrying a `saved_to` path, are elided. The
   excerpt keeps the shape of the result visible so the model recalls it
   instead of imagining it; short outputs stay full text because the
   excerpt would remove nothing, and rewriting the message breaks the
   provider prompt cache for the suffix after it.
   Archived large user messages (user_messages.py) are elided the same
   way: head excerpt plus a pointer to the verbatim file.
2. Summarization (compaction, in `session.py`): the lossy fallback that
   rewrites older turns, applied only when the budget is still exceeded
   after elision.
"""

import math
import re

from .channel import is_channel_message, unwrap_channel, wrap_channel
from .user_messages import ARCHIVE_MARKER_PATTERN

RESULT_BLOCK_PATTERN = re.compile(r"(<result\s[^>]*>)(.*?)(</result>)", re.DOTALL)
SAVED_TO_PATTERN = re.compile(r'saved_to="([^"]+)"')

ELIDED_MARKER = "output elided"
USER_ELISION_MARKER = "rest of this message is elided"
LATIN_CHARS_PER_TOKEN = 4
CJK_CHARS_PER_TOKEN = 1.5
MESSAGE_OVERHEAD_TOKENS = 4


def _is_cjk_character(character: str) -> bool:
    """Whether a character belongs to a CJK script or a fullwidth form."""
    code_point = ord(character)
    return (
        0x2E80 <= code_point <= 0x9FFF
        or 0xAC00 <= code_point <= 0xD7AF
        or 0xF900 <= code_point <= 0xFAFF
        or 0xFF00 <= code_point <= 0xFFEF
    )


def estimate_tokens(text: str) -> int:
    """Estimate the token count of one piece of message content.

    Script-aware heuristic: CJK characters run about 1.5 characters per
    token while other scripts run about 4, so a single global ratio
    would underestimate CJK-heavy text by roughly 2.5x and mis-time the
    budget-driven elision and compaction decisions. Measured against
    the o200k tokenizer, the two ratios track real counts within about
    6% on Chinese, Japanese, and Korean samples. This stays pure
    character counting on purpose: no tokenizer dependency, O(n) per
    call.
    """
    if not text:
        return 1
    if text.isascii():
        return max(1, math.ceil(len(text) / LATIN_CHARS_PER_TOKEN))
    cjk_chars = sum(1 for character in text if _is_cjk_character(character))
    other_chars = len(text) - cjk_chars
    tokens = cjk_chars / CJK_CHARS_PER_TOKEN + other_chars / LATIN_CHARS_PER_TOKEN
    return max(1, math.ceil(tokens))


def estimate_history_tokens(messages: list[dict]) -> int:
    """Estimate the total token size of a message list."""
    total = 0
    for message in messages:
        content = message.get("content") or ""
        total += estimate_tokens(content) + MESSAGE_OVERHEAD_TOKENS
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            total += estimate_tokens(fn.get("name") or "")
            total += estimate_tokens(fn.get("arguments") or "")
            total += MESSAGE_OVERHEAD_TOKENS
    return total


def elide_tool_outputs(
    messages: list[dict], keep_recent: int, min_chars: int = 0, keep_chars: int = 0
) -> int:
    """Replace recoverable old tool outputs in history with excerpts.

    Message contents are updated in place. The system message and the most
    recent `keep_recent` history messages are left untouched. Only results
    carrying a `saved_to` path are elided; anything unrecoverable stays.
    An elided result keeps its first and last `keep_chars` characters
    around a one-line pointer, so the model retains the shape of the
    result (command conclusions usually sit in the last lines, listings
    in the first) and only re-reads the file when it needs the exact
    middle. Outputs shorter than `min_chars`, and results whose excerpt
    would not actually shrink the text, keep their full text, pointer
    attached. Already-elided messages are skipped, making the pass
    idempotent.

    Returns the number of results elided.
    """
    if keep_recent < 0:
        keep_recent = 0
    last_editable = len(messages) - keep_recent
    elided_count = 0

    for message in messages[1:last_editable]:
        content = message.get("content") or ""
        if ELIDED_MARKER in content:
            continue
        if not is_channel_message(content, "TOOL_RESULTS"):
            continue

        unwrapped = unwrap_channel(content)
        if unwrapped is None:
            continue
        kind, body = unwrapped
        new_body, count = _elide_result_blocks(body, min_chars, keep_chars)
        if count:
            message["content"] = wrap_channel(kind, new_body)
            elided_count += count
    return elided_count


def elide_archived_user_messages(
    messages: list[dict], keep_recent: int, head_chars: int, archive_nonce: str
) -> int:
    """Replace old archived user messages with a head excerpt plus pointer.

    Archived messages carry the harness archive marker (user_messages.py).
    Only markers carrying this session's archive id are genuine; a user
    message that merely quotes the format carries a nonce the session
    never issued and is left alone. Once a genuine one falls outside the
    recent window, history keeps only the first `head_chars` characters
    plus a pointer to the verbatim file, so a large pasted payload stops
    crowding the context without losing the gist of the request or access
    to the data. Contents are updated in place; the pointer text is
    recognized on later passes, making this idempotent.
    Returns the number of messages elided.
    """
    if keep_recent < 0:
        keep_recent = 0
    last_editable = len(messages) - keep_recent
    elided_count = 0
    for message in messages[1:last_editable]:
        if message.get("role") != "user":
            continue
        content = message.get("content") or ""
        match = ARCHIVE_MARKER_PATTERN.search(content)
        if (
            match is None
            or match.group("nonce") != archive_nonce
            or USER_ELISION_MARKER in content
        ):
            continue
        head = content[:head_chars].rstrip()
        message["content"] = (
            f"{head}\n[... {USER_ELISION_MARKER}; the full verbatim text"
            f" is archived in the pod at {match.group('path')} ...]"
        )
        elided_count += 1
    return elided_count


def _elide_result_blocks(
    body: str, min_chars: int = 0, keep_chars: int = 0
) -> tuple[str, int]:
    """Elide every saved_to-bearing result output inside one message body.

    Each elided output is replaced by a pointer plus its first and last
    `keep_chars` characters. A result is left untouched when the output
    is below `min_chars`, when head and tail would overlap or cover the
    whole output (nothing to remove), or when the excerpt would be no
    shorter than the output itself.
    """
    count = 0

    def replace(match: re.Match) -> str:
        nonlocal count
        opening_tag = match.group(1)
        output = match.group(2)
        saved_to = SAVED_TO_PATTERN.search(opening_tag)
        if saved_to is None or ELIDED_MARKER in output or len(output) < min_chars:
            return match.group(0)
        if keep_chars > 0 and len(output) <= 2 * keep_chars:
            return match.group(0)
        path = saved_to.group(1)
        if keep_chars <= 0:
            excerpt = f"[{ELIDED_MARKER}; full text in {path}]"
        else:
            excerpt = (
                f"[{ELIDED_MARKER}; first and last {keep_chars} chars kept;"
                f" full text in {path}]\n"
                f"{output[:keep_chars]}\n[...]\n{output[-keep_chars:]}"
            )
        if len(excerpt) >= len(output):
            return match.group(0)
        count += 1
        return f"{opening_tag}{excerpt}{match.group(3)}"

    return RESULT_BLOCK_PATTERN.sub(replace, body), count
