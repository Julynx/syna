"""Transcript rendering and request assembly for history compaction."""

from .channel import harness_notice, is_channel_message, unwrap_channel, wrap_channel
from .config import read_asset_text

COMPACTION_CHANNEL_KIND = "COMPACTION"
COMPACTION_NOTE_PREFIX = "[System]: [Compaction] "


def load_compaction_prompt() -> str:
    """Load the compaction system prompt from the bundled assets."""
    return read_asset_text("compaction_prompt.md")


def format_compaction_message(summary: str) -> str:
    """Format a compaction summary into a harness notice on the compaction channel."""
    explanation = (
        "The conversation grew too long, so the earlier messages (except the"
        f" first) were replaced by the following summary:\n\n{summary.strip()}"
    )
    return wrap_channel(COMPACTION_CHANNEL_KIND, harness_notice(explanation))


def is_summary_message(content: str) -> bool:
    """Return whether a message content is a compaction summary."""
    return is_channel_message(content, COMPACTION_CHANNEL_KIND) or content.startswith(
        COMPACTION_NOTE_PREFIX
    )


def extract_summary_text(content: str) -> str:
    """Extract the raw summary text from a formatted summary message."""
    unwrapped = unwrap_channel(content)
    if unwrapped is not None and unwrapped[0] == COMPACTION_CHANNEL_KIND:
        body = unwrapped[1].strip()
        explanation_marker = ":\n\n"
        if explanation_marker in body:
            return body.split(explanation_marker, 1)[1].strip()
        return body
    if content.startswith(COMPACTION_NOTE_PREFIX):
        return content[len(COMPACTION_NOTE_PREFIX) :].strip()
    return content.strip()


def build_transcript(messages: list[dict]) -> str:
    """Render history messages as labeled transcript blocks.

    Compaction summaries and system channel messages are unwrapped so the
    compaction prompt sees clean, labeled transcript blocks.
    """
    blocks = []
    for message in messages:
        content = message.get("content") or ""
        tool_calls = message.get("tool_calls")
        if is_summary_message(content):
            summary = extract_summary_text(content)
            blocks.append(f"[previous summary]:\n{summary}")
        else:
            unwrapped = unwrap_channel(content)
            if unwrapped is not None:
                kind, body = unwrapped
                blocks.append(f"[{kind.lower()}]:\n{body}")
            elif tool_calls:
                tc_strs = []
                for tc in tool_calls:
                    fn = (
                        tc.get("function")
                        if isinstance(tc, dict)
                        else getattr(tc, "function", None)
                    )
                    fn_name = (
                        fn.get("name")
                        if isinstance(fn, dict)
                        else getattr(fn, "name", "")
                    )
                    fn_args = (
                        fn.get("arguments")
                        if isinstance(fn, dict)
                        else getattr(fn, "arguments", "")
                    )
                    tc_strs.append(f"{fn_name}({fn_args})")
                text = (
                    (content + "\n" if content else "")
                    + "tool calls: "
                    + ", ".join(tc_strs)
                )
                blocks.append(f"[{message['role']}]:\n{text}")
            else:
                blocks.append(f"[{message['role']}]:\n{content}")
    return "\n\n".join(blocks)


def build_compaction_messages(system_prompt: str, messages: list[dict]) -> list[dict]:
    """Assemble the message list for one compaction request."""
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                "Summarize the following conversation transcript:\n\n"
                + build_transcript(messages)
            ),
        },
    ]
