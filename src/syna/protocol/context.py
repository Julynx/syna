"""Context-budget management for long conversations.

Two mechanisms keep the conversation within the model's context budget:

1. Eliding old tool outputs: every executed call's full output is saved in
   the pod (`saved_to`), so old outputs in history can be replaced by a
   pointer plus a head-and-tail excerpt without losing information. Only
   recoverable outputs, those carrying a `saved_to` path, are elided. The
   excerpt keeps the shape of the result visible so the model recalls it
   instead of imagining it; a rewrite only happens when it removes at
   least a minimum token saving, because rewriting the message breaks the
   provider prompt cache for the suffix after it. Old image attachments
   are elided the same way: the list-content message becomes a harness
   notice pointing at the pod file, and the budget stops counting the
   image tokens.
   Archived large user messages (user_messages.py) are elided the same
   way: head excerpt plus a pointer to the verbatim file.
2. Summarization (compaction, in `session.py`): the lossy fallback that
   rewrites older turns, applied only when the budget is still exceeded
   after elision.
"""

import math
import re

from .channel import (
    harness_notice,
    is_channel_message,
    unwrap_channel,
    wrap_channel,
)
from .user_messages import ARCHIVE_MARKER_PATTERN

RESULT_BLOCK_PATTERN = re.compile(r"(<result\s[^>]*>)(.*?)(</result>)", re.DOTALL)
SAVED_TO_PATTERN = re.compile(r'saved_to="([^"]+)"')

ELIDED_MARKER = "output elided"
USER_ELISION_MARKER = "rest of this message is elided"
IMAGE_ELIDED_MARKER = "image elided"
LATIN_CHARS_PER_TOKEN = 4
CJK_CHARS_PER_TOKEN = 1.5
MESSAGE_OVERHEAD_TOKENS = 4
# The rewrite threshold scales with the tokens after the rewritten message,
# divided by this horizon: a batched pass re-bills the cache suffix once per
# elision cadence, so a rewrite repays its re-bill when the saving clears
# suffix / horizon within one cadence window. Fixed at the default cadence
# (elide_every_requests = 10); tuning the cadence does not retune the
# horizon, which only makes aggressive cadences more cache-friendly.
ELISION_SAVINGS_HORIZON = 10


def _suffix_tokens(messages: list[dict]) -> list[int]:
    """Return the token estimate of everything after each message.

    Entry i holds the estimate of messages[i+1:], so the re-bill cost of
    rewriting message i is a lookup. Elided candidates later in the list
    are counted at their current size, which only makes the threshold
    more conservative.
    """
    totals = [0] * len(messages)
    running = 0
    for index in range(len(messages) - 1, -1, -1):
        totals[index] = running
        running += estimate_history_tokens([messages[index]])
    return totals


def _rewrite_threshold(
    min_savings_tokens: int, suffix_tokens: int, request_horizon: int
) -> int:
    """Return the token saving one rewrite must remove.

    The flat `min_savings_tokens` floor guards against rewrites that
    barely shrink the text; the horizon term guards the cache: rewriting
    a message re-bills the whole suffix after it at the uncached rate,
    and a batched pass repeats that re-bill every `request_horizon`
    requests, so the saving must repay the re-bill within one window.
    A horizon of zero or less disables the suffix term.
    """
    if request_horizon <= 0:
        return min_savings_tokens
    return max(min_savings_tokens, suffix_tokens // request_horizon)


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
    """Estimate the total token size of a message list.

    List content (image messages from `inspect_image`) contributes its
    text parts plus the `image_tokens` the attach step recorded: the
    base64 characters would be counted as ordinary text otherwise,
    overestimating a 1024x1024 image a thousandfold.
    """
    total = 0
    for message in messages:
        content = message.get("content") or ""
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += estimate_tokens(part["text"])
        total += MESSAGE_OVERHEAD_TOKENS
        image_tokens = message.get("image_tokens")
        if isinstance(image_tokens, int) and not isinstance(image_tokens, bool):
            total += image_tokens
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            total += estimate_tokens(fn.get("name") or "")
            total += estimate_tokens(fn.get("arguments") or "")
            total += MESSAGE_OVERHEAD_TOKENS
    return total


def elide_tool_outputs(
    messages: list[dict],
    keep_recent: int,
    keep_chars: int = 0,
    min_savings_tokens: int = 0,
    request_horizon: int = ELISION_SAVINGS_HORIZON,
) -> int:
    """Replace recoverable old tool outputs in history with excerpts.

    Message contents are updated in place. The system message and the most
    recent `keep_recent` history messages are left untouched. Only results
    carrying a `saved_to` path are elided; anything unrecoverable stays.
    An elided result keeps its first and last `keep_chars` characters
    around a one-line pointer, so the model retains the shape of the
    result (command conclusions usually sit in the last lines, listings
    in the first) and only re-reads the file when it needs the exact
    middle. A result is rewritten only when the excerpt removes at least
    `_rewrite_threshold`'s saving: rewriting one message re-bills the
    whole cache suffix after it at the uncached rate, so the threshold
    grows with that suffix (horizon-scaled) as well as with the flat
    floor. The budget-exceeded path passes 0 for both, where fitting in
    the budget beats cache economics. The excerpt must also be strictly
    shorter than the output. Already-elided messages are skipped, making
    the pass idempotent.

    Returns the number of results elided.
    """
    if keep_recent < 0:
        keep_recent = 0
    # A window covering the whole list must protect everything: the raw
    # difference goes negative and the negative slice stop would wrap the
    # range around, eliding protected messages.
    last_editable = max(1, len(messages) - keep_recent)
    suffix_tokens = _suffix_tokens(messages)
    elided_count = 0

    for position, message in enumerate(messages[1:last_editable], start=1):
        content = message.get("content") or ""
        if not isinstance(content, str) or ELIDED_MARKER in content:
            continue
        if not is_channel_message(content, "TOOL_RESULTS"):
            continue

        unwrapped = unwrap_channel(content)
        if unwrapped is None:
            continue
        kind, body = unwrapped
        threshold = _rewrite_threshold(
            min_savings_tokens, suffix_tokens[position], request_horizon
        )
        new_body, count = _elide_result_blocks(body, keep_chars, threshold)
        if count:
            message["content"] = wrap_channel(kind, new_body)
            elided_count += count
    return elided_count


def elide_archived_user_messages(
    messages: list[dict],
    keep_recent: int,
    head_chars: int,
    archive_nonce: str,
    min_savings_tokens: int = 0,
    request_horizon: int = ELISION_SAVINGS_HORIZON,
) -> int:
    """Replace old archived user messages with a head excerpt plus pointer.

    Archived messages carry the harness archive marker (user_messages.py).
    Only markers carrying this session's archive id are genuine; a user
    message that merely quotes the format carries a nonce the session
    never issued and is left alone. Once a genuine one falls outside the
    recent window, history keeps only the first `head_chars` characters
    plus a pointer to the verbatim file, so a large pasted payload stops
    crowding the context without losing the gist of the request or access
    to the data. As with tool outputs, the rewrite only happens when it
    clears `_rewrite_threshold`'s saving. Contents are updated in place;
    the pointer text is recognized on later passes, making this
    idempotent.
    Returns the number of messages elided.
    """
    if keep_recent < 0:
        keep_recent = 0
    last_editable = max(1, len(messages) - keep_recent)
    suffix_tokens = _suffix_tokens(messages)
    elided_count = 0
    for position, message in enumerate(messages[1:last_editable], start=1):
        if message.get("role") != "user":
            continue
        content = message.get("content") or ""
        if not isinstance(content, str):
            continue
        match = ARCHIVE_MARKER_PATTERN.search(content)
        if (
            match is None
            or match.group("nonce") != archive_nonce
            or USER_ELISION_MARKER in content
        ):
            continue
        head = content[:head_chars].rstrip()
        replacement = (
            f"{head}\n[... {USER_ELISION_MARKER}; the full verbatim text"
            f" is archived in the pod at {match.group('path')} ...]"
        )
        savings = estimate_tokens(content) - estimate_tokens(replacement)
        threshold = _rewrite_threshold(
            min_savings_tokens, suffix_tokens[position], request_horizon
        )
        if savings < threshold:
            continue
        message["content"] = replacement
        elided_count += 1
    return elided_count


def elide_images(
    messages: list[dict], keep_recent: int, min_savings_tokens: int = 0
) -> int:
    """Replace old image attachments with a text pointer to the pod file.

    Image messages (see `_image_history_message`) ride in history as
    list-content user messages carrying an `image_tokens` estimate; the
    budget estimator counts that estimate, so an old attachment keeps
    crowding the context long after its turn. Outside the recent window
    the whole attachment is replaced by a harness notice naming the
    elision and the pod path, and the estimate is zeroed so the budget
    accounting follows. The image file itself stays in the pod, so the
    replacement is lossless in the same sense as tool-output elision -
    the model can re-attach the file with `inspect_image`. Unlike tool
    outputs there is no horizon term here: the removal is total (no
    excerpt to weigh) and the saving is the image's own recurring token
    cost, so the flat `min_savings_tokens` floor is the only gate.
    Contents are updated in place; the zeroed estimate and the string
    content make later passes skip the message, keeping this idempotent.
    Returns the number of images elided.
    """
    if keep_recent < 0:
        keep_recent = 0
    last_editable = max(1, len(messages) - keep_recent)
    elided_count = 0
    for message in messages[1:last_editable]:
        if not message.get("image_path") or not message.get("image_tokens"):
            continue
        if not isinstance(message.get("content"), list):
            continue
        replacement = wrap_channel(
            "HARNESS_NOTICE",
            harness_notice(
                f"[{IMAGE_ELIDED_MARKER}; the file remains in the pod at"
                f" {message['image_path']}]"
            ),
        )
        savings = message["image_tokens"] - estimate_tokens(replacement)
        if savings < min_savings_tokens:
            continue
        message["content"] = replacement
        message["image_tokens"] = 0
        elided_count += 1
    return elided_count


def recent_window_size(messages: list[dict], max_messages: int, max_tokens: int) -> int:
    """Return how many trailing messages the recent elision window keeps.

    The window is the configured message count, shrunk when those
    messages together exceed `max_tokens` - twelve recent messages can
    hold a fresh ten-thousand-token output, and without this bound the
    immune window would grow without limit while outputs age. The count
    runs over the full message list, so on a short history the system
    message's tokens count toward the cap too, matching the budget
    estimator's accounting. The window is never grown beyond the
    configured count, and the newest message always stays: a window that
    elides the result the model just received would defeat its own
    purpose. `max_tokens` of zero disables the cap.
    """
    if max_messages <= 0:
        return max_messages
    window = messages[-max_messages:]
    if max_tokens <= 0:
        return len(window)
    total = 0
    size = 0
    for message in reversed(window):
        cost = estimate_history_tokens([message])
        if size and total + cost > max_tokens:
            break
        total += cost
        size += 1
    return size


def _elide_result_blocks(
    body: str, keep_chars: int = 0, min_savings_tokens: int = 0
) -> tuple[str, int]:
    """Elide every saved_to-bearing result output inside one message body.

    Each elided output is replaced by a pointer plus its first and last
    `keep_chars` characters. A result is left untouched when the excerpt
    would not be strictly shorter than the output (nothing to remove), or
    when the rewrite would save fewer than `min_savings_tokens` tokens.
    """
    count = 0

    def replace(match: re.Match) -> str:
        nonlocal count
        opening_tag = match.group(1)
        output = match.group(2)
        saved_to = SAVED_TO_PATTERN.search(opening_tag)
        if saved_to is None or ELIDED_MARKER in output:
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
        savings = estimate_tokens(output) - estimate_tokens(excerpt)
        if savings < min_savings_tokens:
            return match.group(0)
        count += 1
        return f"{opening_tag}{excerpt}{match.group(3)}"

    return RESULT_BLOCK_PATTERN.sub(replace, body), count
