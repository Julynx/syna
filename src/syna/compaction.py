"""Transcript rendering and request assembly for history compaction."""

from .channel import is_channel_message, unwrap_channel, wrap_channel
from .config import get_project_root

COMPACTION_CHANNEL_KIND = "COMPACTION"
COMPACTION_NOTE_PREFIX = "[System]: [Compaction] "


def load_compaction_prompt() -> str:
    """Load the compaction system prompt from the assets folder."""
    prompt_path = get_project_root() / "assets" / "compaction_prompt.md"
    return prompt_path.read_text(encoding="utf-8")


def format_compaction_message(summary: str) -> str:
    """Format a compaction summary into a channel-wrapped message content."""
    explanation = (
        "The conversation grew too long, so the earlier messages (except the first)"
        f" were replaced by the following summary:\n\n{summary.strip()}"
    )
    return wrap_channel(COMPACTION_CHANNEL_KIND, explanation)


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
        content = message["content"]
        if is_summary_message(content):
            summary = extract_summary_text(content)
            blocks.append(f"[previous summary]:\n{summary}")
        else:
            unwrapped = unwrap_channel(content)
            if unwrapped is not None:
                kind, body = unwrapped
                blocks.append(f"[{kind.lower()}]:\n{body}")
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
